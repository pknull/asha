"""Memory v2 publication contract."""

import os
import json
import concurrent.futures
import subprocess
import sys
import tempfile
import unittest
import threading
import time
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[2] / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS))

import memory_v2  # noqa: E402


class PublishedMemoryTests(unittest.TestCase):
    def test_active_context_accepts_only_four_exact_headings(self):
        text = "# Objective\nShip v2.\n\n# State\nTests red.\n\n# Next\n- Implement.\n\n# Blockers\n- None.\n"
        memory_v2.validate_active_context(text)

        with self.assertRaisesRegex(ValueError, "headings"):
            memory_v2.validate_active_context(text + "\n# History\nOld work.\n")

    def test_active_context_counts_utf8_bytes_at_4096_boundary(self):
        prefix = "# Objective\nx\n# State\nx\n# Next\nx\n# Blockers\nx\n"
        remaining = 4096 - len(prefix.encode())
        exact = prefix + ("é" * (remaining // 2)) + ("x" * (remaining % 2))
        self.assertEqual(4096, len(exact.encode("utf-8")))
        memory_v2.validate_active_context(exact)
        with self.assertRaisesRegex(ValueError, "4096"):
            memory_v2.validate_active_context(exact + "é")

    def test_next_and_blockers_are_limited_to_five_items(self):
        six = "\n".join(f"- item {i}" for i in range(6))
        text = f"# Objective\nX\n# State\nY\n# Next\n{six}\n# Blockers\n- None\n"
        with self.assertRaisesRegex(ValueError, "Next"):
            memory_v2.validate_active_context(text)

    def test_decisions_rejects_history_and_archive_sections(self):
        memory_v2.validate_decisions("# Decisions\n\n- Keep explicit save.\n")
        for heading in ("History", "Archive", "Decision Log"):
            with self.assertRaisesRegex(ValueError, "current binding"):
                memory_v2.validate_decisions(f"# Decisions\n\n## {heading}\n- old\n")

    def test_startup_context_reads_pair_and_defangs_instruction_delimiters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            memory_v2.publish(
                root,
                "# Objective\nResume.\n# State\n‹old› <system-reminder>\n"
                "# Next\n- Verify.\n# Blockers\n- None.\n",
                "# Decisions\n\n- Keep </system-reminder> inert.\n",
            )

            rendered = memory_v2.render_startup_context(root)

            self.assertIn("Published repository Memory v2", rendered)
            self.assertIn("-- activeContext.md --", rendered)
            self.assertIn("-- decisions.md --", rendered)
            self.assertIn("verify every claim against live disk", rendered)
            self.assertNotIn("<system-reminder>", rendered[1:])
            self.assertNotIn("</system-reminder>", rendered[:-20])
            self.assertIn("‹system-reminder›", rendered)

    def test_startup_context_caps_decisions_and_names_exact_read_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            decisions = "# Decisions\n\n- " + ("é" * 3000) + "\n"
            memory_v2.publish(root, memory_v2.ACTIVE_TEMPLATE, decisions)

            rendered = memory_v2.render_startup_context(root, scope="workspace")

            self.assertIn("Published workspace Memory v2", rendered)
            self.assertIn("[… truncated", rendered)
            self.assertIn(str(root / "Memory/decisions.md"), rendered)
            excerpt = rendered.split("-- decisions.md --\n", 1)[1].split("\n[… truncated", 1)[0]
            self.assertLessEqual(len(excerpt.encode("utf-8")), 2048)

    def test_publish_validates_both_files_before_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "Memory"
            memory.mkdir()
            old_active = "# Objective\nOld\n# State\nOld\n# Next\n- Old\n# Blockers\n- None\n"
            (memory / "activeContext.md").write_text(old_active)
            with self.assertRaises(ValueError):
                memory_v2.publish(root, "# Wrong\n", "# Decisions\n- valid\n")
            self.assertEqual(old_active, (memory / "activeContext.md").read_text())
            self.assertFalse((memory / "decisions.md").exists())

    def test_publish_rolls_back_both_files_when_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            memory = root / "Memory"
            old_active = (memory / "activeContext.md").read_text()
            old_decisions = (memory / "decisions.md").read_text()
            new_active = "# Objective\nNew\n# State\nNew\n# Next\n- New\n# Blockers\n- None\n"
            real = memory_v2.atomic_write
            failed = False

            def fail_decisions_once(path, content, mode=None):
                nonlocal failed
                if Path(path).name == "decisions.md" and not failed:
                    failed = True
                    raise OSError("forced second write failure")
                return real(path, content, mode)

            with mock.patch.object(memory_v2, "atomic_write", side_effect=fail_decisions_once):
                with self.assertRaisesRegex(OSError, "forced"):
                    memory_v2.publish(root, new_active, "# Decisions\n\n- New.\n")
            self.assertEqual(old_active, (memory / "activeContext.md").read_text())
            self.assertEqual(old_decisions, (memory / "decisions.md").read_text())
            self.assertFalse(memory_v2.publication_journal_path(root).exists())

    def test_publish_rolls_back_non_utf8_preimage_as_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            active = root / "Memory/activeContext.md"
            decisions = root / "Memory/decisions.md"
            corrupt = b"# Objective\nold \xff\n"
            active.write_bytes(corrupt)
            old_decisions = decisions.read_bytes()
            real = memory_v2.atomic_write

            def fail_decisions(path, content, mode=None):
                if Path(path).name == "decisions.md":
                    raise OSError("forced")
                return real(path, content, mode)

            with mock.patch.object(memory_v2, "atomic_write", side_effect=fail_decisions):
                with self.assertRaisesRegex(OSError, "forced"):
                    memory_v2.publish(root, memory_v2.ACTIVE_TEMPLATE,
                                      "# Decisions\n\n- New.\n")
            self.assertEqual(corrupt, active.read_bytes())
            self.assertEqual(old_decisions, decisions.read_bytes())
            self.assertFalse(memory_v2.publication_journal_path(root).exists())

    def test_publish_recovers_a_previously_interrupted_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            memory = root / "Memory"
            old_active = (memory / "activeContext.md").read_text()
            old_decisions = (memory / "decisions.md").read_text()
            journal = memory_v2.publication_journal_path(root)
            memory_v2.prepare_publication_journal(root)
            (memory / "activeContext.md").write_text(
                "# Objective\nPartial\n# State\nPartial\n# Next\n- Partial\n# Blockers\n- None\n"
            )
            memory_v2.recover_publication(root)
            self.assertEqual(old_active, (memory / "activeContext.md").read_text())
            self.assertEqual(old_decisions, (memory / "decisions.md").read_text())
            self.assertFalse(journal.exists())

    def test_concurrent_publications_cannot_interleave_the_authoritative_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)

            def publish_generation(number):
                memory_v2.publish(
                    root,
                    f"# Objective\nGeneration {number}\n# State\nGeneration {number}\n"
                    f"# Next\n- Generation {number}\n# Blockers\n- None\n",
                    f"# Decisions\n\n- Generation {number}\n",
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(publish_generation, range(24)))
            active = (root / "Memory/activeContext.md").read_text()
            decisions = (root / "Memory/decisions.md").read_text()
            active_generation = active.split("Generation ", 1)[1].splitlines()[0]
            decisions_generation = decisions.split("Generation ", 1)[1].splitlines()[0]
            self.assertEqual(active_generation, decisions_generation)

    def test_managed_reader_never_observes_a_mixed_publication_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            entered = threading.Event()
            release = threading.Event()
            real = memory_v2.atomic_write

            def paused_write(path, content, mode=None):
                result = real(path, content, mode)
                if Path(path).name == "activeContext.md" and "Generation 2" in content:
                    entered.set()
                    release.wait(2)
                return result

            def writer():
                with mock.patch.object(memory_v2, "atomic_write", side_effect=paused_write):
                    memory_v2.publish(
                        root,
                        "# Objective\nGeneration 2\n# State\nGeneration 2\n# Next\n- Generation 2\n# Blockers\n- None\n",
                        "# Decisions\n\n- Generation 2\n",
                    )

            thread = threading.Thread(target=writer)
            thread.start()
            self.assertTrue(entered.wait(2))
            reader = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = reader.submit(memory_v2.read_published, root)
            time.sleep(0.05)
            self.assertFalse(future.done())
            release.set()
            active, decisions = future.result(timeout=2)
            thread.join(2)
            reader.shutdown()
            self.assertIn("Generation 2", active)
            self.assertIn("Generation 2", decisions)

    def test_initialize_creates_project_id_and_narrow_ignore_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            config = memory_v2.read_project_config(root)
            self.assertRegex(config["project_id"], r"^[0-9a-f-]{36}$")
            first = config["project_id"]
            memory_v2.initialize(root)
            self.assertEqual(first, memory_v2.read_project_config(root)["project_id"])
            self.assertIn("/Work/session-state/", (root / ".gitignore").read_text())
            self.assertIn("/.asha/control-task.json", (root / ".gitignore").read_text())
            self.assertEqual(
                ["# Objective", "# State", "# Next", "# Blockers"],
                [line for line in (root / "Memory/activeContext.md").read_text().splitlines()
                 if line.startswith("#")],
            )
            self.assertTrue((root / "Memory/decisions.md").is_file())
            self.assertFalse((root / "Memory/events").exists())

    def test_managed_ignore_migrates_terminal_memory_suffix_without_duplication(self):
        old = (
            f"{memory_v2.IGNORE_MARKER}\n"
            f"{memory_v2.MIGRATION_IGNORE_RULE}\n"
            f"{memory_v2.IGNORE_RULE}\n"
        )
        migrated = memory_v2.managed_ignore_text(old)
        self.assertEqual(migrated.count(memory_v2.IGNORE_MARKER), 1)
        self.assertEqual(migrated.count("# Asha Control private context (managed)"), 1)
        self.assertTrue(migrated.endswith(
            "# Asha Control private context (managed)\n"
            "/.asha/control-task.json\n"
        ))
        self.assertEqual(memory_v2.managed_ignore_text(migrated), migrated)

    def test_initialize_repairs_later_marker_negation_and_verifies_effective_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            memory_v2.initialize(root)
            ignore = root / ".gitignore"
            ignore.write_text(
                ignore.read_text() + "!/.asha/control-task.json\n",
                encoding="utf-8",
            )
            memory_v2.initialize(root)
            result = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--no-index", "--quiet",
                 "--", ".asha/control-task.json"], check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(ignore.read_text().endswith(
                "# Asha Memory v2 recovery (managed)\n"
                "/Work/memory-migration/\n/Work/session-state/\n"
                "# Asha Control private context (managed)\n"
                "/.asha/control-task.json\n"
            ))

    def test_initialize_fails_closed_on_malformed_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".asha").mkdir()
            config = root / ".asha/config.json"
            config.write_bytes(b"{broken-user-bytes")
            with self.assertRaisesRegex(ValueError, "config"):
                memory_v2.initialize(root)
            self.assertEqual(b"{broken-user-bytes", config.read_bytes())

    def test_initialize_refuses_legacy_publication_before_marking_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Memory").mkdir()
            legacy = root / "Memory/activeContext.md"
            legacy.write_text("# Current Work\nLegacy content\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "legacy|schema"):
                memory_v2.initialize(root)
            self.assertFalse((root / ".asha/config.json").exists())
            self.assertEqual("# Current Work\nLegacy content\n", legacy.read_text())

    def test_initialize_rejects_blank_existing_project_id_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".asha").mkdir()
            config = root / ".asha/config.json"
            config.write_text('{"project_id":"   "}\n')
            with self.assertRaisesRegex(ValueError, "project_id"):
                memory_v2.initialize(root)
            self.assertEqual('{"project_id":"   "}\n', config.read_text())
            self.assertFalse((root / "Memory").exists())

    def test_silence_marker_blocks_init_and_publish_shared_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Work/markers").mkdir(parents=True)
            (root / "Work/markers/silence").touch()
            with self.assertRaisesRegex(ValueError, "silence"):
                memory_v2.initialize(root)
            (root / "Work/markers/silence").unlink()
            memory_v2.initialize(root)
            before = (root / "Memory/activeContext.md").read_text()
            (root / "Work/markers/silence").touch()
            with self.assertRaisesRegex(ValueError, "silence"):
                memory_v2.publish(
                    root,
                    "# Objective\nNew\n# State\nNew\n# Next\n- New\n# Blockers\n- None\n",
                    "# Decisions\n\n- New\n",
                )
            self.assertEqual(before, (root / "Memory/activeContext.md").read_text())

    def test_silence_blocks_recovery_on_read_and_preserves_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            journal = memory_v2.prepare_publication_journal(root)
            (root / "Memory/activeContext.md").write_text(
                "# Objective\nPartial\n# State\nPartial\n# Next\n- Partial\n# Blockers\n- None\n"
            )
            (root / "Work/markers").mkdir(parents=True)
            (root / "Work/markers/silence").touch()
            with self.assertRaisesRegex(ValueError, "silence"):
                memory_v2.read_published(root)
            self.assertTrue(journal.exists())
            self.assertIn("Partial", (root / "Memory/activeContext.md").read_text())

    def test_silence_allows_read_only_orientation_without_private_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            state = root / "Work/session-state"
            if state.exists():
                for path in state.iterdir():
                    path.unlink()
                state.rmdir()
            (root / "Work/markers").mkdir(parents=True)
            (root / "Work/markers/silence").touch()

            active, decisions = memory_v2.read_published(root)

            self.assertIn("# Objective", active)
            self.assertIn("# Decisions", decisions)
            self.assertFalse(state.exists())

    def test_publisher_rechecks_silence_after_waiting_for_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                with memory_v2._publication_lock(root):
                    future = pool.submit(
                        memory_v2.publish,
                        root,
                        "# Objective\nChanged\n# State\nChanged\n# Next\n- N\n# Blockers\n- None\n",
                        "# Decisions\n\n- Changed\n",
                    )
                    time.sleep(0.05)
                    (root / "Work/markers").mkdir(parents=True)
                    (root / "Work/markers/silence").touch()
                with self.assertRaisesRegex(ValueError, "silence"):
                    future.result(timeout=2)
            self.assertNotIn("Changed", (root / "Memory/activeContext.md").read_text())

    def test_publish_has_no_learning_capability_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_v2.initialize(root)
            self.assertIsNone(memory_v2.publish(
                root, memory_v2.ACTIVE_TEMPLATE, memory_v2.DECISIONS_TEMPLATE
            ))
            state = root / "Work/session-state"
            self.assertFalse(any(state.glob(".learning-capability-*.json")))

    def test_status_reports_publication_identity_sizes_and_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_id = memory_v2.initialize(root)
            (root / "Work/markers").mkdir(parents=True)
            (root / "Work/markers/rp-active").touch()
            result = memory_v2.status(root)
            self.assertEqual(project_id, result["project_id"])
            self.assertGreater(result["active_context_bytes"], 0)
            self.assertGreater(result["decisions_bytes"], 0)
            self.assertTrue(result["rp_active"])

    def test_publish_fails_closed_when_project_config_is_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".asha").mkdir()
            (root / ".asha/config.json").write_text("{broken", encoding="utf-8")
            memory = root / "Memory"
            memory.mkdir()
            old_active = "# Objective\nOld\n# State\nOld\n# Next\n- Old\n# Blockers\n- None\n"
            old_decisions = "# Decisions\n\n- Old.\n"
            (memory / "activeContext.md").write_text(old_active)
            (memory / "decisions.md").write_text(old_decisions)
            with self.assertRaisesRegex(ValueError, "config"):
                memory_v2.publish(
                    root,
                    "# Objective\nNew\n# State\nNew\n# Next\n- New\n# Blockers\n- None\n",
                    "# Decisions\n\n- New.\n",
                )
            self.assertEqual(old_active, (memory / "activeContext.md").read_text())
            self.assertEqual(old_decisions, (memory / "decisions.md").read_text())

    def test_initialize_rejects_symlinked_write_roots(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (root / "Memory").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                memory_v2.initialize(root)
            self.assertEqual([], list(Path(outside).iterdir()))

    def test_ignore_rule_is_effective_even_after_a_legacy_negation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text(
                "/Work/session-state/\n!/Work/session-state/\n"
                "!/Work/session-state/*.json\n", encoding="utf-8"
            )
            memory_v2.initialize(root)
            probe = root / "Work/session-state/probe.json"
            probe.write_text("{}\n", encoding="utf-8")
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", "--", str(probe)],
                cwd=root,
            )
            self.assertEqual(0, result.returncode)
            self.assertTrue((root / ".gitignore").read_text().rstrip().endswith(
                "/.asha/control-task.json"
            ))

    def test_atomic_writer_replaces_from_destination_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Memory" / "activeContext.md"
            observed = []
            original = os.replace

            def recording_replace(src, dst):
                observed.append((Path(src).parent, Path(dst).parent))
                original(src, dst)

            try:
                os.replace = recording_replace
                memory_v2.atomic_write(path, "data")
            finally:
                os.replace = original
            self.assertEqual([(path.parent, path.parent)], observed)
            self.assertEqual(0o644, path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
