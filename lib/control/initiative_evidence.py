"""Read-only evidence for retired initiatives.

The legacy initiative engine is retired (Keeper, 2026-10-05). Its records stay
in the Control database's ``records`` table, untouched; this module only reads
them. ``export`` writes every row of that table, whatever its domain, as JSON
Lines with the stored payload text and digest, so each record can be checked
against its digest without Asha.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Mapping, Sequence

from .config import ConfigError, load_config
from .database import DATABASE_NAME, ControlDatabase, DatabaseError
from .store import StoreError
from .text import terminal_safe


LIST_CONTRACT = "asha.initiative-evidence-list.v1"
SHOW_CONTRACT = "asha.initiative-evidence-show.v1"
EXPORT_FIELDS = (
    "record_id", "domain", "scope", "record_key", "payload", "digest",
    "revision", "state", "updated_at",
)
SEAL_FIELDS = ("seal_id", "node_id", "outcome", "jj_commit_id", "sealed_at")


def _usage(stream=sys.stdout) -> None:
    print("""asha initiative: retired initiative evidence (read-only)

Usage:
  asha initiative list [--json]
  asha initiative show <id|slug> [--json]
  asha initiative export        every Control record as JSON Lines on stdout

Initiatives were retired on 2026-10-05; their records stay in the Control
database and these commands only read them.""", file=stream)


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _options(args: Sequence[str], allowed: set[str]) -> tuple[list[str], set[str]]:
    positional, flags = [], set()
    for item in args:
        if item.startswith("--"):
            if item[2:] not in allowed:
                raise ValueError(f"unknown option {item}")
            flags.add(item[2:])
        else:
            positional.append(item)
    return positional, flags


def _open(env: Mapping[str, str]) -> ControlDatabase | None:
    config = load_config(env)
    if not (config.tasks_dir.parent / DATABASE_NAME).exists():
        return None
    return ControlDatabase(config, read_only=True)


def _initiatives(c) -> list[dict[str, Any]]:
    rows = c.execute(
        "SELECT payload FROM records WHERE domain='initiatives' AND scope='registry'"
        " ORDER BY updated_at, record_key"
    )
    return [json.loads(row[0]) for row in rows]


def _summary(initiative: Mapping[str, Any]) -> dict[str, Any]:
    return {key: initiative.get(key) for key in (
        "initiative_id", "slug", "label", "state", "created_at", "updated_at",
    )}


def _list(env: Mapping[str, str], flags: set[str]) -> int:
    db = _open(env)
    initiatives: list[dict[str, Any]] = []
    if db is not None:
        with db, db.transaction() as c:
            initiatives = [_summary(item) for item in _initiatives(c)]
    if "json" in flags:
        _json({"contract": LIST_CONTRACT, "initiatives": initiatives})
        return 0
    if not initiatives:
        print("No initiatives.")
    for item in initiatives:
        print(f"{terminal_safe(str(item['slug'])):<40} {terminal_safe(str(item['state'])):<11} "
              f"{terminal_safe(str(item['updated_at']))[:19]:<19} {item['initiative_id']}")
    return 0


def _show(env: Mapping[str, str], selector: str, flags: set[str]) -> int:
    db = _open(env)
    if db is None:
        raise ValueError(f"initiative not found: {selector}")
    with db, db.transaction() as c:
        matches = [item for item in _initiatives(c)
                   if selector in (item.get("initiative_id"), item.get("slug"))]
        if not matches:
            raise ValueError(f"initiative not found: {selector}")
        if len(matches) > 1:
            raise ValueError(f"slug {selector} names {len(matches)} initiatives; use an id")
        initiative = matches[0]
        scope = initiative["initiative_id"]
        counts = {row[0]: row[1] for row in c.execute(
            "SELECT domain,count(*) FROM records WHERE scope=? AND domain LIKE 'initiative.%'"
            " GROUP BY domain ORDER BY domain", (scope,))}
        seals = []
        for row in c.execute(
            "SELECT payload FROM records WHERE domain='initiative.seals' AND scope=?"
            " ORDER BY updated_at, record_key", (scope,)):
            seal = json.loads(row[0])
            seals.append({key: seal.get(key) for key in SEAL_FIELDS})
    if "json" in flags:
        _json({"contract": SHOW_CONTRACT, "initiative": initiative, "records": counts, "seals": seals})
        return 0
    for key in ("label", "slug", "initiative_id", "state", "created_at", "updated_at", "objective"):
        print(f"{key + ':':<15} {terminal_safe(str(initiative.get(key, '')))}")
    print("records:")
    for domain, count in counts.items():
        print(f"  {domain:<36} {count}")
    for seal in seals:
        print(f"seal {seal['seal_id']}  {terminal_safe(str(seal['node_id']))}  "
              f"{terminal_safe(str(seal['outcome']))}  {seal['jj_commit_id']}")
    return 0


def _export(env: Mapping[str, str]) -> int:
    db = _open(env)
    count = 0
    if db is not None:
        # One read transaction is one snapshot, so the export is consistent
        # while live sessions keep writing their own domains.
        with db, db.transaction() as c:
            for row in c.execute("SELECT " + ",".join(EXPORT_FIELDS) + " FROM records ORDER BY record_id"):
                _json(dict(zip(EXPORT_FIELDS, tuple(row))))
                count += 1
    print(f"asha initiative: exported {count} records", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if env is None else env
    try:
        if not args or args[0] in {"-h", "--help", "help"}:
            _usage(sys.stdout if args else sys.stderr)
            return 0 if args else 2
        verb, tail = args[0], args[1:]
        if verb == "list":
            positional, flags = _options(tail, {"json"})
            if positional:
                raise ValueError("list takes no arguments")
            return _list(values, flags)
        if verb == "show":
            positional, flags = _options(tail, {"json"})
            if len(positional) != 1:
                raise ValueError("show requires one initiative id or slug")
            return _show(values, positional[0], flags)
        if verb == "export":
            positional, flags = _options(tail, set())
            if positional:
                raise ValueError("export takes no arguments")
            return _export(values)
        raise ValueError(f"unknown command {verb}; initiatives are retired and read-only")
    except (ConfigError, StoreError, DatabaseError, ValueError) as exc:
        print(f"asha initiative: {terminal_safe(str(exc))}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # The reader stopped early (`export | head`). Stop quietly, and point
        # stdout at /dev/null so the interpreter's exit flush cannot complain.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 1
