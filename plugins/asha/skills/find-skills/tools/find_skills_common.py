"""Shared validation, hashing, HTTP, frontmatter, and safety helpers."""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping, Sequence
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced by parse_frontmatter
    yaml = None


SEARCH_URL = "https://www.skills.sh/api/search"
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
LOCK_SCHEMA_VERSION = 1
MAX_FILES = 512
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 100
MAX_JSON_CONTAINERS = 100_000
MAX_JSON_TOKENS = 250_000
MAX_YAML_DEPTH = 100
MAX_YAML_EVENTS = 10_000

STANDARD_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
UNSUPPORTED_PORTABLE_KEYS = {"allowed-tools"}

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class FindSkillsError(RuntimeError):
    """Expected, user-actionable failure."""


class ValidationError(FindSkillsError):
    """Candidate or lock data did not meet the contract."""


class CollisionError(FindSkillsError):
    """An existing name or path is owned by something else."""


class ApprovalError(FindSkillsError):
    """A mutating operation lacked explicit approval."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def json_safe(value: Any) -> Any:
    """Return a deterministic JSON-safe representation of YAML-native data."""
    return _json_safe(value, set(), set())


def _json_safe(value: Any, active: set[int], seen: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"yaml_binary_hex": value.hex()}
    container = isinstance(value, (Mapping, set, frozenset, Sequence))
    identity = id(value)
    if container and identity in active:
        return "<recursive reference>"
    if container and identity in seen:
        return "<repeated reference>"
    if container:
        active.add(identity)
        seen.add(identity)
    try:
        return _json_safe_container(value, active, seen)
    finally:
        if container:
            active.remove(identity)


def _json_safe_container(value: Any, active: set[int], seen: set[int]) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str):
                safe_key = key
            else:
                safe_key = f"<non-string {type(key).__name__}: {key!r}>"
            while safe_key in result:
                safe_key += "#"
            result[safe_key] = _json_safe(item, active, seen)
        return result
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item, active, seen) for item in sorted(value, key=repr)]
    if isinstance(value, Sequence):
        return [_json_safe(item, active, seen) for item in value]
    return str(value)


class HttpClient:
    """Small bounded urllib client, injectable in tests."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def get_bytes(
        self,
        url: str,
        *,
        accept: str = "application/octet-stream",
        limit: int = MAX_FILE_BYTES,
    ) -> bytes:
        request = urllib.request.Request(
            url,
            headers={"Accept": accept, "User-Agent": "asha-find-skills/1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > limit:
                    raise ValidationError(f"response exceeds {limit} bytes: {url}")
                data = response.read(limit + 1)
        except ValueError as exc:
            raise ValidationError(
                f"response has an invalid Content-Length: {url}"
            ) from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            raise FindSkillsError(
                f"GET {url} failed with HTTP {exc.code}: {detail.strip()}"
            ) from exc
        except urllib.error.URLError as exc:
            raise FindSkillsError(f"GET {url} failed: {exc.reason}") from exc
        if len(data) > limit:
            raise ValidationError(f"response exceeds {limit} bytes: {url}")
        return data

    def get_json(self, url: str) -> Any:
        raw = self.get_bytes(url, accept="application/json", limit=4 * 1024 * 1024)
        try:
            text = raw.decode("utf-8")
            _validate_json_structure(raw, url)
            return json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ValidationError(f"response was not valid UTF-8 JSON: {url}") from exc


def _validate_json_structure(raw: bytes, url: str) -> None:
    depth = 0
    containers = 0
    tokens = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            containers += 1
            tokens += 1
            if depth > MAX_JSON_DEPTH:
                raise ValidationError(
                    f"JSON nesting depth exceeds {MAX_JSON_DEPTH}: {url}"
                )
            if containers > MAX_JSON_CONTAINERS:
                raise ValidationError(
                    f"JSON container count exceeds {MAX_JSON_CONTAINERS}: {url}"
                )
        elif byte in (0x2C, 0x3A):
            tokens += 1
        elif byte in (0x5D, 0x7D):
            depth = max(0, depth - 1)
        if tokens > MAX_JSON_TOKENS:
            raise ValidationError(f"JSON token count exceeds {MAX_JSON_TOKENS}: {url}")


def validate_relative_path(value: str, *, label: str) -> str:
    unsafe_categories = {"Cc", "Cf", "Cs", "Zl", "Zp"}
    invalid_text = (
        not value
        or value.startswith("/")
        or "//" in value
        or "\\" in value
        or any(unicodedata.category(ch) in unsafe_categories for ch in value)
    )
    if invalid_text:
        raise ValidationError(f"unsafe {label}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValidationError(f"unsafe {label}: {value!r}")
    return str(path)


def github_url(kind: str, source: str, tail: str = "") -> str:
    quoted = "/".join(
        urllib.parse.quote(part, safe="") for part in source.split("/")
    )
    suffix = f"/{tail}" if tail else ""
    return f"{GITHUB_API}/repos/{quoted}/{kind}{suffix}"


def resolve_revision(source: str, ref: str | None, client: HttpClient) -> str:
    requested = urllib.parse.quote(ref or "HEAD", safe="")
    payload = client.get_json(github_url("commits", source, requested))
    if not isinstance(payload, Mapping) or not isinstance(payload.get("sha"), str):
        raise ValidationError("GitHub commit response has no sha")
    revision = payload["sha"].lower()
    if not REVISION_RE.fullmatch(revision):
        raise ValidationError(f"GitHub returned a non-immutable revision: {revision!r}")
    return revision


def _frontmatter_parts(data: bytes) -> tuple[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("SKILL.md must be UTF-8") from exc
    if text.startswith("\ufeff"):
        raise ValidationError("SKILL.md must not begin with a UTF-8 BOM")
    if not text.startswith(("---\n", "---\r\n")):
        raise ValidationError("SKILL.md is missing opening YAML frontmatter")
    match = re.match(
        r"\A---\r?\n(?:(.*?)\r?\n)?---(?:\r?\n|\Z)", text, re.DOTALL
    )
    if not match:
        raise ValidationError("SKILL.md is missing closing YAML frontmatter")
    return match.group(1) or "", text[match.end() :]


def _frontmatter_errors(parsed: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    name = parsed.get("name")
    description = parsed.get("description")
    if not isinstance(name, str) or not name:
        errors.append("missing or non-string required key: name")
    elif len(name) > 64 or not NAME_RE.fullmatch(name):
        errors.append("name must be 1-64 lowercase ASCII letters, digits, or single hyphens")
    if not isinstance(description, str) or not description.strip():
        errors.append("missing or non-string required key: description")
    elif len(description) > 1024:
        errors.append("description exceeds 1024 characters")
    license_value = parsed.get("license")
    if "license" in parsed and not isinstance(license_value, str):
        errors.append("license must be a string")
    compatibility = parsed.get("compatibility")
    if "compatibility" in parsed:
        if compatibility is None or (
            isinstance(compatibility, str) and not compatibility.strip()
        ):
            errors.append("compatibility must be non-empty when provided")
        elif not isinstance(compatibility, str):
            errors.append("compatibility must be a string")
        elif len(compatibility) > 500:
            errors.append("compatibility exceeds 500 characters")
    metadata = parsed.get("metadata")
    if "metadata" in parsed and not isinstance(metadata, Mapping):
        errors.append("metadata must be a mapping")
    elif isinstance(metadata, Mapping):
        errors.extend(
            f"metadata key {key!r} must be a string"
            for key in metadata
            if not isinstance(key, str)
        )
        if any(not isinstance(value, str) for value in metadata.values()):
            errors.append("metadata values must be strings")
    if "allowed-tools" in parsed and not isinstance(parsed["allowed-tools"], str):
        errors.append("allowed-tools must be a string")
    return errors


def parse_frontmatter(data: bytes) -> tuple[dict[str, Any], str, list[str]]:
    frontmatter, body = _frontmatter_parts(data)
    if yaml is None:
        raise FindSkillsError("PyYAML is required to parse SKILL.md frontmatter")
    _validate_yaml_structure(frontmatter)
    try:
        loaded = yaml.safe_load(frontmatter)
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        detail = str(exc).splitlines()[0]
        raise ValidationError(f"frontmatter is not valid YAML: {detail}") from exc
    if loaded is None:
        parsed: dict[str, Any] = {}
    elif not isinstance(loaded, Mapping):
        raise ValidationError("frontmatter YAML must be a mapping")
    elif any(not isinstance(key, str) for key in loaded):
        raise ValidationError("frontmatter YAML keys must be strings")
    else:
        parsed = dict(loaded)
    return parsed, body, _frontmatter_errors(parsed)


def _validate_yaml_structure(frontmatter: str) -> None:
    depth = 0
    try:
        for count, event in enumerate(
            yaml.parse(frontmatter, Loader=yaml.SafeLoader), start=1
        ):
            if count > MAX_YAML_EVENTS:
                raise ValidationError(
                    f"frontmatter YAML exceeds {MAX_YAML_EVENTS} parser events"
                )
            if isinstance(
                event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)
            ):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise ValidationError(
                        f"frontmatter YAML depth exceeds {MAX_YAML_DEPTH}"
                    )
            elif isinstance(
                event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)
            ):
                depth -= 1
    except ValidationError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        detail = str(exc).splitlines()[0]
        raise ValidationError(f"frontmatter is not valid YAML: {detail}") from exc


def split_declared(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, Mapping):
        return [
            f"{key}={value[key]}" for key in sorted(value, key=lambda item: str(item))
        ]
    return [part for part in re.split(r"[\s,]+", str(value).strip()) if part]


def tree_digest(files: Mapping[str, Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(files[path]["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _finding(category: str, path: str, evidence: str) -> dict[str, str]:
    return {"category": category, "path": path, "evidence": evidence[:240]}


SAFETY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "network_calls": (
        re.compile(r"https?://", re.I),
        re.compile(
            r"\b(?:curl|wget|urllib|requests\.|aiohttp|http\.client|socket\.(?:create_connection|socket)|fetch\s*\(|git\s+clone|gh\s+api)",
            re.I,
        ),
    ),
    "shell_out": (
        re.compile(r"\b(?:subprocess\.|os\.(?:system|popen)|child_process|shell\s*=\s*True)\b", re.I),
        re.compile(r"(?:^|\s)(?:bash|/bin/sh|sh\s+-c)(?:\s|$)", re.I | re.M),
    ),
    "package_installation": (
        re.compile(
            r"\b(?:(?:python(?:3)?\s+-m\s+|uv\s+)?pip(?:3)?\s+install|npm\s+install|npx\b|pnpm\b|yarn\b|bun\b)",
            re.I,
        ),
        re.compile(r"\b(?:apt(?:-get)?|brew|dnf|yum)\s+install\b", re.I),
    ),
    "credential_access": (
        re.compile(r"(?:\.ssh/|\.aws/|\.config/gcloud|keychain|credential|api[_ -]?key|token|secret)", re.I),
        re.compile(r"(?:os\.environ|getenv\s*\(|process\.env)", re.I),
    ),
    "path_escape": (
        re.compile(r"(?:^|[\s/'\"`])\.\.(?:/|\\)", re.M),
        re.compile(r"(?:^|[\s'\"`])/(?:etc|home|Users|root|var)/", re.M),
    ),
}


def _pattern_findings(path: str, text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for category, patterns in SAFETY_PATTERNS.items():
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                excerpt = text[max(0, match.start() - 40) : match.end() + 80]
                findings.append(_finding(category, path, excerpt.replace("\n", " ").strip()))
                break
    return findings


def _qualified_call_name(
    function: ast.expr, aliases: Mapping[str, str]
) -> str | None:
    parts: list[str] = []
    current = function
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name) or current.id not in aliases:
        return None
    return ".".join([aliases[current.id], *reversed(parts)])


def _python_import_findings(path: str, text: str) -> list[dict[str, str]]:
    python_shebang = re.match(
        r"\A#![^\r\n]*\bpython(?:\d+(?:\.\d+)*)?\b", text
    )
    if not path.lower().endswith(".py") and not python_shebang:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                aliases[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    findings: list[dict[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _qualified_call_name(node.func, aliases)
        if target is None:
            continue
        if target.startswith("subprocess.") or target in {"os.system", "os.popen"}:
            category = "shell_out"
        elif (
            target.startswith(("urllib.", "requests.", "aiohttp.", "http.client."))
            or target in {"socket.create_connection", "socket.socket"}
        ):
            category = "network_calls"
        else:
            continue
        evidence = ast.get_source_segment(text, node) or target
        findings.append(_finding(category, path, evidence.replace("\n", " ")))
    return findings


def _shape_findings(item: Mapping[str, Any], text: str) -> list[dict[str, str]]:
    path = str(item["path"])
    lower_path = path.lower()
    findings: list[dict[str, str]] = []
    if item.get("executable") and path != "SKILL.md":
        findings.append(_finding("executable_support_file", path, f"upstream mode {item['mode']}"))
    shell_file = lower_path.endswith(".sh") or item["data"].startswith(
        (b"#!/bin/sh", b"#!/usr/bin/env sh", b"#!/usr/bin/env bash", b"#!/bin/bash")
    )
    if shell_file:
        findings.append(_finding("shell_out", path, "shell support file or shell shebang"))
    posix = PurePosixPath(lower_path)
    credential_path = posix.name in {".env", "credentials", "credentials.json", "secrets.json"}
    if credential_path or any(part in {".ssh", ".aws", ".gnupg"} for part in posix.parts):
        findings.append(_finding("credential_access", path, "credential-shaped support path"))
    if item.get("symlink"):
        category = "path_escape" if text.strip().startswith("/") or ".." in PurePosixPath(text.strip()).parts else "symlink_support_file"
        findings.append(_finding(category, path, f"upstream symlink target: {text.strip()}"))
    if item["data"].startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        findings.append(_finding("git_lfs_pointer", path, "repository contains an LFS pointer, not payload bytes"))
    return findings


def assess_safety(files: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for item in files:
        path = item["path"]
        text = item["data"].decode("utf-8", "replace")
        patterns = _pattern_findings(path, text)
        pattern_categories = {finding["category"] for finding in patterns}
        patterns.extend(
            finding
            for finding in _python_import_findings(path, text)
            if finding["category"] not in pattern_categories
        )
        shapes = _shape_findings(item, text)
        if any(item["category"] == "shell_out" for item in patterns):
            shapes = [item for item in shapes if item["category"] != "shell_out"]
        findings.extend(patterns)
        findings.extend(shapes)
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        key = (finding["category"], finding["path"], finding["evidence"])
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def default_repo_root() -> Path:
    configured = os.environ.get("ASHA_ROOT")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[5]


def default_asha_home() -> Path:
    return Path(os.environ.get("ASHA_HOME", "~/.asha")).expanduser().resolve()
