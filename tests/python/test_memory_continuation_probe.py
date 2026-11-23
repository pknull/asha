"""Offline contracts for synthetic continuation evidence; no model calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PROBES = ROOT / 'tests/probes'
sys.path.insert(0, str(PROBES))


def exported_run(project, prompt, output, *, failed=None, preview=None):
    """Hand-written shapes emitted by SessionStore.events and Hub.show."""
    rows = []
    def event(kind, payload, turn='turn-one'):
        rows.append({'sequence': len(rows) + 1, 'session_id': 'session-one',
                     'turn_id': turn, 'kind': kind, 'payload': payload,
                     'created_at': 100.0 + len(rows)})
    event('message-retained', {'message_id': 'message-one', 'digest': hashlib.sha256(prompt.encode()).hexdigest()}, None)
    event('session-created', {'harness': 'codex', 'initiative_id': None}, None)
    event('turn-reserved', {'message_id': 'message-one'})
    event('provider-bound', {'pid': 100, 'process_identity': 'fixture'})
    event('initialized', {'native_id': 'native-one'})
    def tool(identity, body, code=0):
        event('tool', {'tool_id': identity, 'name': 'commandExecution', 'status': 'inProgress'})
        event('tool', {'tool_id': identity, 'name': 'commandExecution',
                       'status': 'completed' if code == 0 else 'failed', 'exit_code': code,
                       'output': body, 'output_truncated': False, 'output_section': 'complete'})
    if failed is not None:
        tool('failure', failed, 1)
    if preview is not None:
        tool('preview', preview)
    tool('readback', output)
    event('completed', {'native_id': 'native-one', 'reason': 'completed',
                        'summary': '{"outcome":"complete","artifact":"result.json"}',
                        'summary_truncated': False})
    event('turn-finished', {'outcome': 'completed', 'reason': 'completed'})
    page = {'session_id': 'session-one', 'consumer': None, 'acknowledged_cursor': 0,
            'events': rows, 'complete': True, 'next_event_cursor': len(rows), 'output_gaps': []}
    shown = {'session_id': 'session-one', 'harness': 'codex', 'transport': 'structured',
             'profile': 'worker', 'generation': 1, 'project': str(project), 'prompt': prompt,
             'activity': 'finished', 'pending_messages': 0}
    return page, shown


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / 'run'

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(PROBES / 'memory_continuation.py'), *map(str, args)],
                              capture_output=True, text=True, timeout=20)

    def prepare(self, case='facts'):
        result = self.invoke('prepare', '--output', self.run, '--case', case)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return self.run / 'project'

    def score(self, project, answer, *, change=None):
        (project / 'result.json').write_text(json.dumps(answer))
        page, shown = exported_run(project, (self.run / 'task.md').read_text(), json.dumps(answer))
        if change:
            change(page, shown)
        (self.run / 'events.json').write_text(json.dumps(page))
        (self.run / 'show.json').write_text(json.dumps(shown))
        result = self.invoke('score', '--run', self.run, '--events', self.run / 'events.json',
                             '--session', self.run / 'show.json')
        return json.loads(result.stdout)

    # Omitting real Memory initialization must make this fail.
    def test_prepare_initializes_real_valid_memory_and_keeps_answer_outside_project(self):
        project = self.prepare()
        checked = subprocess.run([sys.executable, str(ROOT / 'plugins/session/tools/memory_v2.py'),
                                  'validate', '--project-dir', str(project)], capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertFalse((project / 'expected.json').exists())
        self.assertFalse((project / 'result.json').exists())
        self.assertLess(len((project / 'Memory/decisions.md').read_bytes()), 2048)
        manifest = json.loads((self.run / 'manifest.json').read_text())
        self.assertEqual(manifest['case'], 'facts')
        self.assertIn('Memory/activeContext.md', manifest['inputs'])
        self.assertIn('Memory/decisions.md', manifest['inputs'])

    # Treating source retrieval as recall failure or claiming startup delivery breaks this.
    def test_fact_success_keeps_native_startup_delivery_unknown(self):
        project = self.prepare()
        answer = {'release_policy': 'manual-approval', 'retry_limit': 2, 'marker': 'copper'}
        score = self.score(project, answer)
        self.assertEqual(score['infrastructure'], 'valid')
        self.assertEqual(score['task'], 'pass')
        self.assertEqual(score['startup_delivery'], 'unknown')
        self.assertFalse(score['production_qualified'])

    # Conflating the broad operational task with a fact recital breaks this.
    def test_operation_uses_live_failure_and_publication_constraint_without_fact_recital(self):
        project = self.prepare('operation')
        score = self.score(project, {'action': 'repair-checksum', 'publish': False, 'source_status': 'failed'})
        self.assertEqual(score['task'], 'pass')
        score = self.score(project, {'action': 'release', 'publish': True, 'source_status': 'ready'})
        self.assertEqual(score['task'], 'fail')

    # Python bool/int equality must not accept a malformed JSON result schema.
    def test_operation_requires_boolean_publication_field(self):
        project = self.prepare('operation')
        score = self.score(project, {'action': 'repair-checksum', 'publish': 0, 'source_status': 'failed'})
        self.assertEqual(score['task'], 'fail')

    # Removing input fingerprints allows stale evaluations to appear valid.
    def test_changed_memory_or_task_or_fixture_identity_is_unknown(self):
        for target in ('project/Memory/decisions.md', 'task.md', 'expected.json'):
            with self.subTest(target=target):
                self.run = Path(self.temp.name) / target.replace('/', '_')
                project = self.prepare()
                path = self.run / target
                path.write_text(path.read_text() + '\nchanged')
                result = self.score(project, {})
                self.assertEqual(result['task'], 'unknown')

    # Trusting final text or incomplete native events must fail these realistic captures.
    def test_invalid_native_evidence_never_passes(self):
        mutations = {
            'timeout-after-native-final': lambda p, s: s.update(activity='failed'),
            'incomplete-page': lambda p, s: p.update(complete=False),
            'foreign-session': lambda p, s: p['events'][5].update(session_id='other'),
            'foreign-turn': lambda p, s: p['events'][5].update(turn_id='other'),
            'duplicate-tool': lambda p, s: p['events'].insert(6, dict(p['events'][5])),
            'no-start': lambda p, s: p['events'].pop(5),
            'no-final': lambda p, s: p['events'].pop(-2),
            'no-process-finish': lambda p, s: p['events'].pop(),
            'truncated-readback': lambda p, s: p['events'][6]['payload'].update(output_truncated=True),
            'unsupported-harness': lambda p, s: s.update(harness='copilot'),
            'stale-assignment': lambda p, s: s.update(prompt='different task'),
        }
        project = self.prepare()
        answer = {'release_policy': 'manual-approval', 'retry_limit': 2, 'marker': 'copper'}
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                score = self.score(project, answer, change=mutation)
                self.assertNotEqual(score['task'], 'pass')

    # A partial claim is not completed work; its reason is not semantically judged.
    def test_partial_claim_is_separate_from_success(self):
        project = self.prepare()
        def partial(page, shown):
            page['events'][-2]['payload']['summary'] = '{"outcome":"partial","reason":"source unavailable"}'
        score = self.score(project, {}, change=partial)
        self.assertEqual(score['task'], 'partial')

    # An earlier source read is not a final artifact check after later writes.
    def test_matching_output_before_later_tool_execution_is_not_final_readback(self):
        project = self.prepare()
        def later_write(page, shown):
            template = dict(page['events'][5])
            start = {**template, 'payload': {'tool_id': 'write-later', 'name': 'fileChange', 'status': 'inProgress'}}
            end = {**template, 'payload': {'tool_id': 'write-later', 'name': 'fileChange', 'status': 'completed'}}
            page['events'][-2:-2] = [start, end]
            for index, event in enumerate(page['events'], 1):
                event['sequence'] = index
            page['next_event_cursor'] = len(page['events'])
        answer = {'release_policy': 'manual-approval', 'retry_limit': 2, 'marker': 'copper'}
        self.assertNotEqual(self.score(project, answer, change=later_write)['task'], 'pass')

    # Re-preparation must never replace a previously evaluated run.
    def test_prepare_refuses_existing_run_and_preserves_its_bytes(self):
        self.prepare()
        before = (self.run / 'manifest.json').read_bytes()
        result = self.invoke('prepare', '--output', self.run, '--case', 'operation')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.run / 'manifest.json').read_bytes(), before)

    # Invalid optional metadata must remain unknown rather than crash the scorer.
    def test_malformed_output_metadata_returns_unknown_json(self):
        project = self.prepare()
        answer = {'release_policy': 'manual-approval', 'retry_limit': 2, 'marker': 'copper'}
        for value in (None, [], 'broken'):
            with self.subTest(value=value):
                score = self.score(project, answer, change=lambda p, s: p['events'][6].update(output=value))
                self.assertEqual(score['task'], 'unknown')

    # Native turn reservations and provider bindings cannot lose their turn scope.
    def test_unscoped_native_reservation_or_provider_binding_is_unknown(self):
        project = self.prepare()
        answer = {'release_policy': 'manual-approval', 'retry_limit': 2, 'marker': 'copper'}
        for kind in ('turn-reserved', 'provider-bound'):
            with self.subTest(kind=kind):
                def unscoped(page, shown):
                    source = next(row for row in page['events'] if row['kind'] == kind)
                    page['events'].insert(-1, {**source, 'turn_id': None})
                    for index, row in enumerate(page['events'], 1):
                        row['sequence'] = index
                    page['next_event_cursor'] = len(page['events'])
                self.assertEqual(self.score(project, answer, change=unscoped)['task'], 'unknown')

    # A real later-turn event suffix must not be credited to the opening task.
    def test_real_control_later_turn_suffix_cannot_reuse_opening_assignment(self):
        from lib.control.config import load_config
        from lib.control.session_store import SessionStore
        import continuation_evidence as evidence
        project = self.prepare()
        prompt = (self.run / 'task.md').read_text()
        answer = {'release_policy': 'manual-approval', 'retry_limit': 2, 'marker': 'copper'}
        (project / 'result.json').write_text(json.dumps(answer))
        template, shown = exported_run(project, prompt, json.dumps(answer))
        root = Path(self.temp.name)
        config = load_config({'HOME': str(root), 'ASHA_HOME': str(root / 'asha'),
                              'ASHA_CONFIG': str(root / 'config.json'), 'XDG_RUNTIME_DIR': str(root)})
        with SessionStore(config, create=True) as store:
            sid = store.create(cwd=str(project), prompt=prompt, harness='codex')['session_id']
            generation = store.claim_owner(sid)['generation']
            shown['session_id'] = sid
            def emit_turn():
                turn = store.claim_turn(sid, generation)['turn_id']
                # Synthetic native process observation; the store/export path is real.
                with store.db.transaction(write=True) as connection:
                    store._event(connection, sid, 'provider-bound', {'pid': 100, 'process_identity': 'fixture'}, turn)
                for row in template['events']:
                    if row['kind'] in {'initialized', 'tool', 'completed'}:
                        store.observe(sid, generation, turn, row['kind'], row['payload'])
                store.finish(sid, generation, turn, success=True, reason='completed')
            emit_turn()
            first = store.events(sid, after=0, limit=1000)
            evidence.native_evidence(first, shown, project, prompt)
            store.enqueue(sid, 'A different later assignment', key='later')
            emit_turn()
            suffix = store.events(sid, after=first['next_event_cursor'], limit=1000)
            self.assertTrue(suffix['complete'])
            self.assertEqual(suffix['acknowledged_cursor'], 0)
            with self.assertRaises(ValueError):
                evidence.native_evidence(suffix, shown, project, prompt)


if __name__ == '__main__':
    unittest.main()
