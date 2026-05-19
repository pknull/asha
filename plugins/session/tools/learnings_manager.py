#!/usr/bin/env python3
"""
Learnings Manager - Structured pattern tracking with confidence scoring

Manages ~/.asha/learnings.md with:
- Confidence scores (0.3-0.9) that rise/fall over time
- Trigger conditions for when to apply
- Evidence logs tracking where patterns were observed

Usage:
    python learnings_manager.py add --category "Tool Usage" --id "ollama-http" \
        --trigger "Running ollama for large inputs" \
        --action "Use HTTP API with num_predict cap" \
        --project "comfyui" --reason "CLI hung on large prompt"

    python learnings_manager.py confirm --id "ollama-http" --project "threshold"
    python learnings_manager.py contradict --id "ollama-http" --project "other" --reason "CLI worked fine"
    python learnings_manager.py query --category "Tool Usage"
    python learnings_manager.py list
    python learnings_manager.py export
"""

import os
import re
import sys
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict


def _detect_project_root_for_silence() -> Optional[Path]:
    """Best-effort project root detection for silence-marker check.

    Mirrors pattern_analyzer.detect_project_root semantics: env var first,
    then git rev-parse, then upward search from CWD. Returns None if no root
    can be found — callers should fail-open (allow the write) in that case
    rather than block on missing context.
    """
    claude_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if claude_project_dir:
        candidate = Path(claude_project_dir)
        if (candidate / "Memory").is_dir():
            return candidate

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True
        )
        git_root = Path(result.stdout.strip())
        if (git_root / "Memory").is_dir():
            return git_root
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    search_dir = Path.cwd()
    while search_dir != search_dir.parent:
        if (search_dir / "Memory").is_dir():
            return search_dir
        search_dir = search_dir.parent

    return None


def _silence_marker_present() -> bool:
    """Defense-in-depth check for Work/markers/silence in the current project.

    Returns False (fail-open) when no project root is detectable, so that
    out-of-project CLI invocations of learnings_manager are not silently
    blocked. Pattern_analyzer is the primary guard; this is the safety net
    for direct callers.
    """
    root = _detect_project_root_for_silence()
    if root is None:
        return False
    return (root / "Work" / "markers" / "silence").exists()


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class Evidence:
    """Single evidence entry for a learning"""
    date: str
    project: str
    note: str
    effect: str = "confirm"  # confirm, contradict, initial


@dataclass
class Learning:
    """A single learning with confidence tracking"""
    id: str
    category: str
    confidence: float
    trigger: str
    action: str
    evidence: List[Evidence] = field(default_factory=list)

    def add_evidence(self, project: str, note: str, effect: str = "confirm"):
        """Add evidence and adjust confidence"""
        self.evidence.append(Evidence(
            date=datetime.now().strftime("%Y-%m-%d"),
            project=project,
            note=note,
            effect=effect
        ))

        if effect == "confirm":
            # Confidence rises, diminishing returns near 0.9
            self.confidence = min(0.9, self.confidence + 0.1 * (0.9 - self.confidence))
        elif effect == "contradict":
            # Confidence drops faster
            self.confidence = max(0.1, self.confidence - 0.15)
        # initial doesn't change confidence

        self.confidence = round(self.confidence, 2)


# =============================================================================
# File I/O
# =============================================================================

LEARNINGS_PATH = Path.home() / ".asha" / "learnings.md"

# Regex to parse structured learning entries
LEARNING_PATTERN = re.compile(
    r'### (?P<id>[\w-]+)\n'
    r'- \*\*Confidence\*\*: (?P<confidence>[\d.]+)\n'
    r'- \*\*Trigger\*\*: (?P<trigger>.+)\n'
    r'- \*\*Action\*\*: (?P<action>.+)\n'
    r'- \*\*Evidence\*\*:\n(?P<evidence>(?:  - .+\n)*)',
    re.MULTILINE
)

