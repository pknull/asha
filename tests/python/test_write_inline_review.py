"""Offline synthetic protocol, mutation, integration and exposure regression tests."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / 'plugins/write/skills'
SKILL = SKILLS / 'inline-review'
FIXTURES = ROOT / 'tests/fixtures/inline-review'
sys.path.insert(0, str(SKILL / 'scripts'))
import inline_review as ir


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class InlineReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.md'
        self.prose = 'A *light* moved beside [[the door]].\n\n§\n\nThe latch clicked.\n'
        self.path.write_bytes(self.prose.encode())

    def annotate(self, **kwargs):
        return ir.annotate(self.path, 1, 3, speaker='LLM', text='A local observation.', **kwargs)

    def test_annotate_preserves_prose_paragraphs_and_local_note(self):
        before = self.path.read_bytes()
        doc = self.annotate()
        self.assertFalse(doc.diagnostics)
        self.assertEqual(ir.project(doc.text).text.encode(), before)
        self.assertIn('§\n<!-- REVIEW C1 END\nLLM: A local observation.\n', doc.text)
        self.assertEqual(doc.threads[0].excerpt, ''.join(self.prose.splitlines(True)[:3]))
        self.assertEqual(doc.text, '<!-- REVIEW C1 START -->\n' + ''.join(self.prose.splitlines(True)[:3])
                         + '<!-- REVIEW C1 END\nLLM: A local observation.\nSTATUS: open\n-->\n'
                         + ''.join(self.prose.splitlines(True)[3:]))

    def test_author_edit_keeps_id_and_thread(self):
        self.annotate()
        self.path.write_text(ir.read_source(self.path).replace('A *light* moved', 'A *shadow* moved'))
        doc = ir.parse(ir.read_source(self.path)).strict()
        self.assertEqual(doc.threads[0].id, 'C1')
        self.assertIn('A *shadow* moved', doc.threads[0].excerpt)
        self.assertEqual(doc.threads[0].note, 'LLM: A local observation.\nSTATUS: open\n')

    def test_reply_appends_supplied_speaker_without_changing_prior_lines(self):
        before = self.annotate().text
        doc = ir.reply(self.path, 'C1', speaker='AUTHOR', text='Keep this.\n  A local preference.')
        expected = before.replace('STATUS: open\n-->\n', 'STATUS: open\nAUTHOR: Keep this.\n  A local preference.\n-->\n')
        self.assertEqual(doc.text, expected)
        second = ir.reply(self.path, 'C1', speaker='LLM(line-editor)', text='Acknowledged.')
        self.assertEqual(second.threads[0].status, 'open')
        self.assertIn('AUTHOR: Keep this.\n  A local preference.\nLLM(line-editor): Acknowledged.', second.text)

    def test_status_changes_only_status_line_and_reply_does_not_reopen(self):
        original = self.annotate().text
        doc = ir.set_status(self.path, 'C1', 'dismissed')
        self.assertEqual(doc.text, original.replace('STATUS: open', 'STATUS: dismissed'))
        doc = ir.reply(self.path, 'C1', speaker='LLM', text='Acknowledged.')
        self.assertEqual(doc.threads[0].status, 'dismissed')

    def test_archive_retires_id_and_preserves_prose_and_provenance(self):
        doc = self.annotate()
        original = doc.text
        ir.archive(self.path, 'C1')
        self.assertEqual(ir.read_source(self.path), self.prose)
        record = json.loads(ir.read_source(ir.archive_path(self.path)))['threads'][0]
        self.assertEqual(record['id'], 'C1')
        self.assertEqual(record['line'], 1)
        self.assertEqual(record['file'], self.path.name)
        self.assertEqual(record['excerpt'], doc.threads[0].excerpt)
        self.assertEqual(record['provenance']['source_sha256'], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(record['provenance']['operation'], 'explicit archive')
        self.assertEqual(ir.parse(record['thread']).threads[0].id, 'C1')
        self.assert_refused(lambda: self.annotate(id='C1'))
        self.assertEqual(self.annotate().threads[0].id, 'C2')

    def test_archive_interruption_is_recoverable_and_retains_record(self):
        self.annotate()
        real_atomic = ir._atomic
        def interrupt(path, text, expected):
            if path == self.path:
                raise OSError('synthetic interruption')
            return real_atomic(path, text, expected)
        before = self.path.read_bytes()
        with mock.patch.object(ir, '_atomic', side_effect=interrupt):
            with self.assertRaises(OSError):
                ir.archive(self.path, 'C1')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(ir.archive_path(self.path).exists())
        ir.archive(self.path, 'C1')
        self.assertEqual(ir.read_source(self.path), self.prose)
        self.assertEqual(len(json.loads(ir.read_source(ir.archive_path(self.path)))['threads']), 1)

    def test_archive_interrupted_then_changed_thread_refuses_loss(self):
        self.annotate()
        real_atomic = ir._atomic
        def interrupt(path, text, expected):
            if path == self.path:
                raise OSError('synthetic interruption')
            return real_atomic(path, text, expected)
        with mock.patch.object(ir, '_atomic', side_effect=interrupt):
            with self.assertRaises(OSError):
                ir.archive(self.path, 'C1')
        self.path.write_text(ir.read_source(self.path).replace('A *light*', 'A *shadow*'))
        saved = ir.archive_path(self.path).read_bytes()
        self.assert_refused(lambda: ir.archive(self.path, 'C1'))
        self.assertEqual(ir.archive_path(self.path).read_bytes(), saved)

    def assert_refused(self, operation):
        before = self.path.read_bytes()
        with self.assertRaises(ir.ReviewError) as caught:
            operation()
        self.assertTrue(caught.exception.diagnostics)
        for diagnostic in caught.exception.diagnostics:
            self.assertTrue(diagnostic.file)
            self.assertGreaterEqual(diagnostic.line, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_every_malformed_fixture_diagnoses_file_line_and_all_mutations_refuse(self):
        fixtures = sorted((FIXTURES / 'malformed').glob('*.md'))
        self.assertGreaterEqual(len(fixtures), 18)
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                self.path.write_bytes(fixture.read_bytes())
                diagnostics = ir.check(ir.read_source(self.path), str(self.path))
                self.assertTrue(diagnostics)
                self.assertTrue(all(d.file == str(self.path) and d.line >= 1 for d in diagnostics))
                for operation in (
                    lambda: self.annotate(),
                    lambda: ir.reply(self.path, 'C1', speaker='AUTHOR', text='Supplied.'),
                    lambda: ir.set_status(self.path, 'C1', 'resolved'),
                    lambda: ir.archive(self.path, 'C1'),
                ):
                    self.assert_refused(operation)
                with self.assertRaises(ir.ReviewError):
                    ir.project(ir.read_source(self.path), str(self.path))
                self.assertFalse(ir.archive_path(self.path).exists())

    def test_every_grammar_example_and_example_document_parses_cleanly(self):
        grammar = (SKILL / 'references/grammar.md').read_text()
        fragments = re.findall(r'^```markdown\n(.*?)^```', grammar, re.M | re.S)
        self.assertGreaterEqual(len(fragments), 2)
        for index, fragment in enumerate(fragments):
            self.assertEqual(ir.check(fragment, f'grammar-example-{index}'), [])
        for file in (SKILL / 'examples').glob('*.md'):
            self.assertEqual(ir.check(ir.read_source(file), str(file)), [], str(file))
        expected = (SKILL / 'examples/clean.md').read_bytes()
        for name in ('annotated.md', 'with-reply.md', 'with-followup.md'):
            self.assertEqual(ir.project(ir.read_source(SKILL / 'examples' / name)).text.encode(), expected)

    def test_valid_fixture_round_trips(self):
        for file in (FIXTURES / 'valid').glob('*.md'):
            with self.subTest(file=file.name):
                original = ir.read_source(file) + '\nA final synthetic line.\n'
                self.path.write_text(original)
                self.assertEqual(ir.check(original), [])
                line = len(original.splitlines())
                doc = ir.annotate(self.path, line, line, speaker='LLM', text='Observation.')
                self.assertEqual(ir.project(doc.text).text.encode(), ir.project(original).text.encode())

    def test_fences_ignore_markers_and_refuse_annotation(self):
        for fence in ('```', '~~~~', '   ````'):
            original = fence + '\n<!-- REVIEW C7 START -->\n<!-- REVIEW C7 END\nBad example.\n-->\n' + fence + '\n'
            self.path.write_text(original)
            self.assertEqual(ir.parse(original).threads, [])
            self.assertEqual(ir.check(original), [])
            self.assert_refused(lambda: self.annotate())
        self.path.write_text('```\n<!-- REVIEW C7 START -->\n')
        self.assertEqual(ir.check(ir.read_source(self.path)), [])

    def test_frontmatter_comments_projection_and_source_mapping(self):
        text = '---\ntype: synthetic\n---\n<!-- legacy\nnote -->\nA *light* moved. <!-- inline -->\n\n§\n\n[[The door]] stayed shut.\n'
        projection = ir.project(text, 'synthetic.md')
        self.assertEqual(projection.text, 'A *light* moved. \n\n§\n\n[[The door]] stayed shut.\n')
        self.assertEqual(projection.line_map, [6, 7, 8, 9, 10])
        self.assertEqual(projection.source_line(3), 8)
        with self.assertRaises(ValueError):
            projection.source_line(0)
        self.path.write_text(text)
        self.assert_refused(lambda: self.annotate())
        doc = ir.annotate(self.path, 10, 10, speaker='LLM', text='Observation.')
        self.assertIn('<!-- legacy\nnote -->', doc.text)
        self.assertIn('<!-- inline -->', doc.text)
        self.assertEqual(ir.project(doc.text).text, projection.text)

    def test_inline_multiline_comments_preserve_prose_newlines(self):
        projection = ir.project('Before <!-- note\n\nmore --> after.\n\nNext.\n')
        self.assertEqual(projection.text, 'Before \n after.\n\nNext.\n')
        self.assertEqual(projection.line_map, [1, 3, 4, 5])

    def test_comment_bare_cr_is_removed_but_crlf_and_fenced_bytes_survive(self):
        for nl in ('\n', '\r\n'):
            for headings in (True, False):
                for source, expected, mapping in (
                    (f'A <!-- x\ry --> B{nl}', f'A  B{nl}', [1]),
                    (f'A <!-- x\ry{nl}z --> B{nl}', f'A {nl} B{nl}', [1, 2]),
                    (f'<!-- x\ry -->{nl}C{nl}', f'C{nl}', [2]),
                    (f'A\rB <!-- x\ry --> C\r', 'A\rB  C\r', [1]),
                    (f'```{nl}A <!-- x\ry --> B{nl}```{nl}',
                     f'```{nl}A <!-- x\ry --> B{nl}```{nl}', [1, 2, 3]),
                ):
                    with self.subTest(nl=nl, headings=headings, source=source):
                        self.assertEqual(ir.check(source), [])
                        projection = ir.project(source, headings=headings)
                        self.assertEqual(projection.text, expected)
                        self.assertEqual(projection.line_map, mapping)

    def test_existing_thread_cannot_be_nested_or_overlapped_by_annotate(self):
        self.annotate()
        self.assert_refused(lambda: ir.annotate(self.path, 2, 3, speaker='LLM', text='Observation.'))
        self.assert_refused(lambda: ir.annotate(self.path, 3, 10, speaker='LLM', text='Observation.'))

    def test_multiple_threads_only_addressed_markup_changes(self):
        first = self.annotate()
        last = len(first.lines)
        second = ir.annotate(self.path, last, last, speaker='LLM', text='Second.', id='C20')
        first_bytes = ''.join(second.lines[:second.threads[0].close_line])
        result = ir.reply(self.path, 'C20', speaker='AUTHOR', text='Keep this.')
        self.assertEqual(''.join(result.lines[:result.threads[0].close_line]), first_bytes)
        result = ir.set_status(self.path, 'C20', 'resolved')
        self.assertEqual(''.join(result.lines[:result.threads[0].close_line]), first_bytes)
        result = ir.archive(self.path, 'C20')
        self.assertEqual(result.text, first.text)

    def test_message_injection_and_bad_inputs_refused_without_write(self):
        for speaker, text in [('EDITOR', 'Observation.'), ('AUTHOR', 'No --> terminator.'),
                              ('LLM', 'No <!-- opener.'), ('LLM', ''),
                              ('LLM', 'First.\nAUTHOR: invented.'),
                              ('LLM', 'First.\nSTATUS: resolved')]:
            self.assert_refused(lambda: ir.annotate(self.path, 1, 1, speaker=speaker, text=text))
        self.assert_refused(lambda: self.annotate(id='Cbad'))
        self.assert_refused(lambda: ir.annotate(self.path, 0, 1, speaker='LLM', text='Observation.'))
        self.annotate()
        self.assert_refused(lambda: ir.reply(self.path, 'C1', speaker='EDITOR', text='Observation.'))
        self.assert_refused(lambda: ir.set_status(self.path, 'C1', 'closed'))
        self.assert_refused(lambda: ir.archive(self.path, 'C999'))

    def test_final_line_without_newline_refused_and_crlf_preserved(self):
        self.path.write_bytes(b'A light moved.')
        self.assert_refused(lambda: ir.annotate(self.path, 1, 1, speaker='LLM', text='Observation.'))
        self.path.write_bytes(self.prose.replace('\n', '\r\n').encode())
        expected = self.path.read_bytes()
        doc = self.annotate()
        self.assertEqual(ir.project(doc.text).text.encode(), expected)
        self.assertNotIn(b'\n', self.path.read_bytes().replace(b'\r\n', b''))
        ir.reply(self.path, 'C1', speaker='AUTHOR', text='Keep this.')
        ir.set_status(self.path, 'C1', 'resolved')
        ir.archive(self.path, 'C1')
        self.assertEqual(self.path.read_bytes(), expected)

    def test_corrupt_archive_refuses_all_mutations(self):
        self.annotate()
        ir.archive_path(self.path).write_text('{broken')
        self.assert_refused(lambda: ir.reply(self.path, 'C1', speaker='AUTHOR', text='Keep this.'))
        self.assert_refused(lambda: ir.set_status(self.path, 'C1', 'resolved'))
        self.assert_refused(lambda: ir.archive(self.path, 'C1'))
        self.assert_refused(lambda: self.annotate())

    def test_source_lines_use_only_lf_boundaries(self):
        for text, expected in (
            ('', []), ('\n', ['\n']), ('A', ['A']),
            ('A\n', ['A\n']), ('A\n\n', ['A\n', '\n']),
            ('A\r\nB\nC', ['A\r\n', 'B\n', 'C']),
        ):
            with self.subTest(text=text):
                self.assertEqual(ir.parse(text).lines, expected)
                projection = ir.project(text)
                self.assertEqual(projection.text, text)
                self.assertEqual(projection.line_map, list(range(1, len(expected) + 1)))

    def test_non_lf_separators_preserve_editor_line_numbers(self):
        for separator in ('\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            for nl in ('\n', '\r\n'):
                with self.subTest(separator=separator, newline=nl):
                    first = f'A{separator}B{nl}'
                    source = first + f'# Heading{nl}D{nl}Tail{separator}end'
                    self.assertEqual(ir.parse(source).lines,
                                     [first, f'# Heading{nl}', f'D{nl}', f'Tail{separator}end'])
                    for headings, expected, mapping in (
                        (True, source, [1, 2, 3, 4]),
                        (False, first + f'D{nl}Tail{separator}end', [1, 3, 4]),
                    ):
                        projection = ir.project(source, headings=headings)
                        self.assertEqual(projection.text, expected)
                        self.assertEqual(projection.line_map, mapping)
                        self.assertEqual([projection.source_line(i + 1) for i in range(len(mapping))], mapping)
                    malformed = first + f'prefix <!-- REVIEW C1 START -->{nl}'
                    self.assertEqual([d.line for d in ir.check(malformed)], [2])
                    # A non-LF separator cannot make an embedded marker whole-line.
                    malformed = f'A{separator}<!-- REVIEW C1 START -->{nl}'
                    self.assertEqual([d.line for d in ir.check(malformed)], [1])
                    with self.assertRaises(ir.ReviewError):
                        ir.project(malformed)

    def test_annotate_and_thread_commands_follow_editor_lines_after_separators(self):
        for separator in ('\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            for nl in ('\n', '\r\n'):
                with self.subTest(separator=separator, newline=nl):
                    first = f'A{separator}B{nl}'
                    source = first + f'D{nl}'
                    self.path.write_bytes(source.encode())
                    self.assert_refused(lambda: ir.annotate(self.path, 3, 3, speaker='LLM', text='Outside.'))
                    doc = ir.annotate(self.path, 2, 2, speaker='LLM', text='Observation.')
                    self.assertFalse(doc.diagnostics)
                    self.assertEqual(doc.text, first + nl.join((
                        '<!-- REVIEW C1 START -->', 'D', '<!-- REVIEW C1 END',
                        'LLM: Observation.', 'STATUS: open', '-->', '')))
                    projection = ir.project(doc.text)
                    self.assertEqual(projection.text, source)
                    self.assertEqual(projection.line_map, [1, 3])
                    for command in ('list', 'show'):
                        args = [command, str(self.path)] + (['C1'] if command == 'show' else [])
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            self.assertEqual(ir.main(args + ['--json']), 0)
                        thread = json.loads(output.getvalue())[0]
                        self.assertEqual([thread[k] for k in ('start_line', 'end_line', 'status_line', 'close_line')],
                                         [2, 4, 6, 7])
                        self.assertEqual(thread['excerpt'], f'D{nl}')
                    ir.reply(self.path, 'C1', speaker='AUTHOR', text='Keep this.')
                    ir.set_status(self.path, 'C1', 'resolved')
                    self.assertEqual(ir.project(ir.read_source(self.path)).text, source)

    def test_archive_concurrent_external_edit_is_not_overwritten(self):
        self.annotate()
        original_validate = ir._validate_edit
        external = '{"external": "preserve this"}\n'
        def external_edit(before, text):
            result = original_validate(before, text)
            ir.archive_path(self.path).write_text(external)
            return result
        with mock.patch.object(ir, '_validate_edit', side_effect=external_edit):
            self.assert_refused(lambda: ir.archive(self.path, 'C1'))
        self.assertEqual(ir.archive_path(self.path).read_text(), external)

    def test_legacy_comments_are_opaque_but_live_markup_after_them_is_checked(self):
        self.assertEqual(ir.check('<!-- legacy mentions <!-- REVIEW C1 START -->\n'), [])
        text = '<!-- legacy\n--> prefix <!-- REVIEW C1 START -->\n'
        self.assertTrue(ir.check(text, 'synthetic.md'))

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self):
        before = self.path.read_bytes()
        with mock.patch.object(ir.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.annotate()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_stale_snapshot_refused_and_file_mode_preserved(self):
        self.assert_refused(lambda: ir._atomic(self.path, 'different', b'stale'))
        os.chmod(self.path, 0o640)
        self.annotate()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_symlink_mutations_refused(self):
        link = self.path.parent / 'link.md'
        link.symlink_to(self.path)
        with self.assertRaises(ir.ReviewError):
            ir.annotate(link, 1, 1, speaker='LLM', text='Observation.')
        ir.archive_path(self.path).symlink_to(link)
        self.assert_refused(lambda: self.annotate())

    def test_invariant_rejects_unrelated_prose_change(self):
        before = self.annotate()
        with self.assertRaises(ir.ReviewError):
            ir._validate_edit(before, before.text.replace('The latch clicked.', 'Different.'))

    def test_cli_all_subcommands_and_exit_codes(self):
        script = str(SKILL / 'scripts/inline_review.py')
        def run(*args):
            return subprocess.run([sys.executable, script, *map(str, args)], capture_output=True, text=True)
        self.assertEqual(run('check', self.path).returncode, 0)
        added = run('annotate', self.path, 1, 1, '--speaker', 'LLM', '--text', 'Observation.', '--json')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(json.loads(added.stdout)[0]['id'], 'C1')
        self.assertEqual(json.loads(run('list', self.path, '--json').stdout)[0]['status'], 'open')
        self.assertIn('Observation.', run('show', self.path, 'C1').stdout)
        self.assertEqual(run('reply', self.path, 'C1', '--speaker', 'AUTHOR', '--text', 'Keep this.').returncode, 0)
        self.assertEqual(run('status', self.path, 'C1', 'dismissed').returncode, 0)
        self.assertEqual(run('project', self.path).stdout, self.prose)
        self.assertEqual(json.loads(run('project', self.path, '--json').stdout)['text'], self.prose)
        self.assertEqual(run('archive', self.path, 'C1').returncode, 0)
        self.assertEqual(run('show', self.path, 'C1').returncode, 1)
        self.path.write_text('<!-- REVIEW-GRAMMAR v2 -->\n')
        bad = run('check', self.path, '--json')
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(json.loads(bad.stdout)[0]['line'], 1)
        self.assertEqual(run('check', self.path.parent / 'absent.md').returncode, 2)
        self.assertEqual(run('annotate', self.path.parent / 'absent.md', 1, 1, '--speaker', 'LLM', '--text', 'Observation.').returncode, 2)
        self.path.write_bytes(b'\xff')
        self.assertEqual(run('check', self.path).returncode, 2)
        self.assertEqual(run('annotate', self.path).returncode, 2)


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.md'
        self.original = (SKILL / 'examples/with-followup.md').read_text()
        self.path.write_text(self.original)
        self.clean = ir.project(self.original, str(self.path))
        # Dependencies must be stubbed BEFORE importing any consumers. No
        # requests, pypandoc, network, server, Pandoc, or font install is needed.
        self.requests = types.ModuleType('requests')
        self.requests.post = mock.Mock()
        self.requests.exceptions = types.SimpleNamespace(RequestException=RuntimeError)
        self.pandoc = types.ModuleType('pypandoc')
        self.pandoc.convert_file = mock.Mock()
        patcher = mock.patch.dict(sys.modules, {'requests': self.requests, 'pypandoc': self.pandoc})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lt = load_script('inline_test_lt', SKILLS / 'languagetool/scripts/check_file.py')
        self.style = load_script('inline_test_style', SKILLS / 'style-analyzer/scripts/analyze_style.py')
        self.book = load_script('inline_test_book', SKILLS / 'book-export/scripts/book_maker.py')
        for name in ('inline_test_lt', 'inline_test_style', 'inline_test_book'):
            self.addCleanup(sys.modules.pop, name, None)

    def test_languagetool_default_unchanged_and_clean_transport_maps_findings(self):
        self.requests.post.return_value.json.return_value = {'matches': []}
        result = self.lt.check_file(self.path)
        self.assertEqual(self.requests.post.call_args.kwargs['data']['text'], self.original)
        self.assertEqual(result, {'matches': []})
        offset = len(self.clean.text[:self.clean.text.index('Rain')].encode('utf-16-le')) // 2
        self.requests.post.return_value.json.return_value = {'matches': [{'offset': offset}]}
        result = self.lt.check_file(self.path, manuscript=True)
        sent = self.requests.post.call_args.kwargs['data']['text']
        self.assertEqual(sent, self.clean.text)
        self.assertNotIn('AUTHOR:', sent)
        self.assertEqual(result['source_line_map'], self.clean.line_map)
        self.assertEqual(result['matches'][0]['source_line'], self.clean.source_line(3))
        self.assertEqual(self.path.read_text(), self.original)

    def test_languagetool_utf16_offsets(self):
        self.path.write_text('A light 🌧 moved.\n\n<!-- hidden -->\nThe latch clicked.\n')
        projection = ir.project(self.path.read_text())
        offset = len(projection.text[:projection.text.index('The latch')].encode('utf-16-le')) // 2
        self.requests.post.return_value.json.return_value = {'matches': [{'offset': offset}]}
        self.assertEqual(self.lt.check_file(self.path, manuscript=True)['matches'][0]['source_line'], 4)

    def test_languagetool_manuscript_preserves_editor_newlines_and_default_translation(self):
        for nl in ('\n', '\r\n'):
            with self.subTest(nl=nl):
                source = f'A\rB{nl}<!-- x -->{nl}C{nl}'
                self.path.write_bytes(source.encode())
                projection = ir.project(ir.read_source(self.path))
                self.assertEqual(projection.text, f'A\rB{nl}C{nl}')
                self.assertEqual(projection.line_map, [1, 3])
                offset = len(projection.text[:projection.text.index('C')].encode('utf-16-le')) // 2
                self.requests.post.return_value.json.return_value = {'matches': [{'offset': offset}]}
                result = self.lt.check_file(self.path, manuscript=True)
                self.assertEqual(self.requests.post.call_args.kwargs['data']['text'], projection.text)
                self.assertEqual(result['source_line_map'], [1, 3])
                self.assertEqual(result['matches'][0]['projected_line'], 2)
                self.assertEqual(result['matches'][0]['source_line'], 3)
                self.requests.post.return_value.json.return_value = {'matches': []}
                with mock.patch.object(self.lt, '_load_inline_review', side_effect=AssertionError('plain mode loads parser')):
                    self.assertEqual(self.lt.check_file(self.path), {'matches': []})
                self.assertEqual(self.requests.post.call_args.kwargs['data']['text'], 'A\nB\n<!-- x -->\nC\n')
                self.assertEqual(self.path.read_bytes(), source.encode())

    def test_style_default_and_opt_in_file_directory_cli(self):
        self.assertEqual(self.style.read_input_file(self.path), self.original)
        self.assertEqual(self.style.read_input_file(self.path, manuscript=True), self.clean.text)
        for path in (self.path, self.path.parent):
            for flag in ([], ['--manuscript'], ['--clean']):
                stdout = io.StringIO()
                with mock.patch.object(sys, 'argv', ['analyze_style.py', str(path), '--json', *flag]), contextlib.redirect_stdout(stdout):
                    self.style.main()
                result = json.loads(stdout.getvalue())
                expected = self.clean.text if flag else self.original
                self.assertEqual(result['word_count'], self.style.analyze_text(expected).word_count)
                if flag:
                    self.assertEqual(result['source_line_maps'][str(self.path)], self.clean.line_map)
                else:
                    self.assertNotIn('source_line_maps', result)

    def test_book_export_defaults_and_both_clean_transports_and_cleanup(self):
        calls = []
        def convert(path, target, **kwargs):
            calls.append((str(path), target, Path(path).read_text(), kwargs))
        self.pandoc.convert_file.side_effect = convert
        arguments = (str(self.path), str(self.path.parent / 'output'), 'synthetic.tex', 'synthetic.css', self.temp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.book.convert_markdown_to_formats(*arguments)
        self.assertEqual([c[1] for c in calls], ['pdf', 'epub'])
        self.assertTrue(all(c[0] == str(self.path) and c[2] == self.original for c in calls))
        self.assertTrue(all('--resource-path' not in c[3]['extra_args'] for c in calls))
        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.book.convert_markdown_to_formats(*arguments, manuscript=True)
        self.assertEqual([c[1] for c in calls], ['pdf', 'epub'])
        self.assertTrue(all(c[2] == self.clean.text for c in calls))
        self.assertTrue(all('--resource-path' in c[3]['extra_args'] for c in calls))
        self.assertFalse(Path(calls[0][0]).exists())
        self.assertEqual(self.path.read_text(), self.original)

    def test_book_temp_cleanup_on_converter_failure(self):
        paths = []
        def fail(path, *args, **kwargs):
            paths.append(path)
            raise RuntimeError('synthetic converter failure')
        self.pandoc.convert_file.side_effect = fail
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            self.book.convert_markdown_to_formats(str(self.path), 'output', 'synthetic.tex', 'synthetic.css', self.temp.name, manuscript=True)
        self.assertFalse(Path(paths[0]).exists())
        self.assertEqual(self.path.read_text(), self.original)

    def test_all_consumers_refuse_malformed_clean_input_before_transport(self):
        self.path.write_text('<!-- REVIEW-GRAMMAR v2 -->\n')
        for operation in (lambda: self.lt.check_file(self.path, manuscript=True),
                          lambda: self.style.read_input_file(self.path, manuscript=True),
                          lambda: self.book.convert_markdown_to_formats(self.path, 'output', 'synthetic.tex', 'synthetic.css', self.temp.name, manuscript=True)):
            with self.assertRaises(ir.ReviewError):
                operation()
        self.requests.post.assert_not_called()
        self.pandoc.convert_file.assert_not_called()

    def test_symlinked_consumers_import_same_shared_parser(self):
        for index, source in enumerate(('languagetool/scripts/check_file.py', 'style-analyzer/scripts/analyze_style.py', 'book-export/scripts/book_maker.py')):
            link = self.path.parent / f'installed-{index}.py'
            link.symlink_to(SKILLS / source)
            name = f'inline_test_symlink_{index}'
            try:
                module = load_script(name, link)
                self.assertIs(module.inline_review, ir)
                self.assertEqual(module.inline_review.project(self.original).text, self.clean.text)
            finally:
                sys.modules.pop(name, None)


# Frozen from the exact 8ecc6dde candidate before this recovery delta.
# Fence-aware rebase: only tests/fixtures/inline-review/valid/fenced.md changes;
# all other entries and both grammar fragment hashes remain pinned unchanged.
CANDIDATE_PROJECTIONS = {'plugins/write/skills/inline-review/examples/README.md': '96474296145cbd4ef9c510501008bd44bac3436ea9a455344d7e7dab49f37d59',
 'plugins/write/skills/inline-review/examples/annotated.md': '7132de22dc32e0034641ddabc62c1818ea71b5839cdd0de8abf61092c6042ff7',
 'plugins/write/skills/inline-review/examples/clean.md': '38da74e119ef20ddab1221485b2bd2f5ff18fa0b852f22b8bb054364f9044338',
 'plugins/write/skills/inline-review/examples/with-followup.md': '66d71e6f26cb5eef9e9a407a985befb231ce6f565398c40d5956172f8dde843a',
 'plugins/write/skills/inline-review/examples/with-reply.md': '2a6f002a238fc9013658ea0f0a750e9b0916aa77080fa94f41cf5c7467029fd8',
 'tests/fixtures/inline-review/valid/annotated.md': '7132de22dc32e0034641ddabc62c1818ea71b5839cdd0de8abf61092c6042ff7',
 'tests/fixtures/inline-review/valid/fenced.md': 'be9a4f130179c5b4dc8e10d07bcf63b43fbc2521fcec5a6e4c6ec8f49e142c2d',
 'tests/fixtures/inline-review/valid/legacy.md': '6520b5bed923027e679d729668937ca7509a2aa21c3442ff88979eefc8aa25b1'}
CANDIDATE_GRAMMAR_PROJECTIONS = ['f8ed445160231bb5035f588e00073d6763e486e0f23e13bb44abfdd27e6f67cd',
 '74fef5dd986aac45437ce817be704e84f1692012841474bc77b709011ddec967']
CANDIDATE_CONSUMER_DEFAULTS = {'languagetool': '3403b0304f206d87bdaeda70acd6886211117a50520caed993d915c010d5317b',
 'style-analyzer': '65554fcd85e04b2cbf9366002b8e51de42f0012ac2efbb355793d9b3371a854c',
 'book-export': '99a28f2f07440d20478267780321638c53a806a9925fda118b741662d53c35c6'}

class FenceProjectionTests(unittest.TestCase):
    def test_fenced_fixtures_preserve_bytes_and_source_maps(self):
        cases = {
            'fenced-unclosed-comment.md': [1, 2, 3, 4, 5, 6, 8, 13, 14],
            'fenced-info-comment.md': [1, 2, 3, 4, 5, 6, 8, 13, 14],
            'fenced-comment-examples.md': [*range(1, 13), 14, 19, 20],
        }
        for name, mapping in cases.items():
            original = ir.read_source(FIXTURES / 'valid' / name)
            for ending in ('\n', '\r\n'):
                text = original.replace('\n', ending)
                lines = text.splitlines(True)
                expected = ''.join(lines[n - 1] for n in mapping).replace('<!-- live inline -->', '')
                with self.subTest(name=name, ending=ending):
                    self.assertEqual(ir.check(text), [])
                    self.assertEqual([t.id for t in ir.parse(text).threads], ['C1'])
                    for headings in (True, False):
                        projection = ir.project(text, headings=headings)
                        self.assertEqual(projection.text.encode(), expected.encode())
                        self.assertEqual(projection.line_map, mapping)
                        self.assertEqual([projection.source_line(n + 1) for n in range(len(mapping))], mapping)

    def test_outside_comment_swallows_fence_lines(self):
        text = ir.read_source(FIXTURES / 'valid/comment-spanning-fence.md')
        self.assertEqual(ir.check(text), [])
        for headings in (True, False):
            projection = ir.project(text, headings=headings)
            self.assertEqual(projection.text, 'Before \n after.\n\nLive prose.\n')
            self.assertEqual(projection.line_map, [1, 6, 7, 9])

    def test_parser_fence_boundaries_and_unterminated_blocks(self):
        for indent in range(4):
            for delimiter in ('`', '~'):
                for ending in ('\n', '\r\n'):
                    opener = ' ' * indent + delimiter * 4 + 'markdown <!--'
                    body = ['<!-- REVIEW C1 START -->', '# Héading <!-- closed -->',
                            delimiter * 3, ('~' if delimiter == '`' else '`') * 4,
                            '    ' + delimiter * 4, delimiter * 4 + ' trailing', '', '<!--']
                    closer = ' ' * indent + delimiter * 5 + '\t '
                    for closed in (False, True):
                        with self.subTest(indent=indent, delimiter=delimiter, ending=ending, closed=closed):
                            lines = [opener, *body, *([closer] if closed else [])]
                            # Include an unterminated last line and preserve Unicode/newline bytes.
                            block = ending.join(lines)
                            text = block + (ending + '# Outside <!-- live -->' if closed else '')
                            doc = ir.parse(text).strict()
                            self.assertEqual(doc.fenced_lines, set(range(len(lines))))
                            self.assertEqual(doc.threads, [])
                            for headings in (True, False):
                                projection = ir.project(text, headings=headings)
                                expected = block
                                mapping = list(range(1, len(lines) + 1))
                                if closed:
                                    expected += ending
                                    if headings:
                                        expected += '# Outside '
                                        mapping.append(len(lines) + 1)
                                self.assertEqual(projection.text.encode(), expected.encode())
                                self.assertEqual(projection.line_map, mapping)

    def test_invalid_and_commented_openers_are_not_fences(self):
        for opener in ('    ``` <!-- closed -->', '```bad`info <!-- closed -->',
                       '<!-- prefix -->```', '~~ <!-- closed -->'):
            text = opener + '\n# Outside\n<!-- live -->\nBody.\n'
            doc = ir.parse(text).strict()
            self.assertEqual(doc.fenced_lines, set())
            expected = re.sub(r'<!--.*?-->', '', opener) + '\nBody.\n'
            projection = ir.project(text, headings=False)
            self.assertEqual(projection.text, expected)
            self.assertEqual(projection.line_map, [1, 4])
        for opener in ('    ``` <!--', '```bad`info <!--'):
            text = opener + '\nBody.\n'
            self.assertTrue(ir.check(text))
            for headings in (True, False):
                with self.assertRaises(ir.ReviewError):
                    ir.project(text, headings=headings)

    def test_frontmatter_cannot_start_body_fence_or_comment(self):
        text = ('---\n``` <!--\n---\n<!-- REVIEW-GRAMMAR v1 -->\n'
                '# Outside\n<!-- legacy -->\n``` <!--\n# Example\n```\n')
        doc = ir.parse(text).strict()
        self.assertEqual(doc.fenced_lines, {6, 7, 8})
        for headings in (True, False):
            projection = ir.project(text, headings=headings)
            self.assertEqual(projection.text, ('# Outside\n' if headings else '')
                             + '``` <!--\n# Example\n```\n')
            self.assertEqual(projection.line_map, ([5] if headings else []) + [7, 8, 9])

    def test_cli_fence_projection_and_check(self):
        for name in ('fenced-unclosed-comment.md', 'fenced-info-comment.md',
                     'fenced-comment-examples.md', 'comment-spanning-fence.md'):
            path = FIXTURES / 'valid' / name
            for command in (['check'], ['project'], ['project', '--no-headings']):
                with self.subTest(name=name, command=command):
                    result = subprocess.run(['/bin/python3', str(SKILL / 'scripts/inline_review.py'),
                                             command[0], str(path), *command[1:], '--json'],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if command[0] == 'project':
                        expected = ir.project(ir.read_source(path), str(path), headings=len(command) == 1)
                        self.assertEqual(json.loads(result.stdout), vars(expected))


class HeadingProjectionTests(unittest.TestCase):
    def test_all_atx_levels_indents_and_delimiters(self):
        for indent in range(4):
            for level in range(1, 7):
                for suffix in (' heading', '\theading', ''):
                    for ending in ('\n', '\r\n', ''):
                        with self.subTest(indent=indent, level=level, suffix=suffix, ending=ending):
                            text = ' ' * indent + '#' * level + suffix + ending
                            projection = ir.project(text, headings=False)
                            self.assertEqual(projection.text, '')
                            self.assertEqual(projection.line_map, [])
                            self.assertEqual(ir.project(text).text, text)

    def test_prose_setext_and_indented_nonheadings_survive(self):
        text = '#word\n####### heading\n    # indented\n\t# tab\nTitle\n===\nSubtitle\n---\n'
        self.assertEqual(ir.project(text, headings=False).text, text)
        self.assertEqual(ir.project(text, headings=False).line_map, list(range(1, 9)))

    def test_comment_free_heading_blank_boundaries_and_map(self):
        text = ('Before.\r\n\r\n# Heading <!-- trailing -->\r\n\r\n'
                '<!-- prefix -->##\tHeading\r\n\r\n<!-- only -->\r\nAfter.')
        projection = ir.project(text, 'source.md', headings=False)
        self.assertEqual(projection.text, 'Before.\r\n\r\n\r\n\r\nAfter.')
        self.assertEqual(projection.line_map, [1, 2, 4, 6, 8])
        self.assertEqual(projection.file, 'source.md')
        self.assertEqual([projection.source_line(n) for n in range(1, 6)], [1, 2, 4, 6, 8])

    def test_fences_frontmatter_and_commented_fences(self):
        for fence in ('```', '~~~~', '   ````'):
            text = ('---\n# metadata\n```\n---\n# outside\n'
                    + fence + '\n# example <!-- note -->\n##\n' + fence
                    + '\n<!--\n```\n-->\n# outside again\nBody.\n')
            projection = ir.project(text, headings=False)
            # Fence-aware projection retains this example comment, not just its heading.
            self.assertEqual(projection.text, fence + '\n# example <!-- note -->\n##\n' + fence + '\nBody.\n')
            self.assertEqual(projection.line_map, [6, 7, 8, 9, 14])
        # A shorter fence or a different delimiter cannot end a fenced example.
        text = '````python\n```\n# example\n~~~\n## example\n````\n# outside\n'
        self.assertEqual(ir.project(text, headings=False).line_map, [1, 2, 3, 4, 5, 6])
        self.assertEqual(ir.project('```\n# unclosed example\n', headings=False).text,
                         '```\n# unclosed example\n')
        # Comments do not manufacture source fences; invalid backtick info isn't a fence.
        self.assertEqual(ir.project('<!-- note -->```\n# heading\n', headings=False).text, '```\n')
        self.assertEqual(ir.project('```bad`info\n# heading\n', headings=False).text, '```bad`info\n')

    def test_cli_flag_shape_validation_and_other_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sample.md'
            path.write_text('# Heading\n\nBody.\n')
            def run(*args):
                return subprocess.run(['/bin/python3', str(SKILL / 'scripts/inline_review.py'),
                                       *map(str, args)], capture_output=True, text=True)
            result = run('project', path, '--no-headings', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {'text': '\nBody.\n', 'line_map': [2, 3], 'file': str(path)})
            self.assertEqual(run('project', path, '--no-headings').stdout, '\nBody.\n')
            self.assertEqual(run('project', path).stdout, path.read_text())
            for args in [('check',), ('list',), ('show', 'C1'),
                         ('annotate', '1', '1', '--speaker', 'LLM', '--text', 'Note.'),
                         ('reply', 'C1', '--speaker', 'AUTHOR', '--text', 'Reply.'),
                         ('status', 'C1', 'open'), ('archive', 'C1')]:
                result = run(args[0], path, *args[1:], '--no-headings')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
            for fixture in (FIXTURES / 'malformed').glob('*.md'):
                with self.subTest(fixture=fixture.name):
                    with self.assertRaises(ir.ReviewError):
                        ir.project(ir.read_source(fixture), headings=False)
                    result = run('project', fixture, '--no-headings', '--json')
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, '')
                    self.assertIn(str(fixture), result.stderr)

    def test_default_projection_matches_candidate_bytes(self):
        # SHA-256 of {text,line_map,file} from candidate 8ecc6dde, with file='<text>'.
        expected = CANDIDATE_PROJECTIONS
        for relative, digest in expected.items():
            with self.subTest(file=relative):
                text = ir.read_source(ROOT / relative)
                if relative == 'plugins/write/skills/inline-review/examples/README.md':
                    # The accepted smoke-documentation repair changes this input,
                    # not projection behavior. Retain its exact candidate bytes
                    # and hash; separately verify the repaired source below.
                    text = ir.read_source(FIXTURES / 'baselines/examples-readme.md')
                for kwargs in ({}, {'headings': True}):
                    projection = ir.project(text, **kwargs)
                    payload = json.dumps(vars(projection), sort_keys=True).encode()
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
        grammar = (SKILL / 'references/grammar.md').read_text()
        for fragment, digest in zip(re.findall(r'^```markdown\n(.*?)^```', grammar, re.M | re.S),
                                    CANDIDATE_GRAMMAR_PROJECTIONS):
            self.assertEqual(hashlib.sha256(json.dumps(vars(ir.project(fragment)), sort_keys=True).encode()).hexdigest(), digest)

    def test_repaired_readme_changes_only_smoke_statement_and_projects_unchanged(self):
        baseline = ir.read_source(FIXTURES / 'baselines/examples-readme.md')
        expected = baseline.replace(
            'No live Claude or\nCodex behavioral smoke was performed;',
            'No live Claude,\nCodex or Copilot behavioral smoke was run;')
        self.assertNotEqual(expected, baseline)
        current = ir.read_source(SKILL / 'examples/README.md')
        self.assertEqual(current, expected)
        projection = ir.project(current)
        self.assertEqual(projection.text, expected)
        self.assertEqual(projection.line_map, ir.project(baseline).line_map)


class PackagedConsumerTests(unittest.TestCase):
    CONSUMERS = {'languagetool': 'check_file.py', 'style-analyzer': 'analyze_style.py',
                 'book-export': 'book_maker.py'}
    SOURCE = '---\ntype: synthetic\n---\n# Chapter\n\nThe lamp stood. <!-- note -->\n\nRain struck.\n'

    def setUp(self):
        import shutil
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'source.md'
        self.path.write_text(self.SOURCE)
        self.clean = ir.project(self.SOURCE, str(self.path))
        self.layouts = {}
        canonical = self.root / 'canonical/plugins/write/skills'
        for skill in (*self.CONSUMERS, 'inline-review'):
            shutil.copytree(SKILLS / skill, canonical / skill,
                            ignore=shutil.ignore_patterns('__pycache__'))
        self.layouts['canonical'] = canonical
        for layout in ('symlinked', 'copied', 'missing'):
            skills = self.root / layout / 'skills'
            skills.mkdir(parents=True)
            self.layouts[layout] = skills
            for skill in (*self.CONSUMERS, 'inline-review'):
                if layout == 'missing' and skill == 'inline-review':
                    continue
                target = skills / ('write-' + skill)
                if layout == 'symlinked':
                    target.symlink_to(canonical / skill, target_is_directory=True)
                else:
                    shutil.copytree(canonical / skill, target)

    def script(self, layout, consumer):
        name = consumer if layout == 'canonical' else 'write-' + consumer
        return self.layouts[layout] / name / 'scripts' / self.CONSUMERS[consumer]

    def invoke(self, layout, consumer, flag=None, forbid_import=False):
        import builtins
        script = self.script(layout, consumer)
        sent, analyzed = [], []
        requests = types.ModuleType('requests')
        requests.exceptions = types.SimpleNamespace(RequestException=RuntimeError)
        def post(*args, **kwargs):
            sent.append([list(args), kwargs])
            response = mock.Mock()
            response.json.return_value = {'matches': []}
            return response
        requests.post = mock.Mock(side_effect=post)
        pandoc = types.ModuleType('pypandoc')
        def convert(path, target, **kwargs):
            # Temporary clean filenames are not stable; content and args are.
            sent.append([Path(path).read_text(), target, kwargs])
        pandoc.convert_file = mock.Mock(side_effect=convert)
        real_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == 'inline_review':
                raise AssertionError('plain mode imported inline_review')
            return real_import(name, *args, **kwargs)
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [str(script), str(self.path)]
        if consumer == 'style-analyzer':
            argv.append('--json')
        if consumer == 'book-export':
            argv.append(str(self.root / 'output'))
        if flag:
            argv.append(flag)
        code = 0
        with mock.patch.dict(sys.modules, {'requests': requests, 'pypandoc': pandoc}), \
                mock.patch.object(sys, 'argv', argv), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                contextlib.ExitStack() as stack:
            sys.modules.pop('inline_review', None)
            before_path = sys.path.copy()
            if forbid_import:
                stack.enter_context(mock.patch.object(builtins, '__import__', side_effect=guarded_import))
                stack.enter_context(mock.patch.object(importlib.util, 'spec_from_file_location',
                    wraps=importlib.util.spec_from_file_location))
            module = load_script('packaged_consumer_test', script)
            if consumer == 'style-analyzer':
                original = module.analyze_text
                def analyze(text, *args, **kwargs):
                    analyzed.append(text)
                    return original(text, *args, **kwargs)
                stack.enter_context(mock.patch.object(module, 'analyze_text', side_effect=analyze))
            try:
                module.main()
            except SystemExit as exc:
                code = exc.code
            self.assertEqual(sys.path, before_path)
            if forbid_import:
                self.assertNotIn('inline_review', sys.modules)
                # No direct file import either; only the consumer itself was loaded.
                self.assertEqual(importlib.util.spec_from_file_location.call_count, 1)
        return {'code': code, 'stdout': stdout.getvalue(), 'stderr': stderr.getvalue(),
                'sent': sent, 'analyzed': analyzed}

    def default_digest(self, result):
        payload = json.dumps(result, sort_keys=True).replace(str(self.root), '<TEMP>')
        # Book arguments point at each installation's own style/font resources.
        for layout, skills in self.layouts.items():
            for consumer in self.CONSUMERS:
                directory = skills / (consumer if layout == 'canonical' else 'write-' + consumer)
                payload = payload.replace(str(directory).replace(str(self.root), '<TEMP>'),
                                          '<SKILL:' + consumer + '>')
        return hashlib.sha256(payload.encode()).hexdigest()

    def test_all_layouts_default_and_clean_with_transport_and_maps(self):
        for layout in self.layouts:
            for consumer in self.CONSUMERS:
                with self.subTest(layout=layout, consumer=consumer, mode='default'):
                    result = self.invoke(layout, consumer, forbid_import=True)
                    self.assertEqual(result['code'], 0, result['stderr'])
                    self.assertEqual(self.default_digest(result), CANDIDATE_CONSUMER_DEFAULTS[consumer])
                for flag in ('--manuscript', '--clean'):
                    with self.subTest(layout=layout, consumer=consumer, flag=flag):
                        result = self.invoke(layout, consumer, flag)
                        if layout == 'missing':
                            self.assertEqual(result['code'], 2)
                            for name in ('inline-review', 'write-inline-review'):
                                self.assertIn(str(self.layouts[layout] / name / 'scripts/inline_review.py'), result['stderr'])
                            self.assertEqual(result['sent'], [])
                            self.assertEqual(result['analyzed'], [])
                            continue
                        self.assertEqual(result['code'], 0, result['stderr'])
                        if consumer == 'languagetool':
                            self.assertEqual(result['sent'][0][1]['data']['text'], self.clean.text)
                            # main prints the report; separately inspect the API map.
                            with mock.patch.dict(sys.modules, {'requests': types.SimpleNamespace(
                                    post=mock.Mock(return_value=mock.Mock(**{'json.return_value': {'matches': []}})),
                                    exceptions=types.SimpleNamespace(RequestException=RuntimeError))}):
                                module = load_script('packaged_lt_map', self.script(layout, consumer))
                                self.assertEqual(module.check_file(self.path, manuscript=True)['source_line_map'], self.clean.line_map)
                                sys.modules.pop('packaged_lt_map', None)
                        elif consumer == 'style-analyzer':
                            self.assertEqual(result['analyzed'], [self.clean.text])
                            self.assertEqual(json.loads(result['stdout'])['source_line_maps'],
                                             {str(self.path): self.clean.line_map})
                        else:
                            self.assertEqual([item[0] for item in result['sent']], [self.clean.text] * 2)
                            self.assertEqual([item[1] for item in result['sent']], ['pdf', 'epub'])
                        self.assertEqual(self.path.read_text(), self.SOURCE)

    def test_plain_errors_do_not_evaluate_parser_exception_class(self):
        # A default-mode error must not turn into NameError/AttributeError while
        # evaluating an except clause that formerly referenced inline_review.
        self.path.write_bytes(b'\xff')
        result = self.invoke('missing', 'languagetool', forbid_import=True)
        self.assertEqual(result['code'], 2)
        self.assertEqual(result['sent'], [])
        self.path.unlink()
        for consumer in self.CONSUMERS:
            result = self.invoke('missing', consumer, forbid_import=True)
            self.assertIn(result['code'], (1, 2))
            self.assertEqual(result['sent'], [])

    def test_copied_cli_subprocesses_use_stub_dependencies(self):
        stubs = self.root / 'stubs'
        stubs.mkdir()
        (stubs / 'requests.py').write_text(
            'import json\nfrom pathlib import Path\nimport os\n'
            'class exceptions:\n    RequestException = RuntimeError\n'
            'def post(url, **kwargs):\n'
            '    Path(os.environ["CAPTURE"]).write_text(json.dumps(kwargs))\n'
            '    return Response()\n'
            'class Response:\n'
            '    def raise_for_status(self): pass\n'
            '    def json(self): return {"matches": []}\n')
        (stubs / 'pypandoc.py').write_text(
            'import json\nfrom pathlib import Path\nimport os\n'
            'def convert_file(path, target, **kwargs):\n'
            '    with open(os.environ["CAPTURE"], "a") as f:\n'
            '        f.write(json.dumps([Path(path).read_text(), target]) + "\\n")\n')
        for consumer in self.CONSUMERS:
            with self.subTest(consumer=consumer):
                capture = self.root / (consumer + '.json')
                argv = ['/bin/python3', str(self.script('copied', consumer)), str(self.path)]
                if consumer == 'style-analyzer':
                    argv.append('--json')
                if consumer == 'book-export':
                    argv.append(str(self.root / 'output'))
                argv.append('--manuscript')
                result = subprocess.run(argv, cwd=self.root, capture_output=True, text=True,
                                        env={**os.environ, 'PYTHONPATH': str(stubs), 'CAPTURE': str(capture)})
                self.assertEqual(result.returncode, 0, result.stderr)
                if consumer == 'languagetool':
                    self.assertEqual(json.loads(capture.read_text())['data']['text'], self.clean.text)
                elif consumer == 'style-analyzer':
                    data = json.loads(result.stdout)
                    self.assertEqual(data['source_line_maps'], {str(self.path): self.clean.line_map})
                    self.assertEqual(data['word_count'], len(self.clean.text.split()))
                else:
                    self.assertEqual([json.loads(line) for line in capture.read_text().splitlines()],
                                     [[self.clean.text, 'pdf'], [self.clean.text, 'epub']])


if __name__ == '__main__':
    unittest.main()
