"""Operator commands for staged registry activation and recovery."""
import argparse
import json
import os
from pathlib import Path

from .config import load_config
from .database import ControlDatabase, DATABASE_NAME
from .record_registry import RecordRegistry
from .registry_activation import activate_registries, recover_activation, rollback_registries
from .registry_backend import validate_backend
from .registry_guards import GUARD_NAMES
from .registry_migration import STAGED_DOMAINS, stage_registries
from .sessions import refuse_managed_operator
from .store import StoreError


def status(config):
    """Read authoritative markers and counts without selecting or repairing stores."""
    result = {"contract": "asha.registry-status.v1", "backend": "files", "state": "legacy", "counts": {}}
    if not os.path.lexists(config.tasks_dir.parent / DATABASE_NAME):
        return result
    with ControlDatabase(config, read_only=True) as db, db.transaction() as c:
        registry = RecordRegistry("registry-backend", scope="control")
        transition = registry.read(c, "transition")
        active = registry.read(c, "active")
        if transition is not None:
            from .registry_activation import _transition
            row, _ = _transition(c, config)
            return {**result, "backend": "unavailable", "state": "incomplete",
                    "activation_id": row["value"]["activation_id"], "mode": row["value"]["mode"],
                    "resume_effect": "finish " + row["value"]["mode"],
                    "abort_effect": "restore SQLite authority" if row["value"]["mode"] == "rollback" else "restore file authority",
                    "recovery_requires_unchanged_recorded_entries": True,
                    "abort_action": "asha control registry recover --action abort",
                    "next_action": "asha control registry recover --action resume"}
        if active is not None:
            marker = validate_backend(active["value"], config)
            guards = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            if not set(GUARD_NAMES) <= guards:
                raise StoreError("active registry writer guards are missing")
            result.update(backend="sqlite", state="active", activation_id=marker["activation_id"])
            result["counts"] = {domain: c.execute("SELECT count(*) FROM records WHERE domain=?", (domain,)).fetchone()[0]
                                for domain in STAGED_DOMAINS}
            return result
        staging = RecordRegistry("registry-migration", scope="control")
        if staging.read(c, "stage") is not None or staging.read(c, "attempt") is not None:
            return {**result, "backend": "unavailable", "state": "offline-stage",
                    "next_action": "Activate this stage from its original Asha root"}
        return result


def main(argv, *, env=None):
    parser = argparse.ArgumentParser(prog="asha control registry")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "stage", "activate", "recover", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true")
        if name in {"stage", "activate"}:
            command.add_argument("--stage-home", required=True, type=Path)
        if name == "recover":
            command.add_argument("--action", choices=("resume", "abort"), default="resume")
    args = parser.parse_args(argv)
    values = os.environ if env is None else env
    config = load_config(values)
    if args.command == "status":
        payload = status(config)
    else:
        refuse_managed_operator(config, values)
        if any(values.get(key) for key in ("ASHA_CONTROL_TASK_ID", "ASHA_CONTROL_RUN_ID", "ASHA_ROOM_ID")):
            raise StoreError("task workers and Room actors cannot change registry authority")
        try:
            if args.command == "stage":
                payload = stage_registries(config, args.stage_home)
            elif args.command == "activate":
                payload = activate_registries(config, args.stage_home)
            elif args.command == "recover":
                payload = recover_activation(config, action=args.action)
            else:
                payload = rollback_registries(config)
        except OSError as exc:
            raise StoreError("registry operation refused: " + str(exc)) from exc
    if args.json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print("Control registries: " + payload["state"])
        if payload.get("backend"):
            print("Backend: " + payload["backend"])
        if payload.get("next_action"):
            if payload.get("mode"):
                print("Interrupted operation: " + payload["mode"])
                print("Resume will " + payload["resume_effect"] + "; abort will " + payload["abort_effect"])
                print(payload["abort_action"])
            print(payload["next_action"])
    return 0
