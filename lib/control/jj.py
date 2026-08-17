"""Argument-vector-only adapter for the pinned jj 0.38 workspace seam."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence, Any

from .process import bounded_process, capture_bytes, checked_bytes


_COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}", re.ASCII)
_GIT_BRANCH_REF = re.compile(r"refs/heads/[^\s\x00-\x1f\x7f]+", re.ASCII)
_CHANGE_ID = re.compile(r"[k-z]{32}", re.ASCII)
_OPERATION_ID = re.compile(r"[0-9a-f]{128}", re.ASCII)
MAX_OUTPUT_BYTES = 64 * 1024
MAX_TREE_LIST_BYTES = 512 * 1024
MAX_TRACKED_BLOB_BYTES = 16 * 1024 * 1024
MAX_TRACKED_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MATERIALIZATION_ENTRIES = 1024
PRIVATE_CONTEXT_PROBES = (
    ".asha/config.json",
    ".asha/control-task.json",
    "Memory/activeContext.md",
    "Memory/decisions.md",
    "Work/session-state/.asha-control-probe",
)


class JjError(ValueError):
    """A jj precondition, invocation, or identity check failed."""


@dataclass(frozen=True)
class RepositoryFacts:
    root: Path
    git_root: Path


@dataclass(frozen=True)
class WorkspaceIdentity:
    name: str
    change_id: str
    commit_id: str
    parent_commit_ids: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class DiffSummary:
    """Bounded display-only output from an explicit working-copy refresh."""

    summary: str
    refreshed_at: str


def colocated_sync_remediation(
    root: Path, git_head: str | None, working_copy_parent: str,
) -> str | None:
    if git_head is None:
        if working_copy_parent == "0" * 40:
            return None
        head_detail = "git HEAD is unborn"
    elif git_head == working_copy_parent:
        return None
    else:
        head_detail = f"git HEAD {git_head}"
    prefix = f"source working copy is out of sync with jj: {head_detail} "
    middle = f"but jj @- is {working_copy_parent}; run `jj status` in "
    suffix = " to import it, then retry"
    root_text = "".join(
        character if character.isprintable() else "?" for character in str(root)
    )
    root_budget = 500 - len(prefix) - len(middle) - len(suffix)
    if len(root_text) > root_budget:
        root_text = root_text[:root_budget - 3] + "..."
    return prefix + middle + root_text + suffix


class JjAdapter:
    def __init__(self, *, executable: str = "jj", runner: Callable[..., Any] | None = None):
        self.executable = executable
        self.runner = runner

    @staticmethod
    def _bounded_process(argv: list[str], *, cwd: Path | None, limit: int) -> tuple[int, bytes, bytes]:
        return bounded_process(argv, cwd=cwd, limit=limit, error_type=JjError)

    def _run_bytes(
        self, executable: str, args: Sequence[str], *, cwd: Path | None = None,
        limit: int = MAX_OUTPUT_BYTES,
    ) -> bytes:
        argv = [executable, *map(str, args)]
        return checked_bytes(
            argv, cwd=cwd, limit=limit, runner=self.runner, error_type=JjError,
        )

    def _run(self, args: Sequence[str], *, cwd: Path | None = None) -> str:
        try:
            return self._run_bytes(self.executable, args, cwd=cwd).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JjError("jj output was not UTF-8") from exc

    def _run_status(
        self, executable: str, args: Sequence[str], *, cwd: Path | None = None,
        limit: int = MAX_OUTPUT_BYTES,
    ) -> tuple[int, bytes, bytes]:
        return capture_bytes(
            [executable, *map(str, args)], cwd=cwd, limit=limit,
            runner=self.runner, error_type=JjError,
        )

    @staticmethod
    def _one_line(output: str, label: str, pattern: re.Pattern[str] | None = None) -> str:
        lines = output.splitlines()
        if len(lines) != 1 or not lines[0]:
            raise JjError(f"jj returned ambiguous {label}")
        value = lines[0]
        if pattern is not None and pattern.fullmatch(value) is None:
            raise JjError(f"jj returned invalid {label}")
        return value

    def preflight(self, source: Path) -> RepositoryFacts:
        source = Path(source)
        if (not source.is_absolute() or os.path.realpath(source) != str(source) or
                not source.is_dir() or source.is_symlink()):
            raise JjError("source repository must be its exact canonical directory root")
        root = self._one_line(self._run([
            "-R", str(source), "--ignore-working-copy", "root",
        ]), "repository root")
        if root != str(source):
            raise JjError("requested source is not the jj repository root")
        git_raw = self._one_line(self._run([
            "-R", str(source), "--ignore-working-copy", "git", "root",
        ]), "Git backend root")
        git_root = Path(git_raw)
        if not git_root.is_absolute():
            git_root = source / git_root
        git_root = Path(os.path.realpath(git_root))
        if not git_root.is_dir():
            raise JjError("jj repository does not expose a usable Git backend")
        return RepositoryFacts(root=source, git_root=git_root)

    def discover_root(self, start: Path) -> Path:
        """Return the canonical jj repository containing an existing directory."""
        start = Path(start)
        if (not start.is_absolute() or os.path.realpath(start) != str(start) or
                start.is_symlink() or not start.is_dir()):
            raise JjError("repository discovery start must be an exact canonical directory")
        root = self._one_line(self._run([
            "-R", str(start), "--ignore-working-copy", "root",
        ]), "repository root")
        candidate = Path(root)
        if (not candidate.is_absolute() or os.path.realpath(candidate) != str(candidate) or
                candidate.is_symlink() or not candidate.is_dir()):
            raise JjError("jj returned a non-canonical repository root")
        return self.preflight(candidate).root

    def working_copy_parent(self, source: Path) -> str:
        """Read the source working copy's parent without snapshotting it."""
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "log", "-r", "@-",
            "--no-graph", "-T", "commit_id",
        ])
        return self._one_line(output, "working-copy parent commit ID", _GIT_SHA1)

    def git_head(self, git_root: Path) -> str | None:
        """Read Git HEAD, returning ``None`` only for a confirmed unborn branch."""
        repository = Path(git_root)
        returncode, stdout, stderr = self._run_status(
            "git", ["-C", str(repository), "rev-parse", "--verify", "HEAD^{commit}"],
        )
        if returncode == 0:
            try:
                output = stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise JjError("Git HEAD output was not UTF-8") from exc
            try:
                return self._one_line(output, "Git HEAD commit ID", _GIT_SHA1)
            except JjError as exc:
                raise JjError(
                    "Git HEAD was not exactly one full lowercase 40-hex commit ID"
                ) from exc

        # A failed rev-parse is not itself proof of an unborn repository. HEAD
        # must be a valid symbolic branch whose exact ref is absent. Detached,
        # corrupt, inaccessible, and invocation-failure cases all remain errors.
        symbolic_code, symbolic_stdout, _symbolic_stderr = self._run_status(
            "git", ["-C", str(repository), "symbolic-ref", "--quiet", "HEAD"],
        )
        if symbolic_code == 0:
            try:
                symbolic_output = symbolic_stdout.decode("utf-8")
                branch_ref = self._one_line(
                    symbolic_output, "Git HEAD symbolic ref", _GIT_BRANCH_REF,
                )
            except (UnicodeDecodeError, JjError):
                branch_ref = ""
            if branch_ref:
                ref_code, _ref_stdout, _ref_stderr = self._run_status(
                    "git", [
                        "-C", str(repository), "show-ref", "--verify", "--quiet",
                        branch_ref,
                    ],
                )
                if ref_code == 1:
                    return None
        detail = stderr[:4096].decode("utf-8", errors="replace").strip()
        raise JjError(f"Git HEAD could not be resolved: {detail or 'no diagnostic'}")

    def import_git(self, source: Path) -> tuple[dict[str, str], ...]:
        """Import Git refs once without snapshotting the source working copy."""
        self._run([
            "-R", str(source), "--ignore-working-copy", "git", "import",
        ])
        return ({
            "kind": "jj-operation",
            "detail": "recorded a jj operation-log entry for git import",
            "operation": "git import",
        },)

    def resolve_base(self, source: Path, revset: str) -> str:
        if not isinstance(revset, str) or not 1 <= len(revset) <= 500:
            raise JjError("base revset must contain 1-500 characters")
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "log", "-r", revset,
            "--no-graph", "-T", 'commit_id ++ "\\n"',
        ])
        resolved = self._one_line(output, "base commit ID", _COMMIT_ID)
        # jj's default `trunk()` looks for a REMOTE bookmark, so in a repository
        # with no remote it resolves to the all-zero root commit. That commit has
        # no tree, and letting it through fails much later inside `git ls-tree`
        # with an unactionable "fatal: not a tree object". The contract requires
        # refusing an ambiguous or missing base outright, so refuse it here with
        # the remedy attached.
        if set(resolved) == {"0"}:
            raise JjError(
                f"base revset {revset!r} resolved to the empty root commit; "
                "this repository has no usable trunk (jj's default trunk() needs "
                "a remote bookmark). Pass an explicit --base, for example "
                "--base main, or add a remote."
            )
        return resolved

    def require_visible_commit(self, source: Path, commit_id: str) -> None:
        """Confirm an already-resolved full commit ID is visible to this jj repo."""
        if _COMMIT_ID.fullmatch(commit_id) is None:
            raise JjError("visible commit check requires a full commit ID")
        if set(commit_id) == {"0"}:
            raise JjError("visible commit check refuses the empty root commit")
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "log", "-r", commit_id,
            "--no-graph", "-T", 'commit_id ++ "\\n"',
        ])
        observed = self._one_line(output, "visible commit ID", _COMMIT_ID)
        if observed != commit_id:
            raise JjError("resolved commit ID is not visible in the repository")

    def pin_operation(self, source: Path) -> str:
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "operation", "log",
            "--limit", "1", "--no-graph", "--template", 'id ++ "\\n"',
        ])
        return self._one_line(output, "full operation ID", _OPERATION_ID)

    def add_workspace(
        self,
        source: Path,
        destination: Path,
        name: str,
        base_commit_id: str,
        message: str,
        operation_id: str,
    ) -> None:
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise JjError("workspace add requires a full 128-hex operation ID")
        if _COMMIT_ID.fullmatch(base_commit_id) is None:
            raise JjError("workspace add requires a full commit ID")
        self._run([
            "-R", str(source), "--at-operation", operation_id,
            "workspace", "add", "--name", name,
            "--revision", base_commit_id, "--message", message,
            str(destination),
        ])

    def expected_materialization(self, git_root: Path, base_commit_id: str
                                 ) -> dict[str, dict[str, Any]]:
        """Return the bounded Git-backed tree jj must materialize for ``base``."""
        if _COMMIT_ID.fullmatch(base_commit_id) is None:
            raise JjError("tracked-tree inspection requires a full commit ID")
        raw = self._run_bytes(
            "git", ["-C", str(git_root), "ls-tree", "-rz", "--full-tree", "-r", base_commit_id],
            limit=MAX_TREE_LIST_BYTES,
        )
        result: dict[str, dict[str, Any]] = {}
        total = 0
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                mode, kind, oid = header.decode("ascii").split(" ")
                path = raw_path.decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise JjError("Git tree contains an unsupported path or record") from exc
            parts = Path(path).parts
            if (not path or path.startswith("/") or ".." in parts or "//" in path or
                    kind != "blob" or mode not in {"100644", "100755", "120000"}):
                raise JjError("Git tree contains an unsupported entry")
            for index in range(1, len(parts)):
                directory = "/".join(parts[:index])
                existing = result.setdefault(directory, {"type": "directory"})
                if existing != {"type": "directory"}:
                    raise JjError("Git tree path collides with a file")
            content = self._run_bytes(
                "git", ["-C", str(git_root), "cat-file", "blob", oid],
                limit=MAX_TRACKED_BLOB_BYTES,
            )
            total += len(content)
            if total > MAX_TRACKED_TOTAL_BYTES:
                raise JjError("tracked revision exceeds the bounded materialization limit")
            if mode == "120000":
                try:
                    target = content.decode("utf-8")
                except UnicodeError as exc:
                    raise JjError("Git tree contains a non-UTF-8 symlink target") from exc
                fact = {"type": "symlink", "target": target}
            else:
                fact = {
                    "type": "file", "mode": 0o755 if mode == "100755" else 0o644,
                    "sha256": __import__("hashlib").sha256(content).hexdigest(),
                    "size": len(content),
                }
            if path in result:
                raise JjError("Git tree contains colliding paths")
            result[path] = fact
            if len(result) > MAX_MATERIALIZATION_ENTRIES:
                raise JjError(
                    f"tracked revision exceeds {MAX_MATERIALIZATION_ENTRIES} materialized entries"
                )
        return result

    def require_private_context_ignored(self, git_root: Path, destination: Path) -> None:
        """Prove the requested base ignores every controller-private context path."""
        output = self._run_bytes(
            "git", [
                "-C", str(git_root), f"--work-tree={destination}",
                "check-ignore", "--no-index", "--verbose", "--",
                *PRIVATE_CONTEXT_PROBES,
            ],
            limit=MAX_OUTPUT_BYTES,
        )
        try:
            lines = output.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise JjError("Git ignore output was not UTF-8") from exc
        matched: set[str] = set()
        for line in lines:
            _rule, separator, path = line.partition("\t")
            if not separator or path not in PRIVATE_CONTEXT_PROBES or path in matched:
                raise JjError("Git returned ambiguous private-context ignore output")
            matched.add(path)
        missing = set(PRIVATE_CONTEXT_PROBES) - matched
        if missing:
            raise JjError(
                "requested base does not ignore all controller-private context paths"
            )

    def inspect_workspace(
        self, destination: Path, expected_name: str, *, snapshot: bool = False,
        require_empty: bool = True,
    ) -> WorkspaceIdentity:
        prefix = ["-R", str(destination)]
        if not snapshot:
            prefix.append("--ignore-working-copy")
        prefix.extend(["log", "-r", "@", "--no-graph"])
        change_id = self._one_line(
            self._run([*prefix, "-T", 'change_id ++ "\\n"']), "change ID", _CHANGE_ID
        )
        commit_id = self._one_line(
            self._run([*prefix, "-T", 'commit_id ++ "\\n"']), "working commit ID", _COMMIT_ID
        )
        parent_output = self._run([
            *prefix, "-T", 'parents.map(|p| p.commit_id()).join(" ") ++ "\\n"',
        ])
        parent_line = self._one_line(parent_output, "working commit parents")
        parents = tuple(parent_line.split(" "))
        if not parents or any(_COMMIT_ID.fullmatch(item) is None for item in parents):
            raise JjError("jj returned invalid working commit parents")
        description = self._run([*prefix, "-T", "description"]).rstrip("\n")
        if "\n" in description or "\r" in description:
            raise JjError("created workspace description was not one bounded line")
        diff_args = ["-R", str(destination)]
        if not snapshot:
            diff_args.append("--ignore-working-copy")
        diff_args.extend(["diff", "-r", "@", "--summary"])
        if require_empty and self._run(diff_args):
            raise JjError("created workspace working change is not empty")
        registered = self.workspace_identities(destination)
        identity = registered.get(expected_name)
        if identity is None:
            raise JjError("created workspace is not registered under the expected name")
        if identity[:2] != (change_id, commit_id):
            raise JjError("created workspace registration identity disagrees with working copy")
        return WorkspaceIdentity(expected_name, change_id, commit_id, parents, description)

    def diff_summary(self, workspace_path: Path) -> DiffSummary:
        """Snapshot and summarize one exact workspace after an explicit user request."""
        try:
            workspace = Path(workspace_path)
            text = str(workspace)
            valid = (
                not any(unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                        for character in text)
                and workspace.is_absolute()
                and os.path.realpath(workspace) == text
                and not workspace.is_symlink()
                and workspace.is_dir()
            )
        except (OSError, TypeError, ValueError):
            valid = False
        if not valid:
            raise JjError(
                "diff refresh workspace must be its exact canonical directory root"
            )
        output = self._run([
            "-R", str(workspace), "diff", "--summary",
        ])
        safe = "".join(
            character if character in {"\n", "\t"} or character.isprintable() else "?"
            for character in output
        ).rstrip("\n")
        refreshed_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        return DiffSummary(summary=safe or "No changes.", refreshed_at=refreshed_at)

    def workspace_identities(self, repository: Path) -> dict[str, tuple[str, str]]:
        output = self._run([
            "-R", str(repository), "--ignore-working-copy", "workspace", "list",
            "--template", 'name ++ "\\t" ++ target.change_id() ++ "\\t" ++ target.commit_id() ++ "\\n"',
        ])
        result: dict[str, tuple[str, str]] = {}
        for line in output.splitlines():
            fields = line.split("\t")
            if (len(fields) != 3 or not fields[0] or _CHANGE_ID.fullmatch(fields[1]) is None or
                    _COMMIT_ID.fullmatch(fields[2]) is None or fields[0] in result):
                raise JjError("jj returned malformed workspace identity output")
            result[fields[0]] = (fields[1], fields[2])
        return result

    def forget_workspace(self, source: Path, name: str) -> None:
        # Forget through the SOURCE repository, never the destination: the
        # destination is exactly the workspace rollback may be cleaning up,
        # and its .jj may be partial or gone.  jj 0.38 rewrites the colocated
        # Git index file's layout under --ignore-working-copy; staged content
        # is unchanged, and callers compare staged content, not raw bytes.
        operation_id = self.pin_operation(source)
        self._run([
            "-R", str(source), "--ignore-working-copy",
            "--at-operation", operation_id,
            "workspace", "forget", name,
        ])
