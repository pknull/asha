#!/usr/bin/env python3
"""One-time migrator: flat learnings -> OKF concept bundle.

Reads the legacy flat files:
  ~/.asha/learnings.md          (former hot tier)
  ~/.asha/learnings-archive.md  (former cold tier; usually a dotfiles symlink)

and writes one concept file per learning into ~/.asha/learnings/ (plus index.md),
via learnings_manager's renderer + atomic writer.

Guarantees:
  * Non-destructive  — legacy files are READ ONLY; never modified or deleted.
                       They are the rollback path.
  * Idempotent       — existing concept files are the base; legacy entries merge
                       in (evidence unioned, max confidence kept). Re-running, or
                       running after a save already created some files, is safe.
  * Reported, not dropped — '### ' blocks that don't match the canonical schema
                       are counted and reported; they remain in the legacy file.

Tier is advisory and derived from confidence on write (hot iff >= 0.7), matching
runtime selection — so an archived low-confidence entry stays cold, and a curated
hot-file entry below 0.7 also renders cold (injection is confidence-driven).

Usage:
    python migrate_learnings_to_okf.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import learnings_manager as lm  # type: ignore[reportMissingImports]  # sibling module on sys.path[0]

LEGACY_HOT = lm.ASHA_DIR / "learnings.md"
LEGACY_COLD = lm.ASHA_DIR / "learnings-archive.md"

# Canonical flat entry parser (vendored from the pre-refactor learnings_manager,
# so the live module need not retain the old flat format).
_FLAT_LEARNING = re.compile(
    r'### (?P<id>[\w-]+)\n'
    r'- \*\*Confidence\*\*: (?P<confidence>[\d.]+)\n'
    r'- \*\*Trigger\*\*: (?P<trigger>.+)\n'
    r'- \*\*Action\*\*: (?P<action>.+)\n'
    r'- \*\*Evidence\*\*:\n(?P<evidence>(?:  - .+\n?)*)',
    re.MULTILINE
)
_FLAT_CATEGORY = re.compile(r'^## (.+)$', re.MULTILINE)
_ANY_ENTRY_HEADING = re.compile(r'^### ', re.MULTILINE)


def _parse_flat(path: Path):
    """Return (list[Learning], unparsed_block_count) for one legacy flat file."""
    if not path.exists():
        return [], 0
    content = path.read_text(encoding="utf-8")
    learnings = []
    matched = 0

    parts = _FLAT_CATEGORY.split(content)
    for i in range(1, len(parts), 2):
        if i + 1 >= len(parts):
            break
        category = parts[i].strip()
        section = parts[i + 1]
        for m in _FLAT_LEARNING.finditer(section):
            matched += 1
            evidence = []
            for em in lm.EVIDENCE_PATTERN.finditer(m.group("evidence")):
                evidence.append(lm.Evidence(
                    date=em.group("date"),
                    project=em.group("project"),
                    note=em.group("note").strip(),
                    effect=em.group("effect") or "confirm",
                ))
            dates = [e.date for e in evidence] or [lm._today()]
            learnings.append(lm.Learning(
                id=lm._slugify(m.group("id")),
                category=category,
                confidence=round(float(m.group("confidence")), 2),
                trigger=m.group("trigger").strip(),
                action=m.group("action").strip(),
                evidence=evidence,
                created=min(dates),
                updated=max(dates),
            ))

    total_blocks = len(_ANY_ENTRY_HEADING.findall(content))
    return learnings, max(0, total_blocks - matched)


def _merge(into: dict, learning: lm.Learning) -> None:
    """Merge `learning` into the slug-keyed dict, preserving existing identity
    and unioning evidence (dedup by (date, project, note)); keep max confidence."""
    slug = lm._slugify(learning.id)
    if slug not in into:
        into[slug] = learning
        return
    existing = into[slug]
    seen = {(e.date, e.project, e.note) for e in existing.evidence}
    for e in learning.evidence:
        key = (e.date, e.project, e.note)
        if key not in seen:
            existing.evidence.append(e)
            seen.add(key)
    existing.confidence = max(existing.confidence, learning.confidence)
    if not existing.trigger:
        existing.trigger = learning.trigger
    if not existing.action:
        existing.action = learning.action
    existing.created = min(d for d in (existing.created, learning.created) if d) if (existing.created or learning.created) else lm._today()
    existing.updated = max(d for d in (existing.updated, learning.updated) if d) if (existing.updated or learning.updated) else lm._today()


def run(dry_run: bool = False) -> int:
    # Base the merge on any existing concept files (idempotency / post-save safety),
    # then fold in legacy hot, then legacy cold.
    merged: dict = {}
    existing_slugs = set()
    for entries in lm.parse_learnings().values():
        for l in entries:
            slug = lm._slugify(l.id)
            merged[slug] = l
            existing_slugs.add(slug)

    hot, hot_unparsed = _parse_flat(LEGACY_HOT)
    cold, cold_unparsed = _parse_flat(LEGACY_COLD)
    for l in hot:
        _merge(merged, l)
    for l in cold:
        _merge(merged, l)

    new_slugs = [s for s in merged if s not in existing_slugs]
    report = {
        "legacy_hot": str(LEGACY_HOT),
        "legacy_hot_present": LEGACY_HOT.exists(),
        "legacy_cold": str(LEGACY_COLD),
        "legacy_cold_present": LEGACY_COLD.exists(),
        "parsed_hot": len(hot),
        "parsed_cold": len(cold),
        "unparsed_blocks": hot_unparsed + cold_unparsed,
        "already_present": len(existing_slugs),
        "new_files": len(new_slugs),
        "total_after": len(merged),
        "target_dir": str(lm.LEARNINGS_DIR),
        "new_filenames": sorted(f"{s}.md" for s in new_slugs),
    }

    if hot_unparsed + cold_unparsed:
        print(f"warning: {hot_unparsed + cold_unparsed} non-canonical '### ' block(s) "
              f"left in legacy files (not migrated; legacy retained).", file=sys.stderr)

    if dry_run:
        report["dry_run"] = True
        print(json.dumps(report, indent=2))
        return 0

    for slug, learning in merged.items():
        lm._atomic_write_file(lm._learning_path(learning.id), lm._render_learning(learning))
    lm._rebuild_index()

    report["dry_run"] = False
    report["legacy_retained"] = True
    print(json.dumps(report, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migrate flat learnings to the OKF bundle.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change; write nothing")
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
