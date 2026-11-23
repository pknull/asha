"""Revision preferences cannot replace eligibility, source identity or author choice."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'plugins/write/skills/revision-pass/scripts/revision_compare.py'


class RevisionComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, text in {
            'objective.md': 'Make the action easier to follow; keep the clipped voice.\n',
            'original.md': 'She turned. The door shut.\n',
            'candidate.md': 'She turned toward the door. It shut.\n',
            'alternative.md': 'The door shut as she turned.\n',
        }.items():
            (self.root / name).write_text(text)

    def run_cli(self, *args, code=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *args,
                                 '--project', str(self.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def prepare(self, mode='optional', output='comparison', seed='17'):
        args = ['prepare', '--objective', 'objective.md', '--original', 'original.md',
                '--candidate', 'candidate.md', '--mode', mode, '--output', output, '--seed', seed]
        if mode == 'required':
            args.extend(['--candidate', 'alternative.md'])
        self.run_cli(*args)
        return json.loads((self.root / output / 'packet.json').read_text())

    def votes(self, packet, orders):
        votes = {'packet_id': packet['packet_id'],
                 'rankings': [{'reviewer': f'review-{i}', 'order': order}
                              for i, order in enumerate(orders)]}
        (self.root / 'votes.json').write_text(json.dumps(votes))

    # An implicit incumbent victory or mutation of prose would break this contract.
    def test_original_can_be_preferred_for_optional_change_without_author_acceptance(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        packet = self.prepare()
        original = next(row['id'] for row in packet['entries'] if row['role'] == 'original')
        revised = next(row['id'] for row in packet['entries'] if row['role'] == 'revision')
        self.votes(packet, [[original, revised]])
        result = self.run_cli('summarize', '--packet', 'comparison/packet.json', '--judgments', 'votes.json')
        self.assertEqual(result['status'], 'preference')
        self.assertEqual(result['preferred'], original)
        self.assertIsNone(result['author_decision'])
        for name, contents in before.items():
            self.assertEqual((self.root / name).read_bytes(), contents)

    # A universal unchanged-wins gate would allow a requested correction to be vetoed.
    def test_required_change_excludes_original_and_compares_declared_compliant_revisions(self):
        packet = self.prepare(mode='required')
        original = next(row for row in packet['entries'] if row['role'] == 'original')
        self.assertFalse(original['eligible'])
        eligible = [row['id'] for row in packet['entries'] if row['eligible']]
        self.assertEqual(len(eligible), 2)
        self.votes(packet, [eligible])
        result = self.run_cli('summarize', '--packet', 'comparison/packet.json', '--judgments', 'votes.json')
        self.assertEqual(result['preferred'], eligible[0])
        self.assertEqual(result['eligibility'], 'declared-by-caller')
        self.votes(packet, [[original['id'], *eligible]])
        self.assertEqual(self.run_cli('summarize', '--packet', 'comparison/packet.json',
                                      '--judgments', 'votes.json', code=2)['status'], 'unknown')

    # Dropping malformed judgments would turn incomplete evidence into a false win.
    def test_empty_duplicate_incomplete_and_foreign_rankings_remain_unknown(self):
        packet = self.prepare()
        ids = [row['id'] for row in packet['entries'] if row['eligible']]
        for orders in ([], [[ids[0]]], [[ids[0], ids[0]]], [['foreign', ids[1]]], [ids, [ids[0]]]):
            self.votes(packet, orders)
            result = self.run_cli('summarize', '--packet', 'comparison/packet.json', '--judgments', 'votes.json', code=2)
            self.assertEqual(result['status'], 'unknown')
            self.assertIsNone(result['preferred'])

    # Repeated reviewer identities would count the same supplied opinion twice.
    def test_duplicate_reviewer_and_wrong_packet_identity_are_unknown(self):
        packet = self.prepare()
        ids = [row['id'] for row in packet['entries']]
        self.votes(packet, [ids, ids])
        votes = json.loads((self.root / 'votes.json').read_text())
        votes['rankings'][1]['reviewer'] = votes['rankings'][0]['reviewer']
        (self.root / 'votes.json').write_text(json.dumps(votes))
        self.run_cli('summarize', '--packet', 'comparison/packet.json', '--judgments', 'votes.json', code=2)
        votes['rankings'].pop()
        votes['packet_id'] = '0' * 64
        (self.root / 'votes.json').write_text(json.dumps(votes))
        self.run_cli('summarize', '--packet', 'comparison/packet.json', '--judgments', 'votes.json', code=2)

    # An incumbent tie-breaker would mistake absence of preference for approval.
    def test_tie_reports_no_preference(self):
        packet = self.prepare()
        ids = [row['id'] for row in packet['entries']]
        self.votes(packet, [ids, list(reversed(ids))])
        result = self.run_cli('summarize', '--packet', 'comparison/packet.json', '--judgments', 'votes.json')
        self.assertEqual(result['status'], 'tie')
        self.assertIsNone(result['preferred'])
        self.assertIsNone(result['author_decision'])

    # Unbound objectives or candidates permit comparisons against unlike input.
    def test_changes_to_objective_candidate_original_or_presentation_invalidate_packet(self):
        packet = self.prepare()
        ids = [row['id'] for row in packet['entries']]
        self.votes(packet, [ids])
        for relative in ('objective.md', 'candidate.md', 'original.md', 'comparison/comparison.md'):
            path = self.root / relative
            before = path.read_bytes()
            path.write_bytes(before + b'Changed.\n')
            checked = self.run_cli('check', '--packet', 'comparison/packet.json', code=1)
            self.assertEqual(checked['status'], 'changed')
            result = self.run_cli('summarize', '--packet', 'comparison/packet.json', '--judgments', 'votes.json', code=2)
            self.assertEqual(result['status'], 'unknown')
            path.write_bytes(before)

    # An unbound voice/canon input could change the criteria behind an old judgment.
    def test_declared_authority_is_bound_and_supplied_to_the_review(self):
        (self.root / 'voice.md').write_text('---\nversion: 1\n---\nUse concrete action.\n')
        self.run_cli('prepare', '--objective', 'objective.md', '--original', 'original.md',
                     '--candidate', 'candidate.md', '--source', 'voice.md', '--mode', 'optional',
                     '--output', 'comparison')
        presentation = (self.root / 'comparison/comparison.md').read_text()
        self.assertIn('Use concrete action.', presentation)
        (self.root / 'voice.md').write_text('---\nversion: 2\n---\nUse concrete action.\n')
        result = self.run_cli('check', '--packet', 'comparison/packet.json', code=1)
        self.assertEqual(result['status'], 'changed')

    # Leaking filenames/roles into optional presentation defeats neutral labeling.
    def test_presentation_has_neutral_labels_and_clean_prose(self):
        (self.root / 'candidate.md').write_text('She turned toward the door. It shut.\n<!-- editor: revised -->\n')
        packet = self.prepare()
        presentation = (self.root / 'comparison/comparison.md').read_text()
        self.assertIn('Make the action easier to follow', presentation)
        self.assertIn('She turned toward the door. It shut.', presentation)
        self.assertNotIn('editor: revised', presentation)
        self.assertNotIn('original.md', presentation)
        self.assertNotIn('candidate.md', presentation)
        for entry in packet['entries']:
            self.assertIn(entry['id'], presentation)
        self.assertEqual(self.run_cli('check', '--packet', 'comparison/packet.json')['status'], 'unchanged')

    # A fixed original-first order would survive seeds that must change presentation.
    def test_seeded_presentation_is_reproducible_and_can_reorder(self):
        first = self.prepare(seed='1')
        repeat = self.prepare(output='repeat', seed='1')
        other = self.prepare(output='other', seed='0')
        roles = lambda value: [r['role'] for r in value['entries']]
        self.assertEqual(roles(first), roles(repeat))
        self.assertNotEqual(roles(first), roles(other))

    # Overwriting an existing packet destroys the identity of earlier judgments.
    def test_existing_output_and_insufficient_required_candidates_refused(self):
        self.prepare()
        before = (self.root / 'comparison/packet.json').read_bytes()
        self.run_cli('prepare', '--objective', 'objective.md', '--original', 'original.md',
                     '--candidate', 'candidate.md', '--mode', 'optional', '--output', 'comparison', code=2)
        self.assertEqual((self.root / 'comparison/packet.json').read_bytes(), before)
        self.run_cli('prepare', '--objective', 'objective.md', '--original', 'original.md',
                     '--candidate', 'candidate.md', '--mode', 'required', '--output', 'incomplete', code=2)
        self.assertFalse((self.root / 'incomplete').exists())

    # Editing manifest eligibility without its identity must not change eligible answers.
    def test_changed_manifest_and_missing_sources_are_unknown(self):
        packet = self.prepare()
        manifest = self.root / 'comparison/packet.json'
        packet['mode'] = 'required'
        manifest.write_text(json.dumps(packet))
        self.assertEqual(self.run_cli('check', '--packet', 'comparison/packet.json', code=2)['status'], 'unknown')
        self.prepare(output='second')
        (self.root / 'candidate.md').unlink()
        self.assertEqual(self.run_cli('check', '--packet', 'second/packet.json', code=2)['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
