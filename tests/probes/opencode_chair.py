#!/usr/bin/env python3
"""Opt-in, sanitized native OpenCode chair experiment; never a CI inference test.

--prepare-only renders public synthetic inputs without contacting Ollama. An
actual run requires an explicit installed model and executable, refuses a busy
service, and exposes only a model-restricted Unix proxy inside a network
namespace. Output is local evidence, not production qualification.
"""

from contextlib import contextmanager
import argparse
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
PROFILE_INPUTS = ROOT / 'tests/fixtures/opencode-chair-profiles'
MAX_REQUEST = 4 * 1024 * 1024
OLLAMA = 'http://127.0.0.1:11434'
LIMITS = {'steps': 24, 'output': 8192, 'context': 65536,
          'temperature': 0.2, 'reasoningEffort': 'high'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_fixture():
    spec = importlib.util.spec_from_file_location('chair_fixture', ROOT / 'tests/fixtures/opencode-chair/fixture.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_request(path, raw, model):
    if path != '/v1/chat/completions' or len(raw) > MAX_REQUEST:
        raise ValueError('only bounded chat completions are available')
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get('model') != model or
            not isinstance(value.get('messages'), list)):
        raise ValueError('request must use the explicit selected model and messages')
    value.pop('keep_alive', None)
    return value


def require_idle(model, residents, connections):
    if connections.strip():
        raise RuntimeError('Ollama has an established client connection; fixture inference refused')
    if not isinstance(residents, dict) or not isinstance(residents.get('models'), list):
        raise RuntimeError('Ollama resident-model state is unknown')
    for row in residents['models']:
        if not isinstance(row, dict) or model not in (row.get('name'), row.get('model')):
            raise RuntimeError('a different model is resident; fixture will not displace it')


def api_read(path):
    request = urllib.request.Request(OLLAMA + path, headers={'Connection': 'close'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        return json.load(response)


def preflight(model):
    sockets = subprocess.run(['ss', '-Htn', 'state', 'established',
                              '( sport = :11434 or dport = :11434 )'],
                             check=True, capture_output=True, text=True, timeout=10)
    residents = api_read('/api/ps')
    require_idle(model, residents, sockets.stdout)
    installed = api_read('/api/tags')
    if model not in {row.get('name') for row in installed.get('models', [])}:
        raise RuntimeError('selected model is not installed; no downloads are performed')
    return {'resident_models': residents, 'connections': sockets.stdout,
            'ollama_version': api_read('/api/version'),
            'race_limit': 'Read-only preflight is not an exclusive service reservation.'}


def render_profile(repo, home, variant):
    """Exercise the real wrapper using a capture executable and synthetic HOME."""
    home = Path(home).resolve()
    asha = home / '.asha'
    asha.mkdir(parents=True, exist_ok=True)
    for name in ('soul', 'voice', 'keeper', 'operation'):
        source = PROFILE_INPUTS / (name + '.md')
        shutil.copyfile(source, asha / source.name)
    (asha / 'config.json').write_text('{}')
    if variant == 'absent':
        return ''
    skills = home / '.config/opencode/skills'
    skills.mkdir(parents=True, exist_ok=True)
    link = skills / 'fixture-bootstrap'
    link.symlink_to(Path(repo) / 'plugins/session')
    capture = home.parent / 'render.json'
    fake = home.parent / 'render-opencode'
    fake.write_text('''#!/usr/bin/python3
import os, pathlib, sys
if sys.argv[1:] == ['--version']:
    print('1.18.34')
else:
    pathlib.Path(os.environ['CAPTURE']).write_text(os.environ['OPENCODE_CONFIG_CONTENT'])
''')
    fake.chmod(0o700)
    env = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'ASHA_SESSION_PROFILE': 'chair', 'ASHA_OPENCODE_CHAIR_PROFILE': variant,
           'ASHA_OPENCODE_CMD': str(fake), 'CAPTURE': str(capture)}
    try:
        run = subprocess.run(['bash', str(Path(repo) / 'bin/asha'), 'opencode', 'fixture-render'],
                             cwd=home, env=env, capture_output=True, text=True, timeout=30)
        if run.returncode or not capture.is_file():
            raise RuntimeError('profile rendering failed: ' + run.stderr)
        config = json.loads(capture.read_text())
        parts = [Path(path).read_text() for path in config['instructions']]
        # This is path translation into the mounted namespace, not text
        # summarization. The profile bytes and source hashes are recorded.
        text = '\n\n'.join(parts).replace(str(home), '/work/home').replace(str(repo), '/opt/asha')
        return text
    finally:
        link.unlink(missing_ok=True)


def native_config(model, seed, instructions):
    return {
        'model': 'ollama/' + model, 'small_model': 'ollama/' + model,
        'instructions': instructions,
        'provider': {'ollama': {'npm': '@ai-sdk/openai-compatible',
            'options': {'baseURL': 'http://127.0.0.1:1/v1'},
            'models': {model: {'id': model, 'name': model, 'reasoning': True,
                'tool_call': True, 'temperature': True,
                'limit': {'context': LIMITS['context'], 'output': LIMITS['output']},
                'options': {'seed': seed, 'temperature': LIMITS['temperature'],
                            'reasoningEffort': LIMITS['reasoningEffort']}}}}},
        'agent': {'build': {'steps': LIMITS['steps']}},
        'permission': {'*': 'deny', 'read': 'allow', 'glob': 'allow', 'grep': 'allow',
                       'bash': 'allow', 'write': 'allow', 'edit': 'allow'},
    }


def build_bwrap(agent, runtime, sockets, binary, *, published_memory):
    args = ['bwrap', '--tmpfs', '/', '--ro-bind', '/usr', '/usr',
            '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/sbin', '/sbin',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--dir', '/etc', '--dir', '/tmp', '--unshare-user', '--unshare-pid',
            '--unshare-net', '--unshare-ipc', '--unshare-uts', '--hostname', 'chair-fixture',
            '--proc', '/proc', '--dev', '/dev', '--die-with-parent', '--new-session',
            '--bind', str(agent), '/work', '--ro-bind', str(agent / 'asha-shims'), '/opt/asha',
            # Directory projection follows real atomic file replacements. File
            # mounts would retain old inodes after publication. No drafts,
            # configuration, logs, receipts or other backend paths are exposed.
            '--ro-bind', str(published_memory), '/work/home/.asha/chair/Memory',
            '--ro-bind', str(runtime), '/fixture/runtime', '--ro-bind', str(sockets), '/fixture/sockets',
            '--ro-bind', str(sockets / 'control.sock'), '/fixture/control.sock',
            '--ro-bind', str(binary), '/opt/opencode',
            '--ro-bind', str(agent / 'AGENTS.md'), '/work/AGENTS.md',
            '--ro-bind', str(agent / 'bin'), '/work/bin',
            '--chdir', '/work', '--clearenv']
    env = {'HOME': '/work/home', 'PATH': '/work/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'SHELL': '/bin/bash', 'ASHA_HOME': '/work/home/.asha', 'ASHA_ROOT': '/opt/asha',
           'XDG_CONFIG_HOME': '/work/home/.config', 'XDG_DATA_HOME': '/work/home/.local/share',
           'XDG_CACHE_HOME': '/work/home/.cache', 'XDG_STATE_HOME': '/work/home/.local/state',
           'OPENCODE_DISABLE_AUTOUPDATE': '1'}
    for key, value in env.items():
        args += ['--setenv', key, value]
    return args + ['/usr/bin/python3', '/fixture/runtime/runner.py', '--inner']


class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/fixture/sockets/model.sock')


def inner():
    """TCP exists only inside this namespace; its only peer is the fixed UDS."""
    class Relay(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *args):
            pass

        def do_POST(self):
            upstream = UnixHTTPConnection('localhost', timeout=300)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > MAX_REQUEST:
                    self.send_error(413)
                    return
                upstream.request('POST', self.path, body=self.rfile.read(length),
                                 headers={'Content-Type': 'application/json'})
                response = upstream.getresponse()
                self.send_response(response.status)
                self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                self.end_headers()
                while chunk := response.readline():
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException) as exc:
                print('model relay failed: ' + str(exc), file=sys.stderr)
            finally:
                upstream.close()
                self.close_connection = True

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = json.loads(Path('/fixture/runtime/config.json').read_text())
    config['provider']['ollama']['options']['baseURL'] = f'http://127.0.0.1:{server.server_port}/v1'
    os.environ['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
    prompt = Path('/fixture/runtime/prompt.txt').read_text()
    args = ['/opt/opencode', 'run', '--pure', '--model', config['model'], '--format', 'json', prompt]
    try:
        return subprocess.run(args, check=False).returncode
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def model_proxy(socket_path, wire, model):
    lock = threading.Lock()
    count = 0

    class Proxy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal count
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > MAX_REQUEST:
                    raise ValueError('request too large')
                incoming = self.rfile.read(length)
                value = validate_request(self.path, incoming, model)
                with lock:
                    count += 1
                    if count > 40:
                        raise RuntimeError('request budget exhausted')
                    prefix = f'{count:03d}'
                    (wire / f'incoming-{prefix}.json').write_bytes(incoming)
                    raw = json.dumps(value).encode()
                    (wire / f'request-{prefix}.json').write_bytes(raw)
                    gate = preflight(model)
                    (wire / f'preflight-{prefix}.json').write_text(json.dumps(gate, indent=2))
                    request = urllib.request.Request(OLLAMA + self.path, data=raw,
                        headers={'Content-Type': 'application/json', 'Connection': 'close'})
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(request, timeout=300) as response:
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                        self.end_headers()
                        with (wire / f'response-{prefix}.txt').open('wb') as output:
                            while chunk := response.readline():
                                output.write(chunk)
                                self.wfile.write(chunk)
                                self.wfile.flush()
            except (OSError, ValueError, RuntimeError) as exc:
                (wire / f'error-{count:03d}.txt').write_text(str(exc))
                try:
                    self.send_error(503, 'fixture model request refused')
                except OSError:
                    pass
            finally:
                self.close_connection = True

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = Server(str(socket_path), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        socket_path.unlink(missing_ok=True)


def extract_final(path):
    final = ''
    for line in Path(path).read_text(errors='replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        part = row.get('part')
        if row.get('type') == 'text' and isinstance(part, dict) and isinstance(part.get('text'), str):
            final = part['text']
    return final


def wire_evidence(wire, sentinel, model):
    rows = []
    for path in sorted(Path(wire).glob('request-*.json')):
        request = json.loads(path.read_text())
        if not request.get('tools'):
            continue  # An auxiliary title request is not agent delivery evidence.
        system = '\n'.join(str(m.get('content', '')) for m in request.get('messages', [])
                           if m.get('role') == 'system')
        rows.append({'file': path.name, 'model': request.get('model'),
                     'profile_present': bool(sentinel and sentinel in system),
                     'asha_profile_present': any(marker in system for marker in (
                         '# Asha Identity Layer (merged)', '# Asha: compact OpenCode chair')),
                     'temperature': request.get('temperature'), 'seed': request.get('seed'),
                     'max_tokens': request.get('max_tokens', request.get('max_completion_tokens')),
                     'reasoning_effort': request.get('reasoning_effort')})
    return {'main_requests': rows, 'profile_in_main': bool(rows) and all(r['profile_present'] for r in rows),
            'effective_models': sorted({r['model'] for r in rows if isinstance(r['model'], str)}),
            'model_matches': bool(rows) and all(r['model'] == model for r in rows),
            'unknown_fields': 'null means not observed on the wire, never inferred from config'}


def comparison_valid(evidence, variant, seed):
    rows = evidence['main_requests']
    settings_match = bool(rows) and all(
        row['seed'] == seed and row['temperature'] == LIMITS['temperature'] and
        row['max_tokens'] == LIMITS['output'] and row['reasoning_effort'] == LIMITS['reasoningEffort']
        for row in rows)
    delivery = (bool(rows) and all(not row.get('asha_profile_present', False) for row in rows)
                if variant == 'absent' else evidence['profile_in_main'])
    return {'settings_observed_match': settings_match, 'profile_delivery_verified': delivery,
            'comparison_valid': settings_match and delivery and evidence['model_matches'],
            'seed_scope': 'Wire observation establishes the requested seed, not backend determinism.'}


def prepare(output, binary, model, variant, seed):
    output = Path(output).resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError('evidence output must be outside the repository')
    if len(os.fsencode(output / 'sockets/control.sock')) > 103:
        raise ValueError('output path is too long for Unix sockets; use a short path under /tmp')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture_module = load_fixture()
    fixture = fixture_module.ChairFixture(ROOT, output / 'backend')
    agent, runtime, sockets, wire = (output / name for name in ('agent', 'runtime', 'sockets', 'wire'))
    for path in (runtime, sockets, wire):
        path.mkdir(mode=0o700)
    fixture_module.prepare_agent_tree(agent)
    profile = render_profile(ROOT, agent / 'home', variant)
    (runtime / 'instructions.md').write_text(profile)
    shutil.copyfile(Path(__file__), runtime / 'runner.py')
    (runtime / 'prompt.txt').write_text(fixture_module.TASK_PROMPT)
    config = native_config(model, seed, ['/fixture/runtime/instructions.md'] if profile else [])
    (runtime / 'config.json').write_text(json.dumps(config, indent=2))
    argv = build_bwrap(agent, runtime, sockets, binary, published_memory=fixture.chair / 'Memory')
    sources = [Path(__file__), ROOT / 'bin/asha', ROOT / 'harnesses/opencode.sh',
               ROOT / 'identity/identity-merge.sh', ROOT / 'identity/operational-merge.sh',
               ROOT / 'identity/asha-identity-system-prompt.md',
               ROOT / 'identity/opencode-chair-compact.md', ROOT / 'identity/orchestrator-brief.md',
               *sorted(PROFILE_INPUTS.glob('*.md')),
               *sorted((ROOT / 'plugins/session/tools').glob('*.py')),
               ROOT / 'plugins/session/hooks/handlers/operational-budget.sh',
               ROOT / 'tests/fixtures/opencode-chair/fixture.py',
               ROOT / 'tests/fixtures/opencode-chair/shim.py',
               ROOT / 'tests/fixtures/opencode-chair/AGENTS.md']
    manifest = {'variant': variant, 'seed': seed, 'requested_model': model, 'limits': LIMITS,
                'profile_sha256': digest(runtime / 'instructions.md'), 'profile_bytes': len(profile.encode()),
                'config_sha256': digest(runtime / 'config.json'), 'task_sha256': digest(runtime / 'prompt.txt'),
                'binary': str(binary), 'binary_sha256': digest(binary), 'argv': argv,
                'source_sha256': {str(p.relative_to(ROOT)): digest(p) for p in sources},
                'boundaries': ['Synthetic public hot inputs; not the private full identity corpus.',
                    'Native --pure: production lifecycle hooks and full skill catalog are absent.',
                    'Safe session shims; real Memory validator and CAS in isolated backend.',
                    'Only published synthetic Memory is projected read-only for native file reads.',
                    '24-step comparison replaces the 14-step setup smoke; outcomes are not pooled.',
                    'Prompt wording and length both change; no causal length claim.',
                    'Read-only activity checks cannot reserve the shared service.',
                    'Mechanical score requires separate semantic review; never production qualification.']}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return fixture_module, fixture, manifest


def run(args):
    binary = args.opencode_binary.resolve(strict=True)
    fixture_module, fixture, manifest = prepare(args.output, binary, args.model, args.profile, args.seed)
    output = args.output.resolve()
    if args.prepare_only:
        return {'prepared': True, 'inference_run': False, 'output': str(output), 'manifest': manifest}
    try:
        gate = preflight(args.model)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        (output / 'failure.json').write_text(json.dumps({
            'error': str(exc), 'inference_run': False, 'outcome': 'preflight-refused',
            'comparison_valid': False, 'production_qualified': False}, indent=2))
        raise
    (output / 'preflight.json').write_text(json.dumps(gate, indent=2))
    clean_env = {'HOME': str(output / 'agent/home'), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                 'OPENCODE_DISABLE_AUTOUPDATE': '1'}
    version = subprocess.run([str(binary), '--version'], env=clean_env, capture_output=True,
                             text=True, timeout=30, check=True).stdout.strip()
    start = time.monotonic()
    with fixture_module.serve(fixture, output / 'sockets/control.sock'), \
            model_proxy(output / 'sockets/model.sock', output / 'wire', args.model), \
            (output / 'events.jsonl').open('w') as events:
        process = subprocess.Popen(manifest['argv'], env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                                   stdout=events, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            code = 124
    final = extract_final(output / 'events.jsonl')
    profile_text = (output / 'runtime/instructions.md').read_text()
    evidence = wire_evidence(output / 'wire', profile_text, args.model)
    changed_sources = [path for path, expected in manifest['source_sha256'].items()
                       if not (ROOT / path).is_file() or digest(ROOT / path) != expected]
    result = {'exit_code': code, 'seconds': round(time.monotonic() - start, 2),
              'opencode_version': version, 'profile': args.profile, 'seed': args.seed,
              'final': final, 'wire': evidence, 'comparison': comparison_valid(evidence, args.profile, args.seed),
              'score': fixture.score(final),
              'source_changed_during_run': changed_sources,
              'production_qualified': False, 'semantic_review': 'required'}
    if changed_sources:
        result['comparison']['comparison_valid'] = False
    (output / 'snapshot.json').write_text(json.dumps(fixture.snapshot(), indent=2))
    (output / 'result.json').write_text(json.dumps(result, indent=2))
    return result


def main():
    if sys.argv[1:] == ['--inner']:
        return inner()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--opencode-binary', type=Path, required=True)
    parser.add_argument('--model', required=True, help='Exact existing Ollama model name; never downloaded')
    parser.add_argument('--profile', choices=['absent', 'full', 'compact'], required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New local evidence directory outside this repository')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        parser.error('--timeout must be between 1 and 1800 seconds')
    try:
        result = run(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        failure = {'error': str(exc), 'production_qualified': False, 'comparison_valid': False,
                   'outcome': 'setup-or-preflight-refused'}
        print(json.dumps(failure), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return result.get('exit_code', 0)


if __name__ == '__main__':
    raise SystemExit(main())
