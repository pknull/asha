"""Read-only GitHub metadata and bounded PR-head fetch adapters."""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
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
_SCP_REMOTE = re.compile(r"(?:[^/@:\s]+@)?([^/:\s]+):(.+)", re.ASCII)
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


@dataclass(frozen=True)
class ValidatedPrRemote:
    """One preflighted PR fetch URL bound to safe local configuration."""

    name: str
    url: str
    transport: str
    config_digest: str


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

    @staticmethod
    def _repository_identity(url: str) -> tuple[str, str] | None:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https", "ssh", "git"}:
            host = parsed.hostname
            path = parsed.path
        else:
            matched = _SCP_REMOTE.fullmatch(url)
            if matched is None:
                return None
            host, path = matched.groups()
        normalized = path.strip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        if not host or not normalized:
            return None
        return host.lower(), normalized.lower()

    @classmethod
    def _validated_fetch_url(
        cls, url: str, expected: tuple[str, str],
    ) -> tuple[str, str] | None:
        if (
            not isinstance(url, str) or not 1 <= len(url) <= MAX_URL_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
        ):
            return None
        parsed = urlsplit(url)
        if parsed.scheme:
            if parsed.scheme not in {"https", "ssh"}:
                return None
            if (
                not parsed.hostname or parsed.password is not None
                or parsed.query or parsed.fragment
                or (parsed.scheme == "https" and parsed.username is not None)
                or (parsed.scheme == "ssh" and parsed.username not in {None, "git"})
            ):
                return None
            transport = parsed.scheme
        else:
            matched = _SCP_REMOTE.fullmatch(url)
            if matched is None or not url.startswith("git@"):
                return None
            transport = "ssh"
        if cls._repository_identity(url) != expected:
            return None
        return url, transport

    def pr_remote(
        self, git_root: Path, url: str, number: int, *, git=None,
    ) -> ValidatedPrRemote:
        """Select one matching HTTPS/SSH fetch URL without executing config."""
        repository = self._canonical_directory(git_root, "Git backend root")
        requested = self._number(number)
        if git is None:
            from .jj import JjAdapter
            git = JjAdapter(runner=self.runner)
        try:
            configured = git.git_remote_configuration(repository)
        except ValueError as exc:
            raise SourceError(str(exc)) from exc
        parsed = urlsplit(url)
        suffix = f"/pull/{requested}"
        if not parsed.path.endswith(suffix):
            raise SourceError("GitHub pull request URL does not match its source number")
        target = self._repository_identity(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path[:-len(suffix)]}"
        )
        if target is None:
            raise SourceError("GitHub pull request URL has no usable repository identity")
        matches: list[ValidatedPrRemote] = []
        for remote, urls in configured.remotes:
            for candidate in urls:
                validated = self._validated_fetch_url(candidate, target)
                if validated is not None:
                    selected_url, transport = validated
                    matches.append(ValidatedPrRemote(
                        name=remote, url=selected_url, transport=transport,
                        config_digest=configured.config_digest,
                    ))
                    break
        if not matches:
            raise SourceError(
                "could not identify a configured remote with a safe HTTPS/SSH URL "
                "matching the pull request repository"
            )
        return next(
            (candidate for candidate in matches if candidate.name == "origin"),
            sorted(matches, key=lambda candidate: candidate.name)[0],
        )

    def fetch_pr_head(
        self, git_root: Path, remote: ValidatedPrRemote, number: int, *, git=None,
    ) -> tuple[dict[str, str], ...]:
        repository = self._canonical_directory(git_root, "Git backend root")
        requested = self._number(number)
        if (
            not isinstance(remote, ValidatedPrRemote)
            or _REMOTE.fullmatch(remote.name) is None or ".." in remote.name
        ):
            raise SourceError("validated Git remote is missing or invalid")
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
        controller_ref = f"refs/remotes/{remote.name}/asha-control-pr-{requested}"
        if git is None:
            from .jj import JjAdapter
            git = JjAdapter(runner=self.runner)
        try:
            git.fetch_git_ref_exact(
                repository, remote.url, f"{source_ref}:{controller_ref}",
                transport=remote.transport, config_digest=remote.config_digest,
            )
        except ValueError as exc:
            raise SourceError(
                f"safe PR fetch failed without credential helpers or interactive "
                f"prompts: {exc}. For a private PR, fetch the head into this "
                "repository manually, verify it, then retry"
            ) from exc
        return (
            {
                "kind": "fetched-objects",
                "detail": f"fetched Git objects from {remote.name} {source_ref}",
                "remote": remote.name,
                "source_ref": source_ref,
            },
            {
                "kind": "controller-ref",
                "detail": f"updated controller-owned Git ref {controller_ref}",
                "ref": controller_ref,
            },
        )
