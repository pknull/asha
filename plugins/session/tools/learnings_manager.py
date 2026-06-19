#!/usr/bin/env python3
"""
Learnings Manager - Structured pattern tracking with confidence scoring

Storage: an OKF-style bundle at ~/.asha/learnings/ — one concept file per
learning (`<slug>.md`), each with YAML frontmatter (top-level `type: learning`)
and a human-readable body. Recording a learning is an upsert keyed by id, so the
same insight cannot accumulate duplicate copies. A derived `index.md` (OKF
reserved root index) lists the bundle.

This replaces the former single flat ~/.asha/learnings.md (now read only by the
one-time migrator, migrate_learnings_to_okf.py). The public API
(add/confirm/contradict/query/list/export) is unchanged so callers
(pattern_analyzer) and their return-dict contracts are unaffected.

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
    python learnings_manager.py render-hot --max-bytes 3000   # session-start injection
    python learnings_manager.py rebuild-index
    python learnings_manager.py migrate-okf [--dry-run]       # one-time flat->dir migration
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

try:
    import yaml
except ImportError:  # pragma: no cover - fallback path exercised only without PyYAML
    yaml = None


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
    created: str = ""
    updated: str = ""
    extra_body: str = ""  # preserved non-canonical body sections (verbatim)

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
# Paths & constants
# =============================================================================

ASHA_DIR = Path.home() / ".asha"
LEARNINGS_DIR = ASHA_DIR / "learnings"          # OKF bundle (concept files)

RESERVED_SLUGS = {"index", "log"}               # OKF reserved filenames
HOT_MIN_CONFIDENCE = 0.7
HOT_MAX_ENTRIES = 10
HOT_MAX_BYTES = 50_000

# Evidence bullet: indent-tolerant so it matches BOTH the new body format
# ("- date | project | note [effect]") and the legacy flat/canonical format
# ("  - date | project | note [effect]"). MULTILINE so $ anchors per line.
EVIDENCE_PATTERN = re.compile(
    r'^[ \t]*-[ \t]+(?P<date>\d[\d-]*) \| (?P<project>[\w-]+) \| '
    r'(?P<note>.+?)(?:\s*\[(?P<effect>\w+)\])?[ \t]*$',
    re.MULTILINE
)

# A '## Heading' line (used to find/preserve non-Evidence body sections).
_SECTION_RE = re.compile(r'^## (.+?)[ \t]*$', re.MULTILINE)

# YAML frontmatter block at the very start of a file (BOM/CRLF tolerant).
_FRONTMATTER_RE = re.compile(
    r"^﻿?---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|$)", re.DOTALL
)

_DEFAULT_PREAMBLE = (
    "# Learnings\n\n"
    "Cross-project patterns with confidence tracking. "
    "Consulted at session start.\n\n"
    "---"
)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# =============================================================================
# Frontmatter (YAML via PyYAML; JSON-scalar fallback when PyYAML is absent)
# =============================================================================

def _dump_frontmatter(data: Dict[str, Any]) -> str:
    """Render an ordered dict as a '---'-delimited YAML frontmatter block."""
    if yaml is not None:
        body = yaml.safe_dump(
            data, sort_keys=False, allow_unicode=True, default_flow_style=False
        ).strip()
    else:
        # JSON scalars are valid YAML scalars; this safely quotes free text.
        lines = []
        for key, value in data.items():
            if isinstance(value, str):
                lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
            else:
                lines.append(f"{key}: {value}")
        body = "\n".join(lines)
    return f"---\n{body}\n---"


def _load_frontmatter(text: str):
    """Return (data_dict, body_str). Missing/invalid frontmatter -> ({}, text)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fm_text = match.group(1) or ""
    body = text[match.end():]
    if yaml is not None:
        try:
            data = yaml.safe_load(fm_text)
        except yaml.YAMLError:
            data = {}
    else:
        data = {}
        for line in fm_text.splitlines():
            if ":" not in line:
                continue
            key, _, rest = line.partition(":")
            rest = rest.strip()
            if rest[:1] in ('"', "[", "{") or rest[:1].isdigit() or rest in ("true", "false", "null"):
                try:
                    rest = json.loads(rest)
                except (ValueError, json.JSONDecodeError):
                    pass
            data[key.strip()] = rest
    if not isinstance(data, dict):
        data = {}
    return data, body


