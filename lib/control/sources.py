"""Read-only GitHub metadata and bounded PR-head fetch adapters."""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from .model import GIT_OBJECT_ID_PATTERN
from .process import bounded_process, checked_bytes


MAX_OUTPUT_BYTES = 64 * 1024
MAX_TITLE_CHARS = 500
MAX_URL_CHARS = 2048
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", re.ASCII)
_PR_FIELDS = "number,title,url,headRefOid,state,isDraft,isCrossRepository"
_ISSUE_FIELDS = "number,title,url,state"


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


class SourceError(ValueError):
    """A GitHub source could not be read or safely imported."""


class GithubAdapter:
    def __init__(
        self, *, executable: str = "gh", runner: Callable[..., Any] | None = None,
    ) -> None:
        self.executable = executable
        self.runner = runner

    @staticmethod
    def _bounded_process(
        argv: list[str], *, cwd: Path | None, limit: int,
    ) -> tuple[int, bytes, bytes]:
        return bounded_process(argv, cwd=cwd, limit=limit, error_type=SourceError)

    def _run_bytes(
        self, executable: str, args: Sequence[str], *, cwd: Path | None = None,
        limit: int = MAX_OUTPUT_BYTES,
    ) -> bytes:
        argv = [executable, *map(str, args)]
        return checked_bytes(
            argv, cwd=cwd, limit=limit, runner=self.runner,
            error_type=SourceError,
        )

    @staticmethod
    def _canonical_directory(path: Path, label: str) -> Path:
        candidate = Path(path)
        if (
            not candidate.is_absolute()
            or os.path.realpath(candidate) != str(candidate)
            or candidate.is_symlink()
            or not candidate.is_dir()
        ):
            raise SourceError(f"{label} must be an exact canonical directory")
        return candidate

    @staticmethod
    def _number(value: Any) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 9_999_999_999
        ):
            raise SourceError("GitHub source number must be a positive integer")
        return value

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= maximum:
            raise SourceError(f"GitHub {label} must contain 1-{maximum} characters")
        if any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in value):
            raise SourceError(f"GitHub {label} must not contain control characters")
        return value

    @classmethod
    def _url(cls, value: Any) -> str:
        url = cls._text(value, "URL", MAX_URL_CHARS)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SourceError("GitHub URL must use http or https with a host")
        return url

    def preflight(self) -> None:
        if shutil.which(self.executable) is None:
            raise SourceError(
                "GitHub source mode requires `gh`, but the gh CLI is not installed or not found on PATH"
            )
        try:
            self._run_bytes(self.executable, ["auth", "status"])
        except SourceError as exc:
            raise SourceError(
                "GitHub source mode requires an authenticated gh CLI; gh is installed but not authenticated"
            ) from exc

    def _metadata(
        self, source: Path, *, kind: str, number: int, fields: str,
    ) -> dict[str, Any]:
        repository = self._canonical_directory(source, "GitHub source repository")
        requested = self._number(number)
        raw = self._run_bytes(
            self.executable,
            [kind, "view", str(requested), "--json", fields],
            cwd=repository,
        )
        try:
            value = json.loads(raw, object_pairs_hook=_strict_object)
        except (
            _DuplicateJsonKey, json.JSONDecodeError, UnicodeDecodeError,
            RecursionError,
        ) as exc:
            raise SourceError("gh returned malformed bounded JSON metadata") from exc
        expected = frozenset(fields.split(","))
        if not isinstance(value, dict) or value.keys() != expected:
            raise SourceError("gh returned an unexpected metadata field set")
        if self._number(value["number"]) != requested:
            raise SourceError("gh returned metadata for a different source number")
        value["title"] = self._text(value["title"], "title", MAX_TITLE_CHARS)
        value["url"] = self._url(value["url"])
        value["state"] = self._text(value["state"], "state", 20)
        return value

    def pr_metadata(self, source: Path, number: int) -> dict[str, Any]:
        value = self._metadata(
            source, kind="pr", number=number, fields=_PR_FIELDS,
        )
        if value["state"] not in {"OPEN", "CLOSED", "MERGED"}:
            raise SourceError("gh returned an invalid pull request state")
        if not isinstance(value["isDraft"], bool) or not isinstance(
            value["isCrossRepository"], bool
        ):
            raise SourceError("gh returned invalid pull request boolean metadata")
        if (
            not isinstance(value["headRefOid"], str)
            or GIT_OBJECT_ID_PATTERN.fullmatch(value["headRefOid"]) is None
        ):
            raise SourceError("gh returned an invalid pull request head object ID")
        return value

    def issue_metadata(self, source: Path, number: int) -> dict[str, Any]:
        value = self._metadata(
            source, kind="issue", number=number, fields=_ISSUE_FIELDS,
        )
        if value["state"] not in {"OPEN", "CLOSED"}:
            raise SourceError("gh returned an invalid issue state")
        return value

    def fetch_pr_head(
        self, git_root: Path, remote: str, number: int,
    ) -> tuple[dict[str, str], ...]:
        repository = self._canonical_directory(git_root, "Git backend root")
        requested = self._number(number)
        if (
            not isinstance(remote, str)
            or _REMOTE.fullmatch(remote) is None
            or ".." in remote
        ):
            raise SourceError("Git remote name uses an invalid restricted grammar")
        source_ref = f"pull/{requested}/head"
        # jj 0.38 only surfaces refs from namespaces it tracks. Verified
        # directly: a commit reachable solely through refs/asha-control/* stays
        # invisible ("jj git import" reports "Nothing changed" and "jj log -r
        # <sha>" reports the revision does not exist), so the explicit-base rule
        # could never be satisfied for a PR head jj does not already know.
        # The remote-tracking namespace is tracked, and importing it creates an
        # untracked REMOTE bookmark only: the operator's local bookmark
        # namespace stays empty of controller entries and no existing bookmark
        # of either kind moves. See
        # Work/code-orchestrate/20260815-asha-control-inc6/03-amendment-decision-2.md
        controller_ref = f"refs/remotes/{remote}/asha-control-pr-{requested}"
        self._run_bytes(
            "git",
            [
                "-C", str(repository), "fetch", remote,
                f"{source_ref}:{controller_ref}",
            ],
        )
        return (
            {
                "kind": "fetched-objects",
                "detail": f"fetched Git objects from {remote} {source_ref}",
                "remote": remote,
                "source_ref": source_ref,
            },
            {
                "kind": "controller-ref",
                "detail": f"updated controller-owned Git ref {controller_ref}",
                "ref": controller_ref,
            },
        )
