#!/usr/bin/env python3
"""Opt-in standalone reuse of Asha's portable subset (issue #126).

`lib/standalone-components.json` classifies components as standalone-safe,
adapter-required or asha-runtime-required. This tool lists that matrix,
exports exportable components from Git objects at an explicit revision (never
from worktree bytes), and validates an export against a trusted checkout.

It installs nothing, changes no client settings, adds no persona and writes no
Memory. Export and static validation execute nothing from the payload; only
`validate --smoke` runs the exported tools, after their bytes are matched to the
commit in a git checkout, with commands taken from this checkout's manifest and
a scrubbed environment in a throwaway directory (not a sandbox).
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Optional


TOOL_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "lib/standalone-components.json"
CLASSES = ("standalone-safe", "adapter-required", "asha-runtime-required")
EXPORTABLE = ("standalone-safe", "adapter-required")
GENERATED = ("PROVENANCE.json", "STANDALONE.md")
PLACEHOLDERS = ("{python}", "{export}", "{workdir}", "{empty_project}")
SMOKE_TIMEOUT = 60
SMOKE_ENV_KEYS = ("HOME", "LANG", "PATH", "TMPDIR")
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
COMMAND_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
HEX40 = re.compile(r"[0-9a-f]{40}")
MODES = {"100644": 0o644, "100755": 0o755}
MAX_RECORD_BYTES = 4 * 1024 * 1024
COMPONENT_KEYS = frozenset((
    "id", "class", "summary", "files", "license", "license_files", "attribution", "dependencies",
    "optional_commands", "network", "entry", "client_behavior", "adapter", "approvals",
    "limitations", "requires_components", "requested"))
PROVENANCE_KEYS = {"contract", "source", "components", "files", "tree_sha256"}
SOURCE_KEYS = {"commit", "revision_requested", "manifest", "manifest_blob"}
FILE_KEYS = {"path", "mode", "git_blob", "size", "sha256"}


class StandaloneError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --- manifest ---------------------------------------------------------------

def _printable(value: Any) -> str:
    """Neutralise terminal control characters in text taken from an export."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in str(value))


def _safe_revision(value: str) -> bool:
    """A requested revision as recorded: one short printable token, no option, no markup."""
    return 0 < len(value) <= 256 and value.isprintable() and not value.startswith("-") \
        and not any(c in value for c in "` ")


