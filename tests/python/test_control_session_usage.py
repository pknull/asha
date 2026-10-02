"""Issue #111: per-worker model, effort and token use from native records.

Fixtures are synthetic; their shapes follow live records read on 2026-10-02
(Claude transcript ``message.usage`` per content block, Codex rollout
``token_count`` cumulative totals restarting on resume).
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from lib.control.session_usage import compact, line, read, remember, tokens_label
from tests.python.test_control_session_closure import ClosureFixture


def claude_turn(message_id, *, input_tokens, read_tokens, write_tokens, output_tokens, thinking=0,
                model='claude-opus-5-5', effort='medium', blocks=2):
    """One assistant API message as Claude writes it: one line per content block."""
    usage = {'input_tokens': input_tokens, 'cache_creation_input_tokens': write_tokens,
             'cache_read_input_tokens': read_tokens, 'output_tokens': output_tokens,
             'output_tokens_details': {'thinking_tokens': thinking}, 'service_tier': 'standard'}
    return [{'type': 'assistant', 'requestId': 'req_' + message_id, 'uuid': f'{message_id}-{n}', 'effort': effort,
             'isSidechain': False,
             'message': {'id': message_id, 'model': model, 'role': 'assistant', 'type': 'message',
                         'content': [{'type': 'text', 'text': 'block'}], 'usage': usage}}
            for n in range(blocks)]


def write_jsonl(path, entries, *, tail=''):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(e) + '\n' for e in entries) + tail)
    return path


def codex_tokens(total, last):
    def usage(values):
        i, cached, out, reasoning = values
        return {'input_tokens': i, 'cached_input_tokens': cached, 'cache_write_input_tokens': 0,
                'output_tokens': out, 'reasoning_output_tokens': reasoning, 'total_tokens': i + out}
    return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': usage(total), 'last_token_usage': usage(last), 'model_context_window': 258400}}}


def codex_context(model='gpt-6-astra', effort='xhigh'):
    return {'type': 'turn_context', 'payload': {'model': model, 'effort': effort, 'cwd': '/project'}}


class UsageFixture(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {'HOME': str(self.home)}

    def claude_path(self, native_id, project='-work-project'):
        return self.home / '.claude' / 'projects' / project / (native_id + '.jsonl')

    def codex_path(self, thread_id, day='2026/10/02'):
        return self.home / '.codex' / 'sessions' / day / f'rollout-2026-10-02T16-05-37-{thread_id}.jsonl'


class ClaudeUsageTests(UsageFixture):
    def test_each_message_counts_once_and_subagents_add_to_the_totals(self):
        main = write_jsonl(self.claude_path('conv-a'), [
            {'type': 'user', 'message': {'role': 'user', 'content': 'assistant please'}},
            *claude_turn('msg_1', input_tokens=2, read_tokens=60000, write_tokens=30000, output_tokens=250, thinking=40),
            *claude_turn('msg_2', input_tokens=5, read_tokens=90000, write_tokens=1000, output_tokens=100, blocks=3)])
        write_jsonl(main.parent / 'conv-a' / 'subagents' / 'agent-x1.jsonl',
                    claude_turn('msg_sub', input_tokens=10, read_tokens=0, write_tokens=5000, output_tokens=50,
                                model='claude-haiku-4-5', effort='low'))
        usage = read('claude', ['conv-a'], env=self.env)
        self.assertEqual(usage['status'], 'known')
        self.assertEqual(usage['tokens'], {'input': 17, 'cache_read': 150000, 'cache_write': 36000,
                                           'output': 400, 'reasoning': 40, 'total': 186417})
        self.assertAlmostEqual(usage['cache_hit_ratio'], round(150000 / 186017, 4))
        # The main conversation names the worker's model and effort, not its subagents.
        self.assertEqual((usage['model'], usage['effort']), ('claude-opus-5-5', 'medium'))
        self.assertEqual(sorted(r['role'] for r in usage['records']), ['main', 'subagent'])

    def test_resumed_session_spans_generations_and_counts_copied_history_once(self):
        write_jsonl(self.claude_path('conv-a'), [
            *claude_turn('msg_1', input_tokens=3, read_tokens=1000, write_tokens=500, output_tokens=10),
            *claude_turn('msg_2', input_tokens=3, read_tokens=2000, write_tokens=0, output_tokens=20)])
        # Generation 2 bound another conversation whose transcript repeats msg_2.
        write_jsonl(self.claude_path('conv-b', project='-work-project-sub'), [
            *claude_turn('msg_2', input_tokens=3, read_tokens=2000, write_tokens=0, output_tokens=20),
            *claude_turn('msg_3', input_tokens=4, read_tokens=3000, write_tokens=100, output_tokens=30,
                         model='claude-sonnet-5-5', effort='high')])
        usage = read('claude', ['conv-a', 'conv-b'], env=self.env)
        self.assertEqual(usage['tokens']['output'], 60)
        self.assertEqual(usage['tokens']['cache_read'], 6000)
        self.assertEqual((usage['model'], usage['effort']), ('claude-sonnet-5-5', 'high'))
        self.assertEqual(usage['missing'], [])

    def test_synthetic_and_torn_lines_are_skipped(self):
        write_jsonl(self.claude_path('conv-a'), [
            *claude_turn('msg_1', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=5),
            *claude_turn('msg_err', input_tokens=0, read_tokens=0, write_tokens=0, output_tokens=0,
                         model='<synthetic>', effort='')], tail='{"type": "assistant", "message": {"id"')
        usage = read('claude', ['conv-a'], env=self.env)
        self.assertEqual((usage['status'], usage['tokens']['output'], usage['model']), ('known', 5, 'claude-opus-5-5'))

    def test_model_and_effort_names_must_be_printable(self):
        write_jsonl(self.claude_path('conv-a'), [
            *claude_turn('msg_1', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=5),
            *claude_turn('msg_2', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=5,
                         model='evil\x1b[2J', effort='x\x07')])
        usage = read('claude', ['conv-a'], env=self.env)
        self.assertEqual((usage['model'], usage['effort'], usage['tokens']['output']), ('claude-opus-5-5', 'medium', 10))

    def test_unchanged_records_reuse_the_previous_reading(self):
        path = write_jsonl(self.claude_path('conv-a'),
                           claude_turn('msg_1', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=5))
        first = read('claude', ['conv-a'], env=self.env)
        with mock.patch('lib.control.session_usage._claude', side_effect=AssertionError('reparsed')):
            self.assertIs(read('claude', ['conv-a'], env=self.env, previous=first), first)
        with path.open('a') as handle:
            handle.writelines(json.dumps(e) + '\n' for e in claude_turn(
                'msg_2', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=7))
        self.assertEqual(read('claude', ['conv-a'], env=self.env, previous=first)['tokens']['output'], 12)


class CodexUsageTests(UsageFixture):
    def test_cumulative_runs_sum_across_a_resume_in_the_same_rollout(self):
        write_jsonl(self.codex_path('thread-1'), [
            {'type': 'session_meta', 'payload': {'id': 'thread-1'}},
            codex_context(effort='high'),
            codex_tokens((1000, 800, 50, 10), (1000, 800, 50, 10)),
            codex_tokens((1000, 800, 50, 10), (1000, 800, 50, 10)),      # repeated event
            codex_tokens((3000, 2600, 120, 30), (2000, 1800, 70, 20)),
            # Resumed (a later generation): the cumulative count restarts.
            codex_context(model='gpt-6-astra-mini', effort='xhigh'),
            codex_tokens((5000, 4000, 40, 5), (5000, 4000, 40, 5)),
            codex_tokens((11000, 9500, 100, 15), (6000, 5500, 60, 10))])
        usage = read('codex', ['thread-1'], env=self.env)
        self.assertEqual(usage['tokens'], {'input': 1900, 'cache_read': 12100, 'cache_write': 0,
                                           'output': 220, 'reasoning': 45, 'total': 14220})
        self.assertEqual(usage['cache_hit_ratio'], round(12100 / 14000, 4))
        self.assertEqual((usage['model'], usage['effort']), ('gpt-6-astra-mini', 'xhigh'))
        self.assertEqual(usage['source'], 'codex-rollout')

    def test_a_restart_larger_than_the_previous_run_still_starts_a_new_run(self):
        # A resumed thread's first request re-sends the whole context, which can exceed the old total.
        write_jsonl(self.codex_path('thread-1'), [
            codex_tokens((1000, 0, 10, 0), (1000, 0, 10, 0)),
            codex_tokens((4000, 3000, 20, 0), (4000, 3000, 20, 0))])
        self.assertEqual(read('codex', ['thread-1'], env=self.env)['tokens']['total'], 5030)

    def test_archived_rollouts_and_codex_home_are_found(self):
        codex_home = self.home / 'alt-codex'
        write_jsonl(codex_home / 'archived_sessions' / 'rollout-2026-09-01T00-00-00-thread-9.jsonl',
                    [codex_context(), codex_tokens((100, 0, 1, 0), (100, 0, 1, 0))])
        usage = read('codex', ['thread-9'], env=dict(self.env, CODEX_HOME=str(codex_home)))
        self.assertEqual(usage['tokens']['total'], 101)


class UnknownUsageTests(UsageFixture):
    def test_missing_records_unsupported_harnesses_and_unsafe_ids_stay_unknown(self):
        cases = (('claude', ['conv-missing'], 'native record not found'),
                 ('codex', [], 'no native conversation observed'),
                 ('claude', ['../escape'], 'native record not found'),
                 ('copilot', ['c-1'], 'copilot native usage records are not checked yet'),
                 ('opencode', ['o-1'], 'opencode native usage records are not checked yet'))
        for harness, ids, reason in cases:
            with self.subTest(harness=harness, ids=ids):
                usage = read(harness, ids, env=self.env)
                self.assertEqual((usage['status'], usage['reason'], usage['tokens']), ('unknown', reason, None))
                self.assertTrue(line(usage).startswith('tokens unknown: '))
                self.assertEqual(tokens_label(usage), '')

    def test_an_unreadable_record_is_unknown_never_an_error(self):
        path = self.claude_path('conv-a')
        path.parent.mkdir(parents=True)
        path.mkdir()     # a directory where the transcript should be
        self.assertEqual(read('claude', ['conv-a'], env=self.env)['status'], 'unknown')
        with mock.patch('lib.control.session_usage._SIZE_LIMIT', 1):
            path.rmdir()
            write_jsonl(path, claude_turn('m', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=1))
            usage = read('claude', ['conv-a'], env=self.env)
        self.assertEqual(usage['status'], 'unknown')
        self.assertIn('exceeds', usage['reason'])

    def test_partly_missing_generations_are_named(self):
        write_jsonl(self.claude_path('conv-b'),
                    claude_turn('m', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=1))
        usage = read('claude', ['conv-a', 'conv-b'], env=self.env)
        self.assertEqual((usage['status'], usage['missing']), ('known', ['conv-a']))
        self.assertIn('1 conversation(s) without a record', line(usage))


class LabelTests(unittest.TestCase):
    def test_compact_counts(self):
        self.assertEqual([compact(n) for n in (0, 999, 1000, 12345, 999999, 1200000, 45000000, 3 * 10 ** 9)],
                         ['0', '999', '1.0k', '12k', '1000k', '1.2M', '45M', '3.0G'])

    def test_malformed_stored_usage_reads_as_unknown(self):
        for stored in ({'status': 'known'}, {'status': 'known', 'tokens': {'total': 'x'}}, 'junk', [],
                       {'status': 'known', 'tokens': {'total': 5}}, {'status': 'unknown', 'reason': 7}):
            with self.subTest(stored=stored):
                self.assertEqual(tokens_label(stored), '')
                self.assertTrue(line(stored).startswith('tokens unknown: '))

    def test_remember_keeps_distinct_ids_newest_last_and_bounded(self):
        self.assertEqual(remember({'native_id': 'a'}, 'b'), ['a', 'b'])
        self.assertEqual(remember({'native_ids': ['a', 'b']}, 'a'), ['a', 'b'])
        self.assertEqual(remember({}, None), [])
        many = remember({'native_ids': [str(n) for n in range(16)]}, 'new')
        self.assertEqual((len(many), many[0], many[-1]), (16, '1', 'new'))


class HubUsageTests(ClosureFixture):
    """Show and close store the usage on the row; list shows the stored value."""

    def transcript(self, native_id, *turns):
        project = self.root / '.claude' / 'projects' / '-encoded-project'
        return write_jsonl(project / (native_id + '.jsonl'), [entry for turn in turns for entry in turn])

    def observe(self, sid, event, **kwargs):
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)), self.ordered_hooks(sid):
            return self.hub.observe(event, **kwargs)

    def test_show_stores_usage_and_effective_selection_from_the_transcript(self):
        sid = self.launch()['session_id']
        self.observe(sid, 'prompt-submitted', native_id='conv-a')
        self.transcript('conv-a', claude_turn('msg_1', input_tokens=2, read_tokens=6000, write_tokens=2000,
                                              output_tokens=300, thinking=20, effort='xhigh'))
        shown = self.hub.show(sid, refresh_usage=True)
        self.assertEqual(shown['usage']['status'], 'known')
        self.assertEqual(shown['usage']['tokens']['total'], 8302)
        self.assertIn('tokens 8.3k: in 2', shown['usage_line'])
        self.assertIn('cache hit 75%', shown['usage_line'])
        self.assertEqual(shown['tokens'], '8.3k')
        # The record states the effective model and effort, so provenance is no longer unknown.
        self.assertEqual(shown['selection']['model'], {'requested': None, 'effective': 'claude-opus-5-5',
                                                       'provenance': 'reported'})
        self.assertEqual(shown['selection']['effort']['effective'], 'xhigh')
        stored = self.hub.get(sid)
        self.assertEqual(stored['usage']['tokens']['total'], 8302)
        self.assertEqual(stored['selection_reported']['source'], 'native-record')

    def test_list_presents_the_stored_usage_without_reading_records(self):
        sid = self.launch()['session_id']
        self.observe(sid, 'prompt-submitted', native_id='conv-a')
        self.transcript('conv-a', claude_turn('msg_1', input_tokens=2, read_tokens=0, write_tokens=0, output_tokens=40))
        self.hub.show(sid, refresh_usage=True)
        with mock.patch('lib.control.session_usage.read', side_effect=AssertionError('list read a record')):
            rows = self.hub.list()['rows']
        self.assertEqual([(r['session_id'], r['tokens']) for r in rows], [(sid, '42')])

    def test_the_show_verb_refreshes_while_other_shows_present_the_stored_value(self):
        sid = self.launch()['session_id']
        self.observe(sid, 'prompt-submitted', native_id='conv-a')
        self.transcript('conv-a', claude_turn('msg_1', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=4))
        self.assertEqual(self.hub.show(sid)['usage_line'], 'tokens unknown: not read')
        from lib.control import hub_cli
        with mock.patch('sys.stdout'):
            self.assertEqual(hub_cli.dispatch(['show', sid, '--json'], env=self.env), 0)
        self.assertEqual(self.hub.show(sid)['tokens'], '5')

    def test_close_records_usage_and_a_missing_record_never_blocks_it(self):
        sid = self.launch()['session_id']
        self.observe(sid, 'prompt-submitted', native_id='conv-gone')
        closed = self.hub.close(sid, force=True)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(self.hub.get(sid)['usage']['reason'], 'native record not found')
        self.assertEqual(closed['usage_line'], 'tokens unknown: native record not found')

    def test_close_survives_a_failing_reader(self):
        sid = self.launch()['session_id']
        self.observe(sid, 'prompt-submitted', native_id='conv-a')
        with mock.patch('lib.control.session_usage.read', side_effect=RuntimeError('boom')):
            closed = self.hub.close(sid, force=True)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['usage']['status'], 'unknown')

    def test_close_records_the_final_usage(self):
        sid = self.launch()['session_id']
        self.observe(sid, 'prompt-submitted', native_id='conv-a')
        self.transcript('conv-a', claude_turn('msg_1', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=9))
        self.hub.close(sid, force=True)
        self.assertEqual(self.hub.get(sid)['usage']['tokens']['total'], 10)

    def test_resumed_session_sums_every_generation(self):
        sid = self.launch()['session_id']
        self.observe(sid, 'prompt-submitted', native_id='conv-a')
        self.transcript('conv-a', claude_turn('msg_1', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=9))
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Continue')
        self.observe(sid, 'session-start', native_id='conv-b', cwd=str(self.project.resolve()))
        self.transcript('conv-b', claude_turn('msg_2', input_tokens=1, read_tokens=0, write_tokens=0, output_tokens=19))
        usage = self.hub.show(sid, refresh_usage=True)['usage']
        self.assertEqual((usage['native_ids'], usage['tokens']['total']), (['conv-a', 'conv-b'], 30))

    def test_other_harnesses_report_unknown_until_their_records_are_checked(self):
        sid = self.launch(harness='copilot')['session_id']
        shown = self.hub.show(sid, refresh_usage=True)
        self.assertEqual(shown['usage']['reason'], 'copilot native usage records are not checked yet')
        self.assertEqual(shown['tokens'], '')

    def test_dashboard_shows_a_tokens_column_only_when_some_usage_is_known(self):
        from lib.control.session_tui import lines
        row = {'session_id': 's-1', 'project_name': 'asha', 'name': 'Job', 'harness': 'claude',
               'activity': 'working', 'lifecycle': 'open', 'generation': 1, 'reason': 'Working',
               'usage': {'status': 'known', 'tokens': {'total': 1234567, 'input': 1, 'cache_read': 0,
                                                       'cache_write': 0, 'output': 0, 'reasoning': 0}}}
        rendered = '\n'.join(lines({'rows': [row], 'summary': 'x'}, width=100, height=30))
        self.assertIn('1.2M', rendered)
        bare = dict(row, usage=None)
        self.assertNotIn('1.2M', '\n'.join(lines({'rows': [bare], 'summary': 'x'}, width=100, height=30)))


if __name__ == '__main__':
    unittest.main()
