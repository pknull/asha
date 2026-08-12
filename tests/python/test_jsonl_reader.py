#!/usr/bin/env python3
"""Tests for jsonl_reader — native session transcript -> Asha events.

Pins the reader's contract against committed fixture files so future
host format changes (Claude/Codex/Copilot) fail loudly here instead
of silently losing memory at /save time.

Coverage:
  - All four harness parsers extract expected event kinds + counts.
  - to_synth_events maps to the event_store.py dict schema.
  - Prompt dedup: repeated last-prompt entries collapse to one synth event.
  - Schema drift: unknown line types degrade to kind="meta", no crash.
  - Malformed JSON / non-object lines are skipped with stderr warnings.
"""

import io
import json
import os
import sqlite3
import tempfile
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TOOLS_DIR = REPO_ROOT / "plugins" / "session" / "tools"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

sys.path.insert(0, str(TOOLS_DIR))

import jsonl_reader  # type: ignore[reportMissingImports]  # noqa: E402


class ClaudeParserTests(unittest.TestCase):
    """Parse the Claude session jsonl fixture and verify extracted events."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = FIXTURES_DIR / "claude-session-sample.jsonl"
        cls.events = list(jsonl_reader.stream_events(cls.fixture, "claude"))

    def test_yields_user_prompts_from_last_prompt_lines(self):
        prompts = [e for e in self.events if e.kind == "prompt"]
        # Three last-prompt lines in fixture (one is a duplicate of the first).
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[0].text, "Refactor the auth module to use JWT instead of sessions")
        self.assertEqual(prompts[1].text, "Use PyJWT")
        self.assertEqual(prompts[2].text, "Refactor the auth module to use JWT instead of sessions")

    def test_extracts_assistant_tool_use_blocks(self):
        tool_uses = [e for e in self.events if e.kind == "tool_use"]
        tools = [e.tool for e in tool_uses]
        self.assertEqual(tools, ["Read", "Edit", "Write", "AskUserQuestion", "Task"])

    def test_extracts_assistant_text(self):
        texts = [e for e in self.events if e.kind == "assistant_text"]
        self.assertEqual(len(texts), 1)
        self.assertIn("refactor the auth module", texts[0].text.lower())

    def test_skips_user_lines_with_only_tool_results(self):
        # The fixture's `type=user` line has only a tool_result block —
        # it must NOT produce a "prompt" event.
        for ev in self.events:
            if ev.kind == "prompt":
                self.assertNotIn("file contents", ev.text)

    def test_skips_attachment_and_ai_title_lines(self):
        kinds = {e.kind for e in self.events}
        self.assertNotIn("meta", kinds)  # attachment/ai-title parse cleanly to nothing


class CodexParserTests(unittest.TestCase):
    """Parse the Codex rollout fixture and verify extracted events."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = FIXTURES_DIR / "codex-rollout-sample.jsonl"
        cls.events = list(jsonl_reader.stream_events(cls.fixture, "codex"))

    def test_user_prompts_from_response_item_message(self):
        prompts = [e for e in self.events if e.kind == "prompt"]
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0].text, "Add docstrings to the public API in lib/api.py")
        self.assertEqual(prompts[1].text, "Add a test for the change")

    def test_developer_role_messages_are_not_prompts(self):
        for ev in self.events:
            if ev.kind == "prompt":
                self.assertNotIn("permissions", ev.text)

    def test_function_call_and_local_shell_call_become_tool_use(self):
        tool_uses = [e for e in self.events if e.kind == "tool_use"]
        self.assertGreaterEqual(len(tool_uses), 2)
        names = [e.tool for e in tool_uses]
        self.assertIn("shell", names)
        self.assertIn("local_shell", names)

    def test_function_call_output_is_dropped(self):
        # Codex wraps normal stdout in "Chunk ID:N / Wall time:X / Process
        # exited..." blocks that aren't errors. Dropped to avoid leaking
        # them as event/error in to_synth_events (verified live 2026-05-11).
        results = [e for e in self.events if e.kind == "tool_result"]
        self.assertEqual(len(results), 0)

    def test_custom_tool_call_apply_patch_becomes_tool_use(self):
        # Codex's apply_patch is delivered as a custom_tool_call with the
        # patch text in payload.input. The reader emits it as tool_use so
        # _map_tool_use can later extract file paths from the patch.
        apply_patches = [e for e in self.events
                         if e.kind == "tool_use" and e.tool == "apply_patch"]
        self.assertEqual(len(apply_patches), 2)

    def test_apply_patch_maps_to_file_modified_or_created(self):
        synth = jsonl_reader.to_synth_events(
            self.events, project_dir=Path("/home/test/project"), session_id="x"
        )
        # First patch updates lib/api.py → file_modified
        modified = [s for s in synth if s["subtype"] == "file_modified"
                    and s["metadata"]["tool_name"] == "apply_patch"]
        self.assertEqual(len(modified), 1)
        self.assertEqual(modified[0]["payload"]["file_path"], "lib/api.py")
        # Second patch adds tests/test_api.py → file_created
        created = [s for s in synth if s["subtype"] == "file_created"
                   and s["metadata"]["tool_name"] == "apply_patch"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["payload"]["file_path"], "tests/test_api.py")


