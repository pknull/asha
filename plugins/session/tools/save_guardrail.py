#!/usr/bin/env python3
"""
save_guardrail.py — Boundary guard for /session:save.

Treats pattern_analyzer.py's output as untrusted input. Runs after synthesis,
before commit. Three operations:

  1. strip-stubs    Remove auto-fallback stub blocks the synthesizer re-appends
                    to Memory/activeContext.md (## Current Blockers / ## Next Steps
                    / ## What Was Learned with known-fallback content only).

  2. dedup-keeper   Remove calibration signals just appended to ~/.asha/keeper.md
                    that are byte-identical (text portion only) to signals
                    already present from prior saves.

  3. strip-sequence-noise
                    Remove low-value '### sequence-*' tool-adjacency learnings
                    from the SHARED ~/.asha/learnings.md (they accumulate
                    un-deduplicated across projects; one project's noise surfaces
                    in another's save).

All operations are idempotent and write a one-line summary of what was removed
to stderr. Default mode is --apply (changes files). Use --dry-run to inspect.

Usage:
    save_guardrail.py strip-stubs <activeContext.md> [--dry-run]
    save_guardrail.py dedup-keeper <keeper.md> [--dry-run]
    save_guardrail.py strip-sequence-noise <learnings.md> [--dry-run]
    save_guardrail.py all <project_dir> [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# Stub-block stripping
# -----------------------------------------------------------------------------

# Sections the synthesizer re-emits even when user-curated versions exist.
STUB_SECTION_HEADERS = ("Current Blockers", "Next Steps", "What Was Learned")

# Bullet content that marks a section as auto-fallback (no real signal).
STUB_CONTENT_PATTERNS = (
    re.compile(r"^- None detected\s*$"),
    re.compile(r"^- Review and plan next session\s*$"),
    re.compile(r"^- No new patterns detected\s*$"),
    re.compile(r"^- \[ \] Continue work in .+/?\s*$"),
)


def _is_stub_bullet(line: str) -> bool:
    return any(p.match(line) for p in STUB_CONTENT_PATTERNS)


def _split_into_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split markdown into (header_line, body_lines) tuples.

    The first chunk has header "" if the file doesn't start with ##.
    """
    sections: list[tuple[str, list[str]]] = []
    current_header = ""
    current_body: list[str] = []

    for line in text.split("\n"):
        if line.startswith("## "):
            sections.append((current_header, current_body))
            current_header = line
            current_body = []
        else:
            current_body.append(line)

    sections.append((current_header, current_body))
    return sections


def _section_is_pure_stub(header: str, body: list[str]) -> bool:
    """A section is a pure stub if its header matches a stub type AND every
    non-blank body line is a known stub bullet.
    """
    if not header.startswith("## "):
        return False

    header_text = header[3:].strip()
    if header_text not in STUB_SECTION_HEADERS:
        return False

    non_blank = [ln for ln in body if ln.strip()]
    if not non_blank:
        return True  # empty stub section
    return all(_is_stub_bullet(ln) for ln in non_blank)


def strip_stubs(path: Path, dry_run: bool = False) -> int:
    """Remove pure-stub sections from activeContext.md. Returns count removed."""
    if not path.exists():
        print(f"strip-stubs: {path} not found, skipping", file=sys.stderr)
        return 0

    original = path.read_text()
    sections = _split_into_sections(original)

    kept: list[tuple[str, list[str]]] = []
    removed = 0
    for header, body in sections:
        if _section_is_pure_stub(header, body):
            removed += 1
            continue
        kept.append((header, body))

    if removed == 0:
        return 0

    rebuilt_parts: list[str] = []
    for i, (header, body) in enumerate(kept):
        if i == 0 and header == "":
            rebuilt_parts.append("\n".join(body))
        else:
            rebuilt_parts.append(header + "\n" + "\n".join(body))
    rebuilt = "\n".join(rebuilt_parts)

    # Collapse 3+ consecutive blank lines (created by removal) to 2.
    rebuilt = re.sub(r"\n{3,}", "\n\n", rebuilt)
    if not rebuilt.endswith("\n"):
        rebuilt += "\n"

    if dry_run:
        print(f"strip-stubs: would remove {removed} stub section(s) from {path}", file=sys.stderr)
    else:
        path.write_text(rebuilt)
        print(f"strip-stubs: removed {removed} stub section(s) from {path}", file=sys.stderr)
    return removed


# -----------------------------------------------------------------------------
# Calibration-log dedup
# -----------------------------------------------------------------------------

# keeper.md calibration line: ISO_TIMESTAMP | project | "text"
CALIB_LINE = re.compile(r'^(?P<ts>\S+)\s*\|\s*(?P<project>\S+)\s*\|\s*"(?P<text>.+)"$')