# =============================================================================
# Slug / path helpers
# =============================================================================

def _slugify(learning_id: str) -> str:
    """Filesystem-safe slug for a learning id. id == slug == filename stem."""
    slug = re.sub(r'[^a-z0-9]+', '-', str(learning_id).lower()).strip('-')
    slug = re.sub(r'-{2,}', '-', slug)
    if not slug:
        slug = "learning"
    if slug in RESERVED_SLUGS:
        slug = f"{slug}-1"
    return slug


def _learning_path(learning_id: str) -> Path:
    return LEARNINGS_DIR / f"{_slugify(learning_id)}.md"


# =============================================================================
# Rendering
# =============================================================================

def _render_learning(learning: Learning) -> str:
    """Render a Learning as a full OKF concept file (frontmatter + body)."""
    front = {
        "type": "learning",
        "id": learning.id,
        "title": learning.id,
        "description": learning.trigger or learning.action or learning.id,
        "category": learning.category,
        "confidence": learning.confidence,
        "tier": "hot" if learning.confidence >= HOT_MIN_CONFIDENCE else "cold",
        "trigger": learning.trigger,
        "action": learning.action,
        "created": learning.created or _today(),
        "updated": learning.updated or _today(),
    }
    parts = [
        _dump_frontmatter(front),
        "",
        f"# {learning.id}",
        "",
        f"**Trigger:** {learning.trigger}",
        f"**Action:** {learning.action}",
        "",
        "## Evidence",
    ]
    for ev in learning.evidence[-5:]:
        marker = f" [{ev.effect}]" if ev.effect != "confirm" else ""
        parts.append(f"- {ev.date} | {ev.project} | {ev.note}{marker}")
    body = "\n".join(parts)
    if learning.extra_body.strip():
        body = body.rstrip() + "\n\n" + learning.extra_body.strip() + "\n"
    else:
        body = body.rstrip() + "\n"
    return body


def _render_canonical_block(learning: Learning) -> str:
    """Render the compact '### id' block used for session-start injection
    (matches the historical flat-file hot-tier shape byte-for-byte)."""
    lines = [
        f"### {learning.id}",
        f"- **Confidence**: {learning.confidence}",
        f"- **Trigger**: {learning.trigger}",
        f"- **Action**: {learning.action}",
        "- **Evidence**:",
    ]
    for ev in learning.evidence[-5:]:
        marker = f" [{ev.effect}]" if ev.effect != "confirm" else ""
        lines.append(f"  - {ev.date} | {ev.project} | {ev.note}{marker}")
    return "\n".join(lines)


def _extract_extra_sections(body: str) -> str:
    """Concatenate all '## ' body sections except Evidence, verbatim.

    Lets a hand-added '## Notes' / '## Related' survive a write round-trip —
    the one piece of the old preservation ethos worth keeping.
    """
    extras = []
    matches = list(_SECTION_RE.finditer(body))
    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        if heading.lower() == "evidence":
            continue
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        extras.append(body[start:end].rstrip())
    return "\n\n".join(extras)


# =============================================================================
# Per-file storage I/O
# =============================================================================

