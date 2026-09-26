"""Issue #95: launch-time model and effort selection, recorded as evidence.

Controller fixtures; the native flags are the installed CLIs' own spellings
(claude 2.1.283, codex 0.157.0, copilot 1.0.83, opencode 1.18.29 --help).
"""
import json
import time
import unittest
from pathlib import Path

from lib.control.rooms import room_launch_argv
from lib.control.session_selection import evidence, normalize, record_reported, terminal_flags
from lib.control.store import StoreError
from tests.python.test_control_session_closure import ClosureFixture

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = str(ROOT / "bin/asha")


class NormalizeTests(unittest.TestCase):
    def test_omitted_selection_is_empty(self):
        for harness in ("claude", "codex", "copilot", "opencode"):
            self.assertEqual(normalize(harness, "terminal"), {})

    def test_model_must_be_plain_argv_data(self):
        for bad in ("", " opus", "claude opus", "-m", "--model", "a\x00b", "a\nb", "x" * 257, "é" * 129):
            with self.subTest(model=bad):
                with self.assertRaises(StoreError):
                    normalize("claude", "terminal", model=bad)
        self.assertEqual(normalize("claude", "terminal", model="claude-opus-5-5"), {"model": "claude-opus-5-5"})

    def test_fixed_effort_vocabularies_and_free_form_codex(self):
        self.assertEqual(normalize("claude", "terminal", effort="xhigh"), {"effort": "xhigh"})
        self.assertEqual(normalize("copilot", "terminal", effort="minimal"), {"effort": "minimal"})
        for harness, bad in (("claude", "minimal"), ("claude", "HIGH"), ("copilot", "extreme")):
            with self.subTest(harness=harness, effort=bad):
                with self.assertRaises(StoreError):
                    normalize(harness, "terminal", effort=bad)
        self.assertEqual(normalize("codex", "structured", effort="high"), {"effort": "high"})
        for bad in ("", "high medium", '"x"', "x=y", "-high", "a" * 65):
            with self.subTest(effort=bad):
                with self.assertRaises(StoreError):
                    normalize("codex", "terminal", effort=bad)

    def test_opencode_needs_provider_model_and_has_no_terminal_effort(self):
        self.assertEqual(normalize("opencode", "terminal", model="anthropic/claude-sonnet-5"),
                         {"model": "anthropic/claude-sonnet-5"})
        with self.assertRaisesRegex(StoreError, "provider/model"):
            normalize("opencode", "terminal", model="claude-sonnet-5")
        with self.assertRaisesRegex(StoreError, "effort"):
            normalize("opencode", "terminal", effort="high")


class TerminalArgvTests(unittest.TestCase):
    def test_omitted_selection_is_byte_identical_to_the_old_argv(self):
        for harness, tail in (("claude", ["P"]), ("codex", ["--no-daemon", "P"]), ("copilot", ["--interactive", "P"]),
                              ("opencode", ["--prompt", "P"])):
            with self.subTest(harness=harness):
                self.assertEqual(room_launch_argv(ROOT, harness, "P"), [LAUNCHER, harness, *tail])
                self.assertEqual(room_launch_argv(ROOT, harness, "P", selection={}), [LAUNCHER, harness, *tail])

    def test_flags_precede_the_prompt_for_each_harness(self):
        cases = {
            "claude": ({"model": "opus", "effort": "high"}, ["--model", "opus", "--effort", "high", "P"]),
            "codex": ({"model": "gpt-5.5", "effort": "high"},
                      ["--no-daemon", "-m", "gpt-5.5", "-c", 'model_reasoning_effort="high"', "P"]),
            "copilot": ({"model": "gpt-5.4", "effort": "low"},
                        ["--model", "gpt-5.4", "--effort", "low", "--interactive", "P"]),
            "opencode": ({"model": "anthropic/claude-sonnet-5"}, ["-m", "anthropic/claude-sonnet-5", "--prompt", "P"]),
        }
        for harness, (selection, tail) in cases.items():
            with self.subTest(harness=harness):
                self.assertEqual(room_launch_argv(ROOT, harness, "P", selection=selection), [LAUNCHER, harness, *tail])
                flags = [arg for arg in tail if arg != "--no-daemon"]
                self.assertEqual(terminal_flags(harness, selection), flags[:-1] if harness not in {"copilot", "opencode"}
                                 else flags[:-2])

    def test_resume_reapplies_the_stored_selection(self):
        selection = {"model": "opus", "effort": "max"}
        self.assertEqual(room_launch_argv(ROOT, "claude", "P", selection=selection, resume_id="native-1"),
                         [LAUNCHER, "claude", "--model", "opus", "--effort", "max", "--resume", "native-1", "P"])
        self.assertEqual(room_launch_argv(ROOT, "codex", "P", selection={"model": "gpt-5.5"}, resume_id="thread-1"),
                         [LAUNCHER, "codex", "resume", "--no-daemon", "thread-1", "-m", "gpt-5.5", "P"])
        self.assertEqual(room_launch_argv(ROOT, "codex", "P", resume_id="thread-1"),
                         [LAUNCHER, "codex", "resume", "--no-daemon", "thread-1", "P"])


