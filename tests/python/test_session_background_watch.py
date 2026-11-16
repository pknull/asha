"""#151: a standing watch is not background work a close waits on.

Claude's Stop lists every running task in ``background_tasks``, an Artifact
live watch among them: a ``monitor_ws`` task with no timeout that never ends on
its own, labelled ``monitor``. Room 8b455929 published an artifact, and every
later Stop, including the first after a resume, listed its watch, so Control
read it as idle with one background task and both closes withheld the pointer.

Each test drives the real hook bridge with a Claude Stop payload and applies
what it forwarded to the hub, so the bridge's count and the close gate are
checked together.
"""
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from lib.control import session_closure as closure
from tests.python.test_control_session_closure import FastClose, WHEN

BRIDGE = Path(__file__).resolve().parents[2] / 'plugins/session/hooks/handlers/control-event.sh'
# Entries as Claude Code 2.1.325 sends them: `type` is its friendly label.
WATCH = {"id": "w1", "type": "monitor", "status": "running", "description": "Live updates: Starting Cyphers"}
SHELL = {"id": "b1", "type": "shell", "status": "running", "description": "Run the review",
         "command": "timeout 1200 codex exec review"}
OWED = {
    'agent': {"id": "a1", "type": "subagent", "status": "running", "description": "review", "agent_type": "reviewer"},
    'workflow': {"id": "f1", "type": "workflow", "status": "running", "description": "sweep", "name": "sweep"},
    'mcp task': {"id": "t1", "type": "MCP task", "status": "pending", "description": "export", "server": "s", "tool": "x"},
    'mcp monitor': {"id": "m1", "type": "monitor", "status": "running", "description": "stream", "server": "s", "tool": "x"},
    'monitor tool': {"id": "c1", "type": "shell", "status": "running", "description": "Monitor CI", "command": "gh run watch"},
    'unlabelled': {"id": "u1", "status": "running"},
}


class StandingWatchTests(FastClose):
    def setUp(self):
        super().setUp()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / 'bin').mkdir()
        stub = root / 'bin' / 'asha'
        stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$CONTROL_CAPTURE"\n')
        stub.chmod(0o700)
        self.fake_root, self.capture = root, root / 'argv'

    def bridge_stop(self, sid, tasks):
        """End a turn with a Claude Stop listing ``tasks``: the bridge's argv, applied to the hub."""
        self.capture.unlink(missing_ok=True)
        payload = json.dumps({"session_id": "native-151", "stop_hook_active": False, "background_tasks": tasks})
        env = {'PATH': os.environ['PATH'], 'ASHA_ROOT': str(self.fake_root), 'CONTROL_CAPTURE': str(self.capture),
               'ASHA_HUB_SESSION_ID': sid, 'ASHA_HARNESS': 'claude'}
        answered = subprocess.run(['bash', str(BRIDGE), 'Stop'], input=payload, env=env, capture_output=True,
                                  text=True, timeout=30)
        self.assertEqual(answered.stdout.strip(), '{}')
        argv = self.capture.read_text().split('\n')
        self.assertEqual(argv[:4], ['control', 'session', 'event', '--event'])
        value = lambda flag, cast: cast(argv[argv.index(flag) + 1]) if flag in argv else None
        with self.acting_as(sid):
            return self.hub.observe('turn-stopped', background_tasks=value('--background-tasks', int),
                                    emitted_at=value('--emitted-at', float))

    def prompted(self, sid):
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')

    def assert_one_pointer(self, sid):
        worker, result = self.closing_in_thread(sid, wait=1)
        self.until(lambda: self.tmux.sent)
        worker.join()
        self.assertEqual(len(self.tmux.sent), 1)
        self.assertIn(closure.CLOSE_REQUEST_TOKEN, self.tmux.sent[0][1])
        self.assertIsNotNone(result[0]['closure']['pointer_at'])

    def test_a_room_whose_only_background_work_is_an_artifact_watch_is_idle_and_gets_the_pointer(self):
        sid = self.launch()['session_id']
        self.prompted(sid)
        row = self.bridge_stop(sid, [WATCH])
        self.assertEqual((row['native_activity'], row['background_tasks'], row['reason']), ('idle', None, 'turn-stopped'))
        self.assertIsNone(closure.withheld(row))
        self.assert_one_pointer(sid)

    def test_the_watch_a_resume_rearms_still_lets_the_pointer_through(self):
        sid = self.launch()['session_id']
        self.prompted(sid)
        self.bridge_stop(sid, [WATCH])
        self.hub.stop(sid)
        self.tmux.next_ids = [('%43', '$8')]
        self.assertEqual(self.hub.resume(sid, prompt='Save project Memory')['generation'], 2)
        with self.acting_as(sid):
            self.hub.observe('session-start', source='resume')
        self.prompted(sid)
        # The resumed process re-armed the watch before its first prompt; its first Stop lists it.
        self.assertIsNone(closure.withheld(self.bridge_stop(sid, [WATCH])))
        self.assert_one_pointer(sid)

    def test_a_finished_saved_room_with_only_a_watch_closes_at_once(self):
        """D7: the reporting turn ended at a Stop that lists no work owed."""
        sid = self.launch()['session_id']
        self.prompted(sid)
        with self.acting_as(sid):
            self.hub.report(state='finished', body='Done')
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable changed')
        self.bridge_stop(sid, [WATCH])
        self.assertEqual(self.hub.close(sid, wait=1)['lifecycle'], 'closed')
        self.assertEqual([m for m in self.hub.messages(sid) if m['delivery_key'].startswith('close:')], [])
        self.assertEqual(self.tmux.sent, [])

    def test_a_shell_beside_the_watch_still_withholds_and_counts_only_the_shell(self):
        """#141 stands for the shell; the close names the work it actually waits on."""
        sid = self.launch()['session_id']
        self.save(sid)
        self.prompted(sid)
        row = self.bridge_stop(sid, [SHELL, WATCH])
        self.assertEqual((row['native_activity'], row['background_tasks']), ('working', 1))
        closed = self.hub.close(sid, wait=1)
        self.assertRegex(closed['reason'], rf'^Closed, unsaved: the request never reached it \(idle with 1 background '
                                           rf'task since {WHEN}; last save {WHEN}\); resume to save$')
        self.assertEqual(self.tmux.sent, [])

    def test_work_that_ends_and_wakes_the_session_still_withholds(self):
        """Agents, workflows, MCP tasks and monitors, the Monitor tool's shell and an unlabelled entry count."""
        for name, entry in OWED.items():
            with self.subTest(name):
                sid = self.launch(session_id=str(uuid.uuid4()))['session_id']
                self.prompted(sid)
                row = self.bridge_stop(sid, [entry, WATCH])
                self.assertEqual((row['native_activity'], row['background_tasks']), ('working', 1))
                self.assertRegex(closure.withheld(row), rf'^idle with 1 background task since {WHEN}$')
