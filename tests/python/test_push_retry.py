"""Regression tests for push_retry.py (issue #5: unbounded queue growth
on deliberately remoteless repos).

Hermetic: every test builds throwaway git repos under a TemporaryDirectory.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS))

import push_retry  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _make_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("one\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c1")
    return repo


def _commit(repo: Path, content: str) -> None:
    (repo / "f.txt").write_text(content)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", f"c-{content.strip()}")


class QueueCollapseTests(unittest.TestCase):
    """Defect 1: the queue must hold at most one entry (latest HEAD)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _make_repo(self.root)
        self.qfile = push_retry._queue_path(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_repeated_saves_do_not_grow_queue(self):
        for i in range(5):
            _commit(self.repo, f"content-{i}\n")
            result = push_retry.ensure(self.repo, self.repo)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["reason"], "no_remote")
        entries = push_retry._read_queue(self.qfile)
        self.assertEqual(len(entries), 1, "queue must collapse to a single entry")

    def test_entry_tracks_latest_head_and_preserves_first_seen(self):
        push_retry.ensure(self.repo, self.repo)
        first = push_retry._read_queue(self.qfile)[0]
        _commit(self.repo, "two\n")
        push_retry.ensure(self.repo, self.repo)
        entry = push_retry._read_queue(self.qfile)[0]
        rc = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(entry["head"], rc.stdout.strip(), "entry must be the latest HEAD")
        self.assertEqual(entry["first_seen"], first["first_seen"],
                         "first_seen must survive head replacement (age reporting)")
        self.assertEqual(entry["attempts"], first["attempts"] + 1,
                         "attempts must carry over (backoff continuity)")

    def test_legacy_multi_entry_queue_collapses_on_next_ensure(self):
        # Simulate the 59-entry backlog from the field report.
        self.qfile.parent.mkdir(parents=True, exist_ok=True)
        legacy = [
            {"head": f"deadbeef{i:02d}", "branch": "master", "reason": "no_remote",
             "attempts": 1, "first_seen": f"2026-06-{i+1:02d}T00:00:00Z",
             "last_attempt": f"2026-06-{i+1:02d}T00:00:00Z",
             "next_retry_after": f"2026-06-{i+1:02d}T00:01:00Z", "error": ""}
            for i in range(10)
        ]
        self.qfile.write_text("".join(json.dumps(e) + "\n" for e in legacy))
        push_retry.ensure(self.repo, self.repo)
        entries = push_retry._read_queue(self.qfile)
        self.assertEqual(len(entries), 1, "legacy backlog must collapse to one entry")
        self.assertEqual(entries[0]["first_seen"], legacy[-1]["first_seen"],
                         "first_seen carries from the prior tail entry")


class LocalOnlyTests(unittest.TestCase):
    """Defect 2: deliberately-local repos must never queue."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _make_repo(self.root)
        _git(self.repo, "config", "asha.localOnly", "true")
        self.qfile = push_retry._queue_path(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ensure_skips_and_never_queues(self):
        for i in range(3):
            _commit(self.repo, f"v{i}\n")
            result = push_retry.ensure(self.repo, self.repo)
            self.assertEqual(result["status"], "skipped_local_only")
        self.assertEqual(push_retry._read_queue(self.qfile), [],
                         "localOnly repo must accumulate nothing")

    def test_status_reports_local_only(self):
        self.assertTrue(push_retry.status(self.repo)["local_only"])

    def test_config_false_still_queues(self):
        _git(self.repo, "config", "asha.localOnly", "false")
        result = push_retry.ensure(self.repo, self.repo)
        self.assertEqual(result["status"], "queued")


class ClearTests(unittest.TestCase):
    """Defect 3: a cleanup affordance must exist."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _make_repo(self.root)
        self.qfile = push_retry._queue_path(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_clear_drops_entries_and_reports_count(self):
        push_retry.ensure(self.repo, self.repo)
        result = push_retry.clear(self.repo)
        self.assertEqual(result, {"status": "cleared", "dropped": 1})
        self.assertEqual(push_retry._read_queue(self.qfile), [])

    def test_clear_on_empty_queue_is_a_noop(self):
        result = push_retry.clear(self.repo)
        self.assertEqual(result, {"status": "cleared", "dropped": 0})


class PushPathTests(unittest.TestCase):
    """The remote-present paths must be unaffected by the changes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.remote = self.root / "origin.git"
        self.remote.mkdir()
        _git(self.remote, "init", "-q", "--bare")
        self.repo = _make_repo(self.root)
        _git(self.repo, "remote", "add", "origin", str(self.remote))
        _git(self.repo, "push", "-qu", "origin", "master")
        self.qfile = push_retry._queue_path(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_successful_push_clears_queue(self):
        # seed a stale entry, then ensure with a working destination
        push_retry._write_queue(self.qfile, [{"head": "x", "first_seen": "y", "attempts": 1}])
        _commit(self.repo, "pushed\n")
        result = push_retry.ensure(self.repo, self.repo)
        self.assertEqual(result["status"], "pushed")
        self.assertEqual(push_retry._read_queue(self.qfile), [])


if __name__ == "__main__":
    unittest.main()
