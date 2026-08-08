#!/usr/bin/env python3
"""Offline workspace work-item registry and file-adapter contract.

Issue #26 deliberately does not bind Asha to a ticket provider. The core owns
private local records, a preview/scrub boundary, and data-only plans. It never
contacts a network, executes adapter fields, creates Git state, or writes the
canonical knowledge plane.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_root  # noqa: E402


SCHEMA_VERSION = 1
INDEX_FILE = ".asha-workitems-index.json"
DEFAULT_STATUS = "Proposed"
DEFAULT_STALE_DAYS = 30
MAX_ADAPTER_BYTES = 1_048_576
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,119}$")
CORE_FIELDS = (
    "schema_version", "id", "title", "status", "repositories", "created",
    "updated", "links", "relationships",
)
ADAPTER_FIELDS = {
    "external_id", "title", "status", "repositories", "objective",
    "acceptance_criteria", "dependencies", "risks", "decision_links",
    "verification_commands", "source_url", "freshness", "provider",
}
ALLOWED_TARGET_ROOTS = {"repos", "cross-cutting", "decisions", "tickets"}

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_HOME_RE = re.compile(r"(?<![\w.-])/(?:home|Users)/[^/\s]+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?"
    r"(?:-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|\Z)",
    re.I | re.S,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_RE = re.compile(
    r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"AKIA[0-9A-Z]{16})\b"
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|passwd|secret|token)\s*[:=]\s*[^\s,;]+"
)
_CREDENTIAL_URL_RE = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_TRANSCRIPT_LINE_RE = re.compile(
    r"(?im)^\s*(?:[\[{]\s*[\"']?role[\"']?\s*[:=]|role\s*:)"
    r".*(?:user|assistant|system).*$"
)
_BLOCKED_FIELD_RE = re.compile(
    r"(?i)(?:private.?comments?|raw.?transcript|transcript|conversation|"
    r"chat.?log|authorization|auth.?header|cookie|credential|password|"
    r"api.?key|access.?token|secret)"
)


def _issue(code: str, message: str, *, path: Optional[str] = None,
           severity: str = "blocking") -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code, "message": message, "severity": severity,
    }
    if path is not None:
        result["path"] = path
    return result


def _base(operation: str) -> dict[str, Any]:
    return {"operation": operation, "ok": False, "errors": []}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _safe_relative(value: str) -> Optional[str]:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        return None
    return pure.as_posix()


def _contained(root: Path, relative: str) -> Optional[Path]:
    normalized = _safe_relative(relative)
    if normalized is None:
        return None
    try:
        canonical = root.resolve()
        candidate = (root / normalized).resolve()
        candidate.relative_to(canonical)
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def _context(start: Path | str) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    det = project_root.detect_workspace(start=Path(start))
    if det.errors:
        return None, [error._asdict() for error in det.errors]
    if det.root is None or det.manifest is None:
        return None, [_issue("no_workspace", "work items require an explicit workspace manifest")]
    root = det.root.resolve()
    manifest = det.manifest
    memory = manifest.get("memory") or {}
    personal_rel = str(memory.get("personal_root") or "memory-local")
    config = manifest.get("work_items") or {}
    if not isinstance(config, dict):
        return None, [_issue("work_items_config_invalid", "work_items configuration must be an object")]
    registry_rel = str(config.get("private_root") or f"{personal_rel}/work-items")
    try:
        personal = (root / personal_rel).resolve()
        registry = (root / registry_rel).resolve()
        personal.relative_to(root)
        registry.relative_to(personal)
        shared = (root / str(memory.get("shared_root") or "knowledge")).resolve()
        shared.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, [_issue(
            "private_root_escape",
            "work-item root must resolve inside the configured workspace personal_root",
        )]
    repositories = {
        entry.get("path") for entry in manifest.get("repositories", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    stale_days = config.get("stale_days", DEFAULT_STALE_DAYS)
    if isinstance(stale_days, bool) or not isinstance(stale_days, int) or stale_days < 1:
        stale_days = DEFAULT_STALE_DAYS
    return {
        "workspace_root": root,
        "manifest": manifest,
        "registry": registry,
        "registry_rel": registry_rel,
        "shared_root": shared,
        "repositories": repositories,
        "index_enabled": config.get("index_enabled") is True,
        "stale_days": stale_days,
    }, []


def _parse_document(data: bytes) -> tuple[Optional[dict[str, Any]], Optional[str], str]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return None, f"invalid UTF-8: {exc}", ""
    if not text.startswith("---\n"):
        return None, "missing frontmatter", text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, "unclosed frontmatter", text
    values: dict[str, Any] = {}
    for line in text[4:end].split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line or line[:1].isspace():
            return None, f"invalid frontmatter line: {line}", text
        key, raw = line.split(":", 1)
        key, raw = key.strip(), raw.strip()
        if not key or not raw or key in values:
            return None, f"invalid or duplicate frontmatter field: {key}", text
        try:
            values[key] = json.loads(raw)
        except json.JSONDecodeError:
            values[key] = raw.strip("\"'")
    return values, None, text[end + 5:]


def _document_bytes(item: dict[str, Any]) -> bytes:
    ordered = [key for key in CORE_FIELDS if key in item]
    ordered.extend(key for key in item if key not in ordered)
    lines = ["---"]
    for key in ordered:
        lines.append(f"{key}: {json.dumps(item[key], ensure_ascii=False, separators=(',', ':'))}")
    lines.extend(["---", ""])
    return "\n".join(lines).encode("utf-8")


def _validate_id(item_id: str) -> Optional[dict[str, Any]]:
    if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id):
        return _issue(
            "invalid_id", "id must match [a-z0-9][a-z0-9._-]{0,119}",
        )
    return None


def _validate_repositories(values: Any, declared: set[str]) -> list[dict[str, Any]]:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        return [_issue("repositories_invalid", "repositories must be a list of declared paths")]
    unknown = sorted(set(values) - declared)
    if unknown:
        return [_issue(
            "undeclared_repository",
            "work item references repositories absent from the workspace manifest: "
            + ", ".join(unknown),
        )]
    return []


def _validate_item(item: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    id_error = _validate_id(item.get("id"))
    if id_error:
        findings.append(id_error)
    for field in ("title", "status", "created", "updated"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            findings.append(_issue("field_invalid", f"{field} must be a non-empty string"))
    findings.extend(_validate_repositories(item.get("repositories"), ctx["repositories"]))
    if not isinstance(item.get("links"), list) or any(
            not isinstance(value, str) for value in item.get("links", [])):
        findings.append(_issue("links_invalid", "links must be a list of strings"))
    relationships = item.get("relationships")
    if not isinstance(relationships, list):
        findings.append(_issue("relationships_invalid", "relationships must be a list"))
    else:
        for relationship in relationships:
            if not isinstance(relationship, dict) or set(relationship) != {"relation", "target"} \
                    or not all(isinstance(relationship.get(k), str) and relationship[k]
                               for k in ("relation", "target")):
                findings.append(_issue(
                    "relationships_invalid",
                    "each relationship must contain only non-empty relation and target strings",
                ))
                break
    if item.get("schema_version") != SCHEMA_VERSION:
        findings.append(_issue("schema_version_invalid", f"schema_version must be {SCHEMA_VERSION}"))
    return findings


def _stage_file(destination: Path, content: bytes) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def _replace_file(source: Path, destination: Path) -> None:
    """Patch seam for atomic-failure tests."""
    os.replace(source, destination)


def _atomic_write(destination: Path, content: bytes) -> Optional[str]:
    staged: Optional[Path] = None
    try:
        staged = _stage_file(destination, content)
        _replace_file(staged, destination)
        return None
    except OSError as exc:
        return str(exc)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _item_path(ctx: dict[str, Any], item_id: str) -> Optional[Path]:
    if _validate_id(item_id):
        return None
    return _contained(ctx["registry"], f"{item_id}.md")


def _load_item(ctx: dict[str, Any], item_id: str) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    path = _item_path(ctx, item_id)
    if path is None:
        return None, [_validate_id(item_id) or _issue("invalid_id", "invalid id")]
    if not path.is_file() or path.is_symlink():
        return None, [_issue("item_not_found", f"work item not found: {item_id}")]
    try:
        item, why, body = _parse_document(path.read_bytes())
    except OSError as exc:
        return None, [_issue("item_unreadable", f"cannot read work item: {exc}")]
    if why or item is None:
        return None, [_issue("malformed_frontmatter", why or "invalid frontmatter")]
    if body.strip():
        return None, [_issue("body_content_forbidden", "work-item records retain structured frontmatter only")]
    findings = _validate_item(item, ctx)
    if findings:
        return None, findings
    return item, []


def create_item(start: Path | str, item_id: str, title: str,
                repositories: Iterable[str], *, status: str = DEFAULT_STATUS,
                links: Optional[Iterable[str]] = None,
                relationships: Optional[list[dict[str, str]]] = None,
                custom: Optional[dict[str, Any]] = None,
                today: Optional[str] = None) -> dict[str, Any]:
    report = _base("create")
    ctx, errors = _context(start)
    if ctx is None:
        report["errors"] = errors
        return report
    if (id_error := _validate_id(item_id)) is not None:
        report["errors"] = [id_error]
        return report
    repo_values = list(repositories)
    if repo_errors := _validate_repositories(repo_values, ctx["repositories"]):
        report["errors"] = repo_errors
        return report
    path = _item_path(ctx, item_id)
    assert path is not None
    if path.exists() or path.is_symlink():
        report["errors"] = [_issue("item_exists", f"work item already exists: {item_id}")]
        return report
    stamp = today or date.today().isoformat()
    item: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": item_id,
        "title": title,
        "status": status,
        "repositories": repo_values,
        "created": stamp,
        "updated": stamp,
        "links": list(links or []),
        "relationships": list(relationships or []),
    }
    for key, value in (custom or {}).items():
        if not isinstance(key, str) or not FIELD_RE.fullmatch(key):
            report["errors"] = [_issue(
                "custom_field_invalid",
                "custom field names must match [A-Za-z_][A-Za-z0-9_.-]{0,119}",
            )]
            return report
        if key in item:
            report["errors"] = [_issue("reserved_field", f"custom field cannot replace {key}")]
            return report
        item[key] = value
    try:
        json.dumps(item, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        report["errors"] = [_issue(
            "custom_field_invalid", "custom field values must be finite JSON data"
        )]
        return report
    if findings := _validate_item(item, ctx):
        report["errors"] = findings
        return report
    privacy_redactions: list[str] = []
    if _scrub_value(item, privacy_redactions) != item:
        report["errors"] = [_issue(
            "privacy_violation",
            "work item contains secret, transcript, credential, PII, or home-path content",
        )]
        return report
    why = _atomic_write(path, _document_bytes(item))
    if why:
        report["errors"] = [_issue("write_failed", f"atomic work-item write failed: {why}")]
        return report
    report.update({
        "ok": True, "id": item_id,
        "path": f"{ctx['registry_rel']}/{item_id}.md",
    })
    return report


def list_items(start: Path | str) -> dict[str, Any]:
    report = _base("list")
    ctx, errors = _context(start)
    if ctx is None:
        report["errors"] = errors
        return report
    items: list[dict[str, Any]] = []
    if ctx["registry"].is_dir():
        for path in sorted(ctx["registry"].glob("*.md")):
            item, item_errors = _load_item(ctx, path.stem)
            if item_errors:
                report["errors"].extend(item_errors)
                continue
            assert item is not None
            items.append({key: item[key] for key in (
                "id", "title", "status", "repositories", "updated"
            )})
    report.update({"ok": not report["errors"], "items": items, "count": len(items)})
    return report


def show_item(start: Path | str, item_id: str) -> dict[str, Any]:
    report = _base("show")
    ctx, errors = _context(start)
    if ctx is None:
        report["errors"] = errors
        return report
    item, errors = _load_item(ctx, item_id)
    report["errors"] = errors
    if item is not None:
        report.update({"ok": True, "item": item})
    return report


def link_item(start: Path | str, item_id: str, target_id: str, *,
              relation: str = "related", today: Optional[str] = None) -> dict[str, Any]:
    report = _base("link")
    ctx, errors = _context(start)
    if ctx is None:
        report["errors"] = errors
        return report
    item, errors = _load_item(ctx, item_id)
    if errors or item is None:
        report["errors"] = errors
        return report
    target, target_errors = _load_item(ctx, target_id)
    if target_errors or target is None:
        report["errors"] = [_issue("target_not_found", f"relationship target not found: {target_id}")]
        return report
    if not relation.strip():
        report["errors"] = [_issue("relation_invalid", "relationship type must be non-empty")]
        return report
    redactions: list[str] = []
    if _scrub_string(relation, redactions) != relation:
        report["errors"] = [_issue(
            "privacy_violation",
            "relationship type contains content requiring privacy scrubbing",
        )]
        return report
    relationship = {"relation": relation, "target": target_id}
    relationships = item.setdefault("relationships", [])
    if relationship not in relationships:
        relationships.append(relationship)
        item["updated"] = today or date.today().isoformat()
        path = _item_path(ctx, item_id)
        assert path is not None
        if why := _atomic_write(path, _document_bytes(item)):
            report["errors"] = [_issue("write_failed", f"atomic link write failed: {why}")]
            return report
    report.update({"ok": True, "id": item_id, "relationship": relationship})
    return report


def _scrub_string(value: str, redactions: list[str]) -> str:
    def replace(pattern: re.Pattern[str], replacement: str, code: str, text: str) -> str:
        changed, count = pattern.subn(replacement, text)
        if count:
            redactions.append(code)
        return changed

    value = replace(_PRIVATE_KEY_RE, "[REDACTED_SECRET_MATERIAL]", "private_key", value)
    value = replace(_BEARER_RE, "Bearer [REDACTED]", "bearer_credential", value)
    value = replace(_TOKEN_RE, "[REDACTED_TOKEN]", "credential_token", value)
    value = replace(_SECRET_ASSIGN_RE, r"\1=[REDACTED]", "credential_assignment", value)
    value = replace(_CREDENTIAL_URL_RE, r"\1[REDACTED]@", "credential_url", value)
    value = replace(_EMAIL_RE, "[REDACTED_EMAIL]", "email", value)
    value = replace(_PHONE_RE, "[REDACTED_PHONE]", "phone", value)
    value = replace(_SSN_RE, "[REDACTED_SSN]", "ssn", value)
    # Preserve only the path suffix; the username and machine convention go.
    def home_repl(match: re.Match[str]) -> str:
        redactions.append("home_path")
        return "~"
    value = _HOME_RE.sub(home_repl, value)
    value = replace(_TRANSCRIPT_LINE_RE, "[REDACTED_TRANSCRIPT_LINE]", "transcript_line", value)
    return value


def _scrub_value(value: Any, redactions: list[str]) -> Any:
    if isinstance(value, str):
        return _scrub_string(value, redactions)
    if isinstance(value, list):
        return [_scrub_value(item, redactions) for item in value]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if _BLOCKED_FIELD_RE.search(str(key)):
                redactions.append(f"field_removed:{key}")
                continue
            clean[str(key)] = _scrub_value(item, redactions)
        return clean
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def preview_file_candidate(start: Path | str, adapter_file: Path | str) -> dict[str, Any]:
    report = _base("preview")
    ctx, errors = _context(start)
    if ctx is None:
        report["errors"] = errors
        return report
    path = Path(adapter_file)
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > MAX_ADAPTER_BYTES:
            raise OSError("adapter file is not a regular bounded file")
        raw = path.read_bytes()
    except OSError as exc:
        report["errors"] = [_issue("adapter_unavailable", f"file adapter unavailable: {exc}")]
        return report
    try:
        source = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        report["errors"] = [_issue("adapter_invalid", f"file adapter JSON is invalid: {exc}")]
        return report
    if not isinstance(source, dict):
        report["errors"] = [_issue("adapter_invalid", "file adapter candidate must be an object")]
        return report
    ignored = sorted(str(key) for key in source if key not in ADAPTER_FIELDS)
    redactions: list[str] = []
    candidate = {
        key: _scrub_value(source[key], redactions)
        for key in ADAPTER_FIELDS if key in source
    }
    required = ("title", "status", "repositories", "provider", "freshness", "source_url")
    missing = [key for key in required if key not in candidate]
    if missing:
        report["errors"] = [_issue(
            "adapter_fields_missing", "normalized candidate missing: " + ", ".join(missing)
        )]
        return report
    if repo_errors := _validate_repositories(candidate["repositories"], ctx["repositories"]):
        report["errors"] = repo_errors
        return report
    for key in ("title", "status", "provider", "freshness", "source_url"):
        if not isinstance(candidate[key], str) or not candidate[key].strip():
            report["errors"] = [_issue("adapter_field_invalid", f"{key} must be a non-empty string")]
            return report
    list_fields = (
        "acceptance_criteria", "dependencies", "risks", "decision_links",
        "verification_commands",
    )
    for key in list_fields:
        if key in candidate and (not isinstance(candidate[key], list) or any(
                not isinstance(value, str) for value in candidate[key])):
            report["errors"] = [_issue(
                "adapter_field_invalid", f"{key} must be a list of strings"
            )]
            return report
    for key in ("external_id", "objective"):
        if key in candidate and not isinstance(candidate[key], str):
            report["errors"] = [_issue(
                "adapter_field_invalid", f"{key} must be a string"
            )]
            return report
    if _parse_time(candidate["freshness"]) is None:
        report["errors"] = [_issue(
            "adapter_field_invalid", "freshness must be an ISO-8601 timestamp"
        )]
        return report
    digest = _sha(raw)
    token = _sha(_json_bytes({"raw_sha256": digest, "candidate": candidate}))
    report.update({
        "ok": True,
        "candidate": candidate,
        "ignored_fields": ignored,
        "redactions": sorted(set(redactions)),
        "candidate_sha256": digest,
        "preview_token": token,
        "trust": "untrusted-external-input-scrubbed",
    })
    return report


def import_file_candidate(start: Path | str, adapter_file: Path | str, *,
                          item_id: str, preview_token: Optional[str] = None,
                          today: Optional[str] = None) -> dict[str, Any]:
    if not preview_token:
        report = _base("import")
        report["errors"] = [_issue(
            "preview_required", "run preview and pass its preview_token before import"
        )]
        return report
    preview = preview_file_candidate(start, adapter_file)
    if not preview["ok"]:
        preview["operation"] = "import"
        return preview
    if preview_token != preview["preview_token"]:
        report = _base("import")
        report["errors"] = [_issue(
            "preview_mismatch", "adapter candidate changed after preview; preview it again"
        )]
        return report
    candidate = preview["candidate"]
    custom = {
        key: candidate[key] for key in (
            "external_id", "objective", "acceptance_criteria", "dependencies",
            "risks", "verification_commands",
        ) if key in candidate
    }
    custom["adapter_provenance"] = {
        "adapter": "file",
        "provider": candidate["provider"],
        "freshness": candidate["freshness"],
        "source_url": candidate["source_url"],
        "candidate_sha256": preview["candidate_sha256"],
    }
    links = list(candidate.get("decision_links") or [])
    if candidate["source_url"] not in links:
        links.append(candidate["source_url"])
    result = create_item(
        start, item_id, candidate["title"], candidate["repositories"],
        status=candidate["status"], links=links, custom=custom, today=today,
    )
    result["operation"] = "import"
    if result["ok"]:
        result["redactions"] = preview["redactions"]
        result["ignored_fields"] = preview["ignored_fields"]
    return result


def _scan_items(ctx: dict[str, Any]) -> tuple[dict[str, tuple[dict[str, Any], bytes]], list[dict[str, Any]]]:
    found: dict[str, tuple[dict[str, Any], bytes]] = {}
    findings: list[dict[str, Any]] = []
    registry: Path = ctx["registry"]
    if not registry.exists():
        return found, findings
    if not registry.is_dir():
        return found, [_issue("registry_invalid", "work-item root is not a directory")]
    for lexical in sorted(registry.glob("*.md")):
        try:
            resolved = lexical.resolve()
            resolved.relative_to(registry.resolve())
        except (OSError, RuntimeError, ValueError):
            findings.append(_issue("item_escape", "work item resolves outside private root", path=lexical.name))
            continue
        if lexical.is_symlink() or not resolved.is_file():
            findings.append(_issue("item_escape", "symlinked/non-regular work item is refused", path=lexical.name))
            continue
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            findings.append(_issue("item_unreadable", str(exc), path=lexical.name))
            continue
        item, why, body = _parse_document(raw)
        if why or item is None:
            findings.append(_issue("malformed_frontmatter", why or "invalid", path=lexical.name))
            continue
        if body.strip():
            findings.append(_issue(
                "body_content_forbidden", "structured records may not retain raw bodies",
                path=lexical.name,
            ))
        findings.extend({**finding, "path": lexical.name}
                        for finding in _validate_item(item, ctx))
        if item.get("id") != lexical.stem:
            findings.append(_issue(
                "id_filename_mismatch", "frontmatter id must equal filename stem",
                path=lexical.name,
            ))
        redactions: list[str] = []
        scrubbed = _scrub_value(item, redactions)
        if scrubbed != item:
            findings.append(_issue(
                "privacy_violation", "record contains content requiring privacy scrubbing",
                path=lexical.name,
            ))
        if isinstance(item.get("id"), str):
            found[item["id"]] = (item, raw)
    return found, findings


def _parse_time(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _index_payload(items: dict[str, tuple[dict[str, Any], bytes]]) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "items": {
            item_id: {
                "sha256": _sha(raw),
                "status": item.get("status"),
                "updated": item.get("updated"),
            }
            for item_id, (item, raw) in sorted(items.items())
        },
    }


def lint_registry(start: Path | str, *, today: Optional[str] = None) -> dict[str, Any]:
    report = _base("lint")
    ctx, errors = _context(start)
    if ctx is None:
        report["errors"] = errors
        report["findings"] = errors
        return report
    items, findings = _scan_items(ctx)
    ids = set(items)
    for item_id, (item, _) in items.items():
        for relationship in item.get("relationships", []):
            if isinstance(relationship, dict) and relationship.get("target") not in ids:
                findings.append(_issue(
                    "unresolved_relationship",
                    f"relationship target does not exist: {relationship.get('target')}",
                    path=f"{item_id}.md",
                ))
        for link in item.get("links", []):
            if not isinstance(link, str):
                continue
            if re.match(r"^https?://", link):
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", link):
                findings.append(_issue(
                    "invalid_link", f"unsupported link scheme: {link}",
                    path=f"{item_id}.md",
                ))
                continue
            relative = _safe_relative(link)
            target = _contained(ctx["shared_root"], relative) if relative else None
            if target is None:
                findings.append(_issue(
                    "invalid_link", f"link escapes shared knowledge root: {link}",
                    path=f"{item_id}.md",
                ))
            elif not target.is_file():
                findings.append(_issue(
                    "unresolved_link", f"linked knowledge file does not exist: {link}",
                    path=f"{item_id}.md",
                ))
        provenance = item.get("adapter_provenance")
        if provenance is not None:
            required = ("adapter", "provider", "freshness", "source_url", "candidate_sha256")
            if not isinstance(provenance, dict) or any(not provenance.get(key) for key in required):
                findings.append(_issue(
                    "external_reference_unresolved",
                    "adapter provenance lacks provider, freshness, source URL, or digest",
                    path=f"{item_id}.md",
                ))
            else:
                freshness = _parse_time(str(provenance["freshness"]))
                now = _parse_time((today or date.today().isoformat()) + "T00:00:00Z")
                if freshness is None:
                    findings.append(_issue(
                        "external_reference_unresolved", "freshness timestamp is invalid",
                        path=f"{item_id}.md",
                    ))
                elif now is not None and (now - freshness).days > ctx["stale_days"]:
                    findings.append(_issue(
                        "external_reference_stale",
                        f"external reference is older than {ctx['stale_days']} days",
                        path=f"{item_id}.md", severity="advisory",
                    ))
    if ctx["index_enabled"]:
        index_path = ctx["registry"] / INDEX_FILE
        if not index_path.is_file() or index_path.is_symlink():
            findings.append(_issue("index_missing", "enabled work-item index is missing"))
        else:
            try:
                actual = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                actual = None
            if actual != _index_payload(items):
                findings.append(_issue("index_inconsistent", "work-item index does not match registry"))
    blocking = [item for item in findings if item.get("severity") != "advisory"]
    advisory = [item for item in findings if item.get("severity") == "advisory"]
    report.update({
        "ok": not blocking, "errors": blocking, "findings": findings,
        "blocking": blocking, "advisory": advisory, "count": len(items),
    })
    return report


def build_index(start: Path | str) -> dict[str, Any]:
    report = _base("index")
    ctx, errors = _context(start)
    if ctx is None:
        report["errors"] = errors
        return report
    if not ctx["index_enabled"]:
        report["errors"] = [_issue("index_not_enabled", "enable work_items.index_enabled first")]
        return report
    items, findings = _scan_items(ctx)
    blocking = [item for item in findings if item.get("severity") != "advisory"]
    if blocking:
        report["errors"] = blocking
        return report
    path = ctx["registry"] / INDEX_FILE
    if why := _atomic_write(path, _json_bytes(_index_payload(items))):
        report["errors"] = [_issue("write_failed", f"atomic index write failed: {why}")]
        return report
    report.update({"ok": True, "path": f"{ctx['registry_rel']}/{INDEX_FILE}", "count": len(items)})
    return report


def promote_plan(start: Path | str, item_id: str, *, target: str,
                 evidence: Iterable[Path | str], include_worktree_seed: bool = False,
                 git_confirmed: bool = False) -> dict[str, Any]:
    report = _base("promote-plan")
    ctx, errors = _context(start)
    if ctx is None:
        report["errors"] = errors
        return report
    item, errors = _load_item(ctx, item_id)
    if errors or item is None:
        report["errors"] = errors
        return report
    privacy_redactions: list[str] = []
    if _scrub_value(item, privacy_redactions) != item:
        report["errors"] = [_issue(
            "promotion_scrub_required",
            "work item fails the mandatory privacy scrub; correct it before promotion",
        )]
        return report
    normalized_target = _safe_relative(target)
    if normalized_target is None or PurePosixPath(normalized_target).parts[0] not in ALLOWED_TARGET_ROOTS \
            or not normalized_target.endswith(".md"):
        report["errors"] = [_issue("target_invalid", "canonical target must be a safe knowledge .md path")]
        return report
    canonical_target = _contained(ctx["shared_root"], normalized_target)
    if canonical_target is None:
        report["errors"] = [_issue("target_escape", "canonical target resolves outside shared_root")]
        return report
    evidence_values = list(evidence)
    if not evidence_values:
        report["errors"] = [_issue("evidence_required", "promotion plans require repository evidence")]
        return report
    normalized_evidence: list[dict[str, str]] = []
    for value in evidence_values:
        try:
            supplied = Path(value)
            path = supplied.resolve() if supplied.is_absolute() \
                else (ctx["workspace_root"] / supplied).resolve()
            relative = path.relative_to(ctx["workspace_root"]).as_posix()
        except (OSError, RuntimeError, ValueError):
            report["errors"] = [_issue("evidence_escape", "evidence must resolve inside the workspace")]
            return report
        repo = relative.split("/", 1)[0]
        if repo not in item["repositories"] or not path.is_file() or path.is_symlink():
            report["errors"] = [_issue(
                "evidence_invalid", "evidence must be a regular file in a repository nominated by the item",
                path=relative,
            )]
            return report
        try:
            normalized_evidence.append({"path": relative, "sha256": _sha(path.read_bytes())})
        except OSError as exc:
            report["errors"] = [_issue("evidence_unreadable", str(exc), path=relative)]
            return report
    if include_worktree_seed and not git_confirmed:
        report["errors"] = [_issue(
            "git_confirmation_required",
            "worktree seed output requires --confirm-git; it remains data-only",
        )]
        return report
    source_rel = f"{ctx['registry_rel']}/{item_id}.md"
    report.update({
        "ok": True,
        "source": {"kind": "workspace-work-item", "id": item_id, "path": source_rel},
        "evidence": normalized_evidence,
        "target": normalized_target,
        "requires_explicit_promote_apply": True,
        "canonical_write_performed": False,
        "integration": "pass source/evidence/target to workspace_knowledge promotion review",
    })
    if include_worktree_seed:
        report["worktree_seed"] = {
            "data_only": True,
            "git_confirmation_recorded": True,
            "work_item": item_id,
            "repositories": item["repositories"],
            "verification_commands": item.get("verification_commands", []),
        }
    return report


def _custom_fields(values: list[str]) -> tuple[dict[str, Any], Optional[str]]:
    result: dict[str, Any] = {}
    for value in values:
        if "=" not in value:
            return {}, f"custom field must be KEY=VALUE: {value}"
        key, raw = value.split("=", 1)
        if not key:
            return {}, "custom field key is empty"
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result, None


def _render_human(report: dict[str, Any]) -> str:
    operation = report.get("operation", "work-item")
    if not report.get("ok"):
        errors = "; ".join(f"{item.get('code')}: {item.get('message')}"
                           for item in report.get("errors", []))
        return f"work-item {operation}: FAIL — {errors}\n"
    if operation == "list":
        lines = [f"{item['id']}  [{item['status']}]  {item['title']}"
                 for item in report.get("items", [])]
        return ("\n".join(lines) + "\n") if lines else "work-item list: empty\n"
    if operation == "show":
        return json.dumps(report["item"], indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    return f"work-item {operation}: OK\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="workspace_workitems.py")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: str) -> argparse.ArgumentParser:
        value = sub.add_parser(command)
        value.add_argument("--start", default=".")
        value.add_argument("--json", action="store_true")
        return value

    create = common("create")
    create.add_argument("id")
    create.add_argument("--title", required=True)
    create.add_argument("--status", default=DEFAULT_STATUS)
    create.add_argument("--repo", action="append", default=[])
    create.add_argument("--link", action="append", default=[])
    create.add_argument("--field", action="append", default=[])
    listing = common("list")
    show = common("show")
    show.add_argument("id")
    link = common("link")
    link.add_argument("id")
    link.add_argument("target")
    link.add_argument("--relation", default="related")
    preview = common("preview")
    preview.add_argument("--adapter", choices=("file",), default="file")
    preview.add_argument("--file", required=True)
    importing = common("import")
    importing.add_argument("id")
    importing.add_argument("--adapter", choices=("file",), default="file")
    importing.add_argument("--file", required=True)
    importing.add_argument("--preview-token")
    common("lint")
    common("index")
    promote = common("promote-plan")
    promote.add_argument("id")
    promote.add_argument("--target", required=True)
    promote.add_argument("--evidence", action="append", default=[])
    promote.add_argument("--worktree-seed", action="store_true")
    promote.add_argument("--confirm-git", action="store_true")
    args = parser.parse_args(argv[1:])

    if args.command == "create":
        custom, why = _custom_fields(args.field)
        if why:
            report = _base("create")
            report["errors"] = [_issue("custom_field_invalid", why)]
        else:
            report = create_item(
                args.start, args.id, args.title, args.repo, status=args.status,
                links=args.link, custom=custom,
            )
    elif args.command == "list":
        report = list_items(args.start)
    elif args.command == "show":
        report = show_item(args.start, args.id)
    elif args.command == "link":
        report = link_item(args.start, args.id, args.target, relation=args.relation)
    elif args.command == "preview":
        report = preview_file_candidate(args.start, args.file)
    elif args.command == "import":
        report = import_file_candidate(
            args.start, args.file, item_id=args.id,
            preview_token=args.preview_token,
        )
    elif args.command == "lint":
        report = lint_registry(args.start)
    elif args.command == "index":
        report = build_index(args.start)
    else:
        report = promote_plan(
            args.start, args.id, target=args.target, evidence=args.evidence,
            include_worktree_seed=args.worktree_seed,
            git_confirmed=args.confirm_git,
        )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        sys.stdout.write(_render_human(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