EVIDENCE_PATTERN = re.compile(
    # MULTILINE is required: without it, $ matches only end-of-string and the
    # non-greedy .+? swallows whole evidence blocks, so an entry with N
    # evidence lines parses as a single Learning whose `note` field contains
    # all but the last line concatenated. The earlier evidence lines silently
    # vanish on the next write_learnings round-trip. Repro: any entry with
    # 2+ evidence bullets, e.g. "Used 158x" + "Used 35x" became one match
    # capturing only "Used 35x".
    r'  - (?P<date>[\d-]+) \| (?P<project>[\w-]+) \| (?P<note>.+?)(?:\s*\[(?P<effect>\w+)\])?$',
    re.MULTILINE
)

CATEGORY_PATTERN = re.compile(r'^## (.+)$', re.MULTILINE)

# Round-trip preservation cache. Populated by parse_learnings, consumed by
# write_learnings. Keyed by category name; each value is the original raw
# text of that category section (everything between '## Foo' and the next
# '## ' or EOF). On write, structured entries inside that text are replaced
# in-place via regex substitution; non-canonical content (intro prose,
# malformed entries, trailing notes) survives verbatim. Without this, every
# parse → write round-trip silently destroys anything that didn't match the
# canonical schema. Tracked by Todoist 6gVq6vw4W5rHC5ww.
_parsed_sections_cache: Dict[str, str] = {}
_parsed_preamble_cache: str = ""

_DEFAULT_PREAMBLE = (
    "# Learnings\n\n"
    "Cross-project patterns with confidence tracking. "
    "Consulted at session start.\n\n"
    "---\n"
)


def parse_learnings() -> Dict[str, List[Learning]]:
    """Parse learnings.md into structured data.

    Side effect: populates _parsed_sections_cache and _parsed_preamble_cache
    so that a subsequent write_learnings can preserve non-canonical content
    (intro prose, malformed entries, trailing notes) in its original
    position. Pure callers that only consume the dict are unaffected.
    """
    global _parsed_sections_cache, _parsed_preamble_cache
    _parsed_sections_cache = {}
    _parsed_preamble_cache = ""

    if not LEARNINGS_PATH.exists():
        return {}

    content = LEARNINGS_PATH.read_text()
    learnings: Dict[str, List[Learning]] = {}

    # Split by category
    parts = CATEGORY_PATTERN.split(content)

    # parts[0] is everything before the first ## heading — capture as preamble
    if parts:
        _parsed_preamble_cache = parts[0]

    # parts[0] is header, then alternating category name and content
    for i in range(1, len(parts), 2):
        if i + 1 >= len(parts):
            break
        category = parts[i]
        section = parts[i + 1]

        # Cache original section text for round-trip preservation.
        # Last write wins if the same category appears twice — degenerate
        # input, not worth complicating for.
        _parsed_sections_cache[category] = section

        learnings[category] = []

        # Try structured format first
        for match in LEARNING_PATTERN.finditer(section):
            evidence_list = []
            for ev_match in EVIDENCE_PATTERN.finditer(match.group('evidence')):
                evidence_list.append(Evidence(
                    date=ev_match.group('date'),
                    project=ev_match.group('project'),
                    note=ev_match.group('note'),
                    effect=ev_match.group('effect') or 'confirm'
                ))

            learnings[category].append(Learning(
                id=match.group('id'),
                category=category,
                confidence=float(match.group('confidence')),
                trigger=match.group('trigger'),
                action=match.group('action'),
                evidence=evidence_list
            ))

        # If no structured entries, parse legacy bullet format.
        # Guard: if the section already has ### subheadings, the user is
        # using structured format even if it doesn't match the canonical
        # field order. Falling through to bullet parsing would fragment
        # each "- **Field**: value" line into its own learning entry,
        # destroying curated content. Skip legacy parsing in that case.
        if not learnings[category] and not re.search(r'^### ', section, re.MULTILINE):
            legacy_bullets = re.findall(r'^- (.+)$', section, re.MULTILINE)
            for i, bullet in enumerate(legacy_bullets):
                # Generate ID from first few words
                words = re.sub(r'[^\w\s]', '', bullet).split()[:3]
                learning_id = '-'.join(w.lower() for w in words)

                # Split on " — " if present (action separator)
                if ' — ' in bullet:
                    trigger_part, action_part = bullet.split(' — ', 1)
                else:
                    trigger_part = bullet
                    action_part = bullet

                learnings[category].append(Learning(
                    id=learning_id,
                    category=category,
                    confidence=0.6,  # Legacy entries get medium confidence
                    trigger=trigger_part.strip('`'),
                    action=action_part,
                    evidence=[Evidence(
                        date="2026-01-01",
                        project="legacy",
                        note="Migrated from unstructured format",
                        effect="initial"
                    )]
                ))

    return learnings


