from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.config import load_config
from lib.control.jj import JjAdapter
from lib.control.prepare import (
    PreparationError, _verify_plan_materialization, _workspace_paths,
)
from lib.control.transaction import (
    CreationJournalStore, JournalError, MaterializationOwnershipStore,
    MAX_JOURNAL_BYTES,
)
from lib.control.tui import TuiModel, _prompt_line


_ORACLE_CLUSTERS = {
    "e\u0301": 1,
    "©\ufe0f": 2,
    "1\ufe0f\u20e3": 2,
    "👍🏽": 2,
    "🇺🇸": 2,
    "👩\u200d💻": 2,
    "🧑🏽": 2,
    "🧑🏽\u200d💻": 2,
}
_ORACLE_CODEPOINT_WIDTHS = {
    "界": 2, "👩": 2, "💻": 2, "👍": 2, "🏽": 2, "🏾": 2,
    "🧑": 2, "🇺": 1, "🇸": 1, "\u0301": 0, "\ufe0f": 0,
    "\u20e3": 0, "\u200d": 0,
}


def oracle_terminal_cells(value: str) -> int:
    """Independent fixture oracle for the exact terminal sequences under test."""
    total = 0
    offset = 0
    known = sorted(_ORACLE_CLUSTERS, key=len, reverse=True)
    while offset < len(value):
        cluster = next(
            (item for item in known if value.startswith(item, offset)), None,
        )
        if cluster is not None:
            total += _ORACLE_CLUSTERS[cluster]
            offset += len(cluster)
            continue
        total += _ORACLE_CODEPOINT_WIDTHS.get(value[offset], 1)
        offset += 1
    return total


class FakeCurses:
    class error(Exception):
        pass

    KEY_ENTER = 343
    KEY_RESIZE = 410
    KEY_BACKSPACE = 263


class PromptScreen:
    def __init__(self, events, *, width: int = 24) -> None:
        self.events = list(events)
        self.width = width
        self.frames: list[tuple[str, int, int]] = []
        self.moves: list[tuple[int, int]] = []
        self._pending = ""
        self._cursor = 0

    def getmaxyx(self):
        return 8, self.width

    def erase(self):
        self._pending = ""
        self._cursor = 0

    def refresh(self):
        self.frames.append((self._pending, self._cursor, self.width))

    def addnstr(self, y, x, value, limit):
        if y == 7:
            rendered = value[:limit]
            if oracle_terminal_cells(rendered) > max(0, self.width - 1):
                raise AssertionError("prompt exceeded the reserved right-edge bound")
            self._pending = rendered

    def move(self, y, x):
        self.moves.append((y, x))
        if y == 7:
            if not 0 <= x < max(1, self.width):
                raise AssertionError("cursor left the terminal")
            self._cursor = x

    def clrtoeol(self):
        self._pending = ""

    def getch(self):
        event = self.events.pop(0)
        if isinstance(event, tuple):
            _resize, self.width = event
            return FakeCurses.KEY_RESIZE
        return event


