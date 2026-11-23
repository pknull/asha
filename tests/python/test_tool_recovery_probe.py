"""Recovery checks use native event shapes and real temporary artifact bytes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.python.test_memory_continuation_probe import PROBES, exported_run


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / 'run'

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(PROBES / 'tool_recovery.py'), *map(str, args)],
                              capture_output=True, text=True, timeout=20)

    def prepare(self, case='failed-read'):
        result = self.invoke('prepare', '--output', self.run, '--case', case)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return self.run / 'project'

    # A project that cannot pass Control's real resolver has no usable capture path.
    def test_recovery_project_is_accepted_by_real_control_project_resolver(self):
        from lib.control.rooms import resolve_project
        project = self.prepare()
        selected = resolve_project(str(project), env={})
        self.assertEqual(selected['root'], str(project))
        self.assertTrue(selected['project_id'])

    def evidence(self, project, *, failure='FileNotFoundError: source/missing.json', preview=None):
        answer = {'recovery_key': 'quartz', 'count': 37}
        (project / 'result.json').write_text(json.dumps(answer))
        return exported_run(project, (self.run / 'task.md').read_text(), json.dumps(answer),
                            failed=failure, preview=preview)

    def score(self, page, shown):
        (self.run / 'events.json').write_text(json.dumps(page))
        (self.run / 'show.json').write_text(json.dumps(shown))
        result = self.invoke('score', '--run', self.run, '--events', self.run / 'events.json',
                             '--session', self.run / 'show.json')
        return json.loads(result.stdout)

    # Dropping the observed failure barrier incorrectly accepts lucky final bytes.
    def test_failed_read_requires_failure_then_completed_readback_and_artifact(self):
        project = self.prepare()
        page, shown = self.evidence(project)
        self.assertEqual(self.score(page, shown)['task'], 'pass')
        page, shown = self.evidence(project, failure=None)
        self.assertNotEqual(self.score(page, shown)['task'], 'pass')
        page, shown = self.evidence(project, failure='unrelated failure')
        self.assertNotEqual(self.score(page, shown)['task'], 'pass')

    # Accepting an output tail without a completed turn/process must fail here.
    def test_correct_output_cannot_hide_failed_process_or_missing_artifact(self):
        project = self.prepare()
        page, shown = self.evidence(project)
        shown['activity'] = 'failed'
        self.assertEqual(self.score(page, shown)['task'], 'unknown')
        page, shown = self.evidence(project)
        (project / 'result.json').unlink()
        self.assertNotEqual(self.score(page, shown)['task'], 'pass')

    # Reversing trigger/recovery order is not recovery.
    def test_later_failure_and_unfinished_recovery_are_not_success(self):
        project = self.prepare()
        page, shown = self.evidence(project)
        page['events'][5:9] = page['events'][7:9] + page['events'][5:7]
        for index, row in enumerate(page['events'], 1):
            row['sequence'] = index
        self.assertNotEqual(self.score(page, shown)['task'], 'pass')
        page, shown = self.evidence(project)
        page['events'][8]['payload']['exit_code'] = 2
        self.assertNotEqual(self.score(page, shown)['task'], 'pass')

    # Retained-output truncation alone must not stand in for a workload spill.
    def test_spill_case_uses_real_workload_file_and_explicit_preview_trigger(self):
        project = self.prepare('spill-readback')
        produced = subprocess.run([sys.executable, 'workload.py'], cwd=project, capture_output=True, text=True)
        self.assertEqual(produced.returncode, 0, produced.stderr)
        self.assertTrue((project / 'spill.log').is_file())
        page, shown = self.evidence(project, failure=None, preview=produced.stdout)
        self.assertEqual(self.score(page, shown)['task'], 'pass')
        page, shown = self.evidence(project, failure=None)
        page['events'][6]['payload']['output_truncated'] = True
        self.assertNotEqual(self.score(page, shown)['task'], 'pass')

    # Hash-bound workload/source changes must invalidate previously collected evidence.
    def test_changed_recovery_input_is_unknown(self):
        project = self.prepare()
        page, shown = self.evidence(project)
        (project / 'source/data.json').write_text('{"recovery_key":"changed","count":37}')
        self.assertEqual(self.score(page, shown)['task'], 'unknown')


if __name__ == '__main__':
    unittest.main()