def _render_learning(learning: Learning) -> str:
    """Render a Learning as its canonical markdown block (no trailing newline)."""
    lines = [
        f"### {learning.id}",
        f"- **Confidence**: {learning.confidence}",
        f"- **Trigger**: {learning.trigger}",
        f"- **Action**: {learning.action}",
        "- **Evidence**:",
    ]
    for ev in learning.evidence[-5:]:
        effect_marker = f" [{ev.effect}]" if ev.effect != "confirm" else ""
        lines.append(f"  - {ev.date} | {ev.project} | {ev.note}{effect_marker}")
    return "\n".join(lines)


def _reconstruct_section(original: str, entries: List[Learning]) -> str:
    """Re-render structured entries inside a category section, preserving
    everything else verbatim.

    For each '### id'-shaped block matched by LEARNING_PATTERN, substitute
    in the corresponding Learning's freshly rendered block. Anything that
    doesn't match the pattern (intro prose, malformed entries with
    different field orders, trailing notes) is left untouched. New
    entries (id present in `entries` but not in the original text) are
    appended at the end of the section.
    """
    entries_by_id = {l.id: l for l in entries}
    seen_ids: set = set()

    def repl(match):
        entry_id = match.group('id')
        if entry_id in entries_by_id:
            seen_ids.add(entry_id)
            # Preserve the trailing newline that the regex captured (the
            # evidence group ends with \n) so adjacent paragraphs stay
            # separated.
            rendered = _render_learning(entries_by_id[entry_id])
            return rendered + "\n"
        # Entry was deleted from the in-memory dict — drop the original
        # block. Callers that wanted to remove an entry get what they asked
        # for; preservation is for non-matching content, not deletions.
        return ""

    rewritten = LEARNING_PATTERN.sub(repl, original)

    new_entries = [e for e in entries if e.id not in seen_ids]
    if new_entries:
        rendered_new = "\n\n".join(_render_learning(e) for e in new_entries)
        rewritten = rewritten.rstrip() + "\n\n" + rendered_new + "\n"

    return rewritten


def write_learnings(learnings: Dict[str, List[Learning]]):
    """Write learnings back to markdown format, preserving non-canonical
    content captured by parse_learnings.

    Output layout: `<preamble>\\n\\n## Cat1\\n\\n<body1>\\n\\n## Cat2\\n\\n<body2>\\n`.
    Bodies are stripped of leading/trailing whitespace before being joined,
    so successive round-trips are idempotent (no unbounded blank-line growth).
    """
    # Honor Work/markers/silence even when called directly (not via
    # pattern_analyzer). Fails open if no project root is detectable so
    # out-of-project CLI usage still works.
    if _silence_marker_present():
        return

    # Preamble: prefer cached original (preserves user customization);
    # fall back to canonical default for fresh files.
    preamble_raw = _parsed_preamble_cache if _parsed_preamble_cache.strip() else _DEFAULT_PREAMBLE
    sections = [preamble_raw.strip()]

    for category, entries in sorted(learnings.items()):
        if not entries:
            continue

        original_section = _parsed_sections_cache.get(category, "")
        if original_section.strip():
            # Existing category — reconstruct in place, preserve extras
            body = _reconstruct_section(original_section, entries).strip()
        else:
            # New category (not in original file) — emit canonical layout,
            # confidence-sorted
            sorted_entries = sorted(entries, key=lambda x: x.confidence, reverse=True)
            body = "\n\n".join(_render_learning(l) for l in sorted_entries)

        sections.append(f"## {category}\n\n{body}")

    LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEARNINGS_PATH.write_text("\n\n".join(sections) + "\n")


