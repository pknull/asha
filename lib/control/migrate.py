"""`asha migrate` — the one-shot, fail-closed move to the single asha root.

Everything here follows three precedents already recorded in this repository:
the memory-migration pattern (copy with a per-file sha256 manifest, an
idempotence marker, sources treated as evidence), the CHANGELOG's relocation
post-mortem (a readable-looking legacy copy is worse than failing loudly, so
the old roots keep a supersession banner), and the 0775-workspace remediation
style (refuse with the exact operator command, never auto-repair).

The migrator never constructs a ControlConfig and never calls load_config:
`migration_layout` in config.py is the single shared derivation, so the gate
that refuses legacy layouts and the command that fixes them cannot disagree —
and the gate cannot brick the tool.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import (
    LEGACY_BANNER_NAME,
    ConfigError,
    legacy_populated,
    migration_layout,
    namespace_remediation,
    namespace_safety_step,
    reject_symlink_components,
)

MIGRATE_PLAN_CONTRACT = "asha.control-migrate-plan.v1"
MARKER_VERSION = 1
_PHASES = (
    "preflight-passed", "manifest-written", "state-moved", "move-verified",
    "perms-normalized", "husks-retired", "materializations-cleared",
    "workspaces-root-created", "cache-moved", "banners-written",
)
_HUSK_KINDS = ("tasks", "transactions", "prunes")
_MAX_RECORD_BYTES = 1 << 20


class MigrateError(Exception):
    """A migration step refused or failed; the message is operator-ready."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> dict[str, str]:
    """Relative path -> sha256 for every regular file under root."""
    entries: dict[str, str] = {}
    for current, _dirs, files in os.walk(root):
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                raise MigrateError(f"symlink inside the state tree refuses migration: {path}")
            entries[str(path.relative_to(root))] = _sha256(path)
    return entries


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, body.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_bounded_json(path: Path) -> Any:
    if path.stat().st_size > _MAX_RECORD_BYTES:
        raise MigrateError(f"record exceeds the migration read bound: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- inventory
def build_inventory(layout: Mapping[str, Path]) -> dict[str, Any]:
    """Read-only picture of everything the migration would touch."""
    control = layout["legacy_control"]
    counts = {kind: 0 for kind in _HUSK_KINDS}
    lifecycles: dict[str, int] = {}
    non_archived_tasks: list[str] = []
    ownership_sidecars = 0
    if (control / "tasks").is_dir():
        for record in sorted((control / "tasks").glob("*.json")):
            counts["tasks"] += 1
            try:
                lifecycle = _read_bounded_json(record).get("lifecycle", "?")
            except (OSError, ValueError):
                lifecycle = "unreadable"
            lifecycles[lifecycle] = lifecycles.get(lifecycle, 0) + 1
            if lifecycle != "archived":
                non_archived_tasks.append(record.stem)
    if (control / "transactions").is_dir():
        counts["transactions"] = len(list((control / "transactions").glob("*.json")))
        ownership_sidecars = len(list((control / "transactions").glob("*.ownership")))
    if (control / "prunes").is_dir():
        counts["prunes"] = len(list((control / "prunes").glob("*.json")))

    initiatives: list[dict[str, str]] = []
    if (control / "initiatives").is_dir():
        for directory in sorted((control / "initiatives").iterdir()):
            record = directory / "initiative.json"
            if not record.is_file():
                continue
            try:
                data = _read_bounded_json(record)
            except (OSError, ValueError):
                data = {}
            initiatives.append({
                "initiative_id": directory.name,
                "state": str(data.get("state", "unreadable")),
                "slug": str(data.get("slug", "?")),
            })

    materializations: list[dict[str, Any]] = []
    empty_repo_keys: list[str] = []
    workspaces = layout["legacy_workspaces"]
    if workspaces.is_dir():
        for repo_key in sorted(workspaces.iterdir()):
            if not repo_key.is_dir() or repo_key.is_symlink():
                continue
            children = [item for item in repo_key.iterdir()]
            live = [item for item in children if item.name == "materializations" and item.is_dir()]
            if live:
                for workspace in sorted(live[0].iterdir()):
                    if workspace.is_dir() and not workspace.name.startswith("."):
                        size = sum(
                            item.stat().st_size
                            for item in workspace.rglob("*") if item.is_file()
                        )
                        materializations.append({
                            "path": str(workspace), "bytes": size,
                            "repo_key": repo_key.name,
                        })
            if not children:
                empty_repo_keys.append(str(repo_key))

    state_bytes = 0
    if layout["legacy_state"].is_dir():
        state_bytes = sum(
            item.stat().st_size
            for item in layout["legacy_state"].rglob("*") if item.is_file()
        )
    cache_entries = 0
    if layout["legacy_cache"].is_dir():
        cache_entries = len(list(layout["legacy_cache"].iterdir()))

    return {
        "husks": counts, "ownership_sidecars": ownership_sidecars,
        "task_lifecycles": lifecycles, "non_archived_tasks": non_archived_tasks,
        "initiatives": initiatives, "materializations": materializations,
        "empty_repo_keys": empty_repo_keys, "state_bytes": state_bytes,
        "cache_entries": cache_entries,
    }


# ---------------------------------------------------------------- preflight
def preflight(layout: Mapping[str, Path], inventory: Mapping[str, Any], *,
              tmux_sessions: list[str]) -> list[str]:
    """Every refusal, or an empty list. Read-only."""
    problems: list[str] = []
    new_state = layout["new_state"]
    if new_state.exists() and not layout["marker"].exists() and not layout["journal"].exists():
        try:
            tenants = sorted(item.name for item in new_state.iterdir())
        except OSError:
            tenants = ["<unreadable>"]
        if tenants:
            problems.append(
                f"{new_state} already exists holding {', '.join(tenants[:4])}"
                f"{'…' if len(tenants) > 4 else ''} and no migration marker; the "
                "atomic rename cannot merge into it; move it aside and re-run"
            )
        # An empty new_state is replaced atomically by rename(2); no refusal.
    euid = os.geteuid()
    current = Path(layout["asha_home"].anchor)
    boundary = False
    for part in layout["asha_home"].parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        problem, boundary = namespace_safety_step(
            metadata, euid, boundary, namespace_root=current == layout["asha_home"],
        )
        if problem:
            problems.append(
                f"{problem} rejected in ASHA_HOME: {current}"
                f"{namespace_remediation(problem, current)}"
            )
            break
    live = [name for name in tmux_sessions if name.startswith("asha-")]
    if live:
        problems.append(
            f"{len(live)} live Control tmux session(s) exist "
            f"({', '.join(live[:3])}{'…' if len(live) > 3 else ''}); stop them "
            "(asha task stop / tmux kill-session) and re-run"
        )
    for task_id in inventory["non_archived_tasks"]:
        problems.append(
            f"task {task_id} is not archived; only a fully archived registry can migrate"
        )
    for initiative in inventory["initiatives"]:
        if initiative["state"] != "archived":
            problems.append(
                f"initiative {initiative['slug']} is {initiative['state']}; "
                "stop or archive it first"
            )
    try:
        anchor_device = layout["asha_home"].parent.stat().st_dev
        for key in ("legacy_state", "legacy_workspaces", "legacy_cache"):
            path = layout[key]
            if path.exists() and path.stat().st_dev != anchor_device:
                problems.append(
                    f"{path} and {layout['asha_home']} are on different filesystems; "
                    "the atomic rename this command requires is impossible across "
                    "devices; relocate manually"
                )
    except OSError as exc:
        problems.append(f"cannot compare devices: {exc}")
    for key in ("legacy_state", "legacy_workspaces", "legacy_cache", "asha_home"):
        try:
            reject_symlink_components(layout[key], key)
        except ConfigError as exc:
            problems.append(str(exc))
    return problems


# ---------------------------------------------------------------- phases
def _journal_read(layout: Mapping[str, Path]) -> dict[str, Any] | None:
    path = layout["journal"]
    if not path.is_file():
        return None
    try:
        value = _read_bounded_json(path)
    except (OSError, ValueError) as exc:
        raise MigrateError(f"migration journal unreadable: {exc}; inspect {path}") from exc
    if not isinstance(value, dict) or value.get("phase") not in _PHASES:
        raise MigrateError(f"migration journal is malformed; inspect {path}")
    return value


def _journal_write(layout: Mapping[str, Path], phase: str, extra: dict[str, Any]) -> None:
    prior = {}
    if layout["journal"].is_file():
        try:
            prior = _read_bounded_json(layout["journal"])
        except (OSError, ValueError):
            prior = {}
    stamp = extra.pop("stamp", None) or prior.get("stamp")
    payload = {"phase": phase, "at": _now(), **extra}
    if stamp:
        payload["stamp"] = stamp
    _write_private(layout["journal"], payload)


def _phase_reached(journal: dict[str, Any] | None, phase: str) -> bool:
    if journal is None:
        return False
    return _PHASES.index(journal["phase"]) >= _PHASES.index(phase)


def _retire_husks(layout: Mapping[str, Path], stamp: str) -> dict[str, Any]:
    control = layout["new_control"]
    retired = control / f"retired-{stamp}"
    entries: dict[str, str] = {}
    moved = {kind: 0 for kind in _HUSK_KINDS}
    retired.mkdir(mode=0o700, exist_ok=True)
    manifest_path = retired / "manifest.json"
    prior_files: dict[str, str] = {}
    prior_counts: dict[str, int] = {}
    if manifest_path.is_file():
        # A crash between retiring and journaling leaves a manifest beside an
        # incomplete move; a resume MERGES rather than clobbering — the digests
        # of already-retired records are evidence and must survive byte-exact.
        prior = _read_bounded_json(manifest_path)
        prior_files = dict(prior.get("files", {}))
        prior_counts = dict(prior.get("counts", {}))
    for kind in _HUSK_KINDS:
        source = control / kind
        destination = retired / kind
        destination.mkdir(mode=0o700, exist_ok=True)
        if not source.is_dir():
            continue
        patterns = ("*.json", "*.ownership", "*.lock") if kind == "transactions" else ("*.json", "*.lock")
        for pattern in patterns:
            for record in sorted(source.glob(pattern)):
                entries[f"{kind}/{record.name}"] = _sha256(record)
                os.rename(record, destination / record.name)
                moved[kind] += 1
    merged_counts = {
        kind: moved[kind] + int(prior_counts.get(kind, 0)) for kind in _HUSK_KINDS
    }
    manifest = {
        "reason": "path-bound record superseded by the ASHA_HOME migration",
        "retired_at": _now(), "counts": merged_counts,
        "files": {**prior_files, **entries},
    }
    manifest["review_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()
    _write_private(manifest_path, manifest)
    return {"retired_dir": str(retired), "counts": merged_counts}


def materialization_registration_name(repo_key: str, name: str) -> str:
    """The jj workspace name production registers for a materialization.

    Byte-identical to the derivation in prepare.py: the DIRECTORY name is not
    the registration name, and forgetting by directory name strands a dangling
    registration in the source repository for every real materialization.
    """
    return "asha-materialization-" + hashlib.sha256(
        f"{repo_key}\0{name}".encode("utf-8")
    ).hexdigest()[:24]


def _clear_materializations(inventory: Mapping[str, Any], layout: Mapping[str, Path],
                            jj_forget) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    report: list[dict[str, Any]] = []
    residue_errors: list[dict[str, str]] = []
    for item in inventory["materializations"]:
        workspace = Path(item["path"])
        entry: dict[str, Any] = {**item, "forgotten": False, "deleted": False}
        pointer = workspace / ".jj/repo"
        registration = materialization_registration_name(item["repo_key"], workspace.name)
        try:
            source_repo = Path(pointer.read_text().strip()).parent.parent
            jj_forget(source_repo, registration)
            entry["forgotten"] = True
        except Exception as exc:  # noqa: BLE001 - degrade per design: dangling registrations are jj-tolerated
            entry["forget_error"] = str(exc)
        try:
            reject_symlink_components(workspace, "materialization")
            shutil.rmtree(workspace)
            entry["deleted"] = True
        except (OSError, ConfigError) as exc:
            entry["delete_error"] = str(exc)
        report.append(entry)
    workspaces = layout["legacy_workspaces"]
    if workspaces.is_dir():
        for repo_key in sorted(workspaces.iterdir()):
            if repo_key.is_symlink() or not repo_key.is_dir():
                continue
            for child in ("materializations",):
                candidate = repo_key / child
                if not candidate.is_dir():
                    continue
                # Residue includes .journals/ (a DIRECTORY of materialization
                # journal records) and dotfile markers. unlink() on a
                # directory raises, and an uncaught error here — after the
                # state rename — locked the whole CLI in rehearsal. Every
                # residue failure degrades to a report line instead.
                for residue in sorted(candidate.glob(".*")):
                    try:
                        if residue.is_dir() and not residue.is_symlink():
                            reject_symlink_components(residue, "materialization residue")
                            shutil.rmtree(residue)
                        else:
                            residue.unlink(missing_ok=True)
                    except (OSError, ConfigError) as exc:
                        residue_errors.append({"path": str(residue), "error": str(exc)})
                try:
                    candidate.rmdir()
                except OSError:
                    pass
            try:
                repo_key.rmdir()
            except OSError:
                pass
    return report, residue_errors


def _write_banner(directory: Path, layout: Mapping[str, Path]) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / LEGACY_BANNER_NAME).write_text(
        f"Superseded on {_now()} by `asha migrate`.\n\n"
        f"Live state: {layout['new_state']}\n"
        f"Marker:     {layout['marker']}\n\n"
        "A restore that resurrects files here is ignored by the tools; the\n"
        "layout gate keys on the marker and this banner.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- command
def run(args: list[str], env: Mapping[str, str], *, tmux=None, jj=None) -> int:
    dry_run = "--dry-run" in args
    assume_yes = "--yes" in args
    as_json = "--json" in args
    unknown = [item for item in args if item not in ("--dry-run", "--yes", "--json")]
    if unknown:
        raise MigrateError(f"unknown migrate argument: {unknown[0]}")
    layout = migration_layout(env)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if as_json and not dry_run:
        raise MigrateError("--json is only meaningful with --dry-run")
    marker = layout["marker"]
    if marker.is_file():
        layout["journal"].unlink(missing_ok=True)
        layout["staging_manifest"].unlink(missing_ok=True)
        print(f"asha migrate: already migrated on "
              f"{_read_bounded_json(marker).get('migrated_at', '?')}; nothing to do")
        return 0
    journal = _journal_read(layout)

    if not legacy_populated(layout["legacy_control"]) and not legacy_populated(
            layout["legacy_workspaces"]) and journal is None:
        print("asha migrate: no legacy Control state under "
              f"{layout['legacy_state']} or {layout['legacy_workspaces']}; nothing to migrate")
        return 0

    if tmux is None:
        from .tmux import TmuxAdapter, TmuxError
        try:
            sessions = TmuxAdapter().list_sessions()
        except TmuxError:
            sessions = []
    else:
        sessions = tmux
    try:
        inventory = build_inventory(layout)
    except OSError as exc:
        raise MigrateError(
            f"cannot inventory the legacy tree: {exc}; fix permissions and re-run"
        ) from exc
    problems = [] if journal is not None else preflight(layout, inventory, tmux_sessions=sessions)
    if problems:
        for problem in problems:
            print(f"asha migrate: {problem}", file=sys.stderr)
        return 2

    plan = {
        "contract": MIGRATE_PLAN_CONTRACT,
        "legacy": {key: str(layout[key]) for key in
                   ("legacy_state", "legacy_workspaces", "legacy_cache")},
        "new": {key: str(layout[key]) for key in
                ("new_state", "new_workspaces", "new_cache")},
        "inventory": inventory,
        "resume": None if journal is None else journal["phase"],
    }
    if as_json and dry_run:
        print(json.dumps(plan, indent=1, sort_keys=True))
        return 0
    total_mat = sum(item["bytes"] for item in inventory["materializations"])
    if dry_run or not assume_yes:
        print(f"WILL MOVE    {layout['legacy_state']} -> {layout['new_state']} "
              f"({inventory['state_bytes']} bytes)")
        print(f"WILL RETIRE  {inventory['husks']['tasks']} task record(s), "
              f"{inventory['husks']['transactions']} journal(s) "
              f"(+{inventory['ownership_sidecars']} sidecar(s)), "
              f"{inventory['husks']['prunes']} prune record(s) "
              f"-> retired-{stamp}/")
        for item in inventory["materializations"]:
            print(f"WILL DELETE  {item['path']} ({item['bytes']} bytes; regenerable)")
        print(f"WILL MOVE    {layout['legacy_cache']} -> {layout['new_cache']}"
              if layout["legacy_cache"].is_dir() else "             (no cache to move)")
        print(f"WILL CREATE  {layout['new_workspaces']}, banners at both legacy roots, marker")
    if dry_run:
        print("dry run: no changes were made")
        return 0
    if not assume_yes:
        print(
            "asha migrate: preflight passed; re-run with --yes to proceed "
            f"(deletes {total_mat} bytes of regenerable materializations; retires "
            f"{inventory['husks']['tasks']}+{inventory['husks']['transactions']}"
            f"+{inventory['husks']['prunes']} records)"
        )
        return 2

    if jj is None:
        from .jj import JjAdapter

        def jj_forget(source: Path, name: str) -> None:
            JjAdapter().forget_workspace(source, name)
    else:
        jj_forget = jj

    layout["asha_home"].mkdir(mode=0o700, exist_ok=True)
    if journal is not None:
        if journal.get("stamp"):
            stamp = journal["stamp"]
        elif _phase_reached(journal, "manifest-written"):
            raise MigrateError(
                "resume journal carries no date stamp past the manifest phase; "
                "a silently regenerated stamp would split the retirement across "
                f"two directories — inspect {layout['journal']}"
            )
    if not _phase_reached(journal, "preflight-passed"):
        _journal_write(layout, "preflight-passed", {"stamp": stamp})
    if not _phase_reached(journal, "manifest-written"):
        manifest = _tree_manifest(layout["legacy_state"])
        _write_private(layout["staging_manifest"], {"files": manifest, "at": _now()})
        _journal_write(layout, "manifest-written", {"files": len(manifest)})
    if not _phase_reached(journal, "state-moved"):
        # Resume-safe: a crash between the rename and its journal write leaves
        # the tree at the new root with the journal one phase behind. The
        # rename runs only when the source still exists; a moved-but-unlogged
        # tree just advances the journal, and verification (its own phase)
        # still runs on every path to completion.
        if layout["legacy_state"].exists():
            os.rename(layout["legacy_state"], layout["new_state"])
        elif not layout["new_state"].exists():
            raise MigrateError(
                "neither the legacy state tree nor the new one exists; nothing "
                "to move and nothing to resume — inspect "
                f"{layout['journal']}"
            )
        for parent in (layout["new_state"].parent, layout["legacy_state"].parent):
            if parent.is_dir():
                descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        _journal_write(layout, "state-moved", {})
    if not _phase_reached(journal, "move-verified"):
        staged = _read_bounded_json(layout["staging_manifest"])["files"]
        current = _tree_manifest(layout["new_state"])
        if staged != current:
            drift = len(set(staged.items()) ^ set(current.items()))
            raise MigrateError(
                f"post-move verification failed for {drift} file entr(ies); tree "
                f"preserved at {layout['new_state']} — do not delete; see "
                f"{layout['staging_manifest']}"
            )
        _journal_write(layout, "move-verified", {})
    if not _phase_reached(journal, "perms-normalized"):
        os.chmod(layout["new_state"], 0o700)
        if layout["new_control"].is_dir():
            os.chmod(layout["new_control"], 0o700)
            trust = layout["new_control"] / "trust.jsonl"
            if trust.is_file():
                os.chmod(trust, 0o600)
        _journal_write(layout, "perms-normalized", {})
    retired_info: dict[str, Any] = {}
    if not _phase_reached(journal, "husks-retired"):
        retired_info = _retire_husks(layout, stamp)
        _journal_write(layout, "husks-retired", retired_info)
    materialization_report: list[dict[str, Any]] = []
    residue_errors: list[dict[str, str]] = []
    if not _phase_reached(journal, "materializations-cleared"):
        materialization_report, residue_errors = _clear_materializations(
            inventory, layout, jj_forget)
        _journal_write(layout, "materializations-cleared", {})
    if not _phase_reached(journal, "workspaces-root-created"):
        layout["new_workspaces"].mkdir(mode=0o700, exist_ok=True)
        _journal_write(layout, "workspaces-root-created", {})
    if not _phase_reached(journal, "cache-moved"):
        if layout["legacy_cache"].is_dir():
            try:
                os.rename(layout["legacy_cache"], layout["new_cache"])
            except OSError as exc:
                print(f"asha migrate: cache move failed (regenerable, continuing): {exc}",
                      file=sys.stderr)
        _journal_write(layout, "cache-moved", {})
    if not _phase_reached(journal, "banners-written"):
        _write_banner(layout["legacy_state"], layout)
        if layout["legacy_workspaces"].parent.name == "asha":
            _write_banner(layout["legacy_workspaces"].parent, layout)
        _journal_write(layout, "banners-written", {})
    _write_private(marker, {
        "version": MARKER_VERSION, "status": "complete", "migrated_at": _now(),
        "counts": inventory["husks"], "retired": retired_info,
        "materializations": materialization_report or inventory["materializations"],
        "residue_errors": residue_errors,
        "banners": [str(layout["legacy_state"]), str(layout["legacy_workspaces"].parent)],
    })
    layout["journal"].unlink(missing_ok=True)
    layout["staging_manifest"].unlink(missing_ok=True)
    print(f"asha migrate: complete; live state is {layout['new_state']}")
    print(f"  retired records: {retired_info.get('retired_dir', '(resumed run)')}")
    print(f"  marker: {marker}")
    return 0


def main(args: list[str], env: Mapping[str, str] | None = None) -> int:
    try:
        return run(args, os.environ if env is None else env)
    except MigrateError as exc:
        print(f"asha migrate: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"asha migrate: {exc}", file=sys.stderr)
        return 1
