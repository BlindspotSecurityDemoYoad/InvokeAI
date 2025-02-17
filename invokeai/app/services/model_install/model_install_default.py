"""Model installation class."""

import locale
import os
import re
import signal
import threading
import time
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
from shutil import copyfile, copytree, move, rmtree
from tempfile import mkdtemp
from typing import Any, Dict, List, Optional, Union

import torch
import yaml
from huggingface_hub import HfFolder
from pydantic.networks import AnyHttpUrl
from requests import Session

from invokeai.app.services.config import InvokeAIAppConfig
from invokeai.app.services.download import DownloadJob, DownloadQueueServiceBase, TqdmProgress
from invokeai.app.services.events.events_base import EventServiceBase
from invokeai.app.services.invoker import Invoker
from invokeai.app.services.model_records import DuplicateModelException, ModelRecordServiceBase
from invokeai.app.services.model_records.model_records_base import ModelRecordChanges
from invokeai.backend.model_manager.config import (
    AnyModelConfig,
    CheckpointConfigBase,
    InvalidModelConfigException,
    ModelRepoVariant,
    ModelSourceType,
)
from invokeai.backend.model_manager.metadata import (
    AnyModelRepoMetadata,
    HuggingFaceMetadataFetch,
    ModelMetadataFetchBase,
    ModelMetadataWithFiles,
    RemoteModelFile,
)
from invokeai.backend.model_manager.metadata.metadata_base import HuggingFaceMetadata
from invokeai.backend.model_manager.probe import ModelProbe
from invokeai.backend.model_manager.search import ModelSearch
from invokeai.backend.util import InvokeAILogger
from invokeai.backend.util.devices import TorchDevice

from .model_install_base import (
    MODEL_SOURCE_TO_TYPE_MAP,
    HFModelSource,
    InstallStatus,
    LocalModelSource,
    ModelInstallJob,
    ModelInstallServiceBase,
    ModelSource,
    StringLikeSource,
    URLModelSource,
)

TMPDIR_PREFIX = "tmpinstall_"


