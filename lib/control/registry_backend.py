"""Explicit authoritative registry selection; database presence is not activation."""
from datetime import datetime
import importlib
import os
from pathlib import Path
import re
from types import SimpleNamespace

from .database import ControlDatabase, DATABASE_NAME
from .model import canonical_uuid
from .record_registry import RecordRegistry
from .store import StoreError


BACKEND_CONTRACT = "asha.control-registry-backend.v1"
_STORES = {
    "tasks": ("sqlite_tasks", "SQLiteTaskStore"),
    "rooms": ("sqlite_rooms", "SQLiteRoomStore"),
    "initiatives": ("orchestration.sqlite_store", "SQLiteInitiativeStore"),
    "creation-journals": ("sqlite_journals", "SQLiteCreationJournalStore"),
    "prunes": ("sqlite_auxiliary", "SQLitePruneRecordStore"),
    "repository-inits": ("sqlite_colocation", "SQLiteColocationIntentStore"),
    "ownership": ("sqlite_ownership", "SQLiteOwnershipStore"),
}


def control_config(config):
    config = getattr(config, "control", config)
    if hasattr(config, "tasks_dir"):
        return config
    # Rooms historically accept the small shell configuration. Only the same
    # canonical Asha root is needed by their database and lifecycle locks.
    root = Path(config.asha_home)
    return SimpleNamespace(asha_home=root, tasks_dir=root / "state/control/tasks")


def validate_backend(value, config):
    fields = {"contract", "backend", "state", "activation_id", "source_root", "stage_digest", "activated_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise StoreError("invalid Control registry backend marker fields")
    if (value["contract"] != BACKEND_CONTRACT or value["backend"] != "sqlite"
            or value["state"] != "active"):
        raise StoreError("unsupported Control registry backend marker")
    canonical_uuid(value["activation_id"])
    config = control_config(config)
    if value["source_root"] != str(config.asha_home):
        raise StoreError("Control registry backend marker belongs to another root")
    if not isinstance(value["stage_digest"], str) or re.fullmatch(r"[0-9a-f]{64}", value["stage_digest"]) is None:
        raise StoreError("invalid Control registry stage digest")
    try:
        stamp = value["activated_at"]
        if not isinstance(stamp, str) or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", stamp) is None:
            raise ValueError
        datetime.fromisoformat(stamp[:-1] + "+00:00")
    except ValueError as exc:
        raise StoreError("invalid Control registry activation timestamp") from exc
    return value


def selection(connection, config):
    registry = RecordRegistry("registry-backend", scope="control")
    transition = registry.read(connection, "transition")
    if transition is not None:
        # Until activation/recovery atomically removes it, every constructor
        # refuses; an unrecognized transition is never interpreted as legacy.
        raise StoreError("Control registry migration is incomplete; resume or recover activation")
    row = registry.read(connection, "active")
    if row is not None:
        if len(row["raw"]) > 4096:
            raise StoreError("Control registry backend marker is oversized")
        return validate_backend(row["value"], config)
    staged = RecordRegistry("registry-migration", scope="control")
    if staged.read(connection, "attempt") is not None or staged.read(connection, "stage") is not None:
        raise StoreError("offline registry staging root is not an active Control backend")
    return None


def selected_backend(config):
    config = control_config(config)
    path = config.tasks_dir.parent / DATABASE_NAME
    if not os.path.lexists(path):
        return "files"
    with ControlDatabase(config) as database, database.transaction() as connection:
        return "sqlite" if selection(connection, config) is not None else "files"


def construct_store(cls, base, config, domain):
    if cls is not base or selected_backend(config) == "files":
        return object.__new__(cls)
    module, name = _STORES[domain]
    selected = getattr(importlib.import_module("." + module, __package__), name)
    return object.__new__(selected)
