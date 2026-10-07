"""Session experience capture is retired (subtraction N2, Keeper 2026-10-07).

Capture, review, adoption, dispositions, statistics and their CLI are gone,
and a ``session_experience`` user default is ignored. Records already in a
Control database stay where they are and are neither read nor written. Selected
active guidance (``session_guidance``) is a separate feature and stays.
"""
import contextlib
import importlib.util
import io
import json
import re
import uuid
from pathlib import Path
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from lib.control import hub_cli, session_hub, session_layout

ROOT = Path(__file__).resolve().parents[2]

# The last schema that wrote them, so a store from before N2 can be reproduced.
LEGACY_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS hub_experiences (
       report_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES hub_sessions(session_id),
       generation INTEGER NOT NULL, project_id TEXT NOT NULL, source TEXT NOT NULL, delivery_key TEXT NOT NULL,
       close_request_id TEXT, policy_revision INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
       envelope TEXT NOT NULL, supersedes TEXT REFERENCES hub_experiences(report_id),
       origin_report_id TEXT NOT NULL, created_at REAL NOT NULL,
       UNIQUE(session_id,generation,source,delivery_key))''',
    '''CREATE TABLE IF NOT EXISTS hub_experience_reviews (
       review_id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES hub_experiences(report_id),
       report_digest TEXT NOT NULL, policy_revision INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
       reason TEXT NOT NULL, status TEXT NOT NULL, utility_id TEXT UNIQUE,
       created_at REAL NOT NULL, reserved_at REAL, finished_at REAL, packet_digest TEXT, result TEXT,
       UNIQUE(report_id,policy_revision,attempt))''',
)


class ExperienceRetired(ClosureFixture):
    def setUp(self):
        super().setUp()
        # A user default that used to turn capture on; it is ignored now.
        path = self.root / 'user-config.json'
        path.write_text(json.dumps({'session_experience': {'default_mode': 'capture'}}))
        self.hub.env['ASHA_CONFIG'] = str(path)

    def tables(self):
        with self.hub.database() as db, db.transaction() as c:
            return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(hub_cli, 'Hub', return_value=self.hub):
            try:
                code = hub_cli.dispatch(list(argv), env=self.env)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_worker_and_room_briefs_carry_no_capture_instruction(self):
        for profile in ('worker', 'room'):
            with self.subTest(profile=profile), \
                    mock.patch.object(session_hub, 'open_room', wraps=session_hub.open_room) as opened:
                row = self.launch(profile=profile)
                self.hub.close(row['session_id'], force=True)
                brief = opened.call_args.kwargs['prompt']
                self.assertNotIn('experience', brief.lower())
                self.assertNotIn('16 KiB', brief)

    def test_finished_report_requests_and_stores_no_assessment(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            result = self.hub.report(state='finished', body='Done')
        self.assertEqual(result['report']['state'], 'finished')
        self.assertEqual(result['result'], 'Done')
        self.assertNotIn('experience_request', result)
        self.assertNotIn('capture', result)

    def test_report_and_handoff_accept_and_ignore_experience_flags_for_one_release(self):
        # Workers briefed before N2 still pass these; their reports must land.
        sid = self.launch()['session_id']
        report = self.project / 'report.json'
        report.write_text('{"contract": "asha.session-experience.v1"}')
        with self.acting_as(sid):
            code, out, err = self.cli('report', '--state', 'finished', '--text', 'Done',
                                      '--experience-file', str(report), '--key', str(uuid.uuid4()), '--json')
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['report']['text'], 'Done')
            self.assertNotIn('capture', json.loads(out))
            code, out, err = self.cli('handoff', '--outcome', 'no-durable-update', '--detail', 'Nothing new',
                                      '--experience-ref', str(uuid.uuid4()), '--supersedes', str(uuid.uuid4()),
                                      '--key', 'k', '--json')
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['outcome'], 'no-durable-update')
        self.assertFalse({t for t in self.tables() if t.startswith('hub_experience')})

    def test_experience_verb_is_refused_as_retired(self):
        for argv in (['experience', 'policy', '--read-only', '--project', str(self.project), '--json'],
                     ['experience', 'pending', '--project', str(self.project)], ['experience']):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual((code, out), (2, ''))
                self.assertIn('retired', err)

    def test_structured_result_contract_is_retired(self):
        with mock.patch.object(session_hub.Hub, 'launch', return_value={}) as launch:
            code, _out, err = self.cli('launch', '--project', str(self.project), '--prompt', 'Work',
                                       '--transport', 'structured', '--result-contract', 'asha.session-result.v1')
        self.assertEqual(code, 2)
        self.assertIn('--result-contract', err)
        launch.assert_not_called()

    def test_close_request_asks_for_no_assessment(self):
        sid = self.launch()['session_id']
        closing = self.hub.request_close(sid, wait=60)['closure']
        self.assertNotIn('capture', closing)
        body = self.hub.messages(sid)[0]['body']
        self.assertNotIn('experience', body.lower())
        with self.acting_as(sid):
            receipt = self.hub.handoff(closing['request_id'], outcome='no-durable-update', detail='Nothing new')
        self.assertNotIn('capture', receipt)
        self.assertEqual(receipt['outcome'], 'no-durable-update')

    def test_a_fresh_database_holds_guidance_but_no_experience_tables(self):
        self.launch()
        tables = self.tables()
        self.assertIn('hub_guidance_exposures', tables)
        self.assertFalse({t for t in tables if t.startswith('hub_experience')})

    def test_existing_experience_records_stay_in_place_and_are_not_read(self):
        row = self.launch()
        sid = row['session_id']
        with self.hub.database() as db, db.transaction(write=True) as c:
            for statement in LEGACY_SCHEMA:
                c.execute(statement)
            c.execute('INSERT INTO hub_experiences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      ('legacy', sid, 1, row['project_id'], 'completion', 'k', None, -1, '0' * 64, '{}', '{}',
                       None, 'legacy', 1.0))
            c.execute('INSERT INTO hub_experience_reviews(review_id,report_id,report_digest,policy_revision,'
                      'reason,status,created_at) VALUES(?,?,?,?,?,?,?)',
                      ('review', 'legacy', '0' * 64, -1, 'routine-sample', 'completed', 1.0))
        self.hub._update(sid, capture={'status': 'captured', 'report_id': 'legacy'},
                         experience_request={'key': 'k', 'generation': 1, 'status': 'pending'})
        shown = self.hub.show(sid)
        self.assertNotIn('experience_review', shown)
        self.assertNotIn('capture:', ' '.join(session_layout._meta(shown)))
        self.hub.initialize()
        with self.acting_as(sid):
            self.hub.report(state='finished', body='Done')
            self.hub.observe('session-ended')
        self.hub.close(sid, force=True)
        with self.hub.database() as db, db.transaction() as c:
            self.assertEqual(c.execute('SELECT report_id FROM hub_experiences').fetchall()[0][0], 'legacy')
            self.assertEqual(c.execute('SELECT status FROM hub_experience_reviews').fetchall()[0][0], 'completed')

    def test_experience_modules_and_surfaces_are_gone(self):
        for name in ('session_experience', 'experience_cli', 'experience_review', 'experience_adoption',
                     'experience_stats'):
            self.assertIsNone(importlib.util.find_spec('lib.control.' + name), name)
        from lib.control import projects
        from lib.control.doctor import DEFAULT_PROBES
        self.assertFalse(hasattr(projects, 'experience_default'))
        self.assertNotIn('session-experience', DEFAULT_PROBES)
        self.assertNotIn('experience', (ROOT / 'lib/doctor.sh').read_text())
        self.assertFalse((ROOT / 'docs/session-experience.md').exists())
        registry = json.loads((ROOT / 'harnesses/capabilities.json').read_text())
        for harness, entry in registry['harnesses'].items():
            with self.subTest(harness=harness):
                self.assertNotIn('session-experience', entry['capabilities'])
                self.assertNotIn('experience-review', entry['capabilities'])
                self.assertIn('session-guidance', entry['capabilities'])

    def test_agent_instructions_name_no_experience_command(self):
        # Documents may record the retirement; none may tell an agent to run a retired command.
        commands = re.compile(r'experience (policy|pending|show|unreviewed|packet|review|dispose|list|stats|guidance)\b'
                              r'|--experience-(file|ref) [A-Z]|EXPERIENCE_ENABLED|session-experience\.md')
        for relative in ('plugins/session/commands/save.md', 'plugins/session/skills/operate-control/SKILL.md',
                         'plugins/session/skills/project-memory/SKILL.md', 'docs/session-hub.md',
                         'docs/control.md', 'docs/memory-architecture.md', 'docs/harness-enforcement.md',
                         'README.md', 'INSTALLER.md', 'AGENTS.md', 'CLAUDE.md'):
            with self.subTest(document=relative):
                self.assertIsNone(commands.search((ROOT / relative).read_text()))


if __name__ == '__main__':
    import unittest
    unittest.main()