class CopilotParserTests(unittest.TestCase):
    """Parse the Copilot events.jsonl fixture and verify extracted events."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = FIXTURES_DIR / "copilot-events-sample.jsonl"
        cls.events = list(jsonl_reader.stream_events(cls.fixture, "copilot"))

    def test_user_message_becomes_prompt(self):
        prompts = [e for e in self.events if e.kind == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0].text, "List the files in the project")

    def test_tool_execution_start_becomes_tool_use(self):
        tool_uses = [e for e in self.events if e.kind == "tool_use"]
        # Five execution_start events in fixture: shell (×2) + create + ask_user + report_intent.
        self.assertEqual(len(tool_uses), 5)
        self.assertEqual(tool_uses[0].tool, "shell")

    def test_tool_execution_complete_emits_only_on_error(self):
        results = [e for e in self.events if e.kind == "tool_result"]
        # Only the error result surfaces; success result is silent.
        self.assertEqual(len(results), 1)
        self.assertIn("Permission denied", results[0].detail)

    def test_skill_invoked_becomes_skill(self):
        skills = [e for e in self.events if e.kind == "skill"]
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].tool, "orchestrate")

    def test_subagent_started_becomes_agent(self):
        agents = [e for e in self.events if e.kind == "agent"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].tool, "general-purpose")

    def test_create_tool_uses_arguments_dict_not_toolargs(self):
        # Real Copilot puts args in data.arguments (dict), with toolArgs null.
        # Fixture-based check that the reader pulls from the right field.
        creates = [e for e in self.events if e.tool == "create"]
        self.assertEqual(len(creates), 1)
        # detail should be the serialized arguments, NOT "{}"
        self.assertIn("newfile.txt", creates[0].detail)

    def test_create_tool_maps_to_file_created(self):
        synth = jsonl_reader.to_synth_events(
            self.events, project_dir=Path("/home/test/project"), session_id="x"
        )
        creates = [s for s in synth if s["subtype"] == "file_created"
                   and s["metadata"]["tool_name"] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["payload"]["file_path"], "newfile.txt")

    def test_ask_user_maps_to_decision_point(self):
        synth = jsonl_reader.to_synth_events(
            self.events, project_dir=Path("/home/test/project"), session_id="x"
        )
        dps = [s for s in synth if s["subtype"] == "decision_point"
               and s["metadata"]["tool_name"] == "ask_user"]
        self.assertEqual(len(dps), 1)
        self.assertIn("license", dps[0]["payload"]["questions"].lower())

    def test_report_intent_is_dropped(self):
        synth = jsonl_reader.to_synth_events(
            self.events, project_dir=Path("/home/test/project"), session_id="x"
        )
        # report_intent is Copilot internal narration with no synth value.
        for s in synth:
            self.assertNotEqual(s["metadata"]["tool_name"], "report_intent")


class OpenCodeParserTests(unittest.TestCase):
    """Read one exact OpenCode session from its shared SQLite store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="asha_opencode_db_")
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.db = root / "opencode.db"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, data TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO session(id, parent_id, directory) VALUES (?, ?, ?)",
            [
                ("ses-root", None, str(self.project)),
                ("ses-child", "ses-root", str(self.project)),
                ("ses-foreign", None, str(root / "other")),
            ],
        )
        conn.executemany(
            "INSERT INTO message(id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            [
                ("msg-user", "ses-root", 1_770_000_000_000, json.dumps({"role": "user"})),
                ("msg-assistant", "ses-root", 1_770_000_000_100, json.dumps({"role": "assistant"})),
                ("msg-child", "ses-child", 1_770_000_000_200, json.dumps({"role": "user"})),
            ],
        )
        conn.executemany(
            "INSERT INTO part(id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
            [
                ("part-user", "msg-user", "ses-root", 1_770_000_000_001,
                 json.dumps({"type": "text", "text": "Update the OpenCode adapter safely"})),
                ("part-synthetic", "msg-user", "ses-root", 1_770_000_000_002,
                 json.dumps({"type": "text", "text": "hidden", "synthetic": True})),
                ("part-tool", "msg-assistant", "ses-root", 1_770_000_000_101,
                 json.dumps({
                     "type": "tool", "tool": "apply_patch",
                     "state": {
                         "status": "completed",
                         "input": {"patchText": "*** Begin Patch\n*** Update File: src/app.py\n*** End Patch"},
                     },
                 })),
                ("part-error", "msg-assistant", "ses-root", 1_770_000_000_102,
                 json.dumps({
                     "type": "tool", "tool": "bash",
                     "state": {"status": "error", "input": {"command": "false"}, "error": "exit 1"},
                 })),
                ("part-child", "msg-child", "ses-child", 1_770_000_000_201,
                 json.dumps({"type": "text", "text": "child-only text"})),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_child_identity_canonicalizes_to_project_root(self):
        ident = jsonl_reader.resolve_identity(
            self.project,
            harness="opencode",
            session_id="ses-child",
            transcript=self.db,
        )
        self.assertEqual(ident.session_id, "ses-root")
        self.assertEqual(ident.transcript_path, self.db)

    def test_streams_only_the_exact_root_session(self):
        events = list(jsonl_reader.stream_events(self.db, "opencode", "ses-root"))
        self.assertEqual([event.text for event in events if event.kind == "prompt"], [
            "Update the OpenCode adapter safely"
        ])
        self.assertNotIn("child-only text", [event.text for event in events])
        self.assertEqual(
            [event.tool for event in events if event.kind == "tool_use"],
            ["apply_patch", "bash"],
        )
        errors = [event for event in events if event.kind == "tool_result"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].detail, "exit 1")

    def test_opencode_apply_patch_maps_to_file_modified(self):
        events = list(jsonl_reader.stream_events(self.db, "opencode", "ses-root"))
        synth = jsonl_reader.to_synth_events(events, self.project, "ses-root")
        edits = [event for event in synth if event["subtype"] == "file_modified"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["payload"]["file_path"], "src/app.py")

    def test_stream_requires_exact_session_id(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(jsonl_reader.IdentityError):
                list(jsonl_reader.stream_events(self.db, "opencode"))

    def test_project_mismatch_is_refused(self):
        with self.assertRaises(jsonl_reader.IdentityError):
            jsonl_reader.resolve_identity(
                self.project,
                harness="opencode",
                session_id="ses-foreign",
                transcript=self.db,
            )


class SchemaDriftTests(unittest.TestCase):
    """Garbage / unknown lines must degrade safely, never crash."""

    def setUp(self):
        self.fixture = FIXTURES_DIR / "garbage-line.jsonl"

    def test_malformed_json_lines_skipped_with_warning(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            events = list(jsonl_reader.stream_events(self.fixture, "copilot"))
        stderr = buf.getvalue()
        self.assertIn("JSON parse error", stderr)
        # Two valid user.message lines survive; unknown event type is silently
        # dropped by the parser (no kind="meta" emission for known-but-unmapped
        # line types — only the streaming layer's error path warns).
        prompts = [e for e in events if e.kind == "prompt"]
        self.assertEqual(len(prompts), 2)

    def test_non_object_json_lines_skipped(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            events = list(jsonl_reader.stream_events(self.fixture, "copilot"))
        # `[1,2,3]` is JSON-valid but not an object — must be skipped.
        for ev in events:
            self.assertNotEqual(ev.text, "[1,2,3]")

    def test_unknown_harness_yields_nothing_with_warning(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            events = list(jsonl_reader.stream_events(self.fixture, "totally-fake"))
        self.assertEqual(events, [])
        self.assertIn("unknown harness", buf.getvalue())


class ToSynthEventsTests(unittest.TestCase):
    """The adapter that produces event_store.py-shaped dicts."""

    def test_synth_dict_shape(self):
        events = list(jsonl_reader.stream_events(
            FIXTURES_DIR / "claude-session-sample.jsonl", "claude"
        ))
        synth = jsonl_reader.to_synth_events(
            events, project_dir=Path("/home/test/project"), session_id="test-sid"
        )
        self.assertGreater(len(synth), 0)
        for s in synth:
            # Match event_store.py contract exactly.
            self.assertIn("id", s)
            self.assertTrue(s["id"].startswith("evt_"))
            self.assertIn("timestamp", s)
            self.assertEqual(s["session_id"], "test-sid")
            self.assertIn(s["type"], {"context", "event"})
            self.assertIn("subtype", s)
            self.assertIsInstance(s["payload"], dict)
            self.assertIn("source", s["metadata"])
            self.assertEqual(s["metadata"]["source"], "transcript")

    def test_prompt_dedup(self):
        # Fixture has 3 last-prompt lines with 2 unique texts:
        #   "Refactor ..." (long — passes 15-char threshold)
        #   "Use PyJWT"   (9 chars, no '?' — DROPPED by threshold)
        # The duplicate "Refactor ..." should collapse to one synth event.
        events = list(jsonl_reader.stream_events(
            FIXTURES_DIR / "claude-session-sample.jsonl", "claude"
        ))
        synth = jsonl_reader.to_synth_events(
            events, project_dir=Path("/home/test/project"), session_id="test-sid"
        )
        prompts = [s for s in synth if s["subtype"] == "decision"]
        # One survives: dedup collapsed the two "Refactor..." entries; "Use PyJWT"
        # was filtered by the >15-char threshold.
        self.assertEqual(len(prompts), 1)
        # PRIVACY contract (2026-07-26): the rebuild stores a size-stub skeleton,
        # never the verbatim prompt — transcripts can hold content the live hooks
        # were gated against (rp-active is gone by rebuild time).
        detail = prompts[0]["payload"]["detail"]
        self.assertRegex(detail, r"^\[user_input: \d+ chars\]$")
        self.assertNotIn("Refactor", detail)

    def test_short_prompts_below_threshold_dropped(self):
        # "Use PyJWT" is 9 chars (< 15) and has no question mark — under the
        # 15-char hook threshold. But the fixture's last-prompt lines that
        # qualify (long, or contain ?) DO survive. Verify the threshold.
        events = [jsonl_reader.Event(
            timestamp="2026-05-10T10:00:00Z", kind="prompt", actor="user",
            text="hi"
        )]
        synth = jsonl_reader.to_synth_events(
            events, project_dir=Path("/home/test/project"), session_id="x"
        )
        self.assertEqual(synth, [])

    def test_tool_use_edit_maps_to_file_modified(self):
        events = list(jsonl_reader.stream_events(
            FIXTURES_DIR / "claude-session-sample.jsonl", "claude"
        ))
        synth = jsonl_reader.to_synth_events(
            events, project_dir=Path("/home/test/project"), session_id="x"
        )
        edits = [s for s in synth if s["subtype"] == "file_modified"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["payload"]["file_path"], "src/auth.py")
        self.assertEqual(edits[0]["metadata"]["tool_name"], "Edit")

    def test_tool_use_write_maps_to_file_created(self):
        events = list(jsonl_reader.stream_events(
            FIXTURES_DIR / "claude-session-sample.jsonl", "claude"
        ))
        synth = jsonl_reader.to_synth_events(
            events, project_dir=Path("/home/test/project"), session_id="x"
        )
        creates = [s for s in synth if s["subtype"] == "file_created"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["payload"]["file_path"], "src/jwt_helpers.py")

    def test_ask_user_question_maps_to_decision_point(self):
        events = list(jsonl_reader.stream_events(
            FIXTURES_DIR / "claude-session-sample.jsonl", "claude"
        ))
        synth = jsonl_reader.to_synth_events(
            events, project_dir=Path("/home/test/project"), session_id="x"
        )
        dps = [s for s in synth if s["subtype"] == "decision_point"]
        self.assertEqual(len(dps), 1)
        self.assertEqual(dps[0]["payload"]["questions"], "Auth strategy")

    def test_task_tool_maps_to_agent_deployed(self):
        events = list(jsonl_reader.stream_events(
            FIXTURES_DIR / "claude-session-sample.jsonl", "claude"
        ))
        synth = jsonl_reader.to_synth_events(
            events, project_dir=Path("/home/test/project"), session_id="x"
        )
        agents = [s for s in synth if s["subtype"] == "agent_deployed"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["payload"]["agent_type"], "reviewer")


class IdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.saved_env = {k: os.environ.get(k) for k in (
            "ASHA_HARNESS", "ASHA_SESSION_ID", "ASHA_TRANSCRIPT_PATH",
            "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
            "CODEX_MANAGED_BY_NPM", "COPILOT_CLI", "COPILOT_SESSION_ID",
            "OPENCODE", "OPENCODE_SESSION_ID", "XDG_DATA_HOME",
        )}
        for k in self.saved_env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_codex_identity_is_harness_scoped_when_claude_vars_also_exist(self):
        os.environ["ASHA_HARNESS"] = "codex"
        os.environ["CODEX_THREAD_ID"] = "test-codex-001"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "wrong-claude"
        ident = jsonl_reader.resolve_identity(
            Path("/home/test/project"),
            transcript=FIXTURES_DIR / "codex-rollout-sample.jsonl",
        )
        self.assertEqual(ident.harness, "codex")
        self.assertEqual(ident.session_id, "test-codex-001")

    def test_claude_identity_is_harness_scoped_when_codex_vars_also_exist(self):
        os.environ["ASHA_HARNESS"] = "claude"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "test-claude-001"
        os.environ["CODEX_THREAD_ID"] = "wrong-codex"
        ident = jsonl_reader.resolve_identity(
            Path("/home/test/project"),
            transcript=FIXTURES_DIR / "claude-session-sample.jsonl",
        )
        self.assertEqual(ident.harness, "claude")
        self.assertEqual(ident.session_id, "test-claude-001")

    def test_codex_session_id_comes_from_payload_not_rollout_stem(self):
        ident = jsonl_reader.resolve_identity(
            Path("/home/test/project"),
            harness="codex",
            transcript=FIXTURES_DIR / "codex-rollout-sample.jsonl",
        )
        self.assertEqual(ident.session_id, "test-codex-001")
        self.assertNotIn("rollout", ident.session_id)

    def test_explicit_transcript_harness_mismatch_fails(self):
        with self.assertRaises(jsonl_reader.IdentityError):
            jsonl_reader.resolve_identity(
                Path("/home/test/project"),
                harness="claude",
                transcript=FIXTURES_DIR / "codex-rollout-sample.jsonl",
            )

    def test_ambiguous_native_markers_without_asha_harness_fail(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "test-claude-001"
        os.environ["CODEX_THREAD_ID"] = "test-codex-001"
        with self.assertRaises(jsonl_reader.IdentityError):
            jsonl_reader.resolve_identity(Path("/home/test/project"))


class LocateSessionLogTests(unittest.TestCase):
    """Path-resolution rules per harness — pure-function unit checks."""

    def test_unknown_harness_returns_none(self):
        self.assertIsNone(
            jsonl_reader.locate_session_log("nonsense", project_dir=Path("/tmp"))
        )

    def test_claude_slug_is_path_with_dashes(self):
        # White-box check on the slug helper.
        slug = jsonl_reader._project_slug_for_claude(Path("/home/pknull/life"))
        self.assertEqual(slug, "-home-pknull-life")


if __name__ == "__main__":
    unittest.main()