class StructuredArgvTests(unittest.TestCase):
    def test_claude_structured_flags_only_when_requested(self):
        from lib.control.session_harness import claude_argv
        base = claude_argv(ROOT, "native-1")
        self.assertEqual(claude_argv(ROOT, "native-1", selection={}), base)
        chosen = claude_argv(ROOT, "native-1", selection={"model": "opus", "effort": "low"})
        self.assertEqual(chosen[:len(base) - 2], base[:-2])
        self.assertEqual(chosen[-6:], ["--model", "opus", "--effort", "low", "--resume", "native-1"])

    def test_claude_init_reports_the_effective_model(self):
        from lib.control.session_harness import decode_claude
        record = {"type": "system", "subtype": "init", "session_id": "native-1", "model": "claude-opus-5-5",
                  "cwd": "/tmp", "tools": [], "permissionMode": "default"}
        self.assertEqual(list(decode_claude(record)),
                         [("initialized", {"native_id": "native-1", "model": "claude-opus-5-5"})])
        del record["model"]
        self.assertEqual(list(decode_claude(record)), [("initialized", {"native_id": "native-1"})])


class CodexSelectionTests(unittest.TestCase):
    def protocol(self, **kwargs):
        from lib.control.codex_protocol import CodexProtocol
        return CodexProtocol("Assignment", cwd="/tmp", message_id="asha-message", **kwargs)

    def drain(self, protocol):
        data = bytearray()
        while protocol.outbound:
            chunk = protocol.chunk()
            data.extend(chunk)
            protocol.advance(len(chunk))
        return [json.loads(line) for line in data.splitlines()]

    def notify(self, protocol, method, **params):
        return protocol.feed({"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", **params}})

    def start(self, protocol, **response):
        init = self.drain(protocol)[0]
        protocol.feed({"id": init["id"], "result": {"userAgent": "fixture"}})
        request = self.drain(protocol)[1]
        events = protocol.feed({"id": request["id"], "result": {"thread": {"id": "thread-1"},
            "cwd": "/tmp", "approvalPolicy": "untrusted", "approvalsReviewer": "user",
            "sandbox": {"type": "workspaceWrite", "networkAccess": False, "writableRoots": []}, **response}})
        return request, self.drain(protocol)[0], events

    def test_requested_selection_rides_thread_and_turn_start_only_when_given(self):
        request, turn, _ = self.start(self.protocol())
        self.assertNotIn("model", request["params"])
        self.assertNotIn("effort", turn["params"])
        request, turn, _ = self.start(self.protocol(model="gpt-5.5", effort="high"))
        self.assertEqual(request["params"]["model"], "gpt-5.5")
        self.assertEqual(turn["params"]["effort"], "high")
        self.assertNotIn("model", turn["params"])
        request, _, _ = self.start(self.protocol(native_id="thread-1", model="gpt-5.5"))
        self.assertEqual(request["method"], "thread/resume")
        self.assertEqual(request["params"]["model"], "gpt-5.5")

    def test_thread_response_and_reroute_report_the_effective_selection(self):
        p = self.protocol(model="gpt-5.5")
        _, turn, events = self.start(p, model="gpt-5.5-mini", modelProvider="openai", reasoningEffort="medium")
        self.assertEqual(events, [("initialized", {"native_id": "thread-1", "model": "gpt-5.5-mini",
                                                   "effort": "medium"})])
        p.feed({"id": turn["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}})
        rerouted = self.notify(p, "model/rerouted", fromModel="gpt-5.5-mini", toModel="gpt-5.5-nano",
                               reason="highRiskCyberActivity")
        self.assertEqual(rerouted, [("progress", {"subtype": "model-rerouted", "model": "gpt-5.5-nano",
                                                  "from_model": "gpt-5.5-mini", "reason": "highRiskCyberActivity"})])


    def test_turn_effort_override_is_never_reported_from_the_thread_default(self):
        # QA #95: thread/start resolves the default before turn/start overrides it.
        p = self.protocol(model="gpt-5.5", effort="high")
        _, turn, events = self.start(p, model="gpt-5.5", reasoningEffort="medium")
        self.assertEqual(turn["params"]["effort"], "high")
        self.assertEqual(events, [("initialized", {"native_id": "thread-1", "model": "gpt-5.5"})])
        row = record_reported({"spec": {"model": "gpt-5.5", "effort": "high"}}, events[0][1], source="codex-app-server")
        self.assertEqual(evidence(row)["effort"], {"requested": "high", "effective": None, "provenance": "requested"})


class EvidenceTests(unittest.TestCase):
    def test_provenance_is_reported_requested_or_unknown(self):
        unknown = evidence({"spec": {}})
        self.assertEqual(unknown["model"], {"requested": None, "effective": None, "provenance": "unknown"})
        requested = evidence({"spec": {"model": "opus", "effort": "high"}})
        self.assertEqual(requested["model"], {"requested": "opus", "effective": None, "provenance": "requested"})
        self.assertEqual(requested["effort"]["provenance"], "requested")
        reported = evidence({"spec": {"model": "opus"}, "selection_reported": {"model": "claude-opus-5-5"}})
        self.assertEqual(reported["model"], {"requested": "opus", "effective": "claude-opus-5-5",
                                             "provenance": "reported"})
        self.assertEqual(reported["effort"]["provenance"], "unknown")
        self.assertEqual(evidence({})["effort"]["provenance"], "unknown")


class HubSelectionTests(ClosureFixture):
    def test_invalid_selection_is_refused_before_any_pane_starts(self):
        for kwargs in (dict(effort="minimal"), dict(model="-rf"), dict(harness="opencode", effort="high"),
                       dict(harness="copilot", transport="structured", model="x")):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(StoreError):
                    self.launch(**kwargs)
        self.assertEqual(self.tmux.created, [])

    def test_models_tmux_cannot_transport_are_refused_before_any_record_or_pane(self):
        # QA #95: ';' passed normalize but failed tmux argv validation after the pane existed.
        from lib.control.tmux import _validate_argv
        original = self.tmux.respawn
        self.tmux.respawn = lambda pane, argv: (_validate_argv(argv), original(pane, argv))
        for model in (';', 'gpt-5.5;'):
            with self.subTest(model=model):
                with self.assertRaisesRegex(StoreError, 'model'):
                    self.launch(model=model)
                self.assertEqual(self.tmux.created, [])
                self.assertEqual(self.hub.list()['rows'], [])
        # Accepted punctuation reaches the adapter boundary intact.
        for model in ('a$(b)`c`', 'provider/model@v1:x', 'm;x', "it's"):
            with self.subTest(model=model):
                row = self.launch(model=model)
                argv = self.tmux.respawned[-1][1]
                self.assertEqual(argv[argv.index('--model') + 1], model)
                self.assertEqual(row['lifecycle'], 'open')
                self.hub.stop(row['session_id'])

    def test_selection_is_stored_in_spec_passed_to_the_pane_and_idempotent(self):
        sid = "11111111-2222-4333-8444-555555555555"
        row = self.launch(model="opus", effort="high", session_id=sid)
        self.assertEqual(row['spec']['model'], "opus")
        self.assertEqual(row['spec']['effort'], "high")
        argv = self.tmux.respawned[-1][1]
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(row['selection']['model']['provenance'], "requested")
        self.assertEqual(self.launch(model="opus", effort="high", session_id=sid)['session_id'], sid)
        with self.assertRaisesRegex(StoreError, "another assignment"):
            self.launch(model="sonnet", effort="high", session_id=sid)

    def test_omitted_selection_keeps_the_old_spec_and_argv(self):
        row = self.launch()
        self.assertNotIn("model", row['spec'])
        self.assertNotIn("effort", row['spec'])
        self.assertNotIn("--model", self.tmux.respawned[-1][1])
        self.assertEqual(row['selection']['model']['provenance'], "unknown")

    def test_resume_reapplies_the_requested_selection(self):
        row = self.launch(model="opus", effort="low")
        sid = row['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', native_id='native-abc')
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Continue')
        argv = self.tmux.respawned[-1][1]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_structured_stream_reports_effective_values_into_the_session_row(self):
        from lib.control.session_store import SessionStore
        sid = self.launch(transport='structured', harness='codex', model='gpt-5.5', effort='high')['session_id']
        with SessionStore(self.config) as sessions:
            owner = sessions.claim_owner(sid)
            turn = sessions.claim_turn(sid, owner['generation'])
            sessions.observe(sid, owner['generation'], turn['turn_id'], 'initialized',
                             {'native_id': 'thread-1', 'model': 'gpt-5.5', 'effort': 'high'})
            sessions.observe(sid, owner['generation'], turn['turn_id'], 'progress',
                             {'subtype': 'model-rerouted', 'model': 'gpt-5.5-mini', 'from_model': 'gpt-5.5',
                              'reason': 'highRiskCyberActivity'})
        shown = self.hub.show(sid)['selection']
        self.assertEqual(shown['model'], {'requested': 'gpt-5.5', 'effective': 'gpt-5.5-mini', 'provenance': 'reported'})
        self.assertEqual(shown['effort'], {'requested': 'high', 'effective': 'high', 'provenance': 'reported'})
        reroutes = self.hub.get(sid)['selection_reported']['reroutes']
        self.assertEqual(reroutes[-1]['to_model'], 'gpt-5.5-mini')

    def test_structured_owner_passes_the_selection_and_records_the_init_model(self):
        import os
        import sys
        from lib.control.session_harness import ClaudeTransport
        from lib.control.session_store import SessionStore
        from lib.control.sessions import run_turn
        sid = self.launch(transport='structured', harness='claude', model='opus', effort='max')['session_id']
        script = ("import json,sys\n"
                  "def send(v): print(json.dumps(v),flush=True)\n"
                  "init=json.loads(sys.stdin.readline())\n"
                  "send({'type':'control_response','response':{'subtype':'success','request_id':init['request_id']}})\n"
                  "json.loads(sys.stdin.readline())\n"
                  "send({'type':'system','subtype':'init','session_id':'native-sel','model':'claude-opus-5-5'})\n"
                  "send({'type':'result','subtype':'success','result':'done','session_id':'native-sel'})\n")
        seen = []
        def factory(argv, **kwargs):
            seen.append(list(argv))
            return ClaudeTransport([sys.executable, "-c", script], structured=True, timeout=10, **kwargs)
        with SessionStore(self.config) as sessions:
            owner = sessions.claim_owner(sid)
            turn = sessions.claim_turn(sid, owner['generation'])
            run_turn(sessions, sessions.get(sid), turn, env={**os.environ, **self.env}, root=ROOT,
                     transport_factory=factory)
        argv = seen[0]
        self.assertEqual(argv[argv.index('--model') + 1], 'opus')
        self.assertEqual(argv[argv.index('--effort') + 1], 'max')
        shown = self.hub.show(sid)['selection']
        self.assertEqual(shown['model'], {'requested': 'opus', 'effective': 'claude-opus-5-5', 'provenance': 'reported'})
        self.assertEqual(shown['effort'], {'requested': 'max', 'effective': None, 'provenance': 'requested'})

    def test_reported_selection_is_recorded_from_the_structured_stream(self):
        from lib.control.session_selection import record_reported
        row = self.launch(model="opus")
        sid = row['session_id']
        current = self.hub.get(sid)
        updated = record_reported(current, {"model": "claude-opus-5-5"}, source="claude-init")
        self.assertEqual(updated['selection_reported']['model'], "claude-opus-5-5")
        rerouted = record_reported(updated, {"model": "claude-sonnet-5"}, source="model-rerouted",
                                   reroute={"from_model": "claude-opus-5-5", "reason": "fallback"})
        self.assertEqual(rerouted['selection_reported']['model'], "claude-sonnet-5")
        self.assertEqual(rerouted['selection_reported']['reroutes'][-1]['from_model'], "claude-opus-5-5")
        self.assertEqual(evidence(rerouted)['model']['provenance'], "reported")
        self.assertEqual(evidence(rerouted)['model']['requested'], "opus")


if __name__ == "__main__":
    unittest.main()


from tests.python.test_control_session_experience import ExperienceFixture  # noqa: E402


class SelectionExperienceTests(ExperienceFixture):
    def launch(self, **changes):
        return super().launch(**{"model": "opus", "effort": "high", **changes})

    def envelope(self):
        with self.hub.database() as db, db.transaction() as c:
            return json.loads(c.execute('SELECT envelope FROM hub_experiences ORDER BY created_at DESC LIMIT 1').fetchone()[0])

    def test_envelope_and_guidance_manifest_carry_the_selection_evidence(self):
        self.capture()
        envelope = self.envelope()
        self.assertEqual(envelope['model'], {'requested': 'opus', 'effective': None, 'provenance': 'requested'})
        self.assertEqual(envelope['effort'], {'requested': 'high', 'effective': None, 'provenance': 'requested'})
        self.assertIsNone(envelope['harness_version'])
        from lib.control import session_guidance as guidance
        _block, manifest = guidance.resolve(self.hub, self.hub.get(self.sid), [])
        self.assertEqual(manifest['model']['requested'], 'opus')
        self.assertEqual(manifest['effort']['provenance'], 'requested')

    def test_stats_model_filter_matches_requested_or_effective_with_provenance(self):
        self.capture()
        hit = self.experience.stats(self.pid, model='opus')
        self.assertEqual(hit['completions']['explicit_reports'], 1)
        self.assertEqual(hit['models']['opus']['completions'], 1)
        self.assertEqual(hit['models']['opus']['provenance'], {'requested': 2})
        self.assertEqual(hit['current_sessions'], {'opus': {'sessions': 1, 'provenance': {'requested': 1}}})
        self.assertEqual(self.experience.stats(self.pid, model='sonnet')['completions']['explicit_reports'], 0)
        self.assertEqual(self.experience.stats(self.pid, model='unknown')['completions']['explicit_reports'], 0)

    def test_historical_reports_keep_the_model_retained_in_their_envelope(self):
        # QA #95: a later reroute or resume must not move earlier reports between cohorts.
        self.hub._update(self.sid, selection_reported={'model': 'model-A'})
        self.capture()
        self.assertEqual(self.envelope()['model']['effective'], 'model-A')
        captured_at = time.time()
        self.assertEqual(self.experience.stats(self.pid, model='model-A')['completions']['explicit_reports'], 1)
        self.hub._update(self.sid, selection_reported={'model': 'model-B'})
        for window in ({}, {'until': captured_at}):
            with self.subTest(window=window):
                a = self.experience.stats(self.pid, model='model-A', **window)
                self.assertEqual(a['completions']['explicit_reports'], 1)
                self.assertEqual(a['models']['model-A']['provenance'], {'reported': 2})
                b = self.experience.stats(self.pid, model='model-B', **window)
                self.assertEqual(b['completions']['explicit_reports'], 0)
                self.assertNotIn('model-B', b['models'])
        # The current inventory is labelled as such and follows the live row.
        self.assertEqual(self.experience.stats(self.pid)['current_sessions'],
                         {'model-B': {'sessions': 1, 'provenance': {'reported': 1}}})

    def test_close_requests_keep_the_selection_they_were_requested_under(self):
        self.hub._update(self.sid, selection_reported={'model': 'model-A'})
        self.hub.close(self.sid)
        self.assertEqual(self.hub.get(self.sid)['closure']['selection']['model']['effective'], 'model-A')
        self.hub._update(self.sid, selection_reported={'model': 'model-B'})
        closes = self.experience.stats(self.pid, model='model-A')['capture']['closure_states']
        self.assertEqual(sum(closes.values()), 1)
        self.assertEqual(self.experience.stats(self.pid, model='model-B')['capture']['closure_states'], {})


class UnselectedExperienceTests(ExperienceFixture):
    def test_old_rows_without_a_selection_read_as_unknown(self):
        self.capture()
        stats = self.experience.stats(self.pid, model='unknown')
        self.assertEqual(stats['completions']['explicit_reports'], 1)
        self.assertEqual(stats['models']['unknown']['completions'], 1)
        self.assertEqual(stats['current_sessions'], {'unknown': {'sessions': 1, 'provenance': {'unknown': 1}}})
        self.assertEqual(self.experience.stats(self.pid)['completions']['explicit_reports'], 1)


class TuiLaunchFormTests(unittest.TestCase):
    def test_blank_fields_mean_the_native_default(self):
        from lib.control.session_tui import launch_selection
        self.assertEqual(launch_selection('', '  '), {'model': None, 'effort': None})
        self.assertEqual(launch_selection(' opus ', 'high'), {'model': 'opus', 'effort': 'high'})


class SelectionDisplayTests(unittest.TestCase):
    def test_list_and_detail_mark_requested_only_values(self):
        from lib.control.session_tui import lines
        row = {'session_id': 's-1', 'project_name': 'asha', 'name': 'Job', 'harness': 'claude',
               'activity': 'working', 'lifecycle': 'open', 'generation': 1, 'reason': 'Working',
               'spec': {'model': 'opus', 'effort': 'high'},
               'selection_reported': {'model': 'claude-opus-5-5'}}
        rendered = '\n'.join(lines({'rows': [row], 'summary': 'x'}, width=200, height=30))
        self.assertIn('[claude claude-opus-5-5 effort high (req)]', rendered)
        self.assertIn('model claude-opus-5-5 · effort high (requested)', rendered)
        plain = '\n'.join(lines({'rows': [dict(row, spec={}, selection_reported={})], 'summary': 'x'},
                                width=200, height=30))
        self.assertIn('[claude]', plain)
