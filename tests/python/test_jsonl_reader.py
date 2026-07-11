#!/usr/bin/env python3
"""Tests for jsonl_reader — native session transcript -> Asha events.

Pins the reader's contract against committed fixture files so future
host format changes (Claude/Codex/Copilot) fail loudly here instead
of silently losing memory at /save time.

Coverage:
  - All three harness parsers extract expected event kinds + counts.
  - to_synth_events maps to the event_store.py dict schema.
  - Prompt dedup: repeated last-prompt entries collapse to one synth event.
  - Schema drift: unknown line types degrade to kind="meta", no crash.
  - Malformed JSON / non-object lines are skipped with stderr warnings.
"""

import io
import json
import os
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


class OpenCodeStorageTests(unittest.TestCase):
    """Parse a bounded OpenCode directory-storage fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="asha_opencode_")
        self.home = Path(self.tmp.name)
        self.storage = self.home / ".local/share/opencode/storage"
        self.project = self.home / "project"
        self.project.mkdir(parents=True)
        self.sid = "ses_fixture"
        project_id = "project_fixture"
        session_dir = self.storage / "session" / project_id
        session_dir.mkdir(parents=True)
        self.session_path = session_dir / f"{self.sid}.json"
        self.session_path.write_text(json.dumps({
            "id": self.sid,
            "projectID": project_id,
            "directory": str(self.project),
            "time": {"created": 1000, "updated": 4000},
            "title": "Fixture",
        }))

        messages = [
            ("msg_user", "user", 1000),
            ("msg_assistant", "assistant", 2000),
        ]
        for mid, role, created in messages:
            message_dir = self.storage / "message" / self.sid
            message_dir.mkdir(parents=True, exist_ok=True)
            (message_dir / f"{mid}.json").write_text(json.dumps({
                "id": mid, "sessionID": self.sid, "role": role,
                "time": {"created": created},
            }))
            (self.storage / "part" / mid).mkdir(parents=True)

        (self.storage / "part/msg_user/prt_text.json").write_text(json.dumps({
            "id": "prt_text", "sessionID": self.sid, "messageID": "msg_user",
            "type": "text", "text": "Update the configuration",
        }))
        (self.storage / "part/msg_assistant/prt_tool.json").write_text(json.dumps({
            "id": "prt_tool", "sessionID": self.sid, "messageID": "msg_assistant",
            "type": "tool", "tool": "edit",
            "state": {"status": "completed", "input": {
                "filePath": str(self.project / "config.toml"),
                "oldString": "a", "newString": "b",
            }, "time": {"start": 2500}},
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def test_locates_newest_matching_project_session(self):
        with mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home / ".local/share")},
            clear=False,
        ):
            path = jsonl_reader.locate_session_log("opencode", self.project)
        self.assertEqual(path, self.session_path)

    def test_location_honors_xdg_data_home(self):
        custom = self.home / "custom-data"
        target = custom / "opencode/storage"
        target.parent.mkdir(parents=True)
        import shutil
        shutil.copytree(self.storage, target)
        expected = target / "session/project_fixture" / f"{self.sid}.json"
        with mock.patch.dict(
            os.environ,
            {"HOME": str(self.home / "unused-home"), "XDG_DATA_HOME": str(custom)},
            clear=False,
        ):
            path = jsonl_reader.locate_session_log("opencode", self.project)
        self.assertEqual(path, expected)

    def test_resolves_session_identity_from_metadata(self):
        identity = jsonl_reader.resolve_identity(
            self.project, harness="opencode", transcript=self.session_path
        )
        self.assertEqual(identity.session_id, self.sid)
        self.assertEqual(identity.harness, "opencode")

    def test_joins_message_and_part_records(self):
        events = list(jsonl_reader.stream_events(self.session_path, "opencode"))
        prompts = [event for event in events if event.kind == "prompt"]
        tools = [event for event in events if event.kind == "tool_use"]
        self.assertEqual([event.text for event in prompts], ["Update the configuration"])
        self.assertEqual([event.tool for event in tools], ["edit"])

        synth = jsonl_reader.to_synth_events(
            events, project_dir=self.project, session_id=self.sid
        )
        modified = [event for event in synth if event["subtype"] == "file_modified"]
        self.assertEqual(len(modified), 1)
        self.assertEqual(modified[0]["payload"]["file_path"], "config.toml")


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
        self.assertIn("Refactor", prompts[0]["payload"]["detail"])

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
