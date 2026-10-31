#!/usr/bin/env python3
"""One-way migration of the learnings store to the OKF v0.2 record shape.

Run by the Keeper, once, after rehearsing on a copy:

    migrate_learnings_okf.py --yes [--root PATH]

What it does, under the learnings lock and refusing a pending transition
journal: copies the resolved bundle directory to `<target>.pre-okf-v02-YYYYMMDD`
beside it (rollback is `mv`), writes the OKF marker that switches the writer's
output shape, rewrites every record in place with the same filename, regenerates
the indexes, starts `log.md`, writes the stale-candidate list and prints a
report whose last line proves the SessionStart render is byte-identical.
Evidence, state, applicability and retirement reasons are preserved verbatim;
`generated.at` is the record's `created` date; no `verified` entry is written.

Every record is rendered and checked before the backup or the marker is
written, so a record the writer would refuse stops the run with nothing
changed. It refuses to run twice (marker present or any record already in the
new shape), refuses a store whose state directories are symlinks (the backup
would alias the live files), refuses without a verified backup, and never
touches `.transactions/`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import learnings_manager as lm  # noqa: E402

OKF_VERSION = "0.2"


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def migrate(root: Path | None, *, yes: bool, backup_dir: Path | None = None) -> dict:
    if not yes:
        raise ValueError("the migration is one-way; pass --yes after rehearsing on a copy")
    if root is not None:
        lm.learnings_dir = lambda: Path(root)
    target = lm._learning_root()
    if not target.is_dir():
        raise ValueError(f"learnings root is not a directory: {target}")
    marker = target / lm.OKF_MARKER
    if marker.exists():
        raise ValueError(f"already migrated: {marker} exists")
    stamp = date.today().strftime("%Y%m%d")
    backup = backup_dir or target.with_name(f"{target.name}.pre-okf-v02-{stamp}")
    if backup.exists():
        raise ValueError(f"backup already exists; move it aside first: {backup}")
    for relative in (*lm.STATES, ".transactions"):
        if (target / relative).is_symlink():
            raise ValueError(f"{target / relative} is a symlink; a copied backup would alias the live files. "
                             "Replace the link with a real directory (or back up by hand) before migrating.")
    with lm._global_lock(recover=False):  # raises on a pending transition journal
        records = lm._list_state_unlocked()
        for learning in records:
            text = lm._path(learning).read_text(encoding="utf-8")
            if '"status":' in text.split("\n---", 1)[0]:
                raise ValueError(f"record already in the OKF shape: {lm._path(learning)}")
        # Pre-check: render every record in the new shape and refuse before any
        # write, so a failing record leaves the store exactly as it was.
        planned = []
        with lm.force_okf_shape():
            for learning in records:
                learning.generated_at = learning.generated_at or learning.created
                destination = lm._path(learning)
                rendered = lm._render(learning)
                lm._check_record(lm._record(learning), rendered, destination)
                planned.append((destination, rendered))
        before_render = "\n".join(lm._select_active_lines(lm._list_state_unlocked("active"), lm.ACTIVE_RENDER_MAX_BYTES))
        before_names = {s: sorted(p.name for p in (target / s).glob("*.md") if p.name != "index.md")
                        for s in lm.STATES if (target / s).is_dir()}
        shutil.copytree(target, backup, symlinks=True)
        if _tree_digest(backup) != _tree_digest(target):
            shutil.rmtree(backup)
            raise ValueError("backup verification failed; nothing was changed")
        lm._atomic(marker, json.dumps({"okf_version": OKF_VERSION, "migrated": date.today().isoformat(),
                                       "records": len(records), "backup": str(backup),
                                       "writer": lm.GENERATED_BY}, indent=2) + "\n")
        rollback = f"mv {target} {target}.failed-okf-v02 && mv {backup} {target}"
        rewritten = 0
        try:
            for destination, rendered in planned:
                lm._atomic(destination, rendered)
                rewritten += 1
            lm._rebuild_index_unlocked()
            _, okf_log, _ = lm._okf_tools()
            okf_log.append_entry(target, f"**Update** migrated {rewritten} learning records to OKF v{OKF_VERSION}; "
                                         f"backup at {backup.name}", actor=lm.GENERATED_BY)
            after = lm._list_state_unlocked()
            after_render = "\n".join(lm._select_active_lines(lm._list_state_unlocked("active"), lm.ACTIVE_RENDER_MAX_BYTES))
            after_names = {s: sorted(p.name for p in (target / s).glob("*.md") if p.name != "index.md")
                           for s in lm.STATES if (target / s).is_dir()}
            today = date.today()
            stale = [l.id for l in after if l.state == "candidate" and (inst := lm._stale_after(l))
                     and date.fromisoformat(inst[:10]) <= today]
            lm._atomic(target / lm.STALE_LIST,
                       json.dumps({"checked": today.isoformat(), "days": lm.CANDIDATE_TTL_DAYS, "stale": stale}, indent=2) + "\n")
        except (OSError, ValueError) as exc:
            raise ValueError(f"failed after the marker was written ({rewritten} of {len(planned)} records rewritten): {exc}. "
                             f"Roll back with: {rollback}") from exc
    report = {
        "root": str(target), "backup": str(backup), "records": rewritten,
        "states": {s: len(n) for s, n in after_names.items()},
        "filenames_identical": before_names == after_names,
        "evidence_counts_identical": [len(l.evidence) for l in records] == [len(l.evidence) for l in after],
        "stale_candidates": stale,
        "render_active_identical": before_render == after_render,
        "rollback": rollback + ("" if not Path(lm.learnings_dir()).is_symlink()
                                else f"  # {lm.learnings_dir()} stays a symlink to it"),
    }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migrate the learnings store to OKF v0.2 records (one-way).")
    parser.add_argument("--root", type=Path, default=None, help="learnings root (default: the live store); use a copy to rehearse")
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--yes", action="store_true", help="confirm the one-way rewrite")
    args = parser.parse_args(argv)
    try:
        report = migrate(args.root, yes=args.yes, backup_dir=args.backup_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    print(f"render-active byte-identical: {report['render_active_identical']}")
    return 0 if report["render_active_identical"] and report["filenames_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