# =============================================================================
# Operations
# =============================================================================

def add_learning(
    category: str,
    learning_id: str,
    trigger: str,
    action: str,
    project: str,
    reason: str
) -> Dict[str, Any]:
    """Add a new learning or update existing one"""
    learnings = parse_learnings()

    if category not in learnings:
        learnings[category] = []

    # Check if learning already exists
    existing = next((l for l in learnings[category] if l.id == learning_id), None)

    if existing:
        existing.add_evidence(project, reason, "confirm")
        write_learnings(learnings)
        return {
            "status": "updated",
            "id": learning_id,
            "confidence": existing.confidence
        }

    # Create new learning
    learning = Learning(
        id=learning_id,
        category=category,
        confidence=0.3,  # New learnings start low
        trigger=trigger,
        action=action,
        evidence=[Evidence(
            date=datetime.now().strftime("%Y-%m-%d"),
            project=project,
            note=reason,
            effect="initial"
        )]
    )
    learnings[category].append(learning)
    write_learnings(learnings)

    return {
        "status": "created",
        "id": learning_id,
        "confidence": learning.confidence
    }


def confirm_learning(learning_id: str, project: str, reason: str = "Pattern confirmed") -> Dict[str, Any]:
    """Confirm a learning, increasing confidence"""
    learnings = parse_learnings()

    for _, entries in learnings.items():
        for learning in entries:
            if learning.id == learning_id:
                learning.add_evidence(project, reason, "confirm")
                write_learnings(learnings)
                return {
                    "status": "confirmed",
                    "id": learning_id,
                    "confidence": learning.confidence
                }

    return {"status": "not_found", "id": learning_id}


def contradict_learning(learning_id: str, project: str, reason: str) -> Dict[str, Any]:
    """Contradict a learning, decreasing confidence"""
    learnings = parse_learnings()

    for _, entries in learnings.items():
        for learning in entries:
            if learning.id == learning_id:
                old_confidence = learning.confidence
                learning.add_evidence(project, reason, "contradict")

                # Remove if confidence too low
                if learning.confidence < 0.2:
                    entries.remove(learning)
                    write_learnings(learnings)
                    return {
                        "status": "removed",
                        "id": learning_id,
                        "reason": "Confidence dropped below threshold"
                    }

                write_learnings(learnings)
                return {
                    "status": "contradicted",
                    "id": learning_id,
                    "confidence": learning.confidence,
                    "dropped_from": old_confidence
                }

    return {"status": "not_found", "id": learning_id}


def query_learnings(
    category: Optional[str] = None,
    min_confidence: float = 0.0,
    trigger_match: Optional[str] = None
) -> Dict[str, Any]:
    """Query learnings with optional filters"""
    learnings = parse_learnings()
    results = []

    for cat, entries in learnings.items():
        if category and cat != category:
            continue

        for learning in entries:
            if learning.confidence < min_confidence:
                continue

            if trigger_match and trigger_match.lower() not in learning.trigger.lower():
                continue

            results.append({
                "id": learning.id,
                "category": cat,
                "confidence": learning.confidence,
                "trigger": learning.trigger,
                "action": learning.action,
                "evidence_count": len(learning.evidence)
            })

    # Sort by confidence
    results.sort(key=lambda x: x['confidence'], reverse=True)

    return {
        "count": len(results),
        "learnings": results
    }


