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

import re
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict


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
    r'  - (?P<date>[\d-]+) \| (?P<project>[\w-]+) \| (?P<note>.+?)(?:\s*\[(?P<effect>\w+)\])?$'
)

CATEGORY_PATTERN = re.compile(r'^## (.+)$', re.MULTILINE)


def parse_learnings() -> Dict[str, List[Learning]]:
    """Parse learnings.md into structured data"""
    if not LEARNINGS_PATH.exists():
        return {}

    content = LEARNINGS_PATH.read_text()
    learnings: Dict[str, List[Learning]] = {}

    # Split by category
    parts = CATEGORY_PATTERN.split(content)

    # parts[0] is header, then alternating category name and content
    for i in range(1, len(parts), 2):
        if i + 1 >= len(parts):
            break
        category = parts[i]
        section = parts[i + 1]

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

        # If no structured entries, parse legacy bullet format
        if not learnings[category]:
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


def write_learnings(learnings: Dict[str, List[Learning]]):
    """Write learnings back to markdown format"""
    lines = [
        "# Learnings",
        "",
        "Cross-project patterns with confidence tracking. Consulted at session start.",
        "",
        "---",
        ""
    ]

    for category, entries in sorted(learnings.items()):
        if not entries:
            continue

        lines.append(f"## {category}")
        lines.append("")

        # Sort by confidence descending
        for learning in sorted(entries, key=lambda x: x.confidence, reverse=True):
            lines.append(f"### {learning.id}")
            lines.append(f"- **Confidence**: {learning.confidence}")
            lines.append(f"- **Trigger**: {learning.trigger}")
            lines.append(f"- **Action**: {learning.action}")
            lines.append("- **Evidence**:")

            # Show last 5 evidence entries
            for ev in learning.evidence[-5:]:
                effect_marker = f" [{ev.effect}]" if ev.effect != "confirm" else ""
                lines.append(f"  - {ev.date} | {ev.project} | {ev.note}{effect_marker}")

            lines.append("")

        lines.append("")

    # Ensure directory exists
    LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEARNINGS_PATH.write_text('\n'.join(lines))


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