def _extract_calib_block(text: str) -> tuple[str, str, str] | None:
    """Find the ``` fenced block inside ## Calibration Log.

    Returns (head, block_inner, tail) if found, else None. Splits so that
    head + "```\n" + block_inner + "```" + tail == text.
    """
    header_idx = text.find("## Calibration Log")
    if header_idx == -1:
        return None

    open_fence = text.find("```", header_idx)
    if open_fence == -1:
        return None

    inner_start = text.find("\n", open_fence) + 1
    close_fence = text.find("```", inner_start)
    if close_fence == -1:
        return None

    head = text[:inner_start]
    inner = text[inner_start:close_fence]
    tail = text[close_fence:]
    return head, inner, tail


def dedup_keeper(path: Path, dry_run: bool = False) -> int:
    """Remove calibration entries whose text duplicates an EARLIER entry.

    Keeps the earliest-timestamp occurrence of each unique text; strips later
    re-emissions. Returns number of duplicate lines removed.
    """
    if not path.exists():
        print(f"dedup-keeper: {path} not found, skipping", file=sys.stderr)
        return 0

    original = path.read_text()
    parts = _extract_calib_block(original)
    if parts is None:
        print(f"dedup-keeper: no calibration block in {path}, skipping", file=sys.stderr)
        return 0

    head, inner, tail = parts
    lines = inner.split("\n")

    seen_texts: set[str] = set()
    kept_lines: list[str] = []
    removed = 0

    for line in lines:
        m = CALIB_LINE.match(line.strip())
        if m is None:
            kept_lines.append(line)
            continue
        text_key = m.group("text")
        if text_key in seen_texts:
            removed += 1
            continue
        seen_texts.add(text_key)
        kept_lines.append(line)

    if removed == 0:
        return 0

    new_inner = "\n".join(kept_lines)
    rebuilt = head + new_inner + tail

    if dry_run:
        print(f"dedup-keeper: would remove {removed} duplicate signal(s) from {path}", file=sys.stderr)
    else:
        path.write_text(rebuilt)
        print(f"dedup-keeper: removed {removed} duplicate signal(s) from {path}", file=sys.stderr)
    return removed


# -----------------------------------------------------------------------------
# Learnings sequence-noise stripping
# -----------------------------------------------------------------------------

# Tool/agent-adjacency learnings the synthesizer emits ("Follow X with Y").
# In practice these are low-value — especially X->X tautologies from authoring
# loops — and they accumulate un-deduplicated in the SHARED cross-project
# ~/.asha/learnings.md, so noise generated by one project surfaces in another's
# save. Strip them at the boundary; the detector fix prevents most generation,
# this self-heals whatever still lands.
SEQUENCE_ENTRY = re.compile(r"^### sequence-")


def strip_sequence_noise(path: Path, dry_run: bool = False) -> int:
    """Remove '### sequence-*' entries (header + body) from learnings.md.

    A block runs from its '### sequence-' header until the next '### ' entry,
    the next '## ' section, or EOF. Returns the number of entries removed.
    """
    if not path.exists():
        print(f"strip-sequence-noise: {path} not found, skipping", file=sys.stderr)
        return 0

    original = path.read_text()
    kept: list[str] = []
    removed = 0
    skipping = False

    for line in original.split("\n"):
        if line.startswith("### "):
            skipping = bool(SEQUENCE_ENTRY.match(line))
            if skipping:
                removed += 1
                continue  # drop the sequence header itself
        elif skipping and line.startswith("## "):
            skipping = False  # a section header ends the skipped entry's body
        if skipping:
            continue
        kept.append(line)

    if removed == 0:
        return 0

    rebuilt = "\n".join(kept)
    rebuilt = re.sub(r"\n{3,}", "\n\n", rebuilt)
    if not rebuilt.endswith("\n"):
        rebuilt += "\n"

    if dry_run:
        print(f"strip-sequence-noise: would remove {removed} sequence entry(ies) from {path}", file=sys.stderr)
    else:
        path.write_text(rebuilt)
        print(f"strip-sequence-noise: removed {removed} sequence entry(ies) from {path}", file=sys.stderr)
    return removed


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("operation", choices=("strip-stubs", "dedup-keeper", "strip-sequence-noise", "all"))
    parser.add_argument("target", help="Path to file (strip-stubs/dedup-keeper) or project dir (all)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    target = Path(args.target).expanduser()
    total_changes = 0

    if args.operation == "strip-stubs":
        total_changes += strip_stubs(target, dry_run=args.dry_run)
    elif args.operation == "dedup-keeper":
        total_changes += dedup_keeper(target, dry_run=args.dry_run)
    elif args.operation == "strip-sequence-noise":
        total_changes += strip_sequence_noise(target, dry_run=args.dry_run)
    elif args.operation == "all":
        active_context = target / "Memory" / "activeContext.md"
        keeper = Path.home() / ".asha" / "keeper.md"
        learnings = Path.home() / ".asha" / "learnings.md"
        total_changes += strip_stubs(active_context, dry_run=args.dry_run)
        total_changes += dedup_keeper(keeper, dry_run=args.dry_run)
        total_changes += strip_sequence_noise(learnings, dry_run=args.dry_run)

    return 0 if total_changes == 0 or not args.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