def _relpath(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or not value.isprintable():
        raise StandaloneError("invalid_manifest", f"{where}: path must be a printable POSIX string")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise StandaloneError("invalid_manifest", f"{where}: path must stay inside the repository: {value}")
    # A .git component would plant a repository (and its executable config) in the export.
    if any(part.lower() == ".git" for part in parts):
        raise StandaloneError("invalid_manifest", f"{where}: path may not contain a .git component: {value}")
    return str(path)


def _texts(value: Any, where: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise StandaloneError("invalid_manifest", f"{where} must be a list of non-empty strings")
    if required and not value:
        raise StandaloneError("invalid_manifest", f"{where} must not be empty")
    return list(value)


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise StandaloneError("invalid_manifest", f"{where} must be a non-empty string")
    return value


def _validate_smoke(value: Any, where: str) -> None:
    if not isinstance(value, list):
        raise StandaloneError("invalid_manifest", f"{where} must be a list")
    for index, step in enumerate(value):
        argv = step.get("argv") if isinstance(step, dict) and set(step) == {"argv"} else None
        if not isinstance(argv, list) or len(argv) < 2 or argv[0] != "{python}" \
                or any(not isinstance(token, str) for token in argv) or not argv[1].startswith("{export}/"):
            raise StandaloneError("invalid_manifest", f"{where}[{index}] must run an exported file under {{python}}")
        for token in argv[1:]:
            if "{" in token and not any(token == p or token.startswith(p + "/") for p in PLACEHOLDERS):
                raise StandaloneError("invalid_manifest", f"{where}[{index}] has an unknown placeholder: {token}")


def validate_manifest(data: Any) -> dict[str, dict[str, Any]]:
    """Return components by id, or raise StandaloneError. Fails closed."""
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise StandaloneError("invalid_manifest", "standalone manifest schema_version must be 1")
    if not isinstance(data.get("classes"), dict) or set(data["classes"]) != set(CLASSES):
        raise StandaloneError("invalid_manifest", f"classes must define exactly {list(CLASSES)}")
    if not isinstance(data.get("components"), list) or not data["components"]:
        raise StandaloneError("invalid_manifest", "components must be a non-empty list")
    components: dict[str, dict[str, Any]] = {}
    for offset, raw in enumerate(data["components"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not ID_RE.fullmatch(raw["id"]):
            raise StandaloneError("invalid_manifest", f"components[{offset}] needs a valid id")
        cid = raw["id"]
        if cid in components:
            raise StandaloneError("invalid_manifest", f"duplicate component: {cid}")
        if raw.get("class") not in CLASSES:
            raise StandaloneError("invalid_manifest", f"{cid}.class must be one of {list(CLASSES)}")
        _text(raw.get("summary"), f"{cid}.summary")
        if raw["class"] == "asha-runtime-required":
            if "files" in raw or "smoke" in raw:
                raise StandaloneError("invalid_manifest", f"{cid} requires the Asha runtime and lists no export files")
            _text(raw.get("reason"), f"{cid}.reason")
            for path in _texts(raw.get("sources"), f"{cid}.sources", required=True):
                _relpath(path, f"{cid}.sources")
        else:
            for field in ("files", "license_files"):
                for path in _texts(raw.get(field), f"{cid}.{field}", required=True):
                    _relpath(path, f"{cid}.{field}")
            for field in ("license", "entry", "client_behavior"):
                _text(raw.get(field), f"{cid}.{field}")
            for field in ("attribution", "network", "approvals", "limitations", "requires_components"):
                _texts(raw.get(field), f"{cid}.{field}")
            if "optional_commands" in raw:
                _texts(raw["optional_commands"], f"{cid}.optional_commands")
            if raw["class"] == "adapter-required":
                _text(raw.get("adapter"), f"{cid}.adapter")
            deps = raw.get("dependencies")
            if not isinstance(deps, list):
                raise StandaloneError("invalid_manifest", f"{cid}.dependencies must be a list")
            for dep in deps:
                pattern = {"command": COMMAND_RE, "python-module": MODULE_RE}.get(
                    str(dep.get("kind")) if isinstance(dep, dict) else "")
                if pattern is None or set(dep) != {"kind", "name", "needed_for"} \
                        or not isinstance(dep["name"], str) or not pattern.fullmatch(dep["name"]):
                    raise StandaloneError("invalid_manifest", f"{cid} has an invalid dependency: {dep!r}")
                _text(dep["needed_for"], f"{cid}.dependencies.needed_for")
            if "smoke" in raw:
                _validate_smoke(raw["smoke"], f"{cid}.smoke")
        components[cid] = raw
    for cid, raw in components.items():
        for required in raw.get("requires_components", []):
            if components.get(required, {}).get("class") not in EXPORTABLE:
                raise StandaloneError("invalid_manifest", f"{cid} requires a non-exportable or unknown component: {required}")
    return components


def load_local_manifest() -> dict[str, dict[str, Any]]:
    path = TOOL_ROOT / MANIFEST_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StandaloneError("invalid_manifest", f"cannot read {path}: {exc}") from exc
    return validate_manifest(data)


# --- git objects --------------------------------------------------------------

class GitSource:
    """Read-only plumbing over one repository: rev-parse, ls-tree, cat-file."""

    def __init__(self, path: Path):
        self.path = path
        # Inherited GIT_* variables could redirect every command elsewhere.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        # GIT_NO_LAZY_FETCH stops a promisor fetch where git honours it; partial
        # clones are also refused outright below.
        # Replace refs could substitute an object's bytes behind its recorded id.
        self.env.update({"GIT_LITERAL_PATHSPECS": "1", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
                         "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"})
        try:
            self.run("rev-parse", "--git-dir")
        except StandaloneError as exc:
            raise StandaloneError("not_a_git_repository",
                                  f"{path} is not a git repository; pass --source with a git checkout") from exc
        # A bare boolean key ("remote.x.promisor" with no value) means true.
        promisors = [(line.split() + ["true"])[1].lower() for line in
                     self.config("--get-regexp", r"^remote\..*\.promisor$").splitlines() if line.split()]
        if self.config("--get", "extensions.partialClone") or \
                any(value in ("true", "yes", "on", "1") for value in promisors):
            raise StandaloneError("partial_clone",
                                  f"{path} is a partial clone: reading a missing object would fetch it "
                                  "from the network; use a full clone")

    def config(self, *args: str) -> str:
        """`git config` output, or "" when the key is unset."""
        try:
            return self.run("config", *args).decode("utf-8", "replace").strip()
        except StandaloneError:
            return ""

    def run(self, *args: str) -> bytes:
        try:
            done = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True,
                                  env=self.env, stdin=subprocess.DEVNULL, check=False)
        except OSError as exc:
            raise StandaloneError("git_unavailable", f"cannot run git: {exc}") from exc
        if done.returncode != 0:
            raise StandaloneError("git_failed", done.stderr.decode("utf-8", "replace").strip() or "git failed")
        return done.stdout

    def resolve(self, revision: str) -> str:
        if not _safe_revision(revision):
            raise StandaloneError("invalid_revision", f"refusing revision {_printable(revision)!r}")
        try:
            commit = self.run("rev-parse", "--verify", "--quiet", "--end-of-options",
                              f"{revision}^{{commit}}").decode().strip()
        except StandaloneError as exc:
            raise StandaloneError("unknown_revision", f"cannot resolve {revision!r} to a commit") from exc
        if not HEX40.fullmatch(commit):
            raise StandaloneError("unknown_revision", f"cannot resolve {revision!r} to a full commit id")
        return commit

    def entries(self, commit: str, paths: list[str]) -> dict[str, tuple[str, str, str]]:
        """path -> (mode, type, object) for exactly the given paths at commit."""
        raw = self.run("ls-tree", "-z", "--full-tree", commit, "--", *paths)
        found: dict[str, tuple[str, str, str]] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            mode, kind, obj = meta.decode().split()
            found[name.decode("utf-8", "surrogateescape")] = (mode, kind, obj)
        return found

    def is_ancestor(self, commit: str, descendant: str) -> bool:
        try:
            self.run("merge-base", "--is-ancestor", commit, descendant)
        except StandaloneError:
            return False
        return True

    def blob(self, obj: str) -> bytes:
        return self.run("cat-file", "blob", obj)


# --- export -------------------------------------------------------------------

def _closure(components: dict[str, dict[str, Any]], requested: list[str]) -> list[tuple[str, bool]]:
    order: list[tuple[str, bool]] = []
    seen: set[str] = set()
    pending = [(cid, True) for cid in requested]
    while pending:
        cid, asked = pending.pop(0)
        if cid in seen:
            continue
        if cid not in components:
            raise StandaloneError("unknown_component", f"unknown component: {cid}")
        if components[cid]["class"] not in EXPORTABLE:
            raise StandaloneError("requires_asha_runtime",
                                  f"{cid} requires the Asha runtime and is not exportable: {components[cid]['reason']}")
        seen.add(cid)
        order.append((cid, asked))
        pending.extend((needed, False) for needed in components[cid]["requires_components"])
    return order


def _tree_digest(files: list[dict[str, Any]]) -> str:
    rows = sorted(files, key=lambda r: str(r.get("path")))
    lines = "".join(f"{row.get('mode')} {row.get('sha256')} {row.get('path')}\n" for row in rows)
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _component_record(component: dict[str, Any], requested: bool) -> dict[str, Any]:
    record = {key: component[key] for key in sorted(COMPONENT_KEYS - {"requested"}) if key in component}
    record["requested"] = requested
    return record


def _notice(provenance: dict[str, Any]) -> str:
    lines = [
        "# Standalone export", "",
        f"Exported from commit `{provenance['source']['commit']}` by `asha standalone export`.",
        "File bytes come from Git objects at that commit; `PROVENANCE.json` lists each file's",
        "path, mode, Git blob id and SHA-256. Relative paths are the repository's own.", "",
        "This export installs nothing, changes no client settings, adds no persona and",
        "initialises no memory. Approval gates below are instructions to the agent or",
        "user; no client is assumed to enforce them.", "",
        "Validate it before relying on anything here, including this notice. PROVENANCE.json",
        "only vouches for itself; `--source` binds every file, record and this notice to the",
        "commit, which must be reachable from the ref you trust:", "",
        "```bash",
        "asha standalone validate THIS_DIRECTORY --source ASHA_CHECKOUT --trusted-ref BRANCH",
        "asha standalone validate THIS_DIRECTORY --source ASHA_CHECKOUT --trusted-ref BRANCH --smoke",
        "```", "",
        "The first runs nothing exported; the second also runs each tool once.", "",
        "Run --smoke only if you trust the exported Python. It isolates the working",
        "directory, HOME and environment only; that is not operating-system containment,",
        "and the code runs with your user's file-system and network permissions.", "",
    ]
    for component in provenance["components"]:
        lines += [f"## {component['id']} ({component['class']})", "", component["summary"], "",
                  f"- Entry: {component['entry']}", "- Files: " + ", ".join(f"`{f}`" for f in component["files"])]
        if component.get("adapter"):
            lines.append(f"- Adapter required: {component['adapter']}")
        lines.append(f"- Client behaviour: {component['client_behavior']}")
        for dep in component["dependencies"]:
            lines.append(f"- Requires {dep['kind']} `{dep['name']}`: {dep['needed_for']}")
        if component.get("optional_commands"):
            lines.append("- Uses when present: " + ", ".join(f"`{c}`" for c in component["optional_commands"]))
        for host in component["network"]:
            lines.append(f"- Network: {host}")
        for gate in component["approvals"]:
            lines.append(f"- Approval: {gate}")
        for limit in component["limitations"]:
            lines.append(f"- Limitation: {limit}")
        lines.append(f"- Licence: {component['license']} ("
                     + ", ".join(f"`{p}`" for p in component["license_files"]) + ")")
        for note in component["attribution"]:
            lines.append(f"- Attribution: {note}")
        lines.append("")
    return "\n".join(lines)


def export(requested: list[str], revision: str, out: Path, source: Path) -> dict[str, Any]:
    if not requested:
        raise StandaloneError("nothing_requested", "name at least one component to export")
    git = GitSource(source)
    commit = git.resolve(revision)
    manifest_entry = git.entries(commit, [MANIFEST_PATH]).get(MANIFEST_PATH)
    if manifest_entry is None or manifest_entry[1] != "blob":
        raise StandaloneError("missing_manifest", f"{commit} has no {MANIFEST_PATH}; export a later revision")
    try:
        components = validate_manifest(json.loads(git.blob(manifest_entry[2])))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StandaloneError("invalid_manifest", f"{MANIFEST_PATH} at {commit} is not JSON: {exc}") from exc
    selected = _closure(components, requested)
    paths = sorted({path for cid, _ in selected
                    for path in components[cid]["files"] + components[cid]["license_files"]})
    found = git.entries(commit, paths)
    payload: list[tuple[str, str, str, bytes]] = []
    for path in paths:
        if path not in found and any(name.startswith(path + "/") for name in found):
            raise StandaloneError("unsupported_entry", f"{path} at {commit} is a directory; list its files")
        if path not in found:
            raise StandaloneError("missing_at_revision", f"{path} does not exist at {commit}")
        mode, kind, obj = found[path]
        if kind != "blob" or mode not in MODES:
            raise StandaloneError("unsupported_entry", f"{path} at {commit} is {kind} mode {mode}; only regular files export")
        payload.append((path, mode, obj, git.blob(obj)))

    # Every check passed: only now touch the output directory.
    created = not out.exists()
    if not created and (not out.is_dir() or any(out.iterdir())):
        raise StandaloneError("output_not_empty", f"{out} exists and is not an empty directory")
    if not out.parent.is_dir():
        raise StandaloneError("output_parent_missing", f"{out.parent} does not exist; create it first")
    try:
        out.mkdir(exist_ok=True)
        files = []
        for path, mode, obj, data in payload:
            target = out.joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(MODES[mode])
            files.append({"path": path, "mode": mode, "git_blob": obj, "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        provenance = {
            "contract": "asha.standalone-export.v1",
            "source": {"commit": commit, "revision_requested": revision, "manifest": MANIFEST_PATH,
                       "manifest_blob": manifest_entry[2]},
            "components": [_component_record(components[cid], asked) for cid, asked in selected],
            "files": files,
            "tree_sha256": _tree_digest(files),
        }
        (out / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out / "STANDALONE.md").write_text(_notice(provenance), encoding="utf-8")
    except OSError as exc:
        # The directory was absent or empty before; leave it that way.
        if created:
            shutil.rmtree(out, ignore_errors=True)
        else:
            for child in out.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        raise StandaloneError("write_failed", f"cannot write export: {exc}") from exc
    return provenance


# --- validate -----------------------------------------------------------------

def _check_provenance(provenance: Any, root: Path) -> dict[str, Any]:
    """Fail closed with a clean error on any malformed or unexpected record."""
    def bad(why: str) -> StandaloneError:
        return StandaloneError("not_an_export", f"{root}/PROVENANCE.json is not a valid export record: {why}")
    if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_KEYS \
            or provenance.get("contract") != "asha.standalone-export.v1" \
            or not isinstance(provenance.get("tree_sha256"), str):
        raise bad("wrong contract or unexpected top-level keys")
    source = provenance["source"]
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS \
            or not all(isinstance(source[k], str) for k in SOURCE_KEYS):
        raise bad("source must hold exactly commit, revision_requested, manifest and manifest_blob as text")
    if not HEX40.fullmatch(source["commit"]) or not _safe_revision(source["revision_requested"]):
        raise bad("source commit or requested revision is malformed")
    files = provenance["files"]
    if not isinstance(files, list) or not all(
            isinstance(row, dict) and set(row) == FILE_KEYS
            and all(isinstance(row[k], str) for k in ("path", "mode", "git_blob", "sha256"))
            and isinstance(row["size"], int) and not isinstance(row["size"], bool) and row["size"] >= 0
            for row in files):
        raise bad("files must be objects with exactly path, mode, git_blob, size and sha256")
    components = provenance["components"]
    if not isinstance(components, list) or not all(
            isinstance(c, dict) and set(c) <= COMPONENT_KEYS and isinstance(c.get("id"), str)
            and isinstance(c.get("requested"), bool) for c in components):
        raise bad("components must be export records with an id and a requested flag")
    # `export` always writes at least one requested component with files, and
    # lists exactly its components' files and licence files: a hollow or
    # unowned record would otherwise pass while testing nothing.
    if not files or not components or not any(c["requested"] for c in components):
        raise bad("an export carries at least one requested component and its files")
    owned: set[str] = set()
    for component in components:
        for key in ("files", "license_files"):
            value = component.get(key)
            if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
                raise bad(f"every component lists its {key}")
            owned.update(value)
    if owned != {row["path"] for row in files}:
        raise bad("the listed files must be exactly the components' files and licence files")
    for component in components:
        for field in ("dependencies", "network"):
            if not isinstance(component.get(field, []), list):
                raise bad(f"{field} must be a list")
        if not all(isinstance(host, str) for host in component.get("network", [])):
            raise bad("network entries must be text")
        if not all(isinstance(dep, dict) and set(dep) <= {"kind", "name", "needed_for"}
                   and isinstance(dep.get("kind"), str) and isinstance(dep.get("name"), str)
                   for dep in component.get("dependencies", [])):
            raise bad("dependencies must be objects with text kind and name")
    return provenance


def _read_generated(root: Path, name: str) -> tuple[Optional[bytes], Optional[str]]:
    """Read PROVENANCE.json or STANDALONE.md without following a link,
    blocking on a FIFO, or reading without bound. Returns (data, problem)."""
    path = root / name
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if stat.S_ISLNK(info.st_mode):
        return None, "symlink"
    if not stat.S_ISREG(info.st_mode):
        return None, "not-a-regular-file"
    if info.st_size > MAX_RECORD_BYTES:
        return None, "oversized"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return None, "unreadable"
    if len(data) > MAX_RECORD_BYTES:
        return None, "oversized"
    return data, None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json object hook: a duplicate key could hide text a reader of the raw file sees."""
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key in JSON object")
    return dict(pairs)


def _hash_file(path: Path, limit: int) -> Optional[str]:
    """SHA-256 of a regular file read in chunks, or None past limit bytes."""
    digest, total = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        while chunk := handle.read(1 << 20):
            total += len(chunk)
            if total > limit:
                return None
            digest.update(chunk)
    return digest.hexdigest()


def _symlinked(root: Path, parts: tuple[str, ...]) -> bool:
    """True when the file or any directory above it (inside root) is a link."""
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _integrity(root: Path, provenance: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    listed: set[str] = set()
    for row in provenance["files"]:
        try:
            path = _relpath(row["path"], "PROVENANCE.json")
        except StandaloneError:
            problems.append({"kind": "invalid-entry", "path": row["path"]})
            continue
        if path in listed:
            problems.append({"kind": "duplicate-entry", "path": path})
            continue
        listed.add(path)
        parts = PurePosixPath(path).parts
        target = root.joinpath(*parts)
        kind: Optional[str] = None
        # Never follow a link: reading through one would hash a file outside the export.
        try:
            if _symlinked(root, parts):
                kind = "symlink"
            else:
                info = os.lstat(target)
                if not stat.S_ISREG(info.st_mode):
                    kind = "not-a-regular-file"
                elif info.st_size != row["size"] or _hash_file(target, row["size"]) != row["sha256"]:
                    kind = "modified"
                elif bool(info.st_mode & 0o100) != (row["mode"] == "100755"):
                    kind = "mode"
        except FileNotFoundError:
            kind = "missing"
        except OSError:
            kind = "unreadable"
        if kind:
            problems.append({"kind": kind, "path": path})
    # Anything else in the export is unexpected: extra files, links, and
    # directories that hold no listed file. Unlistable ones are reported too.
    expected_dirs = {PurePosixPath(*PurePosixPath(path).parts[:depth]).as_posix()
                     for path in listed for depth in range(1, len(PurePosixPath(path).parts))}

    def relative(path: str) -> str:
        return PurePosixPath(os.path.relpath(path, root)).as_posix()

    def unlistable(error: OSError) -> None:
        problems.append({"kind": "unreadable", "path": relative(str(error.filename or root))})

    for directory, dirs, names in os.walk(root, onerror=unlistable, followlinks=False):
        for name in dirs + names:
            path = relative(os.path.join(directory, name))
            try:
                link = os.path.islink(os.path.join(directory, name))
            except OSError:
                problems.append({"kind": "unreadable", "path": path})
                continue
            # os.walk sorts entries by following links; a link is never "known",
            # so the report cannot reveal what its target is.
            known = not link and (path in listed if name in names else path in expected_dirs)
            if not known and path not in GENERATED:
                problems.append({"kind": "unexpected", "path": path})
    _, notice_problem = _read_generated(root, "STANDALONE.md")
    if notice_problem:
        problems.append({"kind": notice_problem, "path": "STANDALONE.md"})
    if not problems and _tree_digest(provenance["files"]) != provenance["tree_sha256"]:
        problems.append({"kind": "tree-digest", "path": "PROVENANCE.json"})
    return sorted(problems, key=lambda p: (p["path"], p["kind"]))


def _against_source(root: Path, provenance: dict[str, Any], git: GitSource,
                    trusted: str) -> tuple[list[dict[str, str]], dict[str, tuple[str, bytes]]]:
    """Bind the export to a commit the validating user trusts.

    PROVENANCE.json vouches only for itself. The recorded commit must be an
    ancestor of the trusted ref; every file's mode, blob id, size and hash must
    match that commit; the component records must equal what `export` would
    write for the requested ids (closure, order, flags); and STANDALONE.md must
    re-render byte for byte. Returns problems and the verified blobs.
    """
    commit = provenance["source"]["commit"]
    unverified = [{"kind": "source-unverified", "path": "PROVENANCE.json"}]
    try:
        git.run("cat-file", "-e", f"{commit}^{{commit}}")
    except StandaloneError:
        return unverified, {}
    if not git.is_ancestor(commit, trusted):
        return [{"kind": "source-untrusted", "path": "PROVENANCE.json"}], {}
    mismatch = {"kind": "source-mismatch", "path": "PROVENANCE.json"}
    rows = provenance["files"]
    found = git.entries(commit, [row["path"] for row in rows] + [MANIFEST_PATH])
    problems: list[dict[str, str]] = []
    blobs: dict[str, tuple[str, bytes]] = {}
    for row in rows:
        entry = found.get(row["path"])
        if entry is None or entry[1] != "blob" or entry[0] not in MODES \
                or entry[0] != row["mode"] or entry[2] != row["git_blob"]:
            problems.append({"kind": "source-mismatch", "path": row["path"]})
            continue
        data = git.blob(entry[2])
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            problems.append({"kind": "source-mismatch", "path": row["path"]})
            continue
        blobs[row["path"]] = (entry[0], data)
    manifest_entry = found.get(MANIFEST_PATH)
    expected: Optional[list[dict[str, Any]]] = None
    try:
        if manifest_entry is None or provenance["source"]["manifest"] != MANIFEST_PATH \
                or provenance["source"]["manifest_blob"] != manifest_entry[2]:
            raise StandaloneError("missing_manifest", MANIFEST_PATH)
        components = validate_manifest(json.loads(git.blob(manifest_entry[2])))
        requested = [c["id"] for c in provenance["components"] if c["requested"]]
        expected = [_component_record(components[cid], asked) for cid, asked in _closure(components, requested)]
    except (StandaloneError, UnicodeError, json.JSONDecodeError, KeyError):
        expected = None
    owned = sorted({path for record in expected or []
                    for path in record["files"] + record["license_files"]})
    if expected is None or expected != provenance["components"] or owned != [row["path"] for row in rows]:
        problems.append(mismatch)
    if expected is not None:
        notice, _ = _read_generated(root, "STANDALONE.md")
        # Rendered from the source's records, so a rewritten notice cannot
        # hide behind a matching rewrite of PROVENANCE.json.
        if notice != _notice({**provenance, "components": expected}).encode("utf-8"):
            problems.append({"kind": "source-mismatch", "path": "STANDALONE.md"})
    else:
        problems.append({"kind": "source-mismatch", "path": "STANDALONE.md"})
    return sorted(problems, key=lambda p: (p["path"], p["kind"])), blobs


def _module_present(python: Optional[str], name: str) -> bool:
    if python is None:
        return False
    probe = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 3)"
    try:
        done = subprocess.run([python, "-I", "-c", probe, name], capture_output=True,
                              stdin=subprocess.DEVNULL, check=False, timeout=SMOKE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _smoke(steps: list[dict[str, Any]], root: Path, listed: set[str], python: Optional[str],
           workdir: Path, env: dict[str, str]) -> dict[str, Any]:
    if python is None:
        return {"state": "skipped", "reason": "python3 not found on PATH"}
    values = {"{export}": str(root), "{workdir}": str(workdir),
              "{empty_project}": str(workdir / "empty-project")}
    runs = []
    for step in steps:
        # Isolated (-I) and bytecode-free (-B).
        argv = [python, "-I", "-B"]
        for token in step["argv"][1:]:
            for placeholder, value in values.items():
                if token == placeholder or token.startswith(placeholder + "/"):
                    if placeholder == "{export}" and token[len(placeholder) + 1:] not in listed:
                        return {"state": "skipped", "reason": f"{token} is not part of this export"}
                    token = value + token[len(placeholder):]
                    break
            argv.append(token)
        # Not containment: only cwd, HOME and the environment are isolated. The
        # process keeps the user's file-system and network permissions, so the
        # caller must trust the (source-verified) exported Python it runs.
        try:
            done = subprocess.run(argv, cwd=workdir, env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=SMOKE_TIMEOUT, check=False)
            code, tail = done.returncode, (done.stdout + done.stderr)[-800:]
        except subprocess.TimeoutExpired:
            code, tail = None, f"timed out after {SMOKE_TIMEOUT}s"
        runs.append({"argv": step["argv"], "returncode": code, "passed": code == 0, "output_tail": tail})
    return {"state": "passed" if all(r["passed"] for r in runs) else "failed", "runs": runs}


def validate(root: Path, smoke: bool, source: Optional[Path] = None,
             trusted_ref: Optional[str] = None) -> dict[str, Any]:
    data, problem = _read_generated(root, "PROVENANCE.json")
    if problem:
        raise StandaloneError("not_an_export", f"{root}/PROVENANCE.json is {problem}")
    try:
        raw = json.loads((data or b"").decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError, RecursionError) as exc:  # ValueError covers JSON and huge integers
        raise StandaloneError("not_an_export", f"{root}/PROVENANCE.json is not valid JSON: {exc}") from exc
    provenance = _check_provenance(raw, root)
    problems = _integrity(root, provenance)
    # Smoke runs code, so it needs the bytes bound to a commit that a ref the
    # user trusts reaches (--source and --trusted-ref, else this checkout's
    # HEAD), not merely to the export's own self-consistent record.
    source_check, unverifiable, trusted, payload = "not-requested", None, None, {}
    if source is not None or trusted_ref is not None or smoke:
        try:
            git = GitSource(source or TOOL_ROOT)
            if source is not None and trusted_ref is None:
                # HEAD of another checkout may be a branch under review: trust is named.
                raise StandaloneError("trusted_ref_required",
                                      "--source needs --trusted-ref naming the branch, tag or commit you trust")
            trusted = {"ref": trusted_ref or "HEAD", "commit": git.resolve(trusted_ref or "HEAD")}
        except StandaloneError as exc:
            if source is not None or trusted_ref is not None:
                raise
            source_check, unverifiable = "unavailable", f"cannot verify against a git checkout ({exc.code}); pass --source"
        else:
            if problems:
                source_check = "skipped"
            else:
                source_problems, payload = _against_source(root, provenance, git, trusted["commit"])
                problems = sorted(problems + source_problems, key=lambda p: (p["path"], p["kind"]))
                source_check = "failed" if source_problems else "verified"
    python = shutil.which("python3")
    listed = {row["path"] for row in provenance["files"]}
    local = load_local_manifest() if smoke else {}
    run_smoke = smoke and not problems and source_check == "verified"
    workdir: Optional[Path] = None
    env: dict[str, str] = {}
    if run_smoke:
        workdir = Path(tempfile.mkdtemp(prefix="asha-standalone-"))
        for name in ("home", "tmp", "empty-project", "payload"):
            (workdir / name).mkdir()
        # Smoke runs the verified Git bytes from this private directory, never
        # the export's files, which could change after they were checked.
        for path, (mode, blob) in payload.items():
            target = (workdir / "payload").joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            target.chmod(MODES[mode])
        env = {"HOME": str(workdir / "home"), "LANG": "C.UTF-8",
               "PATH": os.environ.get("PATH", os.defpath), "TMPDIR": str(workdir / "tmp")}
    components, missing = [], []
    isolation = None
    try:
        for record in provenance["components"]:
            cid = record["id"]
            deps = []
            for dep in record.get("dependencies", []):
                kind, name = dep["kind"], dep["name"]
                if kind == "command" and COMMAND_RE.fullmatch(name):
                    present = shutil.which(name) is not None
                elif kind == "python-module" and MODULE_RE.fullmatch(name):
                    present = _module_present(python, name)
                else:
                    present = False
                deps.append({"kind": kind, "name": name, "needed_for": dep.get("needed_for"),
                             "state": "present" if present else "missing"})
                if not present:
                    missing.append({"component": cid, "kind": kind, "name": name})
            if not smoke:
                outcome: dict[str, Any] = {"state": "not-requested"}
            elif problems:
                outcome = {"state": "skipped", "reason": "integrity check failed"}
            elif unverifiable:
                outcome = {"state": "skipped", "reason": unverifiable}
            elif not local.get(cid, {}).get("smoke"):
                outcome = {"state": "not-defined"}
            else:
                assert workdir is not None
                outcome = _smoke(local[cid]["smoke"], workdir / "payload", listed, python, workdir, env)
            components.append({"id": cid, "class": record.get("class"), "dependencies": deps,
                               "network": [{"host": host, "state": "declared-not-probed"}
                                           for host in record.get("network", [])],
                               "smoke": outcome})
        if workdir is not None:
            isolation = {"workdir": str(workdir), "env_keys": sorted(env),
                         "home_untouched": not any((workdir / "home").iterdir()),
                         "payload": "source-objects", "sandboxed": False}
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
    smoke_failed = any(c["smoke"]["state"] in ("failed", "skipped") for c in components)
    status = "failed" if problems or smoke_failed or (isolation and not isolation["home_untouched"]) \
        else "missing-dependencies" if missing else "ok"
    return {
        "contract": "asha.standalone-validation.v1",
        "export": str(root),
        "commit": provenance["source"]["commit"],
        "status": status,
        "integrity": {"state": "failed" if problems else "ok", "problems": problems},
        "source_check": source_check,
        "trusted_ref": trusted,
        "python": python,
        "missing": missing,
        "components": components,
        "smoke_isolation": isolation,
    }


# --- CLI ----------------------------------------------------------------------

def _human_list(components: dict[str, dict[str, Any]]) -> str:
    lines = []
    for cls in CLASSES:
        lines.append(f"{cls}:")
        for component in components.values():
            if component["class"] == cls:
                lines.append(f"  {component['id']}: {component['summary']}")
    lines.append("Export: asha standalone export COMPONENT... --revision REV --out DIR [--source GIT_CHECKOUT]")
    return "\n".join(lines)


def _human_validation(result: dict[str, Any]) -> str:
    # Everything below can come from an untrusted export: print it inert.
    safe = _printable
    lines = [f"Validation of {safe(result['export'])} at {safe(result['commit'])}: {result['status']}",
             f"Integrity: {result['integrity']['state']} (source check: {result['source_check']})"]
    if result["trusted_ref"]:
        lines.append(f"Trusted ref: {safe(result['trusted_ref']['ref'])} at {result['trusted_ref']['commit']}")
    lines += [f"  {p['kind']}: {safe(p['path'])}" for p in result["integrity"]["problems"]]
    for component in result["components"]:
        smoke = component["smoke"]
        reason = f" ({safe(smoke['reason'])})" if "reason" in smoke else ""
        lines.append(f"- {safe(component['id'])} ({safe(component['class'])}): smoke {smoke['state']}{reason}")
        lines += [f"    {safe(d['kind'])} {safe(d['name'])}: {d['state']}" for d in component["dependencies"]]
    if result["smoke_isolation"]:
        lines.append("Smoke ran in a throwaway directory with a scrubbed environment; it is not a sandbox.")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asha standalone",
                                     description="List, export and validate Asha's opt-in portable subset.")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="print the component matrix")
    listing.add_argument("--json", action="store_true")
    exporter = sub.add_parser("export", help="export components from git objects at an explicit revision")
    exporter.add_argument("components", nargs="+")
    exporter.add_argument("--revision", required=True, help="commit-ish to export (resolved to a full commit id)")
    exporter.add_argument("--out", required=True, type=Path, help="new or empty output directory")
    exporter.add_argument("--source", type=Path, default=TOOL_ROOT, help="git checkout to read (default: this one)")
    exporter.add_argument("--json", action="store_true")
    checker = sub.add_parser("validate", help="check an export's integrity, dependencies and, with --smoke, its tools")
    checker.add_argument("export", type=Path)
    checker.add_argument("--smoke", action="store_true",
                         help="also run each tool's smoke checks, only if you trust the exported Python: "
                              "not operating-system containment; it runs with your user's file-system and "
                              "network permissions. Needs a verified source check.")
    checker.add_argument("--source", type=Path,
                         help="git checkout holding the export's commit; required in effect for --smoke "
                              "(default for --smoke: this checkout)")
    checker.add_argument("--trusted-ref",
                         help="ref or commit in the source whose history must contain the export's commit "
                              "(required with --source; HEAD of this checkout otherwise)")
    checker.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            components = load_local_manifest()
            if args.json:
                data = json.loads((TOOL_ROOT / MANIFEST_PATH).read_text(encoding="utf-8"))
                print(json.dumps({"contract": "asha.standalone-components.v1", "manifest": MANIFEST_PATH,
                                  "classes": data["classes"], "components": list(components.values())},
                                 indent=2, sort_keys=True))
            else:
                print(_human_list(components))
            return 0
        if args.command == "export":
            provenance = export(args.components, args.revision, args.out.absolute(), args.source)
            if args.json:
                print(json.dumps(provenance, indent=2, sort_keys=True))
            else:
                print(f"exported {', '.join(c['id'] for c in provenance['components'])} "
                      f"at {provenance['source']['commit']} to {args.out}")
            return 0
        result = validate(args.export.absolute(), args.smoke, args.source, args.trusted_ref)
        print(json.dumps(result, indent=2, sort_keys=True) if args.json else _human_validation(result))
        return 0 if result["status"] == "ok" else 1
    except StandaloneError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"contract": "asha.standalone-error.v1",
                              "error": {"code": exc.code, "message": str(exc)}}, indent=2, sort_keys=True))
        else:
            print(f"asha standalone: {exc.code}: {_printable(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