def _atomic_write_file(path: Path, text: str) -> None:
    """Write text to path atomically (tmp + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _parse_file(path: Path) -> Learning:
    """Parse one concept file into a Learning. Frontmatter is authoritative for
    scalar fields; evidence is parsed from the body; unknown '##' sections are
    preserved in extra_body."""
    text = path.read_text(encoding="utf-8")
    data, body = _load_frontmatter(text)

    learning_id = str(data.get("id") or path.stem)
    category = str(data.get("category") or "Uncategorized")
    try:
        confidence = round(float(data.get("confidence", 0.3)), 2)
    except (TypeError, ValueError):
        confidence = 0.3

    evidence: List[Evidence] = []
    for match in EVIDENCE_PATTERN.finditer(body):
        evidence.append(Evidence(
            date=match.group("date"),
            project=match.group("project"),
            note=match.group("note").strip(),
            effect=match.group("effect") or "confirm",
        ))

    return Learning(
        id=learning_id,
        category=category,
        confidence=confidence,
        trigger=str(data.get("trigger") or ""),
        action=str(data.get("action") or ""),
        evidence=evidence,
        created=str(data.get("created") or ""),
        updated=str(data.get("updated") or ""),
        extra_body=_extract_extra_sections(body),
    )


def _write_learning(learning: Learning) -> bool:
    """Persist a single learning (silence-guarded). Returns False if skipped."""
    if _silence_marker_present():
        return False
    if not learning.created:
        learning.created = _today()
    learning.updated = _today()
    _atomic_write_file(_learning_path(learning.id), _render_learning(learning))
    return True


def _delete_learning(learning_id: str) -> bool:
    """Delete a single learning file (silence-guarded). Returns False if skipped."""
    if _silence_marker_present():
        return False
    _learning_path(learning_id).unlink(missing_ok=True)
    return True


def parse_learnings() -> Dict[str, List[Learning]]:
    """Parse the bundle into {category: [Learning]}. Same shape as the legacy
    flat parser, so query/list/export are unaffected."""
    learnings: Dict[str, List[Learning]] = {}
    if not LEARNINGS_DIR.is_dir():
        return learnings
    for path in sorted(LEARNINGS_DIR.glob("*.md")):
        if path.name in ("index.md", "log.md"):
            continue
        try:
            learning = _parse_file(path)
        except Exception:
            # Never let one malformed file crash synthesis/injection.
            continue
        learnings.setdefault(learning.category, []).append(learning)
    return learnings


def write_learnings(learnings: Dict[str, List[Learning]]) -> None:
    """Compat shim: persist a {category: [Learning]} dict as concept files,
    silence-guarded, then rebuild the index. Retained for whole-dict callers."""
    if _silence_marker_present():
        return
    for entries in learnings.values():
        for learning in entries:
            _write_learning(learning)
    _rebuild_index()


def _rebuild_index() -> None:
    """Regenerate index.md (OKF reserved root index; only okf_version frontmatter)."""
    if _silence_marker_present():
        return
    learnings = parse_learnings()
    flat = [(cat, l) for cat, entries in learnings.items() for l in entries]
    # category asc, confidence desc, id asc (stable multi-key)
    flat.sort(key=lambda t: t[1].id)
    flat.sort(key=lambda t: t[1].confidence, reverse=True)
    flat.sort(key=lambda t: t[0].lower())

    lines = [
        '---', 'okf_version: "0.1"', '---', '',
        '# Learnings Index', '',
        f'{len(flat)} concept(s). Generated by learnings_manager.py — do not edit.', '',
        '| id | category | confidence | tier |',
        '|----|----------|-----------|------|',
    ]
    for cat, learning in flat:
        tier = "hot" if learning.confidence >= HOT_MIN_CONFIDENCE else "cold"
        slug = _slugify(learning.id)
        lines.append(f"| [{learning.id}]({slug}.md) | {cat} | {learning.confidence} | {tier} |")
    _atomic_write_file(LEARNINGS_DIR / "index.md", "\n".join(lines) + "\n")


# =============================================================================
# Hot-tier selection / injection
# =============================================================================

def render_hot_tier(
    max_entries: int = HOT_MAX_ENTRIES,
    min_confidence: float = HOT_MIN_CONFIDENCE,
    max_bytes: int = HOT_MAX_BYTES,
) -> str:
    """Render the hot tier for session-start injection.

    Selection: confidence >= min_confidence, total-ordered by
    (confidence desc, updated desc, id asc), capped at max_entries, then grouped
    by category and truncated at an entry boundary under max_bytes. Output matches
    the historical flat hot-file shape so the injected prompt text is unchanged.
    """
    learnings = parse_learnings()
    flat = [l for entries in learnings.values() for l in entries
            if l.confidence >= min_confidence]
    # Stable multi-key sort (apply least-significant first).
    flat.sort(key=lambda l: l.id)
    flat.sort(key=lambda l: l.updated or "", reverse=True)
    flat.sort(key=lambda l: l.confidence, reverse=True)
    selected = flat[:max_entries]

    preamble = _DEFAULT_PREAMBLE
    if not selected:
        return preamble + "\n"

    # Order categories by their best confidence, then name (deterministic).
    best_in_cat: Dict[str, float] = {}
    for l in selected:
        best_in_cat[l.category] = max(best_in_cat.get(l.category, 0.0), l.confidence)
    cats = sorted(best_in_cat, key=lambda c: (-best_in_cat[c], c))

    result = preamble
    emitted = 0
    truncated = False
    for cat in cats:
        cat_entries = sorted(
            [l for l in selected if l.category == cat],
            key=lambda l: (-l.confidence, l.id),
        )
        header_emitted = False
        for learning in cat_entries:
            chunk = ("" if header_emitted else f"\n\n## {cat}")
            chunk += "\n\n" + _render_canonical_block(learning)
            if emitted > 0 and len((result + chunk).encode("utf-8")) > max_bytes:
                truncated = True
                break
            result += chunk
            header_emitted = True
            emitted += 1
        if truncated:
            break
    return result + "\n"


# =============================================================================
# Operations (public API — signatures and return dicts are frozen)
# =============================================================================

def add_learning(
    category: str,
    learning_id: str,
    trigger: str,
    action: str,
    project: str,
    reason: str
) -> Dict[str, Any]:
    """Add a new learning or reinforce an existing one (upsert by id)."""
    path = _learning_path(learning_id)

    if path.exists():
        existing = _parse_file(path)
        existing.add_evidence(project, reason, "confirm")
        _write_learning(existing)
        _rebuild_index()
        return {"status": "updated", "id": existing.id, "confidence": existing.confidence}

    slug = _slugify(learning_id)
    learning = Learning(
        id=slug,
        category=category,
        confidence=0.3,  # New learnings start low
        trigger=trigger,
        action=action,
        evidence=[Evidence(
            date=_today(),
            project=project,
            note=reason,
            effect="initial"
        )],
        created=_today(),
        updated=_today(),
    )
    _write_learning(learning)
    _rebuild_index()
    return {"status": "created", "id": slug, "confidence": learning.confidence}


def confirm_learning(learning_id: str, project: str, reason: str = "Pattern confirmed") -> Dict[str, Any]:
    """Confirm a learning, increasing confidence"""
    path = _learning_path(learning_id)
    if not path.exists():
        return {"status": "not_found", "id": learning_id}
    learning = _parse_file(path)
    learning.add_evidence(project, reason, "confirm")
    _write_learning(learning)
    _rebuild_index()
    return {"status": "confirmed", "id": learning.id, "confidence": learning.confidence}


def contradict_learning(learning_id: str, project: str, reason: str) -> Dict[str, Any]:
    """Contradict a learning, decreasing confidence (remove if it drops <0.2)"""
    path = _learning_path(learning_id)
    if not path.exists():
        return {"status": "not_found", "id": learning_id}
    learning = _parse_file(path)
    old_confidence = learning.confidence
    learning.add_evidence(project, reason, "contradict")

    if learning.confidence < 0.2:
        _delete_learning(learning.id)
        _rebuild_index()
        return {
            "status": "removed",
            "id": learning.id,
            "reason": "Confidence dropped below threshold"
        }

    _write_learning(learning)
    _rebuild_index()
    return {
        "status": "contradicted",
        "id": learning.id,
        "confidence": learning.confidence,
        "dropped_from": old_confidence
    }


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
# Cross-linking ("## Related" sections)
# =============================================================================

# Matches a Related bullet: "- [slug](slug.md) — reason" (reason optional).
_RELATED_RE = re.compile(r'-\s*\[[^\]]+\]\(([^)]+?)(?:\.md)?\)(?:\s*[—-]\s*(.*))?\s*$')


def _parse_related(learning: Learning) -> Dict[str, str]:
    """Return {target_slug: reason} from the learning's '## Related' section."""
    out: Dict[str, str] = {}
    in_related = False
    for line in learning.extra_body.splitlines():
        stripped = line.strip()
        if stripped.lower() == "## related":
            in_related = True
            continue
        if in_related:
            if stripped.startswith("## "):
                break
            m = _RELATED_RE.match(stripped)
            if m:
                out[m.group(1)] = (m.group(2) or "").strip()
    return out


