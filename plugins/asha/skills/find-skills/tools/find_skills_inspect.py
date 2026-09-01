"""Skills.sh discovery and pinned upstream inspection."""

from __future__ import annotations

import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping, Sequence
import urllib.parse

from find_skills_common import (
    GITHUB_RAW,
    MAX_FILES,
    MAX_FILE_BYTES,
    MAX_TREE_BYTES,
    SEARCH_URL,
    SOURCE_RE,
    STANDARD_FRONTMATTER_KEYS,
    UNSUPPORTED_PORTABLE_KEYS,
    HttpClient,
    ValidationError,
    assess_safety,
    github_url,
    json_safe,
    parse_frontmatter,
    resolve_revision,
    sha256_bytes,
    split_declared,
    tree_digest,
    validate_relative_path,
)


def search_url(query: str) -> str:
    query = query.strip()
    if len(query) < 2:
        raise ValidationError("Skills.sh search queries must contain at least 2 characters")
    return f"{SEARCH_URL}?{urllib.parse.urlencode({'q': query})}"


def parse_search_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ValidationError("Skills.sh response must be a JSON object")
    records = payload.get("skills")
    if not isinstance(records, list):
        suffix = f": {payload.get('error')}" if payload.get("error") else ""
        raise ValidationError(f"Skills.sh response has no skills[] array{suffix}")
    return [_parse_search_record(item, index) for index, item in enumerate(records)]


