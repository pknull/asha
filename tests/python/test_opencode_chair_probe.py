"""Deterministic safety and evidence checks for the opt-in inference probe."""
import importlib.util
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('chair_probe', ROOT / 'tests/probes/opencode_chair.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
HAS_BWRAP = sys.platform.startswith('linux') and shutil.which('bwrap') is not None
BWRAP_MISSING = 'Linux bubblewrap unavailable; namespace integration remains unverified'


class ChairProbeTests(unittest.TestCase):
    def test_model_proxy_rejects_other_models_endpoints_and_unbounded_requests(self):
        body = {'model': 'fixture:12b', 'messages': [], 'keep_alive': -1}
        value = probe.validate_request('/v1/chat/completions', json.dumps(body).encode(), 'fixture:12b')
        self.assertNotIn('keep_alive', value)
        for path, payload in [('/api/pull', body), ('/v1/chat/completions', {**body, 'model': 'other'}),
                              ('/v1/chat/completions', []), ('/v1/chat/completions', {'model': 'fixture:12b'})]:
            with self.subTest(path=path, payload=payload), self.assertRaises(ValueError):
                probe.validate_request(path, json.dumps(payload).encode(), 'fixture:12b')
        with self.assertRaises(ValueError):
            probe.validate_request('/v1/chat/completions', b'x' * (probe.MAX_REQUEST + 1), 'fixture:12b')

    def test_busy_gate_requires_no_connections_and_no_other_resident_model(self):
        probe.require_idle('fixture:12b', {'models': []}, '')
        probe.require_idle('fixture:12b', {'models': [{'name': 'fixture:12b'}]}, '')
        for residents, sockets in [({'models': []}, 'ESTAB 0 0 127.0.0.1:11434'),
                                   ({'models': [{'model': 'other:30b'}]}, ''), ({}, '')]:
            with self.subTest(residents=residents), self.assertRaises(RuntimeError):
                probe.require_idle('fixture:12b', residents, sockets)

    def test_sandbox_has_no_host_home_or_host_network(self):
        args = probe.build_bwrap(Path('/tmp/probe/agent'), Path('/tmp/probe/runtime'),
                                 Path('/tmp/probe/sockets'), Path('/opt/test-opencode'),
                                 published_memory=Path('/tmp/probe/backend/home/.asha/chair/Memory'))
        self.assertIn('--unshare-net', args)
        self.assertIn('--unshare-pid', args)
        self.assertIn('--unshare-ipc', args)
        self.assertIn('--clearenv', args)
        self.assertNotIn('/home', args)
        mounts = [args[i + 1] for i, value in enumerate(args) if value in ('--bind', '--ro-bind')]
        self.assertNotIn('/etc', mounts)
        self.assertEqual([path for path in mounts if path.startswith('/tmp/probe/backend')],
                         ['/tmp/probe/backend/home/.asha/chair/Memory'])
        self.assertNotIn('--share-net', args)
        self.assertIn('/opt/asha', args)
        self.assertNotIn(str(ROOT), args)

    def test_wire_main_evidence_does_not_accept_title_only_marker(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'request-001.json').write_text(json.dumps({
                'model': 'fixture:12b', 'messages': [{'role': 'system', 'content': 'SENTINEL'}]}))
            value = probe.wire_evidence(root, 'SENTINEL', 'fixture:12b')
            self.assertFalse(value['profile_in_main'])
            (root / 'request-002.json').write_text(json.dumps({
                'model': 'fixture:12b', 'messages': [{'role': 'system', 'content': 'SENTINEL'}],
                'tools': [{'type': 'function', 'function': {'name': 'bash'}}],
                'temperature': 0.2, 'max_tokens': 8192, 'seed': 104, 'reasoning_effort': 'high'}))
            value = probe.wire_evidence(root, 'SENTINEL', 'fixture:12b')
            self.assertTrue(value['profile_in_main'])
            self.assertEqual(value['effective_models'], ['fixture:12b'])
            self.assertEqual(value['main_requests'][0]['seed'], 104)

    def test_final_extract_uses_last_native_text_not_tool_output(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'events.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in [
                {'type': 'text', 'part': {'text': 'Working.'}},
                {'type': 'tool_use', 'part': {'output': '{"complete":true}'}},
                {'type': 'text', 'part': {'text': '{"complete":false}'}}]))
            self.assertEqual(probe.extract_final(path), '{"complete":false}')

    def test_malformed_native_event_shapes_leave_final_unknown(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'events.jsonl'
            for row in (None, [], 1, 'text', {'type': 'text', 'part': None},
                        {'type': 'text', 'part': []}, {'type': 'text', 'part': {'text': None}}):
                with self.subTest(row=row):
                    path.write_text(json.dumps(row) + '\n')
                    self.assertEqual(probe.extract_final(path), '')

    def test_native_step_budget_covers_read_draft_cas_recovery_and_readback(self):
        for variant in ('absent', 'full', 'compact'):
            config = probe.native_config('fixture:12b', 104, [] if variant == 'absent' else ['/profile.md'])
            self.assertEqual(config['agent']['build']['steps'], 24)

    def test_missing_seed_cannot_be_called_a_matched_comparison(self):
        evidence = {'profile_in_main': True, 'model_matches': True, 'main_requests': [{
            'seed': None, 'temperature': 0.2, 'max_tokens': 8192, 'reasoning_effort': 'high'}]}
        self.assertFalse(probe.comparison_valid(evidence, 'compact', 104)['comparison_valid'])
        evidence['main_requests'][0]['seed'] = 104
        self.assertTrue(probe.comparison_valid(evidence, 'compact', 104)['comparison_valid'])

    def test_absent_delivery_requires_main_wire_without_asha_profile(self):
        evidence = {'profile_in_main': False, 'model_matches': True, 'main_requests': [{
            'seed': 104, 'temperature': 0.2, 'max_tokens': 8192, 'reasoning_effort': 'high',
            'asha_profile_present': True}]}
        self.assertFalse(probe.comparison_valid(evidence, 'absent', 104)['profile_delivery_verified'])
        evidence['main_requests'][0]['asha_profile_present'] = False
        self.assertTrue(probe.comparison_valid(evidence, 'absent', 104)['profile_delivery_verified'])

    def test_profile_render_uses_only_synthetic_home_and_preserves_variant_boundary(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            texts = {}
            for variant in ('absent', 'full', 'compact'):
                home = root / variant / 'home'
                home.mkdir(parents=True)
                texts[variant] = probe.render_profile(ROOT, home, variant)
            self.assertEqual(texts['absent'], '')
            self.assertIn('Synthetic identity fixture', texts['full'])
            self.assertIn('# Asha: compact OpenCode chair', texts['compact'])
            self.assertNotIn('Synthetic identity fixture', texts['compact'])
            self.assertIn('Synthetic operational authority', texts['compact'])
            self.assertNotIn(str(root), texts['full'])
            self.assertNotIn(str(root), texts['compact'])

    @unittest.skipUnless(HAS_BWRAP, BWRAP_MISSING)
    def test_actual_namespace_executes_fake_native_without_inference(self):
        with tempfile.TemporaryDirectory(prefix='chair-probe-smoke-') as name:
            root = Path(name)
            fake = root / 'fake-opencode'
            fake.write_text('''#!/usr/bin/python3
import json, os, subprocess
assert os.environ['HOME'] == '/work/home'
assert os.environ['ASHA_ROOT'] == '/opt/asha'
assert not os.path.exists('/home')
run = subprocess.run(['asha', 'control', 'session', 'list', '--json'], capture_output=True, text=True)
assert run.returncode == 0, run.stderr
assert len(json.loads(run.stdout)['rows']) == 1
print(json.dumps({'type':'text','part':{'text':'{"complete":false}'}}))
''')
            fake.chmod(0o700)
            module, fixture, manifest = probe.prepare(root / 'evidence', fake, 'fixture:12b', 'compact', 104)
            with module.serve(fixture, root / 'evidence/sockets/control.sock'):
                run = subprocess.run(manifest['argv'], env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('"type": "text"', run.stdout)
            self.assertEqual(len(fixture.events), 1)

    @unittest.skipUnless(HAS_BWRAP, BWRAP_MISSING)
    def test_native_reads_published_memory_read_only_and_sees_cas_replacements(self):
        with tempfile.TemporaryDirectory(prefix='chair-memory-smoke-') as name:
            root = Path(name)
            fake = root / 'fake-opencode'
            fake.write_text('''#!/usr/bin/python3
import json, os, pathlib, subprocess
memory = pathlib.Path('/work/home/.asha/chair/Memory')
original = (memory / 'activeContext.md').read_text()
assert 'The existing previous-project Room remains open.' in original
try:
    (memory / 'activeContext.md').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('published Memory was writable')
assert not pathlib.Path('/work/backend').exists()
assert not pathlib.Path('/backend').exists()
tools = '/opt/asha/plugins/session/tools/'
current = json.loads(subprocess.check_output(['python3', tools + 'memory_v2.py', 'read', '--project-dir', str(memory.parent), '--format', 'json']))
pathlib.Path('/work/active.md').write_text(current['activeContext'])
pathlib.Path('/work/decisions.md').write_text(current['decisions'])
attempt = subprocess.run(['python3', tools + 'save_none.py', 'publish', '--start', str(memory.parent), '--active-file', '/work/active.md', '--decisions-file', '/work/decisions.md', '--expected-active', current['digests']['active'], '--expected-decisions', current['digests']['decisions']], capture_output=True)
assert attempt.returncode != 0
assert 'A concurrent operator added a requirement' in (memory / 'activeContext.md').read_text()
assert 'Ask before any future publication outside this fixture.' in (memory / 'decisions.md').read_text()
print(json.dumps({'type':'text','part':{'text':'{"complete":false}'}}))
''')
            fake.chmod(0o700)
            module, fixture, manifest = probe.prepare(root / 'evidence', fake, 'fixture:12b', 'compact', 104)
            with module.serve(fixture, root / 'evidence/sockets/control.sock'):
                run = subprocess.run(manifest['argv'], env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertTrue(fixture.stale_injected)
            self.assertEqual(len(fixture.events), 2)

    def test_api_read_ignores_host_proxy_environment(self):
        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"models":[]}')

        upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(probe, 'OLLAMA', f'http://127.0.0.1:{upstream.server_port}'), \
                    mock.patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}), \
                    mock.patch.object(probe.urllib.request, '_opener', None):
                self.assertEqual(probe.api_read('/api/ps'), {'models': []})
        finally:
            upstream.shutdown()
            upstream.server_close()

    @unittest.skipUnless(HAS_BWRAP, BWRAP_MISSING)
    def test_namespace_model_relay_reaches_only_mock_completion_service(self):
        received = []

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"fixture":true}\n')

        with tempfile.TemporaryDirectory(prefix='chair-proxy-smoke-') as name:
            root = Path(name)
            fake = root / 'fake-opencode'
            fake.write_text('''#!/usr/bin/python3
import json, os, urllib.request
config = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
url = config['provider']['ollama']['options']['baseURL'] + '/chat/completions'
body = {'model':'fixture:12b','messages':[], 'keep_alive':-1}
req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=10) as response:
    assert json.load(response) == {'fixture': True}
print(json.dumps({'type':'text','part':{'text':'{"complete":false}'}}))
''')
            fake.chmod(0o700)
            module, fixture, manifest = probe.prepare(root / 'evidence', fake, 'fixture:12b', 'absent', 104)
            upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            try:
                with mock.patch.object(probe, 'OLLAMA', f'http://127.0.0.1:{upstream.server_port}'), \
                        mock.patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}), \
                        mock.patch.object(probe.urllib.request, '_opener', None), \
                        mock.patch.object(probe, 'preflight', return_value={'fixture': True}), \
                        module.serve(fixture, root / 'evidence/sockets/control.sock'), \
                        probe.model_proxy(root / 'evidence/sockets/model.sock', root / 'evidence/wire', 'fixture:12b'):
                    run = subprocess.run(manifest['argv'], env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                                         capture_output=True, text=True, timeout=30)
            finally:
                upstream.shutdown()
                upstream.server_close()
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(received, [('/v1/chat/completions', {'model': 'fixture:12b', 'messages': []})])
            self.assertTrue((root / 'evidence/wire/response-001.txt').exists())


if __name__ == '__main__':
    unittest.main()