class ModelInstallService(ModelInstallServiceBase):
    """class for InvokeAI model installation."""

    def __init__(
        self,
        app_config: InvokeAIAppConfig,
        record_store: ModelRecordServiceBase,
        download_queue: DownloadQueueServiceBase,
        event_bus: Optional[EventServiceBase] = None,
        session: Optional[Session] = None,
    ):
        """
        Initialize the installer object.

        :param app_config: InvokeAIAppConfig object
        :param record_store: Previously-opened ModelRecordService database
        :param event_bus: Optional EventService object
        """
        self._app_config = app_config
        self._record_store = record_store
        self._event_bus = event_bus
        self._logger = InvokeAILogger.get_logger(name=self.__class__.__name__)
        self._install_jobs: List[ModelInstallJob] = []
        self._install_queue: Queue[ModelInstallJob] = Queue()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._downloads_changed_event = threading.Event()
        self._install_completed_event = threading.Event()
        self._download_queue = download_queue
        self._download_cache: Dict[AnyHttpUrl, ModelInstallJob] = {}
        self._running = False
        self._session = session
        self._install_thread: Optional[threading.Thread] = None
        self._next_job_id = 0

    @property
    def app_config(self) -> InvokeAIAppConfig:  # noqa D102
        return self._app_config

    @property
    def record_store(self) -> ModelRecordServiceBase:  # noqa D102
        return self._record_store

    @property
    def event_bus(self) -> Optional[EventServiceBase]:  # noqa D102
        return self._event_bus

    # make the invoker optional here because we don't need it and it
    # makes the installer harder to use outside the web app
    def start(self, invoker: Optional[Invoker] = None) -> None:
        """Start the installer thread."""

        # Yes, this is weird. When the installer thread is running, the
        # thread masks the ^C signal. When we receive a
        # sigINT, we stop the thread, reset sigINT, and send a new
        # sigINT to the parent process.
        def sigint_handler(signum, frame):
            self.stop()
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.raise_signal(signal.SIGINT)

        signal.signal(signal.SIGINT, sigint_handler)

        with self._lock:
            if self._running:
                raise Exception("Attempt to start the installer service twice")
            self._start_installer_thread()
            self._remove_dangling_install_dirs()
            self._migrate_yaml()
            # In normal use, we do not want to scan the models directory - it should never have orphaned models.
            # We should only do the scan when the flag is set (which should only be set when testing).
            if self.app_config.scan_models_on_startup:
                self._register_orphaned_models()

            # Check all models' paths and confirm they exist. A model could be missing if it was installed on a volume
            # that isn't currently mounted. In this case, we don't want to delete the model from the database, but we do
            # want to alert the user.
            for model in self._scan_for_missing_models():
                self._logger.warning(f"Missing model file: {model.name} at {model.path}")

    def stop(self, invoker: Optional[Invoker] = None) -> None:
        """Stop the installer thread; after this the object can be deleted and garbage collected."""
        if not self._running:
            raise Exception("Attempt to stop the install service before it was started")
        self._logger.debug("calling stop_event.set()")
        self._stop_event.set()
        self._clear_pending_jobs()
        self._download_cache.clear()
        assert self._install_thread is not None
        self._install_thread.join()
        self._running = False

    def _clear_pending_jobs(self) -> None:
        for job in self.list_jobs():
            if not job.in_terminal_state:
                self._logger.warning("Cancelling job {job.id}")
                self.cancel_job(job)
        while True:
            try:
                job = self._install_queue.get(block=False)
                self._install_queue.task_done()
            except Empty:
                break

    def _put_in_queue(self, job: ModelInstallJob) -> None:
        if self._stop_event.is_set():
            self.cancel_job(job)
        else:
            self._install_queue.put(job)

    def register_path(
        self,
        model_path: Union[Path, str],
        config: Optional[Dict[str, Any]] = None,
    ) -> str:  # noqa D102
        model_path = Path(model_path)
        config = config or {}
        if not config.get("source"):
            config["source"] = model_path.resolve().as_posix()
        config["source_type"] = ModelSourceType.Path
        return self._register(model_path, config)

    def install_path(
        self,
        model_path: Union[Path, str],
        config: Optional[Dict[str, Any]] = None,
    ) -> str:  # noqa D102
        model_path = Path(model_path)
        config = config or {}

        info: AnyModelConfig = ModelProbe.probe(Path(model_path), config, hash_algo=self._app_config.hashing_algorithm)

        if preferred_name := config.get("name"):
            preferred_name = Path(preferred_name).with_suffix(model_path.suffix)

        dest_path = (
            self.app_config.models_path / info.base.value / info.type.value / (preferred_name or model_path.name)
        )
        try:
            new_path = self._copy_model(model_path, dest_path)
        except FileExistsError as excp:
            raise DuplicateModelException(
                f"A model named {model_path.name} is already installed at {dest_path.as_posix()}"
            ) from excp

        return self._register(
            new_path,
            config,
            info,
        )

    def heuristic_import(
        self,
        source: str,
        config: Optional[Dict[str, Any]] = None,
        access_token: Optional[str] = None,
        inplace: Optional[bool] = False,
    ) -> ModelInstallJob:
        variants = "|".join(ModelRepoVariant.__members__.values())
        hf_repoid_re = f"^([^/:]+/[^/:]+)(?::({variants})?(?::/?([^:]+))?)?$"
        source_obj: Optional[StringLikeSource] = None

        if Path(source).exists():  # A local file or directory
            source_obj = LocalModelSource(path=Path(source), inplace=inplace)
        elif match := re.match(hf_repoid_re, source):
            source_obj = HFModelSource(
                repo_id=match.group(1),
                variant=match.group(2) if match.group(2) else None,  # pass None rather than ''
                subfolder=Path(match.group(3)) if match.group(3) else None,
                access_token=access_token,
            )
        elif re.match(r"^https?://[^/]+", source):
            # Pull the token from config if it exists and matches the URL
            _token = access_token
            if _token is None:
                for pair in self.app_config.remote_api_tokens or []:
                    if re.search(pair.url_regex, source):
                        _token = pair.token
                        break
            source_obj = URLModelSource(
                url=AnyHttpUrl(source),
                access_token=_token,
            )
        else:
            raise ValueError(f"Unsupported model source: '{source}'")
        return self.import_model(source_obj, config)

    def import_model(self, source: ModelSource, config: Optional[Dict[str, Any]] = None) -> ModelInstallJob:  # noqa D102
        similar_jobs = [x for x in self.list_jobs() if x.source == source and not x.in_terminal_state]
        if similar_jobs:
            self._logger.warning(f"There is already an active install job for {source}. Not enqueuing.")
            return similar_jobs[0]

        if isinstance(source, LocalModelSource):
            install_job = self._import_local_model(source, config)
            self._put_in_queue(install_job)  # synchronously install
        elif isinstance(source, HFModelSource):
            install_job = self._import_from_hf(source, config)
        elif isinstance(source, URLModelSource):
            install_job = self._import_from_url(source, config)
        else:
            raise ValueError(f"Unsupported model source: '{type(source)}'")

        self._install_jobs.append(install_job)
        return install_job

    def list_jobs(self) -> List[ModelInstallJob]:  # noqa D102
        return self._install_jobs

    def get_job_by_source(self, source: ModelSource) -> List[ModelInstallJob]:  # noqa D102
        return [x for x in self._install_jobs if x.source == source]

    def get_job_by_id(self, id: int) -> ModelInstallJob:  # noqa D102
        jobs = [x for x in self._install_jobs if x.id == id]
        if not jobs:
            raise ValueError(f"No job with id {id} known")
        assert len(jobs) == 1
        assert isinstance(jobs[0], ModelInstallJob)
        return jobs[0]

    def wait_for_job(self, job: ModelInstallJob, timeout: int = 0) -> ModelInstallJob:
        """Block until the indicated job has reached terminal state, or when timeout limit reached."""
        start = time.time()
        while not job.in_terminal_state:
            if self._install_completed_event.wait(timeout=5):  # in case we miss an event
                self._install_completed_event.clear()
            if timeout > 0 and time.time() - start > timeout:
                raise TimeoutError("Timeout exceeded")
        return job

    def wait_for_installs(self, timeout: int = 0) -> List[ModelInstallJob]:  # noqa D102
        """Block until all installation jobs are done."""
        start = time.time()
        while len(self._download_cache) > 0:
            if self._downloads_changed_event.wait(timeout=0.25):  # in case we miss an event
                self._downloads_changed_event.clear()
            if timeout > 0 and time.time() - start > timeout:
                raise TimeoutError("Timeout exceeded")
        self._install_queue.join()

        return self._install_jobs

    def cancel_job(self, job: ModelInstallJob) -> None:
        """Cancel the indicated job."""
        job.cancel()
        with self._lock:
            self._cancel_download_parts(job)

    def prune_jobs(self) -> None:
        """Prune all completed and errored jobs."""
        unfinished_jobs = [x for x in self._install_jobs if not x.in_terminal_state]
        self._install_jobs = unfinished_jobs

    def _migrate_yaml(self) -> None:
        db_models = self.record_store.all_models()

        legacy_models_yaml_path = (
            self._app_config.legacy_models_yaml_path or self._app_config.root_path / "configs" / "models.yaml"
        )

        # The old path may be relative to the root path
        if not legacy_models_yaml_path.exists():
            legacy_models_yaml_path = Path(self._app_config.root_path, legacy_models_yaml_path)

        if legacy_models_yaml_path.exists():
            with open(legacy_models_yaml_path, "rt", encoding=locale.getpreferredencoding()) as file:
                legacy_models_yaml = yaml.safe_load(file)

            yaml_metadata = legacy_models_yaml.pop("__metadata__")
            yaml_version = yaml_metadata.get("version")

            if yaml_version != "3.0.0":
                raise ValueError(
                    f"Attempted migration of unsupported `models.yaml` v{yaml_version}. Only v3.0.0 is supported. Exiting."
                )

            self._logger.info(
                f"Starting one-time migration of {len(legacy_models_yaml.items())} models from {str(legacy_models_yaml_path)}. This may take a few minutes."
            )

            if len(db_models) == 0 and len(legacy_models_yaml.items()) != 0:
                for model_key, stanza in legacy_models_yaml.items():
                    _, _, model_name = str(model_key).split("/")
                    model_path = Path(stanza["path"])
                    if not model_path.is_absolute():
                        model_path = self._app_config.models_path / model_path
                    model_path = model_path.resolve()

                    config: dict[str, Any] = {}
                    config["name"] = model_name
                    config["description"] = stanza.get("description")
                    legacy_config_path = stanza.get("config")
                    if legacy_config_path:
                        # In v3, these paths were relative to the root. Migrate them to be relative to the legacy_conf_dir.
                        legacy_config_path: Path = self._app_config.root_path / legacy_config_path
                        if legacy_config_path.is_relative_to(self._app_config.legacy_conf_path):
                            legacy_config_path = legacy_config_path.relative_to(self._app_config.legacy_conf_path)
                        config["config_path"] = str(legacy_config_path)
                    try:
                        id = self.register_path(model_path=model_path, config=config)
                        self._logger.info(f"Migrated {model_name} with id {id}")
                    except Exception as e:
                        self._logger.warning(f"Model at {model_path} could not be migrated: {e}")

            # Rename `models.yaml` to `models.yaml.bak` to prevent re-migration
            legacy_models_yaml_path.rename(legacy_models_yaml_path.with_suffix(".yaml.bak"))

        # Unset the path - we are done with it either way
        self._app_config.legacy_models_yaml_path = None

    def unregister(self, key: str) -> None:  # noqa D102
        self.record_store.del_model(key)

    def delete(self, key: str) -> None:  # noqa D102
        """Unregister the model. Delete its files only if they are within our models directory."""
        model = self.record_store.get_model(key)
        model_path = self.app_config.models_path / model.path

        if model_path.is_relative_to(self.app_config.models_path):
            # If the models is in the Invoke-managed models dir, we delete it
            self.unconditionally_delete(key)
        else:
            # Else we only unregister it, leaving the file in place
            self.unregister(key)

    def unconditionally_delete(self, key: str) -> None:  # noqa D102
        model = self.record_store.get_model(key)
        model_path = self.app_config.models_path / model.path
        if model_path.is_file() or model_path.is_symlink():
            model_path.unlink()
        elif model_path.is_dir():
            rmtree(model_path)
        self.unregister(key)

    def download_and_cache(
        self,
        source: Union[str, AnyHttpUrl],
        access_token: Optional[str] = None,
        timeout: int = 0,
    ) -> Path:
        """Download the model file located at source to the models cache and return its Path."""
        model_hash = sha256(str(source).encode("utf-8")).hexdigest()[0:32]
        model_path = self._app_config.convert_cache_path / model_hash

        # We expect the cache directory to contain one and only one downloaded file.
        # We don't know the file's name in advance, as it is set by the download
        # content-disposition header.
        if model_path.exists():
            contents = [x for x in model_path.iterdir() if x.is_file()]
            if len(contents) > 0:
                return contents[0]

        model_path.mkdir(parents=True, exist_ok=True)
        job = self._download_queue.download(
            source=AnyHttpUrl(str(source)),
            dest=model_path,
            access_token=access_token,
            on_progress=TqdmProgress().update,
        )
        self._download_queue.wait_for_job(job, timeout)
        if job.complete:
            assert job.download_path is not None
            return job.download_path
        else:
            raise Exception(job.error)

    # --------------------------------------------------------------------------------------------
    # Internal functions that manage the installer threads
    # --------------------------------------------------------------------------------------------
    def _start_installer_thread(self) -> None:
        self._install_thread = threading.Thread(target=self._install_next_item, daemon=True)
        self._install_thread.start()
        self._running = True

    def _install_next_item(self) -> None:
        self._logger.debug(f"Installer thread {threading.get_ident()} starting")
        while True:
            if self._stop_event.is_set():
                break
            self._logger.debug(f"Installer thread {threading.get_ident()} polling")
            try:
                job = self._install_queue.get(timeout=1)
            except Empty:
                continue
            assert job.local_path is not None
            try:
                if job.cancelled:
                    self._signal_job_cancelled(job)

                elif job.errored:
                    self._signal_job_errored(job)

                elif job.waiting or job.downloads_done:
                    self._register_or_install(job)

            except Exception as e:
                # Expected errors include InvalidModelConfigException, DuplicateModelException, OSError, but we must
                # gracefully handle _any_ error here.
                self._set_error(job, e)

            finally:
                # if this is an install of a remote file, then clean up the temporary directory
                if job._install_tmpdir is not None:
                    rmtree(job._install_tmpdir)
                self._install_completed_event.set()
                self._install_queue.task_done()
        self._logger.info(f"Installer thread {threading.get_ident()} exiting")

    def _register_or_install(self, job: ModelInstallJob) -> None:
        # local jobs will be in waiting state, remote jobs will be downloading state
        job.total_bytes = self._stat_size(job.local_path)
        job.bytes = job.total_bytes
        self._signal_job_running(job)
        job.config_in["source"] = str(job.source)
        job.config_in["source_type"] = MODEL_SOURCE_TO_TYPE_MAP[job.source.__class__]
        # enter the metadata, if there is any
        if isinstance(job.source_metadata, (HuggingFaceMetadata)):
            job.config_in["source_api_response"] = job.source_metadata.api_response

        if job.inplace:
            key = self.register_path(job.local_path, job.config_in)
        else:
            key = self.install_path(job.local_path, job.config_in)
        job.config_out = self.record_store.get_model(key)
        self._signal_job_completed(job)

    def _set_error(self, job: ModelInstallJob, excp: Exception) -> None:
        if any(x.content_type is not None and "text/html" in x.content_type for x in job.download_parts):
            job.set_error(
                InvalidModelConfigException(
                    f"At least one file in {job.local_path} is an HTML page, not a model. This can happen when an access token is required to download."
                )
            )
        else:
            job.set_error(excp)
        self._signal_job_errored(job)

    # --------------------------------------------------------------------------------------------
    # Internal functions that manage the models directory
    # --------------------------------------------------------------------------------------------
    def _remove_dangling_install_dirs(self) -> None:
        """Remove leftover tmpdirs from aborted installs."""
        path = self._app_config.models_path
        for tmpdir in path.glob(f"{TMPDIR_PREFIX}*"):
            self._logger.info(f"Removing dangling temporary directory {tmpdir}")
            rmtree(tmpdir)

    def _scan_for_missing_models(self) -> list[AnyModelConfig]:
        """Scan the models directory for missing models and return a list of them."""
        missing_models: list[AnyModelConfig] = []
        for model_config in self.record_store.all_models():
            if not (self.app_config.models_path / model_config.path).resolve().exists():
                missing_models.append(model_config)
        return missing_models

    def _register_orphaned_models(self) -> None:
        """Scan the invoke-managed models directory for orphaned models and registers them.

        This is typically only used during testing with a new DB or when using the memory DB, because those are the
        only situations in which we may have orphaned models in the models directory.
        """

        installed_model_paths = {
            (self._app_config.models_path / x.path).resolve() for x in self.record_store.all_models()
        }

        # The bool returned by this callback determines if the model is added to the list of models found by the search
        def on_model_found(model_path: Path) -> bool:
            resolved_path = model_path.resolve()
            # Already registered models should be in the list of found models, but not re-registered.
            if resolved_path in installed_model_paths:
                return True
            # Skip core models entirely - these aren't registered with the model manager.
            if str(resolved_path).startswith(str(self.app_config.models_path / "core")):
                return False
            try:
                model_id = self.register_path(model_path)
                self._logger.info(f"Registered {model_path.name} with id {model_id}")
            except DuplicateModelException:
                # In case a duplicate models sneaks by, we will ignore this error - we "found" the model
                pass
            return True

        self._logger.info(f"Scanning {self._app_config.models_path} for orphaned models")
        search = ModelSearch(on_model_found=on_model_found)
        found_models = search.search(self._app_config.models_path)
        self._logger.info(f"{len(found_models)} new models registered")

    def sync_model_path(self, key: str) -> AnyModelConfig:
        """
        Move model into the location indicated by its basetype, type and name.

        Call this after updating a model's attributes in order to move
        the model's path into the location indicated by its basetype, type and
        name. Applies only to models whose paths are within the root `models_dir`
        directory.

        May raise an UnknownModelException.
        """
        model = self.record_store.get_model(key)
        models_dir = self.app_config.models_path
        old_path = self.app_config.models_path / model.path

        if not old_path.is_relative_to(models_dir):
            # The model is not in the models directory - we don't need to move it.
            return model

        new_path = models_dir / model.base.value / model.type.value / old_path.name

        if old_path == new_path or new_path.exists() and old_path == new_path.resolve():
            return model

        self._logger.info(f"Moving {model.name} to {new_path}.")
        new_path = self._move_model(old_path, new_path)
        model.path = new_path.relative_to(models_dir).as_posix()
        self.record_store.update_model(key, ModelRecordChanges(path=model.path))
        return model

    def _copy_model(self, old_path: Path, new_path: Path) -> Path:
        if old_path == new_path:
            return old_path
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if old_path.is_dir():
            copytree(old_path, new_path)
        else:
            copyfile(old_path, new_path)
        return new_path

    def _move_model(self, old_path: Path, new_path: Path) -> Path:
        if old_path == new_path:
            return old_path

        new_path.parent.mkdir(parents=True, exist_ok=True)

        # if path already exists then we jigger the name to make it unique
        counter: int = 1
        while new_path.exists():
            path = new_path.with_stem(new_path.stem + f"_{counter:02d}")
            if not path.exists():
                new_path = path
            counter += 1
        move(old_path, new_path)
        return new_path

    def _register(
        self, model_path: Path, config: Optional[Dict[str, Any]] = None, info: Optional[AnyModelConfig] = None
    ) -> str:
        config = config or {}

        info = info or ModelProbe.probe(model_path, config, hash_algo=self._app_config.hashing_algorithm)

        model_path = model_path.resolve()

        # Models in the Invoke-managed models dir should use relative paths.
        if model_path.is_relative_to(self.app_config.models_path):
            model_path = model_path.relative_to(self.app_config.models_path)

        info.path = model_path.as_posix()

        if isinstance(info, CheckpointConfigBase):
            # Checkpoints have a config file needed for conversion. Same handling as the model weights - if it's in the
            # invoke-managed legacy config dir, we use a relative path.
            legacy_config_path = self.app_config.legacy_conf_path / info.config_path
            if legacy_config_path.is_relative_to(self.app_config.legacy_conf_path):
                legacy_config_path = legacy_config_path.relative_to(self.app_config.legacy_conf_path)
            info.config_path = legacy_config_path.as_posix()
        self.record_store.add_model(info)
        return info.key

    def _next_id(self) -> int:
        with self._lock:
            id = self._next_job_id
            self._next_job_id += 1
        return id

    def _guess_variant(self) -> Optional[ModelRepoVariant]:
        """Guess the best HuggingFace variant type to download."""
        precision = TorchDevice.choose_torch_dtype()
        return ModelRepoVariant.FP16 if precision == torch.float16 else None

    def _import_local_model(self, source: LocalModelSource, config: Optional[Dict[str, Any]]) -> ModelInstallJob:
        return ModelInstallJob(
            id=self._next_id(),
            source=source,
            config_in=config or {},
            local_path=Path(source.path),
            inplace=source.inplace or False,
        )

    def _import_from_hf(self, source: HFModelSource, config: Optional[Dict[str, Any]]) -> ModelInstallJob:
        # Add user's cached access token to HuggingFace requests
        source.access_token = source.access_token or HfFolder.get_token()
        if not source.access_token:
            self._logger.info("No HuggingFace access token present; some models may not be downloadable.")

        metadata = HuggingFaceMetadataFetch(self._session).from_id(source.repo_id, source.variant)
        assert isinstance(metadata, ModelMetadataWithFiles)
        remote_files = metadata.download_urls(
            variant=source.variant or self._guess_variant(),
            subfolder=source.subfolder,
            session=self._session,
        )

        return self._import_remote_model(
            source=source,
            config=config,
            remote_files=remote_files,
            metadata=metadata,
        )

    def _import_from_url(self, source: URLModelSource, config: Optional[Dict[str, Any]]) -> ModelInstallJob:
        # URLs from HuggingFace will be handled specially
        metadata = None
        fetcher = None
        try:
            fetcher = self.get_fetcher_from_url(str(source.url))
        except ValueError:
            pass
        kwargs: dict[str, Any] = {"session": self._session}
        if fetcher is not None:
            metadata = fetcher(**kwargs).from_url(source.url)
        self._logger.debug(f"metadata={metadata}")
        if metadata and isinstance(metadata, ModelMetadataWithFiles):
            remote_files = metadata.download_urls(session=self._session)
        else:
            remote_files = [RemoteModelFile(url=source.url, path=Path("."), size=0)]
        return self._import_remote_model(
            source=source,
            config=config,
            metadata=metadata,
            remote_files=remote_files,
        )

    def _import_remote_model(
        self,
        source: HFModelSource | URLModelSource,
        remote_files: List[RemoteModelFile],
        metadata: Optional[AnyModelRepoMetadata],
        config: Optional[Dict[str, Any]],
    ) -> ModelInstallJob:
        # TODO: Replace with tempfile.tmpdir() when multithreading is cleaned up.
        # Currently the tmpdir isn't automatically removed at exit because it is
        # being held in a daemon thread.
        if len(remote_files) == 0:
            raise ValueError(f"{source}: No downloadable files found")
        tmpdir = Path(
            mkdtemp(
                dir=self._app_config.models_path,
                prefix=TMPDIR_PREFIX,
            )
        )
        install_job = ModelInstallJob(
            id=self._next_id(),
            source=source,
            config_in=config or {},
            source_metadata=metadata,
            local_path=tmpdir,  # local path may change once the download has started due to content-disposition handling
            bytes=0,
            total_bytes=0,
        )
        # In the event that there is a subfolder specified in the source,
        # we need to remove it from the destination path in order to avoid
        # creating unwanted subfolders
        if isinstance(source, HFModelSource) and source.subfolder:
            root = Path(remote_files[0].path.parts[0])
            subfolder = root / source.subfolder
        else:
            root = Path(".")
            subfolder = Path(".")

        # we remember the path up to the top of the tmpdir so that it may be
        # removed safely at the end of the install process.
        install_job._install_tmpdir = tmpdir
        assert install_job.total_bytes is not None  # to avoid type checking complaints in the loop below

        files_string = "file" if len(remote_files) == 1 else "file"
        self._logger.info(f"Queuing model install: {source} ({len(remote_files)} {files_string})")
        self._logger.debug(f"remote_files={remote_files}")
        for model_file in remote_files:
            url = model_file.url
            path = root / model_file.path.relative_to(subfolder)
            self._logger.debug(f"Downloading {url} => {path}")
            install_job.total_bytes += model_file.size
            assert hasattr(source, "access_token")
            dest = tmpdir / path.parent
            dest.mkdir(parents=True, exist_ok=True)
            download_job = DownloadJob(
                source=url,
                dest=dest,
                access_token=source.access_token,
            )
            self._download_cache[download_job.source] = install_job  # matches a download job to an install job
            install_job.download_parts.add(download_job)

            self._download_queue.submit_download_job(
                download_job,
                on_start=self._download_started_callback,
                on_progress=self._download_progress_callback,
                on_complete=self._download_complete_callback,
                on_error=self._download_error_callback,
                on_cancelled=self._download_cancelled_callback,
            )
        return install_job

    def _stat_size(self, path: Path) -> int:
        size = 0
        if path.is_file():
            size = path.stat().st_size
        elif path.is_dir():
            for root, _, files in os.walk(path):
                size += sum(self._stat_size(Path(root, x)) for x in files)
        return size

    # ------------------------------------------------------------------
    # Callbacks are executed by the download queue in a separate thread
    # ------------------------------------------------------------------
    def _download_started_callback(self, download_job: DownloadJob) -> None:
        self._logger.info(f"Model download started: {download_job.source}")
        with self._lock:
            install_job = self._download_cache[download_job.source]
            install_job.status = InstallStatus.DOWNLOADING

            assert download_job.download_path
            if install_job.local_path == install_job._install_tmpdir:
                partial_path = download_job.download_path.relative_to(install_job._install_tmpdir)
                dest_name = partial_path.parts[0]
                install_job.local_path = install_job._install_tmpdir / dest_name

            # Update the total bytes count for remote sources.
            if not install_job.total_bytes:
                install_job.total_bytes = sum(x.total_bytes for x in install_job.download_parts)

    def _download_progress_callback(self, download_job: DownloadJob) -> None:
        with self._lock:
            install_job = self._download_cache[download_job.source]
            if install_job.cancelled:  # This catches the case in which the caller directly calls job.cancel()
                self._cancel_download_parts(install_job)
            else:
                # update sizes
                install_job.bytes = sum(x.bytes for x in install_job.download_parts)
                self._signal_job_downloading(install_job)

    def _download_complete_callback(self, download_job: DownloadJob) -> None:
        self._logger.info(f"Model download complete: {download_job.source}")
        with self._lock:
            install_job = self._download_cache[download_job.source]

            # are there any more active jobs left in this task?
            if install_job.downloading and all(x.complete for x in install_job.download_parts):
                self._signal_job_downloads_done(install_job)
                self._put_in_queue(install_job)

            # Let other threads know that the number of downloads has changed
            self._download_cache.pop(download_job.source, None)
            self._downloads_changed_event.set()

    def _download_error_callback(self, download_job: DownloadJob, excp: Optional[Exception] = None) -> None:
        with self._lock:
            install_job = self._download_cache.pop(download_job.source, None)
            assert install_job is not None
            assert excp is not None
            install_job.set_error(excp)
            self._logger.error(
                f"Cancelling {install_job.source} due to an error while downloading {download_job.source}: {str(excp)}"
            )
            self._cancel_download_parts(install_job)

            # Let other threads know that the number of downloads has changed
            self._downloads_changed_event.set()

    def _download_cancelled_callback(self, download_job: DownloadJob) -> None:
        with self._lock:
            install_job = self._download_cache.pop(download_job.source, None)
            if not install_job:
                return
            self._downloads_changed_event.set()
            self._logger.warning(f"Model download canceled: {download_job.source}")
            # if install job has already registered an error, then do not replace its status with cancelled
            if not install_job.errored:
                install_job.cancel()
            self._cancel_download_parts(install_job)

            # Let other threads know that the number of downloads has changed
            self._downloads_changed_event.set()

    def _cancel_download_parts(self, install_job: ModelInstallJob) -> None:
        # on multipart downloads, _cancel_components() will get called repeatedly from the download callbacks
        # do not lock here because it gets called within a locked context
        for s in install_job.download_parts:
            self._download_queue.cancel_job(s)

        if all(x.in_terminal_state for x in install_job.download_parts):
            # When all parts have reached their terminal state, we finalize the job to clean up the temporary directory and other resources
            self._put_in_queue(install_job)

    # ------------------------------------------------------------------------------------------------
    # Internal methods that put events on the event bus
    # ------------------------------------------------------------------------------------------------
    def _signal_job_running(self, job: ModelInstallJob) -> None:
        job.status = InstallStatus.RUNNING
        self._logger.info(f"Model install started: {job.source}")
        if self._event_bus:
            self._event_bus.emit_model_install_running(str(job.source))

    def _signal_job_downloading(self, job: ModelInstallJob) -> None:
        if self._event_bus:
            parts: List[Dict[str, str | int]] = [
                {
                    "url": str(x.source),
                    "local_path": str(x.download_path),
                    "bytes": x.bytes,
                    "total_bytes": x.total_bytes,
                }
                for x in job.download_parts
            ]
            assert job.bytes is not None
            assert job.total_bytes is not None
            self._event_bus.emit_model_install_downloading(
                str(job.source),
                local_path=job.local_path.as_posix(),
                parts=parts,
                bytes=job.bytes,
                total_bytes=job.total_bytes,
                id=job.id,
            )

    def _signal_job_downloads_done(self, job: ModelInstallJob) -> None:
        job.status = InstallStatus.DOWNLOADS_DONE
        self._logger.info(f"Model download complete: {job.source}")
        if self._event_bus:
            self._event_bus.emit_model_install_downloads_done(str(job.source))

    def _signal_job_completed(self, job: ModelInstallJob) -> None:
        job.status = InstallStatus.COMPLETED
        assert job.config_out
        self._logger.info(f"Model install complete: {job.source}")
        self._logger.debug(f"{job.local_path} registered key {job.config_out.key}")
        if self._event_bus:
            assert job.local_path is not None
            assert job.config_out is not None
            key = job.config_out.key
            self._event_bus.emit_model_install_completed(str(job.source), key, id=job.id)

    def _signal_job_errored(self, job: ModelInstallJob) -> None:
        self._logger.error(f"Model install error: {job.source}\n{job.error_type}: {job.error}")
        if self._event_bus:
            error_type = job.error_type
            error = job.error
            assert error_type is not None
            assert error is not None
            self._event_bus.emit_model_install_error(str(job.source), error_type, error, id=job.id)

    def _signal_job_cancelled(self, job: ModelInstallJob) -> None:
        self._logger.info(f"Model install canceled: {job.source}")
        if self._event_bus:
            self._event_bus.emit_model_install_cancelled(str(job.source), id=job.id)

    @staticmethod
    def get_fetcher_from_url(url: str) -> ModelMetadataFetchBase:
        if re.match(r"^https?://huggingface.co/[^/]+/[^/]+$", url.lower()):
            return HuggingFaceMetadataFetch
        raise ValueError(f"Unsupported model source: '{url}'")
