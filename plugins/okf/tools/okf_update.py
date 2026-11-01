#!/usr/bin/env python3
"""Record a writer's concept writes in their OKF bundle: reindex, log, gate.

A writer (the canon-writer agent, a save path, a migration) hands over the
paths it touched. For each path the nearest marked ancestor is its bundle root:
a directory holding `.okfconfig`, `.okfignore`, the learnings marker
`.okf-0.2.json`, or an `index.md` that declares `okf_version`. Per bundle the
tool rewrites the generated indexes, appends one log entry naming the concepts,
and runs the strict gate. Concepts are never edited: a malformed one fails the
gate and the verdict names it.

Paths outside any bundle, excluded by the bundle's `.okfignore`, reserved
(`index.md`, `log.md`), not markdown or under a hidden directory (`other`) are
reported and skipped. A missing path (or a directory) or a malformed `--date`
refuses the whole run before anything is written; an OS or decode error while
writing one bundle is reported under `errors` (exit 2) and the other bundles
still run; indexes that bundle wrote before the error stay, and a rerun
repairs the rest.

Exit 0: every touched bundle passed (or nothing was bundled). Exit 1: a gate
failed or a path is missing. Exit 2: usage, or the validator could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import okf_index  # noqa: E402
import okf_log  # noqa: E402
import okf_validate  # noqa: E402
from okf_io import CONFIG_FILE, IGNORE_FILE, RESERVED, effective_excludes, is_excluded, read_scalars  # noqa: E402

LEARNINGS_MARKER = ".okf-0.2.json"
PLUGINS_DIR = Path(__file__).resolve().parents[2]
_VERSION_RE = re.compile(r"^\*\*Version\*\*: (\d+\.\d+\.\d+)$", re.M)


def is_bundle_root(directory: Path) -> bool:
    for marker in (CONFIG_FILE, IGNORE_FILE, LEARNINGS_MARKER):
        if (directory / marker).is_file():
            return True
    index = directory / "index.md"
    if index.is_file():
        try:
            return bool(read_scalars(index.read_text(encoding="utf-8")).get("okf_version"))
        except OSError:
            return False
    return False


def find_bundle_root(path: Path) -> Path | None:
    """The nearest ancestor of `path` that marks a bundle root, else None."""
    current = Path(os.path.abspath(path)).parent
    while True:
        if is_bundle_root(current):
            return current
        if current.parent == current:
            return None
        current = current.parent


def plugin_actor(plugin: str) -> str:
    """`asha/<version>` from the plugin README, the sole plugin-version authority."""
    readme = PLUGINS_DIR / plugin / "README.md"
    if not readme.is_file():
        raise ValueError(f"no plugin README at {readme}")
    match = _VERSION_RE.search(readme.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{readme} declares no **Version**: X.Y.Z line")
    return f"asha/{match.group(1)}"


def group_paths(paths) -> dict:
    """Sort the given paths into bundled (root -> concept ids), unbundled, excluded, reserved and missing."""
    report = {"bundles": {}, "unbundled": [], "excluded": [], "reserved": [], "other": [], "missing": []}
    for raw in paths:
        path = Path(os.path.abspath(raw))
        if not path.is_file():
            report["missing"].append(str(path))
            continue
        root = find_bundle_root(path)
        if root is None:
            report["unbundled"].append(str(path))
            continue
        rel = path.relative_to(root).as_posix()
        if path.name in RESERVED:
            report["reserved"].append(str(path))
            continue
        if is_excluded(rel, effective_excludes(root)):
            report["excluded"].append(str(path))
            continue
        if not rel.endswith(".md") or any(part.startswith(".") for part in Path(rel).parts[:-1]):
            # an attachment, or a note under a hidden directory (Obsidian's .trash): the walker
            # sees neither, so neither is a concept, and recording one would claim a false pass.
            # A dot-named note in a visible directory is walked, so it is recorded like any other.
            report["other"].append(str(path))
            continue
        concept = rel[:-3]
        report["bundles"].setdefault(root, [])
        if concept not in report["bundles"][root]:
            report["bundles"][root].append(concept)
    return report


def record(root: Path, concepts: list, message: str, actor: str | None, verb: str, day: str | None,
           gate: bool = True) -> dict:
    """Reindex, log and gate one bundle. Returns the per-bundle result."""
    changed = [p.relative_to(root).as_posix() for p in okf_index.write_indexes(root)]
    names = ", ".join(f"`{c}`" for c in concepts)
    log_path = okf_log.append_entry(root, f"**{verb}** {names}: {message}", actor=actor, day=day)
    changed.append(log_path.relative_to(root).as_posix())
    result = {"root": str(root), "concepts": concepts, "changed": changed, "gate": None}
    if gate:
        verdict = okf_validate.run(str(root), strict=True)
        result["gate"] = {key: verdict[key] for key in ("rc", "status", "files", "errors", "lints", "asha_lints", "links")}
        result["gate"]["output"] = "\n".join(line for line in verdict["output"].splitlines() if line.strip())
        if verdict.get("count_problems"):
            result["gate"]["output"] += "\n" + "\n".join(verdict["count_problems"])
        if verdict["asha_lints"]:
            result["gate"]["output"] += "\n" + "\n".join(verdict["asha_lints"])
    return result


def update(paths, message: str, actor: str | None, verb: str = "Update", day: str | None = None,
           gate: bool = True) -> tuple[dict, int]:
    report = group_paths(paths)
    out = {"bundles": [], "unbundled": report["unbundled"], "excluded": report["excluded"],
           "reserved": report["reserved"], "other": report["other"], "missing": report["missing"], "errors": []}
    if report["missing"]:
        return out, 1
    if day is not None:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError
            date.fromisoformat(day)
        except ValueError:
            raise ValueError(f"log date must be a real YYYY-MM-DD date: {day!r}") from None
    verb = verb.strip()
    if not verb:
        raise ValueError("--verb must not be empty")
    rc = 0
    for root, concepts in report["bundles"].items():
        try:
            result = record(root, concepts, message, actor, verb, day, gate)
        except (OSError, UnicodeDecodeError) as exc:
            # indexes written before the failure stay on disk; the next run repairs them
            out["errors"].append(f"{root}: {exc}")
            rc = 2
            continue
        out["bundles"].append(result)
        if result["gate"] is not None:
            rc = max(rc, result["gate"]["rc"])
    return out, rc


def _text(out: dict, rc: int) -> str:
    lines = []
    for bundle in out["bundles"]:
        gate = bundle["gate"]
        if gate is None:
            verdict = "not gated"
        elif gate["files"] is None:
            verdict = f"gate {gate['status']} (rc={gate['rc']})"
        else:
            verdict = f"gate {gate['status']} (rc={gate['rc']}, {gate['files']} file(s))"
        lines.append(f"{bundle['root']}: {len(bundle['concepts'])} concept(s) recorded; {verdict}")
        lines.extend(f"  changed: {path}" for path in bundle["changed"])
        if gate is not None and gate["rc"] != 0:
            lines.extend(f"  {line}" for line in gate["output"].splitlines())
    for key in ("unbundled", "excluded", "reserved", "other", "missing", "errors"):
        lines.extend(f"{key}: {path}" for path in out[key])
    lines.append(f"okf_update: {'ok' if rc == 0 else 'failed'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Record concept writes in their OKF bundle: reindex, log, gate.")
    parser.add_argument("paths", nargs="*", help="files a writer created or edited")
    parser.add_argument("--actor", help="log/trust actor, e.g. asha/0.5.5 or human:keeper")
    parser.add_argument("--plugin", help="derive the actor from plugins/<name>/README.md's version")
    parser.add_argument("--print-actor", action="store_true", help="print the resolved actor and exit")
    parser.add_argument("--message", help="what changed, for the log entry")
    parser.add_argument("--verb", default="Update", help="bold verb opening the log entry (default: Update)")
    parser.add_argument("--date", help="log date YYYY-MM-DD (default today)")
    parser.add_argument("--no-gate", action="store_true", help="reindex and log without running the strict gate")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.actor and args.plugin:
        parser.error("--actor and --plugin are exclusive")
    actor = args.actor
    if args.plugin:
        try:
            actor = plugin_actor(args.plugin)
        except ValueError as exc:
            parser.error(str(exc))
    if args.print_actor:
        if not actor:
            parser.error("--print-actor needs --actor or --plugin")
        print(actor)
        return 0
    if not args.paths:
        parser.error("no paths given")
    if not actor:
        parser.error("an actor is required: --actor or --plugin")
    if not args.message or not args.message.strip():
        parser.error("--message is required")
    try:
        out, rc = update(args.paths, args.message, actor, args.verb, args.date, gate=not args.no_gate)
    except ValueError as exc:
        parser.error(str(exc))
    if out["errors"] or any(b["gate"] and b["gate"]["rc"] == 2 for b in out["bundles"]):
        rc = 2
    print(json.dumps(out, indent=2) if args.json else _text(out, rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
