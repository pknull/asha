"""Source changes invalidate applicability without changing manuscript or history."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'plugins/write/skills/novel-state/scripts/artifact_freshness.py'


class ArtifactFreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'chapter.md').write_text('The latch clicked.\n')
        (self.root / 'voice.md').write_text('---\nversion: 1\n---\nKeep the cadence.\n')
        (self.root / 'report.md').write_text('The latch remains closed.\n')

    def run_cli(self, *args, code=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *args,
                                 '--project', str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def capture(self, output='inputs.json', *extra):
        return self.run_cli('capture', '--manuscript', 'chapter.md', '--source',
                            'voice.md', '--output', output, *extra)

    def record(self, output='receipt.json'):
        return self.run_cli('record', '--snapshot', 'inputs.json', '--artifact',
                            'report.md', '--output', output)

    def prepare(self):
        self.capture()
        self.record()

    # Removing the input or output binding must make this end-to-end check fail.
    def test_receipt_binds_declared_inputs_and_artifact_without_writing_them(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.prepare()
        receipt = json.loads((self.root / 'receipt.json').read_text())
        self.assertEqual(receipt['artifacts'][0]['sha256'],
                         hashlib.sha256(b'The latch remains closed.\n').hexdigest())
        result = self.run_cli('check', '--receipt', 'receipt.json')
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(result['coverage'], 'declared-inputs-only')
        self.assertEqual(len(result['inputs']), 2)
        for name, contents in before.items():
            self.assertEqual((self.root / name).read_bytes(), contents)

    # Hashing raw manuscript bytes instead of the clean projection breaks this.
    def test_editorial_comments_change_without_invalidating_prose_applicability(self):
        self.prepare()
        (self.root / 'chapter.md').write_text(
            '<!-- REVIEW C1 START -->\nThe latch clicked.\n'
            '<!-- REVIEW C1 END\nLLM: Keep this.\nSTATUS: open\n'
            'AUTHOR: Agreed.\n-->\n')
        self.assertEqual(self.run_cli('check', '--receipt', 'receipt.json')['status'], 'unchanged')

    # Ignoring a manuscript input lets an obsolete report appear unchanged.
    def test_prose_change_marks_only_that_input_and_preserves_receipt(self):
        self.prepare()
        receipt = (self.root / 'receipt.json').read_bytes()
        (self.root / 'chapter.md').write_text('The latch opened.\n')
        result = self.run_cli('check', '--receipt', 'receipt.json', code=1)
        self.assertEqual(result['status'], 'changed')
        self.assertEqual([(r['path'], r['status']) for r in result['inputs']],
                         [('chapter.md', 'changed'), ('voice.md', 'unchanged')])
        self.assertEqual((self.root / 'receipt.json').read_bytes(), receipt)

    # Using manuscript projection for authority files would miss this change.
    def test_authority_frontmatter_is_hashed_as_raw_bytes(self):
        self.prepare()
        (self.root / 'voice.md').write_text('---\nversion: 2\n---\nKeep the cadence.\n')
        result = self.run_cli('check', '--receipt', 'receipt.json', code=1)
        self.assertEqual(result['inputs'][1]['status'], 'changed')

    # Omitting the output hash would certify a report replaced after capture.
    def test_modified_report_is_detected(self):
        self.prepare()
        (self.root / 'report.md').write_text('A different conclusion.\n')
        result = self.run_cli('check', '--receipt', 'receipt.json', code=1)
        self.assertEqual(result['artifacts'][0]['status'], 'changed')

    # Reading new hashes at record time would silently bless analysis on old inputs.
    def test_changed_input_during_analysis_refuses_receipt(self):
        self.capture()
        (self.root / 'chapter.md').write_text('The latch opened.\n')
        result = self.run_cli('record', '--snapshot', 'inputs.json', '--artifact',
                              'report.md', '--output', 'receipt.json', code=2)
        self.assertIn('changed', result['error'])
        self.assertFalse((self.root / 'receipt.json').exists())

    # A missing input or receipt cannot establish applicability.
    def test_missing_inputs_and_legacy_receipt_are_unknown(self):
        self.prepare()
        (self.root / 'voice.md').unlink()
        result = self.run_cli('check', '--receipt', 'receipt.json', code=2)
        self.assertEqual(result['inputs'][1]['status'], 'missing')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.run_cli('check', '--receipt', 'legacy.json', code=2)['status'], 'unknown')

    # A permissive clean view could silently omit a broken review thread.
    def test_malformed_manuscript_projection_is_unknown(self):
        self.prepare()
        (self.root / 'chapter.md').write_text('<!-- REVIEW C1 START -->\nThe latch clicked.\n')
        result = self.run_cli('check', '--receipt', 'receipt.json', code=2)
        self.assertEqual(result['inputs'][0]['status'], 'unreadable')
        self.assertEqual(result['status'], 'unknown')

    # Opening outputs with truncation would destroy historical receipts or sources.
    def test_capture_and_record_never_overwrite_existing_paths(self):
        self.prepare()
        for command, args, protected in (
            ('capture', ['--source', 'voice.md'], 'chapter.md'),
            ('capture', ['--source', 'voice.md'], 'inputs.json'),
            ('record', ['--snapshot', 'inputs.json', '--artifact', 'report.md'], 'receipt.json'),
            ('record', ['--snapshot', 'inputs.json', '--artifact', 'report.md'], 'report.md'),
        ):
            before = (self.root / protected).read_bytes()
            self.run_cli(command, *args, '--output', protected, code=2)
            self.assertEqual((self.root / protected).read_bytes(), before)

    # Resolving a current-pointer to its old target permanently would hide retargeting.
    def test_current_pointer_is_checked_at_its_declared_path(self):
        (self.root / 'current.md').symlink_to('chapter.md')
        self.run_cli('capture', '--manuscript', 'current.md', '--output', 'inputs.json')
        self.record()
        (self.root / 'other.md').write_text('A second section.\n')
        (self.root / 'current.md').unlink()
        (self.root / 'current.md').symlink_to('other.md')
        result = self.run_cli('check', '--receipt', 'receipt.json', code=1)
        self.assertEqual(result['inputs'][0]['path'], 'current.md')
        self.assertEqual(result['inputs'][0]['status'], 'changed')

    # An accidental current-pointer loop must stay an unknown applicability result.
    def test_current_pointer_loop_returns_unknown_without_a_traceback(self):
        (self.root / 'current.md').symlink_to('chapter.md')
        self.run_cli('capture', '--manuscript', 'current.md', '--output', 'inputs.json')
        self.record()
        (self.root / 'current.md').unlink()
        (self.root / 'current.md').symlink_to('current.md')
        result = self.run_cli('check', '--receipt', 'receipt.json', code=2)
        self.assertEqual(result['status'], 'unknown')

    # A manuscript can also carry authority metadata; both declarations must survive.
    def test_manuscript_with_authority_metadata_can_bind_both_projections(self):
        (self.root / 'chapter.md').write_text('---\ncanon: proposed\n---\nThe latch clicked.\n')
        self.run_cli('capture', '--manuscript', 'chapter.md', '--source', 'chapter.md', '--output', 'inputs.json')
        self.record()
        (self.root / 'chapter.md').write_text('---\ncanon: accepted\n---\nThe latch clicked.\n')
        result = self.run_cli('check', '--receipt', 'receipt.json', code=1)
        self.assertEqual([row['status'] for row in result['inputs']], ['unchanged', 'changed'])

    # A partial or forged-shaped receipt cannot produce a vacuous unchanged verdict.
    def test_empty_duplicate_and_malformed_receipts_are_unknown(self):
        self.prepare()
        original = json.loads((self.root / 'receipt.json').read_text())
        for update in ({'inputs': []}, {'inputs': original['inputs'] * 2},
                       {'schema': 'unknown'}, {'artifacts': []}):
            (self.root / 'broken.json').write_text(json.dumps({**original, **update}))
            self.assertEqual(self.run_cli('check', '--receipt', 'broken.json', code=2)['status'], 'unknown')

    # Unreadable paths must not be silently dropped from declared coverage.
    def test_non_file_input_refuses_capture_and_duplicate_declaration_refuses(self):
        (self.root / 'directory').mkdir()
        self.run_cli('capture', '--source', 'directory', '--output', 'inputs.json', code=2)
        self.run_cli('capture', '--source', 'voice.md', '--source', 'voice.md',
                     '--output', 'inputs.json', code=2)
        self.assertFalse((self.root / 'inputs.json').exists())


if __name__ == '__main__':
    unittest.main()