class GoalViewportTests(unittest.TestCase):
    def test_ascii_suffix_viewport_is_bounded_and_returns_exact_logical_value(self) -> None:
        logical = "abcdefghijklmnopqrstuvwxyz"
        screen = PromptScreen([*(ord(char) for char in logical), 10], width=24)

        result = _prompt_line(
            screen, FakeCurses(), TuiModel([]), "Goal: ", hint="Esc cancel",
        )

        self.assertEqual(result, logical)
        line, cursor, width = screen.frames[-1]
        self.assertIn("…", line)
        self.assertTrue(line.endswith(logical[-3:]))
        self.assertLessEqual(oracle_terminal_cells(line), width - 1)
        self.assertEqual(cursor, oracle_terminal_cells(line))

    def test_cjk_combining_and_narrow_widths_never_overflow(self) -> None:
        logical = "界界e\u0301界"
        events = [*(ord(char) for char in logical), ("resize", 2), ord("x"),
                  ("resize", 1), 127, ("resize", 10), 10]
        screen = PromptScreen(events, width=12)

        result = _prompt_line(
            screen, FakeCurses(), TuiModel([]), "Goal: ", hint="Esc cancel",
        )

        self.assertEqual(result, logical)
        for line, cursor, width in screen.frames:
            self.assertLessEqual(oracle_terminal_cells(line), max(0, width - 1))
            self.assertEqual(cursor, oracle_terminal_cells(line))

    def test_resize_recomputes_viewport_without_changing_two_hundred_character_value(self) -> None:
        logical = "a" * 200
        events = [*(ord(char) for char in logical), ("resize", 20),
                  ("resize", 120), 10]
        screen = PromptScreen(events, width=100)

        result = _prompt_line(
            screen, FakeCurses(), TuiModel([]), "Goal: ", hint="Esc cancel",
            maximum=200,
        )

        self.assertEqual(result, logical)
        widths = {width for _line, _cursor, width in screen.frames}
        self.assertTrue({20, 100, 120}.issubset(widths))
        self.assertTrue(all(
            oracle_terminal_cells(line) <= max(0, width - 1)
            for line, _cursor, width in screen.frames
        ))

    def test_combining_and_emoji_clusters_are_drawn_whole_with_exact_cursor(self) -> None:
        cases = (
            ("e\u0301", 3),
            ("©\ufe0f", 4),
            ("1\ufe0f\u20e3", 4),
            ("👍🏽", 4),
            ("🇺🇸", 4),
            ("👩\u200d💻", 4),
        )
        for logical, width in cases:
            with self.subTest(logical=logical):
                screen = PromptScreen(
                    [*(ord(character) for character in logical), 10], width=width,
                )

                result = _prompt_line(
                    screen, FakeCurses(), TuiModel([]), "Goal: ",
                    maximum=len(logical),
                )

                self.assertEqual(result, logical)
                line, cursor, actual_width = screen.frames[-1]
                self.assertEqual(line, "…" + logical)
                self.assertEqual(cursor, actual_width - 1)
                self.assertEqual(cursor, oracle_terminal_cells(line))

    def test_standalone_joiner_is_rejected_but_valid_zwj_sequence_is_retained(self) -> None:
        logical = "👩\u200d💻"
        screen = PromptScreen(
            [0x200D, *(ord(character) for character in logical), 10], width=4,
        )

        result = _prompt_line(
            screen, FakeCurses(), TuiModel([]), "Goal: ", maximum=len(logical),
        )

        self.assertEqual(result, logical)

    def test_dangling_joiner_is_not_submitted_while_duplicate_or_standalone_selector_is_rejected(self) -> None:
        dangling = PromptScreen([ord("👩"), 0x200D, 10, 27], width=8)
        selector = PromptScreen(
            [0xFE0F, ord("©"), 0xFE0F, 0xFE0F, 10], width=8,
        )

        dangling_result = _prompt_line(
            dangling, FakeCurses(), TuiModel([]), "Goal: ", maximum=3,
        )
        selector_result = _prompt_line(
            selector, FakeCurses(), TuiModel([]), "Goal: ", maximum=3,
        )

        self.assertIsNone(dangling_result)
        self.assertEqual(selector_result, "©\ufe0f")

    def test_standalone_and_duplicate_extensions_are_rejected_without_overflow(self) -> None:
        events = [
            0x1F3FD, 0x0301, 0x20E3,
            ord("©"), 0xFE0F, 0x1F3FD,
            ord("👍"), 0x1F3FD, 0x1F3FE,
            ord("👩"), 0x200D, ord("💻"), 0x1F3FD, 10,
        ]
        screen = PromptScreen(events, width=4)

        result = _prompt_line(
            screen, FakeCurses(), TuiModel([]), "Goal: ", maximum=7,
        )

        self.assertEqual(result, "©\ufe0f👍🏽👩\u200d💻")
        for line, cursor, width in screen.frames:
            self.assertLessEqual(oracle_terminal_cells(line), max(0, width - 1))
            self.assertEqual(cursor, oracle_terminal_cells(line))

    def test_every_intermediate_grapheme_frame_is_bounded_at_widths_one_to_four(self) -> None:
        logical_values = (
            "e\u0301", "©\ufe0f", "1\ufe0f\u20e3", "👍🏽", "🇺🇸", "👩\u200d💻",
        )
        for width in range(1, 5):
            for logical in logical_values:
                with self.subTest(width=width, logical=logical):
                    screen = PromptScreen(
                        [*(ord(character) for character in logical), 10],
                        width=width,
                    )

                    result = _prompt_line(
                        screen, FakeCurses(), TuiModel([]), "Goal: ",
                        maximum=len(logical),
                    )

                    self.assertEqual(result, logical)
                    for line, cursor, actual_width in screen.frames:
                        cells = oracle_terminal_cells(line)
                        self.assertLessEqual(cells, max(0, actual_width - 1))
                        self.assertEqual(cursor, cells)

    def test_valid_person_modifier_is_retained_and_arbitrary_zwj_is_rejected(self) -> None:
        logical = "🧑🏽\u200d💻👍👍"
        events = [
            ord("🧑"), ord("🏽"), 0x200D, ord("💻"),
            ord("👍"), 0x200D, ord("👍"), 10,
        ]
        for width in range(1, 5):
            with self.subTest(width=width):
                screen = PromptScreen(events, width=width)

                result = _prompt_line(
                    screen, FakeCurses(), TuiModel([]), "Goal: ",
                    maximum=len(logical),
                )

                self.assertEqual(result, logical)
                for line, cursor, actual_width in screen.frames:
                    cells = oracle_terminal_cells(line)
                    self.assertLessEqual(cells, max(0, actual_width - 1))
                    self.assertEqual(cursor, cells)

    def test_preloaded_arbitrary_zwj_is_measured_conservatively_but_not_submitted(self) -> None:
        logical = "👍\u200d👍"
        for width in range(1, 7):
            with self.subTest(width=width):
                screen = PromptScreen([10, 27], width=width)

                result = _prompt_line(
                    screen, FakeCurses(), TuiModel([]), "Goal: ",
                    initial=logical, maximum=len(logical),
                )

                self.assertIsNone(result)
                for line, cursor, actual_width in screen.frames:
                    cells = oracle_terminal_cells(line)
                    self.assertLessEqual(cells, max(0, actual_width - 1))
                    self.assertEqual(cursor, cells)

    def test_dangling_profession_joiner_remains_bounded_but_is_not_submitted(self) -> None:
        logical = "👩\u200d"
        for width in range(1, 5):
            with self.subTest(width=width):
                screen = PromptScreen(
                    [ord("👩"), 0x200D, 10, 27], width=width,
                )

                result = _prompt_line(
                    screen, FakeCurses(), TuiModel([]), "Goal: ",
                    maximum=len(logical),
                )

                self.assertIsNone(result)
                for line, cursor, actual_width in screen.frames:
                    cells = oracle_terminal_cells(line)
                    self.assertLessEqual(cells, max(0, actual_width - 1))
                    self.assertEqual(cursor, cells)