def _strip_related(extra_body: str) -> str:
    """Return extra_body with any existing '## Related' section removed."""
    if not extra_body.strip():
        return ""
    parts = re.split(r'(?m)^(?=## )', extra_body)
    kept = [p for p in parts if not p.strip().lower().startswith("## related")]
    return "".join(kept).rstrip()


def _render_related(links: Dict[str, str]) -> str:
    lines = ["## Related"]
    for slug in sorted(links):
        reason = links[slug]
        lines.append(f"- [{slug}]({slug}.md)" + (f" — {reason}" if reason else ""))
    return "\n".join(lines)


def _apply_related(learning: Learning, links: Dict[str, str]) -> bool:
    """Set the learning's '## Related' section to `links` (slug->reason), preserving
    any other body sections. Returns True if extra_body changed (idempotent)."""
    base = _strip_related(learning.extra_body)
    if links:
        block = _render_related(links)
        new_extra = (base + "\n\n" + block).strip() if base else block
    else:
        new_extra = base
    if new_extra == learning.extra_body.strip():
        return False
    learning.extra_body = new_extra
    return True


def link_learnings(source_id: str, targets: List[str], reason: str = "",
                   bidirectional: bool = False) -> Dict[str, Any]:
    """Idempotently add '## Related' links from source_id to each target, merging
    with existing links. Dangling targets and self-links are skipped; reciprocal
    links are added when bidirectional. Silence-guarded via _write_learning."""
    src_path = _learning_path(source_id)
    if not src_path.exists():
        return {"status": "not_found", "id": source_id}
    src = _parse_file(src_path)
    src_slug = _slugify(source_id)

    links = _parse_related(src)
    for tid in targets:
        tslug = _slugify(tid)
        if tslug == src_slug or not _learning_path(tid).exists():
            continue
        links[tslug] = reason or links.get(tslug, "")

    changed: List[str] = []
    if _apply_related(src, links) and _write_learning(src):
        changed.append(src.id)

    if bidirectional:
        for tid in targets:
            tslug = _slugify(tid)
            if tslug == src_slug or not _learning_path(tid).exists():
                continue
            tl = _parse_file(_learning_path(tid))
            tlinks = _parse_related(tl)
            tlinks[src_slug] = reason or tlinks.get(src_slug, "")
            if _apply_related(tl, tlinks) and _write_learning(tl):
                changed.append(tl.id)

    _rebuild_index()
    return {"status": "linked", "id": source_id, "changed": sorted(set(changed))}


