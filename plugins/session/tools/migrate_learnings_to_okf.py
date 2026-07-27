#!/usr/bin/env python3
"""One-time migrator: flat learnings -> OKF concept bundle.

Reads the legacy flat files:
  ~/.asha/learnings.md          (former hot tier)
  ~/.asha/learnings-archive.md  (former cold tier; usually a dotfiles symlink)

and writes one concept file per learning into ~/.asha/learnings/ (plus index.md),
via learnings_manager's renderer + atomic writer.

Guarantees:
  * Content-preserving — legacy entries are never altered or deleted; the files
                       remain the rollback path. After a successful migration
                       each legacy file gains a prepended supersession banner
                       (original content verbatim below it) so it can no longer
                       masquerade as a current store — a stale-looking-current
                       flat file is a decoy that silently breaks backup
                       arrangements (see issue #12).
  * Symlink-safe     — banner stamping writes through symlinks (atomic replace
                       of the resolved target), so a dotfiles-tracked copy
                       becomes self-describing as stale while the symlink at
                       ~/.asha survives. When a legacy file IS a symlink (or
                       resolves outside ~/.asha), the migration additionally
                       warns that the new bundle directory is outside that
                       backup arrangement's coverage.
  * Idempotent       — existing concept files are the base; legacy entries merge
                       in (evidence unioned, max confidence kept). Re-running, or
                       running after a save already created some files, is safe;
                       the banner is stamped at most once.
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


def _superseded_banner() -> str:
    return (
        f"{lm.SUPERSEDED_SENTINEL}\n"
        f"> **SUPERSEDED {lm._today()}** — this flat file was migrated to the OKF concept\n"
        f"> bundle at `~/.asha/learnings/` and is **no longer updated**. It is retained\n"
        f"> verbatim below as a frozen pre-migration snapshot (rollback path only).\n"
        f">\n"
        f"> If this file is under backup or version control (e.g. a dotfiles symlink),\n"
        f"> move that coverage to the `learnings/` and `learnings-archive/` directories —\n"
        f"> restoring from this file alone would resurrect pre-migration state.\n"
        f"\n"
    )


def _stamp_superseded(path: Path) -> bool:
    """Prepend the supersession banner to a legacy flat file (idempotent).

    Writes through symlinks — atomic replace of the *resolved* target — so the
    symlink at the legacy path survives and an externally-tracked copy becomes
    self-describing as stale. Returns True iff the file was stamped this call.
    """
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    if lm.SUPERSEDED_SENTINEL in content:
        return False
    lm._atomic_write_file(path.resolve(), _superseded_banner() + content)
    return True


def _backup_coverage_warnings() -> list:
    """One warning per legacy file whose backing store lives outside ~/.asha.

    Such a file is (typically) covered by an existing backup arrangement — a
    dotfiles symlink being the natural case — that does NOT cover the bundle
    directory the store is moving to. Silence here is how a user ends up with a
    plausible-looking stale backup and an unprotected live store.
    """
    warnings = []
    asha_resolved = lm.ASHA_DIR.resolve()
    for path in (LEGACY_HOT, LEGACY_COLD):
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            outside = asha_resolved not in path.resolve().parents
        except OSError:
            outside = False
        if path.is_symlink() or outside:
            warnings.append(
                f"{path} resolves outside ~/.asha (symlink/external target). The live "
                f"store is moving to {lm.LEARNINGS_DIR}/ — a directory that arrangement "
                f"does NOT cover. Extend your backup/VCS to the bundle directory; the "
                f"flat file is frozen at migration and will no longer be updated."
            )
    return warnings


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
    coverage_warnings = _backup_coverage_warnings()
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
        "backup_coverage_warnings": coverage_warnings,
    }

    if hot_unparsed + cold_unparsed:
        print(f"warning: {hot_unparsed + cold_unparsed} non-canonical '### ' block(s) "
              f"left in legacy files (not migrated; legacy retained).", file=sys.stderr)
    for warning in coverage_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if dry_run:
        report["dry_run"] = True
        print(json.dumps(report, indent=2))
        return 0

    for slug, learning in merged.items():
        lm._atomic_write_file(lm._learning_path(learning.id), lm._render_learning(learning))
    lm._rebuild_index()

    stamped = [p.name for p in (LEGACY_HOT, LEGACY_COLD) if _stamp_superseded(p)]

    report["dry_run"] = False
    report["legacy_retained"] = True
    report["legacy_stamped"] = stamped
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