def list_categories() -> Dict[str, Any]:
    """List all categories with counts"""
    learnings = parse_learnings()

    categories = []
    for cat, entries in learnings.items():
        if entries:
            avg_confidence = sum(l.confidence for l in entries) / len(entries)
            categories.append({
                "category": cat,
                "count": len(entries),
                "avg_confidence": round(avg_confidence, 2)
            })

    return {"categories": categories}


def export_learnings() -> Dict[str, Any]:
    """Export all learnings as JSON"""
    learnings = parse_learnings()

    export = {}
    for cat, entries in learnings.items():
        export[cat] = [
            {
                "id": l.id,
                "confidence": l.confidence,
                "trigger": l.trigger,
                "action": l.action,
                "evidence": [asdict(e) for e in l.evidence]
            }
            for l in entries
        ]

    return export


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Learnings Manager - Pattern tracking with confidence",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Add command
    add_parser = subparsers.add_parser("add", help="Add or reinforce a learning")
    add_parser.add_argument("--category", "-c", required=True, help="Category (e.g., 'Tool Usage')")
    add_parser.add_argument("--id", "-i", required=True, help="Learning ID (kebab-case)")
    add_parser.add_argument("--trigger", "-t", required=True, help="When to apply this")
    add_parser.add_argument("--action", "-a", required=True, help="What to do")
    add_parser.add_argument("--project", "-p", required=True, help="Project where learned")
    add_parser.add_argument("--reason", "-r", required=True, help="Why we learned this")

    # Confirm command
    confirm_parser = subparsers.add_parser("confirm", help="Confirm a learning (raises confidence)")
    confirm_parser.add_argument("--id", "-i", required=True, help="Learning ID")
    confirm_parser.add_argument("--project", "-p", required=True, help="Project confirming")
    confirm_parser.add_argument("--reason", "-r", default="Pattern confirmed", help="Confirmation note")

    # Contradict command
    contradict_parser = subparsers.add_parser("contradict", help="Contradict a learning (lowers confidence)")
    contradict_parser.add_argument("--id", "-i", required=True, help="Learning ID")
    contradict_parser.add_argument("--project", "-p", required=True, help="Project contradicting")
    contradict_parser.add_argument("--reason", "-r", required=True, help="Why it was wrong")

    # Query command
    query_parser = subparsers.add_parser("query", help="Query learnings")
    query_parser.add_argument("--category", "-c", help="Filter by category")
    query_parser.add_argument("--min-confidence", "-m", type=float, default=0.0, help="Minimum confidence")
    query_parser.add_argument("--trigger", "-t", help="Match trigger text")

    # List command
    subparsers.add_parser("list", help="List categories")

    # Export command
    subparsers.add_parser("export", help="Export all learnings as JSON")

    # Migrate command
    subparsers.add_parser("migrate", help="Migrate legacy format to structured")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "add":
            result = add_learning(
                category=args.category,
                learning_id=args.id,
                trigger=args.trigger,
                action=args.action,
                project=args.project,
                reason=args.reason
            )
        elif args.command == "confirm":
            result = confirm_learning(args.id, args.project, args.reason)
        elif args.command == "contradict":
            result = contradict_learning(args.id, args.project, args.reason)
        elif args.command == "query":
            result = query_learnings(
                category=args.category,
                min_confidence=args.min_confidence,
                trigger_match=args.trigger
            )
        elif args.command == "list":
            result = list_categories()
        elif args.command == "export":
            result = export_learnings()
        elif args.command == "migrate":
            # Just parse and write - conversion happens automatically
            learnings = parse_learnings()
            write_learnings(learnings)
            result = {"status": "migrated", "categories": len(learnings)}
        else:
            result = {"error": f"Unknown command: {args.command}"}

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
