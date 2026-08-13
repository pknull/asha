"""Bounded, unpublished recovery-state tests."""

import concurrent.futures
import json
import os
import sys
import tempfile
import unittest
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[2] / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS))

import recovery_state  # noqa: E402


class RecoveryStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".asha").mkdir()
        (self.root / ".asha/config.json").write_text(
            json.dumps({"initialized": True, "memory_version": 2, "project_id": "project-1"})
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_is_project_local_atomic_private_and_bounded(self):
        path = recovery_state.update(
            self.root,
            {"session_id": "../odd/session", "harness": "claude", "event": "prompt",
             "prompt": "x" * 5000},
        )
        self.assertEqual(
            self.root / "Work/session-state" /
            f"claude-{recovery_state._filename_component('../odd/session', 'unknown')}.json",
            path,
        )
        self.assertTrue(path.resolve().is_relative_to((self.root / "Work/session-state").resolve()))
        self.assertLessEqual(len(path.read_bytes()), 2048)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertFalse(any(p.name.startswith(f".{path.name}.tmp") for p in path.parent.iterdir()))

    def test_long_harness_and_session_ids_still_produce_a_contained_snapshot(self):
        path = recovery_state.update(self.root, {
            "session_id": "s" * 1000, "harness": "h" * 1000, "event": "start",
        })
        self.assertIsNotNone(path)
        self.assertLessEqual(len(path.name.encode("utf-8")), 180)
        self.assertTrue(path.is_file())
        self.assertIn(path, recovery_state._validated_snapshot_paths(self.root))
        self.assertEqual("s" * 160, recovery_state.latest(self.root)["session_id"])
        other = recovery_state.update(self.root, {
            "session_id": ("s" * 999) + "x", "harness": "h" * 1000,
            "event": "start",
        })
        self.assertNotEqual(path, other)
        self.assertIn(other, recovery_state._validated_snapshot_paths(self.root))
        old = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(path, (old, old))
        self.assertIn(path, recovery_state.sweep(self.root, days=7))

    def test_update_redacts_secrets_dedupes_and_caps_paths(self):
        paths = [f"src/{i}.py" for i in range(12)] + ["src/3.py"]
        path = recovery_state.update(self.root, {
            "session_id": "s1", "harness": "codex", "event": "tool",
            "prompt": "token=ghp_abcdefghijklmnopqrstuvwxyz123456",
            "tool_name": "Edit", "paths": paths,
        })
        data = json.loads(path.read_text())
        self.assertNotIn("ghp_", path.read_text())
        self.assertIn("[REDACTED]", path.read_text())
        self.assertEqual(10, len(data["paths"]))
        self.assertEqual(len(data["paths"]), len(set(data["paths"])))

    def test_update_redacts_all_supported_secret_families(self):
        specimens = [
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "Bearer standalonebearertokenvalue",
            "credentials='quoted value with spaces and enough length'",
            "auth: Basic dXNlcjpwYXNzd29yZA==",
            "Basic c3RhbmRhbG9uZS1iYXNpYw==",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_value",
            "AKIAIOSFODNN7EXAMPLE",
            "-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----",
            "github_pat_11AA22BB33CC44DD55EE66FF77GG88HH99II",
            "glpat-abcdefghijklmnopqrstuvwxyz",
            "npm_abcdefghijklmnopqrstuvwxyz123456",
            "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234567",
        ]
        path = recovery_state.update(self.root, {
            "session_id": "redact", "harness": "codex", "event": "prompt",
            "prompt": " | ".join(specimens),
        })
        persisted = path.read_text()
        for secret in ("abcdefghijklmnopqrstuvwxyz", "standalonebearer", "quoted value",
                       "dXNlcj", "c3Rhbm", "eyJhb", "AKIA", "secret material",
                       "github_pat_", "glpat-", "npm_", "AIza"):
            self.assertNotIn(secret, persisted)

    def test_update_redacts_url_and_database_dsn_userinfo(self):
        path = recovery_state.update(self.root, {
            "session_id": "urls", "harness": "codex", "event": "prompt",
            "prompt": ("https://alice:correct-horse-battery@example.com/repo.git "
                       "postgresql://dbuser:db-password@db.example/app"),
        })
        persisted = path.read_text()
        self.assertNotIn("alice", persisted)
        self.assertNotIn("correct-horse", persisted)
        self.assertNotIn("dbuser", persisted)
        self.assertNotIn("db-password", persisted)
        self.assertIn("[REDACTED]@example.com", persisted)

    def test_update_redacts_prefixed_suffixed_query_and_environment_secrets(self):
        path = recovery_state.update(self.root, {
            "session_id": "assignment-secrets", "harness": "codex", "event": "prompt",
            "prompt": (
                "access_token=query-secret&client_secret=oauth-secret "
                "export DATABASE_PASSWORD=database-secret "
                "AWS_SECRET_ACCESS_KEY=aws-secret-value refresh_token=refresh-secret"
            ),
        })
        persisted = path.read_text()
        for secret in ("query-secret", "oauth-secret", "database-secret",
                       "aws-secret-value", "refresh-secret"):
            self.assertNotIn(secret, persisted)
        self.assertGreaterEqual(persisted.count("[REDACTED]"), 5)

    def test_secret_shaped_identity_is_not_exposed_in_filename_or_record(self):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJ"
        path = recovery_state.update(self.root, {
            "session_id": secret, "harness": "codex", "event": "start",
        })
        self.assertIsNotNone(path)
        self.assertNotIn("ghp_", path.name)
        self.assertNotIn("ghp_", path.read_text())

    def test_secret_shaped_identity_remains_valid_for_latest_and_expiry(self):
        secret = "github_pat_11AA22BB33CC44DD55EE66FF77GG88HH99II"
        path = recovery_state.update(self.root, {
            "session_id": secret, "harness": "copilot", "event": "start",
        })
        self.assertEqual("[REDACTED]", recovery_state.latest(self.root)["session_id"])
        old = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(path, (old, old))
        self.assertEqual([path], recovery_state.sweep(self.root, days=7))

    def test_short_sanitized_identities_do_not_collide(self):
        first = recovery_state.update(self.root, {
            "session_id": "a/b", "harness": "foo/bar", "event": "start",
        })
        second = recovery_state.update(self.root, {
            "session_id": "a?b", "harness": "foo?bar", "event": "start",
        })
        self.assertNotEqual(first, second)
        self.assertEqual("a/b", json.loads(first.read_text())["session_id"])
        self.assertEqual("a?b", json.loads(second.read_text())["session_id"])

    def test_missing_identity_is_rejected_instead_of_merged_as_unknown(self):
        self.assertIsNone(recovery_state.update(self.root, {"event": "start"}))
        self.assertFalse((self.root / "Work/session-state").exists())

    def test_cli_missing_identity_does_not_create_unknown_snapshot(self):
        tool = TOOLS / "recovery_state.py"
        subprocess.run(
            [sys.executable, str(tool), "update", "--project-dir", str(self.root)],
            input='{"event":"prompt","prompt":"x"}', text=True, check=True,
        )
        directory = self.root / "Work/session-state"
        self.assertFalse(directory.exists() and list(directory.glob("*.json")))

    def test_symlinked_recovery_directory_fails_open_without_external_write(self):
        with tempfile.TemporaryDirectory() as outside:
            work = self.root / "Work"
            work.mkdir()
            (work / "session-state").symlink_to(Path(outside), target_is_directory=True)
            self.assertIsNone(recovery_state.update(self.root, {
                "session_id": "s", "harness": "h", "event": "start",
            }))
            self.assertEqual([], list(Path(outside).iterdir()))

    def test_copilot_camel_case_tool_payload_records_paths_and_errors(self):
        path = recovery_state.update(self.root, {
            "sessionId": "native", "harness": "copilot", "event": "tool",
            "toolName": "edit", "toolArgs": {"path": "src/camel.py"},
            "toolResult": {"error": "write failed"},
        })
        data = json.loads(path.read_text())
        self.assertEqual(["src/camel.py"], data["paths"])
        self.assertEqual("write failed", data["blocker"])
        recovery_state.update(self.root, {
            "sessionId": "native", "harness": "copilot", "event": "tool",
            "toolName": "edit", "toolArgs": '{"filePath":"src/second.py"}',
            "errors": ["second failure"],
        })
        data = json.loads(path.read_text())
        self.assertIn("src/second.py", data["paths"])
        self.assertIn("second failure", data["blocker"])

    def test_single_path_string_is_one_path_not_characters(self):
        path = recovery_state.update(self.root, {
            "session_id": "s1", "harness": "codex", "event": "tool",
            "paths": "src/one.py",
        })
        self.assertEqual(["src/one.py"], json.loads(path.read_text())["paths"])

    def test_malformed_payload_fails_open_without_writing(self):
        self.assertIsNone(recovery_state.update(self.root, "not-an-object"))
        self.assertFalse((self.root / "Work/session-state").exists())

    def test_sessions_are_isolated_and_concurrent_updates_remain_valid(self):
        def write(i):
            return recovery_state.update(self.root, {
                "session_id": "same" if i < 20 else "other",
                "harness": "claude", "event": "tool", "tool_name": f"Tool{i}",
                "paths": [f"file-{i}"],
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(40)))
        files = sorted((self.root / "Work/session-state").glob("*.json"))
        self.assertEqual(2, len(files))
        for path in files:
            json.loads(path.read_text())

    def test_sweep_expires_only_snapshots_older_than_seven_days(self):
        current = recovery_state.update(
            self.root, {"session_id": "new", "harness": "claude", "event": "start"}
        )
        stale = recovery_state.update(
            self.root, {"session_id": "old", "harness": "claude", "event": "start"}
        )
        old = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(stale, (old, old))
        removed = recovery_state.sweep(self.root, days=7)
        self.assertEqual([stale], removed)
        self.assertTrue(current.exists())

    def test_sweep_and_latest_ignore_publication_journal_and_unrelated_json(self):
        snapshot = recovery_state.update(
            self.root, {"session_id": "valid", "harness": "claude", "event": "start"}
        )
        directory = snapshot.parent
        journal = directory / ".memory-publication-transaction.json"
        unrelated = directory / "notes.json"
        journal.write_text('{"version":2,"state":"prepared"}\n')
        unrelated.write_text('{"prompt":"must not be recovery"}\n')
        old = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(journal, (old, old))
        os.utime(unrelated, (old, old))
        self.assertEqual([], recovery_state.sweep(self.root, days=7))
        self.assertTrue(journal.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual("valid", recovery_state.latest(self.root)["session_id"])

    def test_seal_only_updates_timestamp_and_prunes(self):
        path = recovery_state.update(
            self.root, {"session_id": "s1", "harness": "claude", "event": "prompt",
                        "prompt": "keep me"}
        )
        before = json.loads(path.read_text())
        recovery_state.seal(self.root, "claude", "s1")
        after = json.loads(path.read_text())
        self.assertEqual(before["prompt"], after["prompt"])
        self.assertEqual(before["last_action"], after["last_action"])
        self.assertEqual(before["paths"], after["paths"])
        self.assertEqual(before["blocker"], after["blocker"])
        self.assertNotEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(after["updated_at"], after["sealed_at"])


if __name__ == "__main__":
    unittest.main()