def _parse_search_record(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValidationError(f"skills[{index}] must be an object")
    missing = [key for key in ("id", "skillId", "name", "source") if not item.get(key)]
    if missing:
        raise ValidationError(
            f"skills[{index}] is missing required field(s): {', '.join(missing)}"
        )
    source, skill_id, record_id = map(str, (item["source"], item["skillId"], item["id"]))
    if not SOURCE_RE.fullmatch(source):
        raise ValidationError(f"skills[{index}].source is not owner/repo: {source}")
    if record_id != f"{source}/{skill_id}":
        raise ValidationError(
            f"skills[{index}].id does not match source/skillId: {record_id}"
        )
    installs = item.get("installs", 0)
    if not isinstance(installs, (int, float)) or isinstance(installs, bool):
        raise ValidationError(f"skills[{index}].installs must be numeric")
    if isinstance(installs, float) and not math.isfinite(installs):
        raise ValidationError(f"skills[{index}].installs must be finite")
    return {
        "id": record_id,
        "skillId": skill_id,
        "name": str(item["name"]),
        "installs": installs,
        "source": source,
    }


def search_skills(query: str, client: HttpClient | None = None) -> list[dict[str, Any]]:
    client = client or HttpClient()
    return parse_search_payload(client.get_json(search_url(query)))


def parse_candidate(
    candidate: str | None, source: str | None = None, skill_id: str | None = None
) -> tuple[str, str]:
    if candidate:
        bits = candidate.strip().strip("/").split("/")
        if len(bits) < 3:
            raise ValidationError("candidate must be owner/repo/skillId")
        found_source, found_skill_id = "/".join(bits[:2]), "/".join(bits[2:])
        if source and source != found_source:
            raise ValidationError("--source conflicts with the candidate id")
        if skill_id and skill_id != found_skill_id:
            raise ValidationError("--skill-id conflicts with the candidate id")
        source, skill_id = found_source, found_skill_id
    if not source or not skill_id:
        raise ValidationError("provide owner/repo/skillId or both --source and --skill-id")
    if not SOURCE_RE.fullmatch(source):
        raise ValidationError(f"source must be an owner/repo pair: {source}")
    validate_relative_path(skill_id, label="skillId")
    return source, skill_id


def _choose_skill_root(
    tree: Sequence[Mapping[str, Any]], skill_id: str, explicit_path: str | None
) -> tuple[str, str]:
    _validate_tree_entries(tree)
    if explicit_path:
        requested = explicit_path.strip("/")
        root = "." if requested == "." else validate_relative_path(
            requested, label="skill path"
        )
        expected = "SKILL.md" if root == "." else f"{root}/SKILL.md"
        if not any(item.get("path") == expected and item.get("type") == "blob" for item in tree):
            raise ValidationError(f"pinned tree has no {expected}")
        return root, expected
    leaf = PurePosixPath(skill_id).name
    candidates = [
        str(item["path"])
        for item in tree
        if isinstance(item.get("path"), str)
        and item.get("type") == "blob"
        and PurePosixPath(str(item["path"])).name == "SKILL.md"
        and (
            str(item["path"]) == "SKILL.md"
            or PurePosixPath(str(item["path"])).parent.name == leaf
        )
    ]
    if not candidates:
        raise ValidationError(
            f"no SKILL.md directory matching skillId {skill_id!r} in the pinned tree; use --skill-path"
        )
    candidates = _preferred_candidate(candidates, skill_id)
    if len(candidates) != 1:
        raise ValidationError(
            "multiple matching SKILL.md directories; pass --skill-path: "
            + ", ".join(sorted(candidates))
        )
    chosen = candidates[0]
    root = "." if chosen == "SKILL.md" else str(PurePosixPath(chosen).parent)
    return root, chosen


def _preferred_candidate(candidates: list[str], skill_id: str) -> list[str]:
    if len(candidates) == 1:
        return candidates
    suffixes = (
        f"skills/{skill_id}/SKILL.md",
        f".agents/skills/{skill_id}/SKILL.md",
        f".claude/skills/{skill_id}/SKILL.md",
    )
    preferred = [path for suffix in suffixes for path in candidates if path == suffix]
    return preferred if len(preferred) == 1 else candidates


def _fetch_repository(
    source: str, ref: str | None, client: HttpClient
) -> tuple[str, list[Mapping[str, Any]]]:
    revision = resolve_revision(source, ref, client)
    payload = client.get_json(github_url("git/trees", source, f"{revision}?recursive=1"))
    if not isinstance(payload, Mapping) or not isinstance(payload.get("tree"), list):
        raise ValidationError("GitHub tree response has no tree[]")
    if payload.get("truncated"):
        raise ValidationError("GitHub tree response was truncated; refusing an incomplete import")
    tree = payload["tree"]
    _validate_tree_entries(tree)
    return revision, tree


def _validate_tree_entries(tree: Sequence[Any]) -> None:
    for index, item in enumerate(tree):
        if not isinstance(item, Mapping):
            raise ValidationError(f"GitHub tree[{index}] must be an object")


def _selected_entries(
    tree: Sequence[Mapping[str, Any]], root: str, skill_md_path: str
) -> tuple[list[Mapping[str, Any]], list[str]]:
    _validate_tree_entries(tree)
    prefix = "" if root == "." else f"{root}/"
    entries: list[Mapping[str, Any]] = []
    unsupported: list[str] = []
    seen: set[str] = set()
    for item in tree:
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith(prefix):
            continue
        relative = validate_relative_path(path[len(prefix) :], label="repository path")
        if relative in seen:
            raise ValidationError(f"pinned tree contains a duplicate path: {relative}")
        seen.add(relative)
        if item.get("type") == "blob":
            entries.append(item)
        elif item.get("type") == "commit":
            unsupported.append(f"submodule:{relative}")
    if len(entries) > MAX_FILES:
        raise ValidationError(f"skill has {len(entries)} files; limit is {MAX_FILES}")
    if not any(item.get("path") == skill_md_path for item in entries):
        raise ValidationError("SKILL.md disappeared from the selected tree")
    return entries, unsupported


def _fetch_files(
    entries: Sequence[Mapping[str, Any]], root: str, source: str, revision: str, client: HttpClient
) -> tuple[list[dict[str, Any]], list[str]]:
    files: list[dict[str, Any]] = []
    unsupported: list[str] = []
    total = 0
    prefix = "" if root == "." else f"{root}/"
    for item in sorted(entries, key=lambda entry: str(entry.get("path"))):
        relative = str(item["path"])[len(prefix) :]
        size = item.get("size")
        if isinstance(size, int) and size > MAX_FILE_BYTES:
            raise ValidationError(f"support file exceeds {MAX_FILE_BYTES} bytes: {relative}")
        data = _fetch_blob(source, revision, str(item["path"]), client)
        total += len(data)
        if total > MAX_TREE_BYTES:
            raise ValidationError(f"skill tree exceeds {MAX_TREE_BYTES} bytes")
        mode = str(item.get("mode", "100644"))
        if mode == "120000":
            unsupported.append(f"symlink:{relative}")
        files.append(_file_record(relative, data, mode))
    return files, unsupported


def _fetch_blob(source: str, revision: str, path: str, client: HttpClient) -> bytes:
    quoted_path = urllib.parse.quote(path, safe="/")
    return client.get_bytes(f"{GITHUB_RAW}/{source}/{revision}/{quoted_path}", limit=MAX_FILE_BYTES)


def _file_record(path: str, data: bytes, mode: str) -> dict[str, Any]:
    return {
        "path": path,
        "data": data,
        "sha256": sha256_bytes(data),
        "bytes": len(data),
        "mode": mode,
        "executable": mode.endswith("755"),
        "symlink": mode == "120000",
    }


def _skill_evidence(
    files: Sequence[dict[str, Any]], root: str, skill_id: str
) -> tuple[dict[str, Any], str, list[str], list[str], dict[str, Any]]:
    skill_file = next(item for item in files if item["path"] == "SKILL.md")
    frontmatter, body, errors = parse_frontmatter(skill_file["data"])
    name = frontmatter.get("name") if isinstance(frontmatter.get("name"), str) else ""
    expected_name = (
        PurePosixPath(skill_id).name if root == "." else PurePosixPath(root).name
    )
    if name and name != expected_name:
        errors.append(
            f"frontmatter name {name!r} does not match selected skill {expected_name!r}"
        )
    unknown = sorted(set(frontmatter) - STANDARD_FRONTMATTER_KEYS)
    unsupported = sorted(set(unknown) | (set(frontmatter) & UNSUPPORTED_PORTABLE_KEYS))
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    declared = _declared_evidence(frontmatter, metadata, files)
    return frontmatter, body, errors, unsupported, declared


def _declared_evidence(
    frontmatter: Mapping[str, Any], metadata: Mapping[str, Any], files: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    allowed = split_declared(frontmatter.get("allowed-tools"))
    manifests = {
        "requirements.txt", "pyproject.toml", "package.json", "gemfile",
        "go.mod", "cargo.toml",
    }
    return {
        "tools": sorted(set(allowed + split_declared(metadata.get("tools")))),
        "permissions": sorted(set(allowed + split_declared(metadata.get("permissions")))),
        "dependencies": {
            "compatibility": frontmatter.get("compatibility"),
            "declared": split_declared(metadata.get("dependencies"))
            + split_declared(metadata.get("requires")),
            "manifests": sorted(
                item["path"] for item in files if PurePosixPath(item["path"]).name.lower() in manifests
            ),
        },
    }


def _license_report(
    tree: Sequence[Mapping[str, Any]], frontmatter: Mapping[str, Any],
    source: str, revision: str, client: HttpClient,
) -> dict[str, Any]:
    _validate_tree_entries(tree)
    license_paths = [
        str(item.get("path"))
        for item in tree
        if item.get("type") == "blob"
        and "/" not in str(item.get("path"))
        and re.fullmatch(r"(?i)(?:LICENSE|LICENCE|COPYING)(?:\.[A-Za-z0-9._-]+)?", str(item.get("path")))
    ]
    license_file = None
    if license_paths:
        chosen = sorted(license_paths)[0]
        data = client.get_bytes(
            f"{GITHUB_RAW}/{source}/{revision}/{urllib.parse.quote(chosen, safe='/')}",
            limit=1024 * 1024,
        )
        license_file = {"path": chosen, "sha256": sha256_bytes(data), "bytes": len(data)}
    return {
        "declared": frontmatter.get("license"),
        "spdx_id": None,
        "repository_file": license_file,
    }


def _file_records(files: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        item["path"]: {
            "sha256": item["sha256"],
            "bytes": item["bytes"],
            "executable": item["executable"],
            "upstream_mode": item["mode"],
        }
        for item in files
    }


def inspect_candidate(
    source: str, skill_id: str, *, ref: str | None = None,
    skill_path: str | None = None, client: HttpClient | None = None,
) -> dict[str, Any]:
    """Fetch and assess one candidate without writing anything."""
    if not SOURCE_RE.fullmatch(source):
        raise ValidationError(f"source must be owner/repo: {source}")
    validate_relative_path(skill_id, label="skillId")
    client = client or HttpClient()
    revision, tree = _fetch_repository(source, ref, client)
    root, skill_md_path = _choose_skill_root(tree, skill_id, skill_path)
    entries, unsupported_shapes = _selected_entries(tree, root, skill_md_path)
    files, fetched_shapes = _fetch_files(entries, root, source, revision, client)
    unsupported_shapes.extend(fetched_shapes)
    frontmatter, body, errors, unsupported_keys, declared = _skill_evidence(
        files, root, skill_id
    )
    safety = assess_safety(files)
    blockers = [f"frontmatter:{message}" for message in errors]
    blockers.extend(f"unsupported-key:{key}" for key in unsupported_keys)
    blockers.extend(f"unsupported-shape:{shape}" for shape in sorted(set(unsupported_shapes)))
    if any(finding["category"] == "git_lfs_pointer" for finding in safety):
        blockers.append("unsupported-shape:git-lfs-pointer")
    return _inspection_document(
        source, skill_id, revision, root, files, frontmatter, body, errors,
        unsupported_keys, declared, safety, blockers,
        _license_report(tree, frontmatter, source, revision, client),
    )


def _inspection_document(
    source: str, skill_id: str, revision: str, root: str, files: list[dict[str, Any]],
    frontmatter: Mapping[str, Any], body: str, errors: list[str], unsupported_keys: list[str],
    declared: Mapping[str, Any], safety: list[dict[str, str]], blockers: list[str],
    license_report: Mapping[str, Any],
) -> dict[str, Any]:
    records = _file_records(files)
    return {
        "schema_version": 1, "candidate": f"{source}/{skill_id}", "source": source,
        "skill_id": skill_id, "revision": revision, "upstream_path": root,
        "name": frontmatter.get("name") if isinstance(frontmatter.get("name"), str) else "",
        "description": frontmatter.get("description"),
        "skill_markdown": next(item["data"] for item in files if item["path"] == "SKILL.md").decode("utf-8"),
        "frontmatter": frontmatter, "frontmatter_errors": errors,
        "unsupported_keys": unsupported_keys, "dependencies": declared["dependencies"],
        "tools": declared["tools"], "permissions": declared["permissions"],
        "license": license_report, "files": records, "tree_digest": tree_digest(records),
        "safety_findings": safety, "import_blockers": blockers, "importable": not blockers,
        "_file_payloads": files, "_body": body,
    }


def inspection_report(inspection: Mapping[str, Any]) -> dict[str, Any]:
    return json_safe(
        {key: value for key, value in inspection.items() if not key.startswith("_")}
    )