class MaterializationPlanTests(unittest.TestCase):
    def test_plan_uses_one_metadata_read_and_no_per_blob_subprocesses(self) -> None:
        records = []
        sizes = []
        for index in range(2088):
            size = 71_643_083 if index == 0 else 147_000
            sizes.append(size)
            path = f"group-{index // 10:03d}/file-{index:04d}.bin"
            oid = hashlib.sha1(path.encode()).hexdigest()
            records.append(
                f"100644 blob {oid} {size}\t{path}".encode() + b"\0"
            )
        raw = b"".join(records)
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, raw, b"")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text(
                "ref: refs/heads/main\n", encoding="utf-8",
            )
            plan = JjAdapter(runner=runner).materialization_plan(
                root / ".git", "a" * 40, exact_root=root,
            )

        self.assertEqual(len(calls), 1)
        self.assertIn("ls-tree", calls[0])
        self.assertNotIn("cat-file", calls[0])
        self.assertEqual(plan.blob_count, 2088)
        self.assertGreater(plan.directory_count, 199)
        self.assertEqual(plan.total_blob_bytes, sum(sizes))
        self.assertGreater(plan.total_blob_bytes, 377_174_965)
        self.assertGreater(max(entry.size for entry in plan.entries), 71_643_082)
        self.assertLess(len(json.dumps(plan.record())), 1024)

    def test_streaming_verification_accepts_blob_over_v1_limit_and_detects_inode_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            destination = root / "workspace"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(
                ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "config", "user.name", "Test"], check=True,
            )
            payload = b"x" * (16 * 1024 * 1024 + 1)
            for index in range(4):
                (source / f"large-{index}.bin").write_bytes(payload)
            (source / "small.txt").write_text("small\n", encoding="utf-8")
            (source / "small-link").symlink_to("small.txt")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
            commit = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
            ).strip()
            plan = JjAdapter().materialization_plan(
                source / ".git", commit, exact_root=source,
            )
            self.assertGreater(plan.total_blob_bytes, 64 * 1024 * 1024)
            destination.mkdir()
            subprocess.run(
                ["git", "-C", str(source), "checkout-index", "-a", f"--prefix={destination}/"],
                check=True,
            )
            (source / ".jj").mkdir()
            (source / ".jj" / "repo").write_text("source", encoding="utf-8")
            (destination / ".jj" / "working_copy").mkdir(parents=True)
            (destination / ".jj" / "repo").write_bytes(
                str(source / ".jj" / "repo").encode(),
            )
            (destination / ".jj" / "working_copy" / "checkout").write_text("checkout")
            (destination / ".jj" / "working_copy" / "tree_state").write_text("state")
            (destination / ".jj" / "working_copy" / "type").write_bytes(b"local")

            facts, _private, root_fact = _verify_plan_materialization(
                destination, source, plan,
            )
            mode_path = destination / "large-1.bin"
            mode_path.chmod(0o600)
            with self.assertRaisesRegex(PreparationError, "identity changed"):
                _verify_plan_materialization(
                    destination, source, plan,
                    expected_root=root_fact, expected_facts=facts,
                )
            mode_index = next(
                index for index, entry in enumerate(plan.entries)
                if entry.path == "large-1.bin"
            )
            mode_path.chmod(facts[mode_index][2])
            replacement = destination / "replacement"
            replacement.write_bytes(payload)
            os.replace(replacement, destination / "large-0.bin")

            with self.assertRaisesRegex(PreparationError, "identity changed"):
                _verify_plan_materialization(
                    destination, source, plan,
                    expected_root=root_fact, expected_facts=facts,
                )

    def test_metadata_walk_rejects_an_opened_directory_inode_swap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "directory").mkdir()
            root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            real_fstat = os.fstat

            def changed_child(fd: int):
                metadata = real_fstat(fd)
                if fd != root_fd:
                    fields = list(metadata)
                    fields[1] += 1
                    return os.stat_result(fields)
                return metadata

            try:
                with mock.patch("lib.control.prepare.os.fstat", side_effect=changed_child):
                    with self.assertRaisesRegex(PreparationError, "directory changed"):
                        _workspace_paths(root_fd, 1)
            finally:
                os.close(root_fd)


class OwnershipSidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        home = root / "home"
        home.mkdir()
        self.config = load_config({
            "HOME": str(home), "ASHA_CONFIG": str(root / "missing.json"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        })
        self.store = MaterializationOwnershipStore(self.config)
        self.task_id = str(uuid.uuid4())
        self.plan_digest = "a" * 64
        self.facts = [[1, index + 1, 0o644, os.geteuid()] for index in range(1100)]

    def test_sidecar_is_private_atomic_digest_bound_and_idempotent_after_rename(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "after rename"):
            self.store.write(
                self.task_id, self.plan_digest, self.facts,
                failure_injector=lambda phase: (_ for _ in ()).throw(
                    RuntimeError("after rename")
                ) if phase == "sidecar:renamed" else None,
            )

        binding = self.store.write(self.task_id, self.plan_digest, self.facts)

        self.assertEqual(binding["entry_count"], len(self.facts))
        self.assertEqual(self.store.read(binding), self.facts)
        path = Path(binding["path"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertLess(path.stat().st_size, 64 * len(self.facts))

    def test_sidecar_temp_interruption_corruption_replacement_and_symlink_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "temp"):
            self.store.write(
                self.task_id, self.plan_digest, self.facts,
                failure_injector=lambda phase: (_ for _ in ()).throw(
                    RuntimeError("temp")
                ) if phase == "sidecar:temp-written" else None,
            )
        self.assertFalse(self.store.path(self.task_id).exists())
        binding = self.store.write(self.task_id, self.plan_digest, self.facts)
        path = Path(binding["path"])

        original = path.read_bytes()
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        os.replace(replacement, path)
        with self.assertRaisesRegex(JournalError, "identity"):
            self.store.read(binding)
        path.unlink()
        binding = self.store.write(self.task_id, self.plan_digest, self.facts)
        path = Path(binding["path"])
        original = path.read_bytes()
        path.write_bytes(original[:-1])
        with self.assertRaises(JournalError):
            self.store.read(binding)
        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaisesRegex(JournalError, "0600"):
            self.store.read(binding)
        path.chmod(0o600)
        path.unlink()
        path.symlink_to(Path(self.temp.name) / "foreign")
        with self.assertRaisesRegex(JournalError, "symlink"):
            self.store.read(binding)

    def test_v1_creation_journal_remains_readable(self) -> None:
        journal_store = CreationJournalStore(self.config)
        repository = Path(self.temp.name).resolve() / "repository"
        workspace = self.config.workspace_root / "repository-aaaaaaaaaaaaaaaa" / "task"
        journal = {
            "contract": "asha.control-creation-journal.v1",
            "task_id": self.task_id, "invocation_id": "c" * 32,
            "phase": "intent", "launch_attempted": False,
            "config": {
                "workspace_root": str(self.config.workspace_root),
                "tasks_dir": str(self.config.tasks_dir),
                "runtime_dir": str(self.config.runtime_dir),
            },
            "repository": {
                "root": str(repository), "identity": "repo:" + "a" * 64,
                "git_root": str(repository), "repo_key": "repository-aaaaaaaaaaaaaaaa",
            },
            "task": {
                "record_path": str(self.config.tasks_dir / f"{self.task_id}.json"),
                "slug": "task", "label": "Task", "digest": None, "failure": None,
            },
            "workspace": {
                "path": str(workspace), "name": "asha-task-11111111",
                "root_fact": None, "created_parents": [],
            },
            "jj": {
                "pinned_operation_id": "a" * 128, "base_commit_id": "b" * 40,
                "change_id": None, "working_commit_id": None,
                "description": "Task", "registration_state": "absent",
                "last_registration": None,
            },
            "expected_materialization": {}, "materialized_owned": None,
            "recovery_owned": None, "planned_context": None, "context_owned": {},
            "removal": {
                "entries_removed": 0, "root_removed": False,
                "parents_removed": 0,
            },
        }

        path = journal_store.save(journal)

        self.assertLessEqual(path.stat().st_size, MAX_JOURNAL_BYTES)
        self.assertEqual(journal_store.read(self.task_id), journal)

    def test_v2_journal_keeps_only_compact_plan_and_sidecar_binding(self) -> None:
        journal_store = CreationJournalStore(self.config)
        repository = Path(self.temp.name).resolve() / "repository"
        workspace = self.config.workspace_root / "repository-aaaaaaaaaaaaaaaa" / "task"
        binding = self.store.write(self.task_id, self.plan_digest, self.facts)
        journal = {
            "contract": "asha.control-creation-journal.v2",
            "task_id": self.task_id, "invocation_id": "c" * 32,
            "phase": "intent", "launch_attempted": False,
            "config": {
                "workspace_root": str(self.config.workspace_root),
                "tasks_dir": str(self.config.tasks_dir),
                "runtime_dir": str(self.config.runtime_dir),
            },
            "repository": {
                "root": str(repository), "identity": "repo:" + "a" * 64,
                "git_root": str(repository), "repo_key": "repository-aaaaaaaaaaaaaaaa",
            },
            "task": {
                "record_path": str(self.config.tasks_dir / f"{self.task_id}.json"),
                "slug": "task", "label": "Task", "digest": None, "failure": None,
            },
            "workspace": {
                "path": str(workspace), "name": "asha-task-11111111",
                "root_fact": None, "created_parents": [],
            },
            "jj": {
                "pinned_operation_id": "a" * 128, "base_commit_id": "b" * 40,
                "change_id": None, "working_commit_id": None,
                "description": "Task", "registration_state": "absent",
                "last_registration": None,
            },
            "materialization_plan": {
                "contract": "asha.control-materialization-plan.v1",
                "base_commit_id": "b" * 40, "digest": self.plan_digest,
                "blob_count": 1100, "directory_count": 0,
                "entry_count": 1100, "total_blob_bytes": 400_000_000,
            },
            "materialization_ownership": {"sidecar": binding, "private": {
                ".jj": {"type": "directory"},
                ".jj/repo": {"type": "file"},
                ".jj/working_copy": {"type": "directory"},
                ".jj/working_copy/checkout": {"type": "file"},
                ".jj/working_copy/tree_state": {"type": "file"},
                ".jj/working_copy/type": {"type": "file"},
            }},
            "recovery_owned": None, "planned_context": None, "context_owned": {},
            "removal": {
                "entries_removed": 0, "root_removed": False,
                "parents_removed": 0,
            },
        }

        path = journal_store.save(journal)

        self.assertLess(path.stat().st_size, 8 * 1024)
        self.assertNotIn("expected_materialization", path.read_text())
        self.assertEqual(journal_store.read(self.task_id), journal)


if __name__ == "__main__":
    unittest.main()