def prune_dangling_links() -> Dict[str, Any]:
    """Drop '## Related' entries whose target file no longer exists, bundle-wide."""
    if not LEARNINGS_DIR.is_dir():
        return {"status": "pruned", "files": []}
    pruned: List[str] = []
    for path in sorted(LEARNINGS_DIR.glob("*.md")):
        if path.name in ("index.md", "log.md"):
            continue
        learning = _parse_file(path)
        existing = _parse_related(learning)
        if not existing:
            continue
        kept = {s: r for s, r in existing.items() if (LEARNINGS_DIR / f"{s}.md").exists()}
        if kept != existing and _apply_related(learning, kept) and _write_learning(learning):
            pruned.append(learning.id)
    if pruned:
        _rebuild_index()
    return {"status": "pruned", "files": sorted(pruned)}


def link_candidates(days: int = 7) -> Dict[str, Any]:
    """Learnings updated within `days`, plus a compact bundle summary — the bounded
    input for the /save link-suggestion step."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    learnings = parse_learnings()
    flat = [l for entries in learnings.values() for l in entries]
    candidates = [
        {"id": l.id, "category": l.category, "trigger": l.trigger,
         "action": l.action, "confidence": l.confidence,
         "related": sorted(_parse_related(l).keys())}
        for l in flat if (l.updated or "") >= cutoff
    ]
    bundle = [{"id": l.id, "category": l.category, "trigger": l.trigger} for l in flat]
    return {
        "window_days": days,
        "since": cutoff,
        "candidates": sorted(candidates, key=lambda c: c["id"]),
        "bundle": sorted(bundle, key=lambda c: c["id"]),
    }


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

    # Render-hot command (session-start injection)
    render_parser = subparsers.add_parser("render-hot", help="Render the hot tier for injection")
    render_parser.add_argument("--max-bytes", type=int, default=HOT_MAX_BYTES, help="Byte budget")
    render_parser.add_argument("--max-entries", type=int, default=HOT_MAX_ENTRIES, help="Entry cap")

    # Rebuild-index command
    subparsers.add_parser("rebuild-index", help="Regenerate index.md")

    # Migrate command (one-time flat -> directory)
    migrate_okf_parser = subparsers.add_parser(
        "migrate-okf", help="Migrate legacy flat learnings to the OKF bundle")
    migrate_okf_parser.add_argument("--dry-run", action="store_true")
    migrate_alias = subparsers.add_parser("migrate", help="Alias for migrate-okf")
    migrate_alias.add_argument("--dry-run", action="store_true")

    # Cross-linking commands
    link_parser = subparsers.add_parser("link", help="Add/merge '## Related' cross-links")
    link_parser.add_argument("--id", "-i", required=True, help="Source learning id")
    link_parser.add_argument("--to", required=True, help="Comma-separated target ids")
    link_parser.add_argument("--reason", "-r", default="", help="Short why for the link")
    link_parser.add_argument("--bidirectional", "-b", action="store_true", help="Also add reciprocal links")

    subparsers.add_parser("prune-links", help="Drop dangling '## Related' entries bundle-wide")

    cand_parser = subparsers.add_parser("link-candidates", help="Recently-updated learnings + bundle summary (JSON)")
    cand_parser.add_argument("--days", type=int, default=7, help="Rolling window in days")

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
        elif args.command == "render-hot":
            # Plain text to stdout (consumed by session-start.sh), not JSON.
            sys.stdout.write(render_hot_tier(max_entries=args.max_entries, max_bytes=args.max_bytes))
            return
        elif args.command == "rebuild-index":
            _rebuild_index()
            result = {"status": "rebuilt", "dir": str(LEARNINGS_DIR)}
        elif args.command == "link":
            targets = [t.strip() for t in args.to.split(",") if t.strip()]
            result = link_learnings(args.id, targets, args.reason, args.bidirectional)
        elif args.command == "prune-links":
            result = prune_dangling_links()
        elif args.command == "link-candidates":
            result = link_candidates(days=args.days)
        elif args.command in ("migrate-okf", "migrate"):
            migrator = Path(__file__).parent / "migrate_learnings_to_okf.py"
            cmd = [sys.executable, str(migrator)]
            if getattr(args, "dry_run", False):
                cmd.append("--dry-run")
            sys.exit(subprocess.call(cmd))
        else:
            result = {"error": f"Unknown command: {args.command}"}

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
