#!/usr/bin/env python3
"""Deterministic inline process and capability brokerage.

The broker is advisory. It reads registries, never invokes a selected
capability, mutates memory, creates isolation, or publishes work.
Optional agent surfaces are wrappers around these same protocols.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


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
PROHIBITED_OVERRIDE_KEYS = {
    "command", "commands", "shell", "exec", "executable", "action", "actions",
    "permissions", "harness_support", "output_contract", "kind", "ownership", "process",
    "dependencies",
}
ALLOWED_OVERRIDE_KEYS = {
    "id", "enabled", "description", "categories", "task_patterns",
    "prerequisites", "required_config", "risk", "approval", "fallback",
}
# Dependency edges (issue #124). The registry is their only authority; a
# condition is typed data the resolver evaluates, never a command it runs.
DEPENDENCY_RELATIONS = ("requires", "optional")
CONDITION_TYPES = ("always", "command-missing")
COMMAND_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
MAX_PROBE_COMMANDS = 16
# "skipped": the edge's source does not apply here, so it was not evaluated.
ACTIVATION_RANK = {"inactive": 0, "skipped": 0, "unevaluated": 1, "active": 2}
NODE_ACTIVATION = {0: "inactive", 1: "conditional", 2: "active"}


class BrokerError(Exception):
    """Typed user/configuration error; never silently downgraded."""

    def __init__(self, code: str, message: str, *, path: Optional[Path] = None,
                 details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.path = str(path) if path else None
        self.details = details


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
        if not isinstance(support, dict) or set(support) != {"claude", "codex", "copilot", "opencode"}:
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
    for cap_id, cap in result.items():
        _validate_dependencies(cap_id, cap, result, path)
    _reject_cycles(result, path)
    return result


def _validate_condition(cap_id: str, target: str, condition: Any, path: Path) -> None:
    where = f"{cap_id} -> {target} condition"
    if not isinstance(condition, dict) or condition.get("type") not in CONDITION_TYPES:
        raise BrokerError("invalid_registry", f"{where} must be one of {list(CONDITION_TYPES)}", path=path)
    if condition["type"] == "always":
        if set(condition) != {"type"}:
            raise BrokerError("invalid_registry", f"{where} 'always' takes no other fields", path=path)
        return
    command = condition.get("command")
    if set(condition) != {"type", "command"} or not isinstance(command, str) \
            or not COMMAND_NAME_RE.fullmatch(command):
        raise BrokerError("invalid_registry", f"{where} needs one bare command name", path=path)


def _validate_dependencies(cap_id: str, cap: dict[str, Any], entries: dict[str, dict[str, Any]],
                           path: Path) -> None:
    edges = cap.get("dependencies")
    if edges is None:
        return
    if not isinstance(edges, list):
        raise BrokerError("invalid_registry", f"{cap_id}.dependencies must be a list", path=path)
    process_ids = set((cap.get("process") or {}).get("capability_ids", []))
    seen: set[str] = set()
    for offset, edge in enumerate(edges):
        if not isinstance(edge, dict) or not isinstance(edge.get("id"), str) \
                or edge.get("relation") not in DEPENDENCY_RELATIONS \
                or not set(edge).issubset({"id", "relation", "when", "reason"}):
            raise BrokerError("invalid_registry", f"{cap_id}.dependencies[{offset}] is invalid", path=path)
        target = edge["id"]
        if "reason" in edge and (not isinstance(edge["reason"], str) or not edge["reason"]):
            raise BrokerError("invalid_registry", f"{cap_id} -> {target} reason must be non-empty", path=path)
        if "when" in edge:
            _validate_condition(cap_id, target, edge["when"], path)
        if target in seen or target in process_ids:
            raise BrokerError("conflicting_metadata", f"{cap_id} declares {target} more than once",
                              path=path, details={"from": cap_id, "to": target})
        seen.add(target)
        if target not in entries:
            raise BrokerError("unknown_identifier", f"{cap_id} depends on unknown capability: {target}",
                              path=path, details={"from": cap_id, "to": target})


def _edges(cap: dict[str, Any]) -> list[dict[str, Any]]:
    """Every outgoing edge, process capabilities first, in declaration order."""
    edges = [
        {"id": target, "relation": "requires", "when": {"type": "always"}, "source": "process"}
        for target in (cap.get("process") or {}).get("capability_ids", [])
    ]
    for edge in cap.get("dependencies") or []:
        edges.append({**edge, "when": edge.get("when", {"type": "always"}), "source": "dependencies"})
    return edges


def _reject_cycles(entries: dict[str, dict[str, Any]], path: Path) -> None:
    # Conditions are ignored here: an edge that is inactive on this machine is
    # still part of the declared graph and must not hide a cycle.
    state: dict[str, int] = {}
    for start in sorted(entries):
        if state.get(start):
            continue
        stack: list[tuple[str, Iterator[dict[str, Any]]]] = [(start, iter(_edges(entries[start])))]
        trail = [start]
        state[start] = 1
        while stack:
            node, pending = stack[-1]
            edge = next(pending, None)
            if edge is None:
                stack.pop()
                trail.pop()
                state[node] = 2
                continue
            target = edge["id"]
            if state.get(target) == 1:
                cycle = trail[trail.index(target):] + [target]
                raise BrokerError("dependency_cycle", "dependency cycle: " + " -> ".join(cycle),
                                  path=path, details={"cycle": cycle})
            if not state.get(target):
                state[target] = 1
                trail.append(target)
                stack.append((target, iter(_edges(entries[target]))))


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


class _Probe:
    """Explicit, bounded PATH lookups. A command is located, never run."""

    def __init__(self, requested: bool):
        self.requested = requested
        self.results: dict[str, bool] = {}
        self.truncated = False

    def command_found(self, command: str) -> Optional[bool]:
        if not self.requested:
            return None
        if command not in self.results:
            if len(self.results) >= MAX_PROBE_COMMANDS:
                self.truncated = True
                return None
            self.results[command] = shutil.which(command) is not None
        return self.results[command]

    def report(self) -> dict[str, Any]:
        if not self.requested:
            return {"requested": False}
        return {
            "requested": True, "kind": "path-lookup", "truncated": self.truncated,
            "commands": [{"name": name, "found": found} for name, found in self.results.items()],
        }


def _condition_state(condition: dict[str, Any], probe: _Probe) -> str:
    if condition["type"] == "always":
        return "active"
    found = probe.command_found(condition["command"])
    if found is None:
        return "unevaluated"
    return "inactive" if found else "active"


def _describe_condition(condition: dict[str, Any]) -> str:
    return "always" if condition["type"] == "always" else f"{condition['type']} {condition['command']}"


def capability_plan(cap_id: str, registry: Registry, harness: str, *, probe: bool = False) -> dict[str, Any]:
    """Resolve one explicitly selected capability's dependency closure.

    Inspection only: it reads the already-loaded registry, checks that
    configuration names are present (never their values), and with ``probe``
    looks commands up on PATH. It never runs, installs, authenticates, loads a
    skill, writes state, or executes the selected workflow.
    """
    if cap_id not in registry.entries:
        raise BrokerError("unknown_identifier", f"unknown capability: {cap_id}", details={"to": cap_id})
    lookups = _Probe(probe)
    edges: list[dict[str, Any]] = []
    order: list[str] = []
    # Iterative depth-first walk, edges in declaration order. The registry was
    # proven acyclic at load, so post-order is a dependencies-first order.
    stack: list[tuple[str, Iterator[dict[str, Any]]]] = [(cap_id, iter(_edges(registry.entries[cap_id])))]
    visited = {cap_id}
    while stack:
        node, pending = stack[-1]
        edge = next(pending, None)
        if edge is None:
            stack.pop()
            order.append(node)
            continue
        target = edge["id"]
        row = {
            "from": node, "to": target, "relation": edge["relation"], "source": edge["source"],
            "condition": dict(edge["when"]), "condition_state": "unevaluated",
        }
        if "reason" in edge:
            row["reason"] = edge["reason"]
        edges.append(row)
        if target not in visited:
            visited.add(target)
            stack.append((target, iter(_edges(registry.entries[target]))))

    depth = {cap_id: 0}
    frontier = [cap_id]
    while frontier:
        following: list[str] = []
        for node in frontier:
            for row in edges:
                if row["from"] == node and row["to"] not in depth:
                    depth[row["to"]] = depth[node] + 1
                    following.append(row["to"])
        frontier = following

    # Evaluate conditions and propagate root-first (reverse post-order is a
    # topological order), so a node's activation is final before its own edges
    # are looked at: edges below an inactive node are never probed.
    activation = {cap_id: ACTIVATION_RANK["active"]}
    # required_active: an all-"requires" path whose conditions all hold;
    # required_live: one whose conditions may hold (unevaluated counts);
    # declared: an all-"requires" path whatever the conditions say.
    required_active = {cap_id}
    required_live = {cap_id}
    declared = {cap_id}
    for node in reversed(order):
        for row in (e for e in edges if e["from"] == node):
            if activation.get(node, 0) > ACTIVATION_RANK["inactive"]:
                row["condition_state"] = _condition_state(row["condition"], lookups)
            else:
                row["condition_state"] = "skipped"
            edge_rank = ACTIVATION_RANK[row["condition_state"]]
            target = row["to"]
            activation[target] = max(activation.get(target, 0), min(activation.get(node, 0), edge_rank))
            requires = row["relation"] == "requires"
            if requires and node in required_active and edge_rank == ACTIVATION_RANK["active"]:
                required_active.add(target)
            if requires and node in required_live and edge_rank > ACTIVATION_RANK["inactive"]:
                required_live.add(target)
            if requires and node in declared:
                declared.add(target)

    nodes: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    conditional_blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    prerequisites: list[dict[str, Any]] = []
    config_rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    approvals: list[dict[str, str]] = []
    for node_id in sorted(order, key=lambda value: (depth[value], order.index(value))):
        cap = registry.entries[node_id]
        support = registry.support(cap, harness)
        state = NODE_ACTIVATION[activation.get(node_id, 0)]
        live = state != "inactive"
        enabled = cap.get("enabled", True) is not False
        present = {name: bool(os.environ.get(name)) for name in cap["required_config"]}
        node_blockers: list[dict[str, Any]] = []
        if not enabled:
            node_blockers.append({"capability": node_id, "reason": "disabled-by-override"})
        if support["status"] == "unsupported":
            node_blockers.append({"capability": node_id, "reason": "unsupported-on-harness",
                                  "detail": support["capability_ref"]})
        absent = [name for name, found in present.items() if not found]
        if absent:
            node_blockers.append({"capability": node_id, "reason": "missing-configuration", "names": absent})
        nodes.append({
            "id": node_id, "kind": cap["kind"], "description": cap["description"],
            "depth": depth[node_id], "activation": state,
            "requirement": "required" if node_id in declared else "optional",
            "enabled": enabled, "declared_support": support,
            "availability": {"state": "unverified", "basis": "declared-support-only"},
            "prerequisites": list(cap["prerequisites"]),
            "required_config": [{"name": name, "present": found} for name, found in present.items()],
            "approval_requirements": list(cap["approval"]), "risk": cap["risk"],
            "fallback": support["fallback"] if support["status"] == "unsupported" else cap["fallback"],
            "blockers": node_blockers,
        })
        if not live:
            continue
        if node_id in required_active:
            blockers.extend(node_blockers)
        elif node_id in required_live:
            conditional_blockers.extend(node_blockers)
        else:
            warnings.extend(node_blockers)
        # Aggregated items carry the same classification as the blocker lists:
        # required for certain, required only if a condition holds, or optional.
        label = {"applicability": "required" if node_id in required_active
                 else "conditional" if node_id in required_live else "optional"}
        prerequisites.extend({"capability": node_id, "name": name, "state": "unverified", **label}
                             for name in cap["prerequisites"])
        for name, found in present.items():
            config_rows.append({"capability": node_id, "name": name, "present": found,
                                "verifies": "presence-only", **label})
            if not found:
                missing.append({"type": "config", "capability": node_id, "name": name, **label})
            else:
                unverified.append({"type": "credential", "capability": node_id, "name": name,
                                   "detail": "presence does not establish authentication or authorization"})
        approvals.extend({"capability": node_id, "approval": value, **label} for value in cap["approval"])
        unverified.append({"type": "availability", "capability": node_id,
                           "detail": "declared harness support only; installation and runtime availability not inspected"})

    foundation_needed = False
    for row in edges:
        if row["condition"]["type"] != "command-missing":
            continue
        if activation.get(row["from"], 0) == ACTIVATION_RANK["inactive"]:
            continue
        if row["condition_state"] == "unevaluated":
            detail = ("probe bound reached; not evaluated" if lookups.requested
                      else "not evaluated; rerun with --probe for a PATH lookup")
            unverified.append({"type": "condition", "edge": f"{row['from']} -> {row['to']}",
                               "condition": _describe_condition(row["condition"]), "detail": detail})
        elif row["condition_state"] == "active":
            requires = row["relation"] == "requires"
            applicability = ("required" if requires and row["from"] in required_active
                             else "conditional" if requires and row["from"] in required_live else "optional")
            missing.append({"type": "command", "capability": row["from"], "name": row["condition"]["command"],
                            "remedy": row["to"], "applicability": applicability})
            foundation_needed = foundation_needed or applicability == "required"

    selected = registry.entries[cap_id]
    root_support = registry.support(selected, harness)
    if blockers:
        status = "blocked"
    elif foundation_needed:
        # A required command is known to be missing; its foundation must run,
        # with its approvals, before the selected capability. Certain, so it
        # outranks blockers that hold only behind an unevaluated condition.
        status = "needs-foundation"
    elif conditional_blockers:
        status = "conditionally-blocked"
    else:
        status = "no-known-blockers"
    return {
        "contract": "asha.capability-plan.v1",
        "selected": {"id": cap_id, "kind": selected["kind"], "description": selected["description"]},
        "harness": harness,
        "execution_mode": "inspection",
        "advisory_only": True,
        "status": status,
        "nodes": nodes,
        "edges": edges,
        "resolution_order": [node for node in order if activation.get(node, 0) != ACTIVATION_RANK["inactive"]],
        "prerequisites": prerequisites,
        "required_config": config_rows,
        "missing": missing,
        "unverified": unverified,
        "approval_requirements": approvals,
        "blockers": blockers,
        "conditional_blockers": conditional_blockers,
        "warnings": warnings,
        "fallback": root_support["fallback"] if root_support["status"] == "unsupported" else selected["fallback"],
        "probe": lookups.report(),
        "registry": {
            "path": str(REGISTRY_PATH), "schema_version": 1,
            "harness_registry_path": str(HARNESS_REGISTRY_PATH), "harness_schema_version": 3,
            "overrides": registry.override_paths,
        },
        "prohibited_automatic_actions": [
            "install", "authenticate", "load-skills", "write-memory", "execute-workflow",
            "start-loop", "create-worktree", "publish", "commit", "push", "merge", "delete",
            "destructive-command",
        ],
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
        # Never open a FIFO or device: an append would block the broker.
        if path.exists() and not path.is_file():
            return
        event = {
            "version": 1, "timestamp": int(time.time()), "event": command,
            "harness": harness, "status": "ok",
            "result_count": len(result.get("relevant_sources", result.get("selected", []))),
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(event, separators=(",", ":")) + "\n").encode())
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except OSError:
        pass


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


def _human_plan(result: dict[str, Any]) -> str:
    lines = [f"Plan for {result['selected']['id']} [inspection only, {result['harness']}]: {result['status']}"]
    for node in result["nodes"]:
        support = node["declared_support"]
        lines.append(f"- {node['id']} ({node['kind']}, {node['requirement']}, {node['activation']}): "
                     f"declared {support['status']} ({support['capability_ref']}); availability unverified")
    for edge in result["edges"]:
        lines.append(f"  edge {edge['from']} -> {edge['to']} [{edge['relation']}] when "
                     f"{_describe_condition(edge['condition'])}: {edge['condition_state']}")
    lines.append("Order: " + " -> ".join(result["resolution_order"]))
    if result["prerequisites"]:
        lines.append("Prerequisites (unverified): " + ", ".join(
            f"{item['capability']}: {item['name']}" for item in result["prerequisites"]))
    if result["approval_requirements"]:
        lines.append("Approvals: " + ", ".join(
            f"{item['capability']}: {item['approval']}" for item in result["approval_requirements"]))
    for item in result["missing"]:
        lines.append(f"Missing {item['type']}: {item['name']} ({item['capability']})"
                     + (f"; remedy: {item['remedy']}" if "remedy" in item else ""))
    for label in ("blockers", "conditional_blockers", "warnings"):
        for item in result[label]:
            lines.append(f"{label.replace('_', ' ').capitalize()}: {item['capability']}: {item['reason']}")
    if any(item["type"] == "condition" for item in result["unverified"]):
        lines.append("Conditions not evaluated; --probe performs PATH lookups only.")
    lines.append("Fallback: " + result["fallback"])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asha broker", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)
    route = sub.add_parser("process-route")
    match = sub.add_parser("capabilities-match")
    plan = sub.add_parser("capabilities-plan",
                          help="read-only dependency plan for one explicitly named capability")
    for child in (route, match):
        child.add_argument("task", nargs="+")
    plan.add_argument("capability")
    plan.add_argument("--probe", action="store_true",
                      help="look conditional commands up on PATH (never runs them)")
    for child in (route, match, plan):
        child.add_argument("--json", action="store_true", dest="as_json")
        child.add_argument("--project-root")
        child.add_argument("--harness", choices=("claude", "codex", "copilot", "opencode"), default=os.environ.get("ASHA_HARNESS", "claude"))
        child.add_argument("--override", action="append", default=[])
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    task = args.capability.strip() if args.command == "capabilities-plan" else " ".join(args.task).strip()
    as_json = args.as_json
    try:
        if not task:
            raise BrokerError("empty_task", "task must be non-empty")
        project = _project_root(args.project_root)
        registry = load_registry(project, args.override)
        if args.command == "capabilities-plan":
            # Inspection writes nothing, telemetry included.
            result = capability_plan(task, registry, args.harness, probe=args.probe)
            print(json.dumps(result, indent=2, sort_keys=True) if as_json else _human_plan(result))
            return 0
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
        if exc.details is not None:
            error["error"]["details"] = exc.details
        if as_json:
            print(json.dumps(error, indent=2, sort_keys=True))
        else:
            print(f"asha broker: {exc.code}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
