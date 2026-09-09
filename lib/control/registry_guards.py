"""Writer admission and persistent refusal of stale SQLite registry writers."""
from contextlib import contextmanager
import fcntl
import os

from .database import ControlDatabase, DATABASE_NAME
from .record_registry import RecordRegistry
from .store import StoreError, _directory_fd, _managed_start


GUARD_NAMES = tuple("registry_write_guard_" + action for action in ("insert", "update", "delete"))


def drop_guards(c):
    for name in GUARD_NAMES:
        c.execute("DROP TRIGGER IF EXISTS " + name)


def install_guards(c, domains):
    drop_guards(c)
    names = ",".join("'" + domain.replace("'", "''") + "'" for domain in domains)
    inactive = ("NOT EXISTS (SELECT 1 FROM records WHERE domain='registry-backend' AND scope='control' "
                "AND record_key='active') OR EXISTS (SELECT 1 FROM records WHERE domain='registry-backend' "
                "AND scope='control' AND record_key='transition')")
    for action, name in zip(("INSERT", "UPDATE", "DELETE"), GUARD_NAMES):
        target = ("NEW.domain IN (" + names + ")" if action == "INSERT" else
                  "OLD.domain IN (" + names + ")" if action == "DELETE" else
                  "(OLD.domain IN (" + names + ") OR NEW.domain IN (" + names + "))")
        c.execute(f"CREATE TRIGGER {name} BEFORE {action} ON records WHEN ({target}) AND ({inactive}) "
                  "BEGIN SELECT RAISE(ABORT,'SQLite registry writes are inactive during migration or rollback'); END")


@contextmanager
def migration_lock(config, *, exclusive):
    root = config.tasks_dir.parent / "registry-locks" / "migration"
    with _directory_fd(root, create=True,
            managed_start=_managed_start(root, ("control", "registry-locks", "migration"))) as fd:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StoreError("Control registry writer or migration is active; retry after it finishes") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def mutation_guard(config):
    with migration_lock(config, exclusive=False):
        if os.path.lexists(config.tasks_dir.parent / DATABASE_NAME):
            with ControlDatabase(config) as db, db.transaction() as c:
                registry = RecordRegistry("registry-backend", scope="control")
                if registry.read(c, "transition") is not None:
                    raise StoreError("Control registry migration is incomplete")
                active = registry.read(c, "active")
                if active is not None:
                    from .registry_backend import validate_backend
                    validate_backend(active["value"], config)
                elif c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (GUARD_NAMES[0],)).fetchone():
                    raise StoreError("SQLite registry writes are inactive after rollback")
        yield


@contextmanager
def legacy_mutation_guard(config):
    """Fence cached file writers before they create even an inert source lock."""
    from .registry_backend import control_config, selected_backend
    config = control_config(config)
    with migration_lock(config, exclusive=False):
        if selected_backend(config) != "files":
            raise StoreError("legacy registry writes are inactive after SQLite activation")
        yield
