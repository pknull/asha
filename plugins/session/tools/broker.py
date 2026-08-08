#!/usr/bin/env python3
"""Deterministic inline memory, process, and capability brokerage.

The broker is advisory. It reads catalogues and registries, never invokes a
selected capability, mutates memory, creates isolation, or publishes work.
Optional agent surfaces are wrappers around these same protocols.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
import project_root as workspace_project_root  # noqa: E402

ASHA_ROOT = TOOL_DIR.parents[2]
REGISTRY_PATH = ASHA_ROOT / "plugins" / "session" / "broker" / "capabilities.json"
HARNESS_REGISTRY_PATH = ASHA_ROOT / "harnesses" / "capabilities.json"
SUPPORT_VALUES = {"native", "rendered", "partial", "unsupported"}
RISK_RANK = {"low": 0, "medium": 1, "high": 2}
WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.+-]*", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"\[([^]]+)]\(([^)]+\.md)\)")
MEMORY_LINE_RE = re.compile(
    r"^\s*-\s*\[([^]]+)]\(([^)]+\.md)\)\s*(?:[-–—:]\s*)?(.*)$"
)
LEARNING_ROW_RE = re.compile(
    r"^\|\s*\[([^]]+)]\(([^)]+\.md)\)\s*\|\s*([^|]*)\|\s*([0-9.]+)\s*\|\s*([^|]*)\|"
)
SENSITIVE_RE = re.compile(
    r"(?:BEGIN (?:RSA |OPENSSH )?PRIVATE KEY|(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=])",
    re.IGNORECASE,
)
PROHIBITED_OVERRIDE_KEYS = {
    "command", "commands", "shell", "exec", "executable", "action", "actions",
    "permissions", "harness_support", "output_contract", "kind", "ownership", "process",
}
ALLOWED_OVERRIDE_KEYS = {
    "id", "enabled", "description", "categories", "task_patterns",
    "prerequisites", "required_config", "risk", "approval", "fallback",
}


class BrokerError(Exception):
    """Typed user/configuration error; never silently downgraded."""

    def __init__(self, code: str, message: str, *, path: Optional[Path] = None):
        super().__init__(message)
        self.code = code
        self.path = str(path) if path else None


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BrokerError("missing_registry", f"required registry is missing: {path}", path=path) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrokerError("invalid_json", f"cannot read valid JSON from {path}: {exc}", path=path) from exc
    if not isinstance(value, dict):
        raise BrokerError("invalid_registry", f"registry root must be an object: {path}", path=path)
    return value


def _strings(value: Any, field: str, path: Path) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise BrokerError("invalid_registry", f"{field} must be a list of non-empty strings", path=path)
    if len(value) != len(set(value)):
        raise BrokerError("invalid_registry", f"{field} contains duplicates", path=path)
    return list(value)


def _validate_registry(data: dict[str, Any], path: Path) -> dict[str, dict[str, Any]]:
    if data.get("schema_version") != 1:
        raise BrokerError("unsupported_registry_version", "broker registry schema_version must be 1", path=path)
    harness_ref = data.get("harness_registry")
    if not isinstance(harness_ref, dict) or harness_ref.get("path") != "../../../harnesses/capabilities.json" or harness_ref.get("schema_version") != 3:
        raise BrokerError("invalid_registry", "harness_registry must reference harnesses/capabilities.json schema v3", path=path)
    entries = data.get("capabilities")
    if not isinstance(entries, list) or not entries:
        raise BrokerError("invalid_registry", "capabilities must be a non-empty list", path=path)
    result: dict[str, dict[str, Any]] = {}
    required = {
        "id", "kind", "description", "categories", "task_patterns", "prerequisites",
        "required_config", "risk", "approval", "output_contract", "permissions",
        "harness_support", "fallback", "ownership",
    }
    for offset, raw in enumerate(entries):
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise BrokerError("invalid_registry", f"capabilities[{offset}] lacks required fields", path=path)
        cap_id = raw.get("id")
        if not isinstance(cap_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", cap_id):
            raise BrokerError("invalid_registry", f"capabilities[{offset}].id is invalid", path=path)
        if cap_id in result:
            raise BrokerError("duplicate_identifier", f"duplicate capability identifier: {cap_id}", path=path)
        for field in ("categories", "task_patterns", "prerequisites", "required_config", "approval", "permissions"):
            _strings(raw.get(field), f"{cap_id}.{field}", path)
        if raw.get("risk") not in RISK_RANK:
            raise BrokerError("invalid_registry", f"{cap_id}.risk is invalid", path=path)
        support = raw.get("harness_support")
        if not isinstance(support, dict) or set(support) != {"claude", "codex", "copilot"}:
            raise BrokerError("invalid_registry", f"{cap_id}.harness_support must name all harnesses", path=path)
        for harness, ref in support.items():
            if not isinstance(ref, dict) or set(ref) != {"capability_ref", "fallback"}:
                raise BrokerError("invalid_registry", f"{cap_id}.{harness} support reference is invalid", path=path)
            expected = f"{harness}.capabilities."
            if not isinstance(ref["capability_ref"], str) or not ref["capability_ref"].startswith(expected):
                raise BrokerError("invalid_registry", f"{cap_id}.{harness} references another harness", path=path)
        result[cap_id] = dict(raw)
    for cap_id, cap in result.items():
        process = cap.get("process")
        if process is None:
            continue
        if not isinstance(process, dict) or not isinstance(process.get("priority"), int):
            raise BrokerError("invalid_registry", f"{cap_id}.process is invalid", path=path)
        for target in _strings(process.get("capability_ids"), f"{cap_id}.process.capability_ids", path):
            if target not in result:
                raise BrokerError("unknown_identifier", f"{cap_id} references unknown capability: {target}", path=path)
        _strings(process.get("verification"), f"{cap_id}.process.verification", path)
    return result


def _find_ancestor(start: Path, relative: str) -> Optional[Path]:
    current = start.resolve()
    for candidate in (current, *current.parents):
        path = candidate / relative
        if path.exists():
            return path
    return None


def _override_paths(project_root: Path, explicit: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    asha_home = Path(os.environ.get("ASHA_HOME", str(Path.home() / ".asha"))).expanduser()
    user_override = asha_home / "broker-capabilities.override.json"
    if user_override.exists():
        paths.append(user_override)
    workspace_override = _find_ancestor(project_root, ".asha/broker-capabilities.override.json")
    if workspace_override and workspace_override not in paths:
        paths.append(workspace_override)
    env_override = os.environ.get("ASHA_BROKER_OVERRIDE")
    if env_override:
        paths.append(Path(env_override).expanduser())
    paths.extend(Path(value).expanduser() for value in explicit)
    return paths


def _merge_override(entries: dict[str, dict[str, Any]], path: Path) -> None:
    data = _json_file(path)
    if data.get("schema_version") != 1 or not isinstance(data.get("capabilities"), list):
        raise BrokerError("invalid_override", "override requires schema_version 1 and a capabilities list", path=path)
    for offset, override in enumerate(data["capabilities"]):
        if not isinstance(override, dict) or not isinstance(override.get("id"), str):
            raise BrokerError("invalid_override", f"capabilities[{offset}] requires an id", path=path)
        cap_id = override["id"]
        if cap_id not in entries:
            raise BrokerError("unknown_identifier", f"override cannot add unknown capability: {cap_id}", path=path)
        keys = set(override)
        dangerous = keys & PROHIBITED_OVERRIDE_KEYS
        unknown = keys - ALLOWED_OVERRIDE_KEYS
        if dangerous:
            raise BrokerError("permission_widening", f"override for {cap_id} may not change {sorted(dangerous)}", path=path)
        if unknown:
            raise BrokerError("invalid_override", f"override for {cap_id} has unknown fields: {sorted(unknown)}", path=path)
        base = entries[cap_id]
        if "enabled" in override and override["enabled"] is not False:
            raise BrokerError("permission_widening", f"override for {cap_id} may only set enabled to false", path=path)
        if "risk" in override:
            risk = override["risk"]
            if risk not in RISK_RANK or RISK_RANK[risk] < RISK_RANK[base["risk"]]:
                raise BrokerError("permission_widening", f"override for {cap_id} cannot lower risk", path=path)
        for field in ("prerequisites", "required_config", "approval"):
            if field in override:
                values = _strings(override[field], f"{cap_id}.{field}", path)
                if not set(base[field]).issubset(values):
                    raise BrokerError("permission_widening", f"override for {cap_id} cannot remove {field}", path=path)
        for field in ("categories", "task_patterns"):
            if field in override:
                _strings(override[field], f"{cap_id}.{field}", path)
        for field in ("description", "fallback"):
            if field in override and (not isinstance(override[field], str) or not override[field]):
                raise BrokerError("invalid_override", f"override for {cap_id} has invalid {field}", path=path)
        for key, value in override.items():
            if key != "id":
                base[key] = value


@dataclass
class Registry:
    entries: dict[str, dict[str, Any]]
    harnesses: dict[str, Any]
    override_paths: list[str]

    def support(self, cap: dict[str, Any], harness: str) -> dict[str, Any]:
        if harness not in self.harnesses:
            raise BrokerError("unknown_harness", f"unknown harness: {harness}")
        support_ref = cap["harness_support"][harness]
        parts = support_ref["capability_ref"].split(".")
        if parts[:2] != [harness, "capabilities"] or len(parts) != 3:
            raise BrokerError("invalid_registry", f"invalid support reference for {cap['id']}: {support_ref['capability_ref']}")
        primitive = self.harnesses[harness].get("capabilities", {}).get(parts[2])
        if not isinstance(primitive, dict) or primitive.get("support") not in SUPPORT_VALUES:
            raise BrokerError("invalid_registry", f"unresolved harness support reference: {support_ref['capability_ref']}")
        return {
            "status": primitive["support"],
            "capability_ref": support_ref["capability_ref"],
            "surface": primitive.get("surface", ""),
            "limitations": list(primitive.get("limitations", [])),
            "fallback": support_ref["fallback"],
        }


def load_registry(project_root: Path, explicit_overrides: Iterable[str] = ()) -> Registry:
    entries = _validate_registry(_json_file(REGISTRY_PATH), REGISTRY_PATH)
    harness_data = _json_file(HARNESS_REGISTRY_PATH)
    if harness_data.get("schema_version") != 3 or not isinstance(harness_data.get("harnesses"), dict):
        raise BrokerError("unsupported_registry_version", "harness registry schema_version must be 3", path=HARNESS_REGISTRY_PATH)
    paths = _override_paths(project_root, explicit_overrides)
    for path in paths:
        _merge_override(entries, path)
    return Registry(entries, harness_data["harnesses"], [str(path.resolve()) for path in paths])


def _tokens(text: str) -> set[str]:
    stop = {"a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "the", "to", "with"}
    values: set[str] = set()
    for raw in WORD_RE.findall(text):
        normalized = raw.strip("._+-").lower()
        for token in (normalized, *re.split(r"[-_.+]", normalized)):
            if len(token) > 1 and token not in stop:
                values.add(token)
    return values


def _pattern_score(task: str, patterns: Iterable[str]) -> tuple[int, list[str]]:
    lower = task.lower()
    task_tokens = _tokens(task)
    matched: list[str] = []
    score = 0
    for pattern in patterns:
        normalized = pattern.lower().strip()
        if not normalized:
            continue
        if normalized in lower:
            matched.append(pattern)
            score += 100 + len(_tokens(pattern))
        else:
            overlap = task_tokens & _tokens(pattern)
            if overlap:
                matched.append(pattern)
                score += len(overlap)
    return score, sorted(set(matched))


def _project_root(value: Optional[str]) -> Path:
    if value:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise BrokerError("invalid_project_root", f"project root is not a directory: {path}", path=path)
        return path
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _workspace(project_root: Path) -> tuple[Optional[Path], Optional[dict[str, Any]], list[dict[str, str]]]:
    detection = workspace_project_root.detect_workspace(start=project_root)
    if detection.errors:
        root = detection.root
        manifest_path = (root / ".asha" / "workspace.json") if root else project_root
        message = "; ".join(f"{error.code}: {error.message}" for error in detection.errors)
        return root, None, [{
            "code": "invalid_workspace_manifest",
            "path": str(manifest_path),
            "message": message,
        }]
    if detection.root is None or detection.manifest is None:
        return None, None, []
    root = detection.root.resolve()
    manifest_path = root / ".asha" / "workspace.json"
    manifest = detection.manifest
    warnings: list[dict[str, str]] = []
    memory = manifest.get("memory") if isinstance(manifest, dict) else None
    operational = memory.get("operational_root", "Memory") if isinstance(memory, dict) else "Memory"
    if not isinstance(operational, str) or operational.startswith("/") or ".." in Path(operational).parts:
        warnings.append({"code": "invalid_workspace_operational_root", "path": str(manifest_path), "message": "workspace operational root is unsafe"})
        return root, None, warnings
    try:
        resolved = (root / operational).resolve()
    except (OSError, RuntimeError, ValueError):
        warnings.append({"code": "invalid_workspace_operational_root", "path": str(manifest_path), "message": "workspace operational root cannot be resolved"})
        return root, None, warnings
    if resolved != root and root not in resolved.parents:
        warnings.append({"code": "workspace_escape", "path": str(manifest_path), "message": "workspace operational root escapes workspace"})
        return root, None, warnings
    return root, {"manifest_path": manifest_path, "operational_root": resolved, "manifest": manifest}, warnings


@dataclass
class Budget:
    total: int
    timeout_ms: int
    used: int = 0
    exhausted: bool = False
    timed_out: bool = False

    def __post_init__(self) -> None:
        self.started = time.monotonic()

    def read(self, path: Path) -> Optional[bytes]:
        if self.timeout_ms <= 0 or (time.monotonic() - self.started) * 1000 >= self.timeout_ms:
            self.timed_out = True
            return None
        remaining = self.total - self.used
        if remaining <= 0:
            self.exhausted = True
            return None
        try:
            with path.open("rb") as handle:
                value = handle.read(remaining + 1)
        except (OSError, ValueError):
            return None
        if len(value) > remaining:
            self.exhausted = True
            value = value[:remaining]
        self.used += len(value)
        return value


def _safe_target(root: Path, relative: str) -> Optional[Path]:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        return None
    try:
        target = (root / relative).resolve()
        canonical = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if target == canonical or canonical not in target.parents:
        return None
    return target


def _memory_catalogue(root: Path, authority: str, scope: str, budget: Budget, signature: "hashlib._Hash") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index = root / "MEMORY.md"
    status = {"path": str(index), "authority": authority, "scope": scope, "available": index.is_file()}
    if not index.is_file():
        status["reason"] = "catalogue_unavailable"
        return [], status
    raw = budget.read(index)
    if raw is None:
        status["reason"] = "budget_or_timeout"
        return [], status
    signature.update(str(index.resolve()).encode())
    signature.update(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        status["reason"] = "invalid_utf8"
        return [], status
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = MEMORY_LINE_RE.match(line)
        if not match:
            continue
        target = _safe_target(root, match.group(2))
        if target is None:
            continue
        description = " ".join(part.strip() for part in (match.group(1), match.group(3)) if part.strip())
        if not description or SENSITIVE_RE.search(description):
            continue
        entries.append({
            "id": target.stem,
            "description": description,
            "path": str(target),
            "catalogue_path": str(index.resolve()),
            "authority": authority,
            "scope": scope,
        })
    status["entries"] = len(entries)
    return entries, status


def _learning_catalogue(root: Path, budget: Budget, signature: "hashlib._Hash") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index = root / "index.md"
    status = {"path": str(index), "authority": "evaluated-local", "scope": "user", "available": index.is_file()}
    if not index.is_file():
        status["reason"] = "catalogue_unavailable"
        return [], status
    raw = budget.read(index)
    if raw is None:
        status["reason"] = "budget_or_timeout"
        return [], status
    signature.update(str(index.resolve()).encode())
    signature.update(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        status["reason"] = "invalid_utf8"
        return [], status
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = LEARNING_ROW_RE.match(line)
        if not match:
            continue
        target = _safe_target(root, match.group(2))
        if target is None:
            continue
        description = " ".join((match.group(1), match.group(3).strip(), match.group(5).strip()))
        if SENSITIVE_RE.search(description):
            continue
        try:
            confidence = float(match.group(4))
        except ValueError:
            continue
        entries.append({
            "id": match.group(1), "description": description, "path": str(target),
            "catalogue_path": str(index.resolve()), "authority": "evaluated-local",
            "scope": "user", "confidence": confidence,
        })
    status["entries"] = len(entries)
    return entries, status


def context_brief(task: str, project_root: Path, *, byte_budget: int, timeout_ms: int, limit: int) -> dict[str, Any]:
    if byte_budget < 1:
        raise BrokerError("invalid_budget", "--budget-bytes must be positive")
    if timeout_ms < 0:
        raise BrokerError("invalid_timeout", "--timeout-ms cannot be negative")
    budget = Budget(byte_budget, timeout_ms)
    signature = hashlib.sha256()
    entries: list[dict[str, Any]] = []
    source_status: list[dict[str, Any]] = []
    workspace_root, workspace, warnings = _workspace(project_root)

    source_roots: list[tuple[Path, str, str]] = []
    configured = os.environ.get("ASHA_MEMORY_DIR")
    if configured:
        source_roots.append((Path(configured).expanduser(), "project-operational", "project"))
    source_roots.append((project_root / "Memory", "project-operational", "project"))
    if workspace:
        source_roots.append((workspace["operational_root"], "workspace-operational", "workspace"))
    seen: set[str] = set()
    for root, authority, scope in source_roots:
        try:
            key = str(root.resolve())
        except (OSError, RuntimeError, ValueError):
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        found, status = _memory_catalogue(root, authority, scope, budget, signature)
        entries.extend(found)
        source_status.append(status)

    learnings_root = Path(os.environ.get("ASHA_LEARNINGS_DIR", str(Path.home() / ".asha" / "learnings"))).expanduser()
    found, status = _learning_catalogue(learnings_root, budget, signature)
    entries.extend(found)
    source_status.append(status)

    ranked: list[dict[str, Any]] = []
    query = _tokens(task)
    for item in entries:
        overlap = sorted(query & _tokens(item["description"] + " " + item["id"]))
        if not overlap:
            continue
        row = dict(item)
        row["score"] = round(len(overlap) / max(len(query), 1), 6)
        row["reason"] = f"Catalogue match on: {', '.join(overlap)}"
        row["reason_status"] = "inference"
        row["claim_status"] = "catalogue-backed"
        ranked.append(row)
    authority_order = {"workspace-operational": 0, "project-operational": 1, "evaluated-local": 2}
    ranked.sort(key=lambda row: (-row["score"], authority_order.get(row["authority"], 9), row["id"], row["path"]))
    relevant = ranked[:limit]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in relevant:
        grouped.setdefault(item["id"], []).append(item)
    contradictions = []
    for item_id, values in sorted(grouped.items()):
        descriptions = {value["description"] for value in values}
        if len(descriptions) > 1:
            contradictions.append({
                "id": item_id,
                "status": "requires-review",
                "source_paths": sorted(value["catalogue_path"] for value in values),
                "reason": "The same catalogue identifier has differing descriptions across memory planes.",
            })
    open_questions = [
        {"question": f"Is unavailable configured context required from {item['path']}?", "source_path": item["path"]}
        for item in source_status if not item.get("available")
    ]
    if warnings:
        open_questions.extend({"question": warning["message"], "source_path": warning["path"]} for warning in warnings)

    return {
        "contract": "asha.context-brief.v1",
        "task": task,
        "workspace": str(workspace_root) if workspace_root else None,
        "active_repository": str(project_root),
        "execution_mode": "inline",
        "read_only": True,
        "budget": {
            "configured_bytes": byte_budget, "bytes_read": budget.used,
            "timeout_ms": timeout_ms, "budget_exhausted": budget.exhausted,
            "timed_out": budget.timed_out,
        },
        "source_signature": signature.hexdigest(),
        "source_status": source_status,
        "relevant_sources": relevant,
        "no_relevant_context": len(relevant) == 0,
        "contradictions": contradictions,
        "open_questions": open_questions,
        "warnings": warnings,
    }


def process_route(task: str, registry: Registry, harness: str) -> dict[str, Any]:
    candidates: list[tuple[int, int, str, list[str], dict[str, Any]]] = []
    for cap_id, cap in registry.entries.items():
        process = cap.get("process")
        if process is None or cap.get("enabled", True) is False or cap_id == "process.none":
            continue
        score, matched = _pattern_score(task, cap["task_patterns"])
        if score:
            candidates.append((-score, process["priority"], cap_id, matched, cap))
    if candidates:
        _, _, cap_id, matched, selected = sorted(candidates)[0]
    else:
        cap_id, matched, selected = "process.none", [], registry.entries["process.none"]
    support = registry.support(selected, harness)
    return {
        "contract": "asha.process-route.v1",
        "task": task,
        "execution_mode": "inline",
        "advisory_only": True,
        "recommended": cap_id.removeprefix("process."),
        "registry_id": cap_id,
        "reason": f"Matched registry patterns: {', '.join(matched)}" if matched else "No specialized workflow matched.",
        "risk": selected["risk"],
        "prerequisites": list(selected["prerequisites"]),
        "verification": list(selected["process"]["verification"]),
        "approval_requirements": list(selected["approval"]),
        "selected_capability_ids": list(selected["process"]["capability_ids"]),
        "harness": harness,
        "harness_support": support,
        "fallback": selected["fallback"] if support["status"] != "unsupported" else support["fallback"],
        "prohibited_automatic_actions": ["start-loop", "create-worktree", "publish", "commit", "push", "merge", "delete", "destructive-command"],
    }


def capability_match(task: str, registry: Registry, harness: str) -> dict[str, Any]:
    route = process_route(task, registry, harness)
    selected_ids = list(route["selected_capability_ids"])
    scored: list[tuple[int, str]] = []
    for cap_id, cap in registry.entries.items():
        if cap.get("process") is not None or cap.get("enabled", True) is False:
            continue
        score, _ = _pattern_score(task, cap["task_patterns"])
        if score:
            scored.append((-score, cap_id))
    for _, cap_id in sorted(scored):
        if cap_id not in selected_ids:
            selected_ids.append(cap_id)
    selected: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for cap_id in selected_ids:
        cap = registry.entries.get(cap_id)
        if cap is None:
            raise BrokerError("unknown_identifier", f"route references unknown capability: {cap_id}")
        support = registry.support(cap, harness)
        missing_config = [name for name in cap["required_config"] if not os.environ.get(name)]
        row = {
            "id": cap_id, "kind": cap["kind"], "reason": cap["description"],
            "support": support, "prerequisites": list(cap["prerequisites"]),
            "required_config": list(cap["required_config"]), "missing_config": missing_config,
            "risk": cap["risk"], "approval_requirements": list(cap["approval"]),
            "output_contract": cap["output_contract"], "fallback": cap["fallback"],
            "registry_source": str(REGISTRY_PATH),
        }
        if cap.get("enabled", True) is False:
            row["unavailable_reason"] = "disabled-by-override"
            unavailable.append(row)
        elif support["status"] == "unsupported" or missing_config:
            row["unavailable_reason"] = "unsupported" if support["status"] == "unsupported" else "missing-configuration"
            unavailable.append(row)
        else:
            selected.append(row)
    return {
        "contract": "asha.capability-match.v1",
        "task": task,
        "execution_mode": "inline",
        "advisory_only": True,
        "harness": harness,
        "process": route["recommended"],
        "process_registry_id": route["registry_id"],
        "selected": selected,
        "unavailable": unavailable,
        "fallback": route["fallback"] if not unavailable else "Use each unavailable capability's inline fallback; do not simulate an unsupported surface.",
        "registry": {
            "path": str(REGISTRY_PATH), "schema_version": 1,
            "harness_registry_path": str(HARNESS_REGISTRY_PATH), "harness_schema_version": 3,
            "overrides": registry.override_paths,
        },
        "prohibited_automatic_actions": route["prohibited_automatic_actions"],
    }


def _silenced(project_root: Path) -> bool:
    if (project_root / "Work" / "markers" / "silence").is_file():
        return True
    workspace_root, _, _ = _workspace(project_root)
    return bool(workspace_root and (workspace_root / "Work" / "markers" / "silence").is_file())


def _telemetry(command: str, result: dict[str, Any], project_root: Path, harness: str) -> None:
    # Read-only by default. Diagnostics become a deliberate side effect only
    # when the operator explicitly opts in.
    if os.environ.get("ASHA_BROKER_TELEMETRY") != "1" or _silenced(project_root):
        return
    asha_home = Path(os.environ.get("ASHA_HOME", str(Path.home() / ".asha"))).expanduser()
    path = asha_home / "state" / "broker-events.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                return
        event = {
            "version": 1, "timestamp": int(time.time()), "event": command,
            "harness": harness, "status": "ok",
            "result_count": len(result.get("relevant_sources", result.get("selected", []))),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, (json.dumps(event, separators=(",", ":")) + "\n").encode())
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except OSError:
        pass


def _human_context(result: dict[str, Any]) -> str:
    lines = [f"Context brief: {len(result['relevant_sources'])} relevant source(s) [inline]"]
    if result["no_relevant_context"]:
        lines.append("no_relevant_context")
    for item in result["relevant_sources"]:
        confidence = f" confidence={item['confidence']:.2f}" if "confidence" in item else ""
        lines.append(f"- {item['id']} [{item['authority']}/{item['scope']}{confidence}] {item['path']}")
        lines.append(f"  {item['reason']}")
    if result["contradictions"]:
        lines.append(f"Contradictions: {len(result['contradictions'])} (review required)")
    if result["open_questions"]:
        lines.append(f"Open questions: {len(result['open_questions'])}")
    budget = result["budget"]
    lines.append(f"Budget: {budget['bytes_read']}/{budget['configured_bytes']} bytes; timeout={budget['timed_out']}")
    return "\n".join(lines)


def _human_route(result: dict[str, Any]) -> str:
    lines = [f"Process: {result['recommended']} [inline, {result['risk']} risk]", result["reason"]]
    if result["prerequisites"]:
        lines.append("Prerequisites: " + ", ".join(result["prerequisites"]))
    if result["approval_requirements"]:
        lines.append("Approvals: " + ", ".join(result["approval_requirements"]))
    lines.append("Verification: " + ", ".join(result["verification"]))
    lines.append(f"Harness: {result['harness_support']['status']} ({result['harness_support']['capability_ref']})")
    lines.append("Fallback: " + result["fallback"])
    return "\n".join(lines)


def _human_capabilities(result: dict[str, Any]) -> str:
    lines = [f"Capabilities for {result['process']}: {len(result['selected'])} selected, {len(result['unavailable'])} unavailable [inline]"]
    for item in result["selected"]:
        lines.append(f"- {item['id']}: {item['support']['status']} ({item['support']['capability_ref']})")
    for item in result["unavailable"]:
        reason = item.get("unavailable_reason", item["support"]["status"])
        lines.append(f"- unavailable {item['id']}: {reason}; fallback: {item['fallback']}")
    lines.append("Fallback: " + result["fallback"])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asha broker", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)
    context = sub.add_parser("context-brief")
    route = sub.add_parser("process-route")
    match = sub.add_parser("capabilities-match")
    for child in (context, route, match):
        child.add_argument("task", nargs="+")
        child.add_argument("--json", action="store_true", dest="as_json")
        child.add_argument("--project-root")
        child.add_argument("--harness", choices=("claude", "codex", "copilot"), default=os.environ.get("ASHA_HARNESS", "claude"))
        child.add_argument("--override", action="append", default=[])
    # Keep environment defaults as strings until main's guarded validation.
    # argparse does not apply ``type`` to defaults; converting here would let a
    # malformed environment variable escape the BrokerError boundary with a
    # traceback before argument parsing even starts.
    context.add_argument("--budget-bytes", default=os.environ.get("ASHA_BROKER_CONTEXT_BYTES", "16384"))
    context.add_argument("--timeout-ms", default=os.environ.get("ASHA_BROKER_TIMEOUT_MS", "250"))
    context.add_argument("--limit", type=int, default=5)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    task = " ".join(args.task).strip()
    as_json = args.as_json
    try:
        if not task:
            raise BrokerError("empty_task", "task must be non-empty")
        project = _project_root(args.project_root)
        if args.command == "context-brief":
            try:
                args.budget_bytes = int(args.budget_bytes)
                args.timeout_ms = int(args.timeout_ms)
            except (TypeError, ValueError) as exc:
                raise BrokerError(
                    "invalid_environment",
                    "ASHA_BROKER_CONTEXT_BYTES and ASHA_BROKER_TIMEOUT_MS must be integers",
                ) from exc
            if args.limit < 1:
                raise BrokerError("invalid_limit", "--limit must be positive")
            result = context_brief(task, project, byte_budget=args.budget_bytes, timeout_ms=args.timeout_ms, limit=args.limit)
            human = _human_context(result)
        else:
            registry = load_registry(project, args.override)
            if args.command == "process-route":
                result = process_route(task, registry, args.harness)
                human = _human_route(result)
            else:
                result = capability_match(task, registry, args.harness)
                human = _human_capabilities(result)
        _telemetry(args.command, result, project, args.harness)
        print(json.dumps(result, indent=2, sort_keys=True) if as_json else human)
        return 0
    except BrokerError as exc:
        error = {"contract": "asha.broker-error.v1", "error": {"code": exc.code, "message": str(exc), "path": exc.path}}
        if as_json:
            print(json.dumps(error, indent=2, sort_keys=True))
        else:
            print(f"asha broker: {exc.code}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
