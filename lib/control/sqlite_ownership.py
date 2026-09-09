"""Ownership sidecars for SQLite journals with unchanged retained inode bindings."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat

from .store import StoreError, _directory_fd, _managed_start, _task_lock
from .transaction import MaterializationOwnershipStore, JournalError, _validate_sidecar_binding
from .registry_guards import mutation_guard


class _RetainedOwnershipReader(MaterializationOwnershipStore):
    @contextmanager
    def _directory(self, *, create):
        if create:
            raise JournalError("retained ownership directory is read-only")
        parent = self.directory.parent
        with _directory_fd(parent, create=False,
                managed_start=_managed_start(parent, ("control",))) as parent_fd:
            if parent_fd is None:
                yield None
                return
            try:
                fd = os.open(self.directory.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd)
            except FileNotFoundError:
                yield None
                return
            except OSError as exc:
                raise JournalError(f"cannot open retained ownership directory: {exc}") from exc
            try:
                metadata = os.fstat(fd)
                if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) not in {0o700, 0o500}):
                    raise JournalError("retained ownership directory ownership or mode changed")
                # Sidecar files still require 0600: mode is part of file_fact.
                yield fd
            finally:
                os.close(fd)


class SQLiteOwnershipStore(MaterializationOwnershipStore):
    def __init__(self, config):
        super().__init__(config)
        self.config = config
        self.directory = config.tasks_dir.parent / "materialization-ownership"
        self._managed_start = _managed_start(self.directory, ("control", "materialization-ownership"))
        self.retained = _RetainedOwnershipReader(config)
        self.lock_root = config.tasks_dir.parent / "registry-locks" / "ownership"

    def _write_guard(self):
        return mutation_guard(self.config)

    @staticmethod
    def _existing(store, task_id):
        # Use the base locator to avoid recursing into the resolving path method.
        name = MaterializationOwnershipStore.path(store, task_id).name
        try:
            with store._directory(create=False) as directory_fd:
                if directory_fd is None:
                    return None
                try:
                    return store._read_raw(directory_fd, name)
                except JournalError as exc:
                    if str(exc) == "materialization ownership sidecar is missing":
                        return None
                    raise
        except StoreError as exc:
            raise JournalError(str(exc)) from exc

    def _locations(self, task_id):
        retained = self._existing(self.retained, task_id)
        current = self._existing(self, task_id)
        if retained is not None and current is not None:
            raise JournalError("ownership sidecar exists in both current and retained roots")
        return retained, current

    def path(self, task_id):
        retained, _ = self._locations(task_id)
        return self.retained.path(task_id) if retained is not None else super().path(task_id)

    def write(self, task_id, plan_digest, facts, *, failure_injector=None):
        raw = self._raw(task_id, plan_digest, facts)
        try:
            with mutation_guard(self.config), _directory_fd(self.lock_root, create=True,
                    managed_start=_managed_start(self.lock_root, ("control", "registry-locks", "ownership"))) as fd:
                with _task_lock(fd, task_id):
                    retained, _ = self._locations(task_id)
                    if retained is not None:
                        if retained[0] != raw:
                            raise JournalError("retained ownership sidecar is foreign or corrupt")
                        return self.retained._binding(task_id, plan_digest, raw, len(facts), retained[1])
                    return super().write(task_id, plan_digest, facts, failure_injector=failure_injector)
        except StoreError as exc:
            raise JournalError(str(exc)) from exc

    def read(self, binding):
        binding = _validate_sidecar_binding(binding, "materialization ownership")
        path = Path(binding["path"])
        self._locations(path.stem)
        if path.parent == self.retained.directory:
            return self.retained.read(binding)
        return super().read(binding)
