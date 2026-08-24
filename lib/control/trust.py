"""Harness workspace trust: propagate the Keeper's existing decision, never invent one.

Every Control worker runs in a fresh jj workspace, which each harness treats as
an unseen directory and gates behind its own trust prompt. A blocked worker
looks alive, so the prompt costs a manual answer per workspace.

The rule here is inheritance, not authority: Control grants trust for a
workspace only when the Keeper has already trusted its **source repository** in
at least one harness. Given that, the grant covers every harness with a known
store, so a later run under a different harness is not blocked again. Every
grant is appended to a durable ledger and reported to the caller; nothing here
grants trust for a repository the Keeper never trusted anywhere.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

TRUST_LEDGER_CONTRACT = "asha.control-workspace-trust.v1"
# Harnesses with a known, writable trust store. OpenCode has no trust gate, so
# it is reported as unsupported rather than silently counted as granted.
TRUST_HARNESSES: tuple[str, ...] = ("claude", "codex", "copilot")
UNSUPPORTED_HARNESSES: tuple[str, ...] = ("opencode",)
TRUST_MODES = ("inherit", "never")


class TrustError(ValueError):
    """A trust store could not be read or written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve())


def store_path(home: Path, harness: str) -> Path:
    return {
        "claude": home / ".claude.json",
        "codex": home / ".codex" / "config.toml",
        "copilot": home / ".copilot" / "config.json",
    }[harness]


def _atomic_write(path: Path, data: str) -> None:
    """Replace the store in one rename, preserving its mode."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False,
    )
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except OSError as exc:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise TrustError(f"could not write {path}: {exc}") from exc


def _copilot_split(raw: str) -> tuple[str, dict[str, Any]]:
    """Copilot's config carries a leading comment header; keep it verbatim."""
    lines = raw.splitlines(keepends=True)
    index = 0
    while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("//")):
        index += 1
    header = "".join(lines[:index])
    try:
        return header, json.loads("".join(lines[index:]) or "{}")
    except ValueError as exc:
        raise TrustError(f"copilot config is not readable JSON: {exc}") from exc


def is_trusted(home: Path, harness: str, path: Path | str) -> bool | None:
    """True/False for a readable store, None when the harness has no usable store."""
    if harness in UNSUPPORTED_HARNESSES:
        return None
    if harness not in TRUST_HARNESSES:
        raise TrustError(f"unknown harness: {harness}")
    target = _canonical(path)
    store = store_path(home, harness)
    if not store.is_file():
        return None
    try:
        raw = store.read_text(encoding="utf-8")
    except OSError:
        return None
    if harness == "claude":
        try:
            entry = json.loads(raw).get("projects", {}).get(target)
        except ValueError as exc:
            raise TrustError(f"claude config is not readable JSON: {exc}") from exc
        return bool(entry and entry.get("hasTrustDialogAccepted") is True)
    if harness == "codex":
        # Exact-path only: a trusted ancestor is Codex's own broadening, not a
        # decision this module may read as trust for a specific workspace.
        return f'[projects."{target}"]' in raw
    header, value = _copilot_split(raw)
    del header
    folders = value.get("trustedFolders")
    return isinstance(folders, list) and target in folders


def trusting_harnesses(home: Path, path: Path | str) -> list[str]:
    """Harnesses whose store already trusts this exact path."""
    return [name for name in TRUST_HARNESSES if is_trusted(home, name, path) is True]


def _grant_one(home: Path, harness: str, target: str) -> str:
    """Write one store; returns 'granted', 'already', or 'unavailable'."""
    state = is_trusted(home, harness, target)
    if state is None:
        return "unavailable"
    if state is True:
        return "already"
    store = store_path(home, harness)
    raw = store.read_text(encoding="utf-8")
    if harness == "claude":
        value = json.loads(raw)
        projects = value.setdefault("projects", {})
        entry = dict(projects.get(target) or {})
        entry["hasTrustDialogAccepted"] = True
        entry.setdefault("hasCompletedProjectOnboarding", True)
        projects[target] = entry
        _atomic_write(store, json.dumps(value, indent=2) + "\n")
        return "granted"
    if harness == "codex":
        suffix = "" if raw.endswith("\n") or not raw else "\n"
        _atomic_write(store, f'{raw}{suffix}\n[projects."{target}"]\ntrust_level = "trusted"\n')
        return "granted"
    header, value = _copilot_split(raw)
    folders = value.get("trustedFolders")
    value["trustedFolders"] = ([] if not isinstance(folders, list) else list(folders)) + [target]
    _atomic_write(store, header + json.dumps(value, indent=2) + "\n")
    return "granted"


def grant(
    home: Path, path: Path | str, *, harnesses: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Trust one exact path in every named harness store that can hold it."""
    target = _canonical(path)
    names = TRUST_HARNESSES if harnesses is None else tuple(harnesses)
    outcome: dict[str, list[str]] = {"granted": [], "already": [], "unavailable": []}
    for harness in names:
        if harness in UNSUPPORTED_HARNESSES:
            outcome["unavailable"].append(harness)
            continue
        try:
            outcome[_grant_one(home, harness, target)].append(harness)
        except (OSError, ValueError) as exc:
            raise TrustError(f"{harness} trust store could not be updated: {exc}") from exc
    return {"path": target, **outcome}


def record_grant(state_dir: Path, entry: Mapping[str, Any]) -> Path:
    """Append one durable audit line; the ledger is never rewritten."""
    ledger = Path(state_dir) / "trust.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"contract": TRUST_LEDGER_CONTRACT, "recorded_at": _now(), **dict(entry)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return ledger


def inherit_workspace_trust(
    home: Path, *, source: Path | str, workspace: Path | str,
    mode: str = "inherit", state_dir: Path | None = None,
) -> dict[str, Any]:
    """Grant workspace trust across harnesses when the source repository is trusted.

    Returns a report in every case; `applied` is False when the mode is off or
    the source is trusted nowhere, and the reason says which.
    """
    if mode not in TRUST_MODES:
        raise TrustError(f"workspace_trust must be one of {list(TRUST_MODES)}")
    report: dict[str, Any] = {
        "applied": False, "mode": mode, "source": _canonical(source),
        "workspace": _canonical(workspace), "inherited_from": [],
        "granted": [], "already": [], "unavailable": [], "reason": "",
    }
    if mode == "never":
        report["reason"] = "control.workspace_trust is never"
        return report
    inherited = trusting_harnesses(home, source)
    report["inherited_from"] = inherited
    if not inherited:
        report["reason"] = "source repository is not trusted in any harness store"
        return report
    outcome = grant(home, report["workspace"])
    report.update({
        "applied": bool(outcome["granted"]),
        "granted": outcome["granted"], "already": outcome["already"],
        "unavailable": outcome["unavailable"],
        "reason": (
            f"source is trusted in {', '.join(inherited)}; workspace trusted in "
            + (", ".join(outcome["granted"]) if outcome["granted"] else "no new store")
        ),
    })
    if state_dir is not None and outcome["granted"]:
        record_grant(state_dir, {
            "workspace": report["workspace"], "source": report["source"],
            "inherited_from": inherited, "granted": outcome["granted"],
        })
    return report


__all__ = [
    "TRUST_HARNESSES", "TRUST_LEDGER_CONTRACT", "TRUST_MODES", "TrustError",
    "UNSUPPORTED_HARNESSES", "grant", "inherit_workspace_trust", "is_trusted",
    "record_grant", "store_path", "trusting_harnesses",
]
