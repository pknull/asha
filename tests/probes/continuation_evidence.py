"""Small shared staging and Control-export checks; no inference or live Control access.

Consumes SessionStore.events pages and Hub.show rows, not model-written receipts.
The two probes deliberately support only a fresh structured Codex worker.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 'asha-continuation-fixture/v1'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def parse(text):
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def read(path):
    return parse(Path(path).read_bytes())


def memory_module():
    path = ROOT / 'plugins/session/tools/memory_v2.py'
    spec = importlib.util.spec_from_file_location('probe_memory_v2', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def relative_file(root, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('invalid fixture-relative path')
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('missing or out-of-scope fixture file: ' + value)
    return path


def prepare(output, probe, case, files, task, answer, source_paths, *, memory=None, extra=None):
    run = Path(output).resolve()
    run.mkdir(parents=True, exist_ok=False)
    project = run / 'project'
    project.mkdir()
    for name, data in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # Both probes launch through Control, whose project resolver requires v2.
    module = memory_module()
    module.initialize(project)
    if memory:
        before = module.read_published_snapshot(project)
        module.publish(project, memory['active'], memory['decisions'],
                       expected_preimages=module.snapshot_digests(before), publication_source='synthetic-probe')
        # Render only for inspection; this is not evidence of native delivery.
        (run / 'startup-preview.txt').write_text(module.render_startup_context(project))
    module.read_published(project)
    run_id = str(uuid.uuid4())
    prompt = ('Synthetic probe ' + run_id + '\n\n' + task.rstrip() + '\n\n'
              'Write result.json, then read it back as your last native command. Do not edit the inputs '
              'or inspect files outside this fixture project. '
              'End with exactly one JSON object: {"outcome":"complete","artifact":"result.json"}. '
              'If you cannot finish, use {"outcome":"partial","reason":"specific reason"}.\n')
    (run / 'task.md').write_text(prompt)
    (run / 'expected.json').write_bytes(encoded({'answer': answer, **(extra or {})}))
    inputs = {p.relative_to(project).as_posix(): sha(p.read_bytes())
              for p in sorted(project.rglob('*')) if p.is_file()}
    sources = {path: sha((ROOT / path).read_bytes()) for path in source_paths}
    manifest = {'schema': SCHEMA, 'probe': probe, 'case': case, 'run_id': run_id,
                'inputs': inputs, 'task_sha256': sha(prompt.encode()),
                'expected_sha256': sha((run / 'expected.json').read_bytes()), 'sources': sources,
                'comparison_key': sha(encoded({'probe': probe, 'case': case, 'files': files and {
                    k: sha(v) for k, v in files.items()}, 'memory': memory, 'task': task, 'sources': sources}))}
    manifest['manifest_id'] = sha(encoded(manifest))
    (run / 'manifest.json').write_bytes(encoded(manifest))
    return {'run': str(run), 'project': str(project), 'task': str(run / 'task.md'),
            'manifest_id': manifest['manifest_id'], 'inference_run': False,
            'boundary': 'Expected answers are outside the project, not filesystem-isolated.'}


def validate_fixture(run, probe):
    run = Path(run).resolve()
    manifest = read(run / 'manifest.json')
    if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA or manifest.get('probe') != probe:
        raise ValueError('unsupported fixture identity')
    body = {key: value for key, value in manifest.items() if key != 'manifest_id'}
    if manifest.get('manifest_id') != sha(encoded(body)):
        raise ValueError('fixture manifest changed')
    project = run / 'project'
    for key, root in (('inputs', project), ('sources', ROOT)):
        hashes = manifest.get(key)
        if not isinstance(hashes, dict) or not hashes:
            raise ValueError('missing fixture fingerprints')
        for name, digest in hashes.items():
            if sha(relative_file(root, name).read_bytes()) != digest:
                raise ValueError('fixture/source changed: ' + name)
    for name, field in (('task.md', 'task_sha256'), ('expected.json', 'expected_sha256')):
        if sha((run / name).read_bytes()) != manifest.get(field):
            raise ValueError('fixture changed: ' + name)
    return project, manifest, read(run / 'expected.json'), (run / 'task.md').read_text()


def native_evidence(page, session, project, prompt):
    """Validate a complete first-turn Control export and retain observed outputs.

Control's turn-finished outcome alone can retain native success after a later
transport failure. Hub.show.activity must also be finished, not failed.
"""
    if not isinstance(page, dict) or not isinstance(session, dict):
        raise ValueError('expected Control events page and session show objects')
    if (session.get('harness'), session.get('transport'), session.get('profile')) != ('codex', 'structured', 'worker'):
        raise ValueError('unsupported evidence: only structured Codex worker exports are supported')
    if session.get('generation') != 1 or session.get('project') != str(project) or session.get('prompt') != prompt:
        raise ValueError('session is not the fresh fixture assignment')
    if session.get('activity') != 'finished' or session.get('pending_messages') != 0:
        raise ValueError('native process outcome is failed, pending or unknown')
    sid = session.get('session_id')
    if not isinstance(sid, str) or not sid or page.get('session_id') != sid:
        raise ValueError('foreign session export')
    if page.get('complete') is not True or page.get('output_gaps') != [] or page.get('acknowledged_cursor') != 0:
        raise ValueError('incomplete or retained-gap event export; capture from cursor zero without a consumer')
    rows = page.get('events')
    if not isinstance(rows, list) or not rows:
        raise ValueError('missing native events')
    sequence, turn, native, final, finished = 0, None, None, None, False
    started, tools = {}, []
    bound, opening, created = False, None, False
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get('session_id') != sid or type(row.get('sequence')) is not int or row['sequence'] <= sequence:
            raise ValueError('duplicate, unordered or foreign native event')
        sequence = row['sequence']
        kind, payload, tid = row.get('kind'), row.get('payload'), row.get('turn_id')
        output = row.get('output', {})
        if not isinstance(output, dict):
            raise ValueError('invalid native output metadata')
        if not isinstance(payload, dict) or payload.get('output_missing') or output.get('available') is False:
            raise ValueError('missing native event payload')
        # `complete` describes the requested page, not its starting cursor.
        # SessionStore creates these first two records in one transaction.
        if kind == 'message-retained':
            if (index != 0 or tid is not None or opening is not None
                    or payload.get('digest') != sha(prompt.encode())
                    or not isinstance(payload.get('message_id'), str) or not payload['message_id']):
                raise ValueError('missing or changed opening assignment provenance')
            opening = payload['message_id']
        elif kind == 'session-created':
            if index != 1 or tid is not None or opening is None or payload.get('harness') != 'codex':
                raise ValueError('missing opening session provenance')
            created = True
        if tid is None:
            if kind in {'tool', 'completed', 'failed', 'turn-finished', 'initialized',
                        'turn-reserved', 'provider-bound'}:
                raise ValueError('unscoped native event')
            continue  # Control lifecycle records outside a native turn.
        if kind == 'turn-reserved':
            if (turn is not None or not isinstance(tid, str) or not tid
                    or not created or payload.get('message_id') != opening):
                raise ValueError('not a single fresh turn')
            turn = tid
        if turn is None or tid != turn or finished:
            raise ValueError('foreign turn or event after process completion')
        if kind == 'provider-bound':
            if bound:
                raise ValueError('duplicate provider binding')
            bound = True
        elif kind == 'initialized':
            if native is not None or not isinstance(payload.get('native_id'), str) or not payload['native_id']:
                raise ValueError('missing or duplicate native identity')
            native = payload['native_id']
        elif kind == 'tool':
            if not native or final is not None:
                raise ValueError('tool outside the active native turn')
            identity, name, status = payload.get('tool_id'), payload.get('name'), payload.get('status')
            if not isinstance(identity, str) or not identity or name not in ('commandExecution', 'fileChange', 'mcpToolCall'):
                raise ValueError('unsupported native tool evidence')
            if status == 'inProgress':
                if identity in started or any(t['tool_id'] == identity for t in tools):
                    raise ValueError('duplicate native tool start')
                started[identity] = name
            else:
                if started.pop(identity, None) != name or status not in ('completed', 'failed', 'declined'):
                    raise ValueError('unmatched or invalid native tool completion')
                tools.append({**payload, 'sequence': sequence})
        elif kind in ('completed', 'failed'):
            if final is not None or kind != 'completed' or started or payload.get('native_id') != native:
                raise ValueError('failed, duplicate or incomplete native final')
            if payload.get('summary_truncated') is not False or not isinstance(payload.get('summary'), str):
                raise ValueError('missing or truncated final answer')
            final = parse(payload['summary'])
        elif kind == 'turn-finished':
            if final is None or payload.get('outcome') != 'completed' or payload.get('reason') != 'completed':
                raise ValueError('missing successful process completion')
            finished = True
    if not turn or not native or not bound or not finished or started or page.get('next_event_cursor') != sequence:
        raise ValueError('incomplete native evidence')
    if not isinstance(final, dict) or final.get('outcome') not in ('complete', 'partial'):
        raise ValueError('unsupported final verification format')
    if final['outcome'] == 'complete' and final != {'outcome': 'complete', 'artifact': 'result.json'}:
        raise ValueError('unsupported complete claim')
    if final['outcome'] == 'partial' and (set(final) != {'outcome', 'reason'} or not isinstance(final['reason'], str) or not final['reason'].strip()):
        raise ValueError('partial claim needs a reason')
    return {'session_id': sid, 'turn_id': turn, 'native_id': native, 'tools': tools, 'final': final,
            'elapsed_seconds': rows[-1]['created_at'] - rows[0]['created_at']}


def matching_output(tool, answer):
    if (tool.get('name') != 'commandExecution' or tool.get('status') != 'completed'
            or type(tool.get('exit_code')) is not int or tool['exit_code'] != 0
            or tool.get('output_truncated') is not False or tool.get('output_section') != 'complete'):
        return False
    try:
        return encoded(parse(tool.get('output', ''))) == encoded(answer)
    except (ValueError, TypeError):
        return False


def score(run, events, session, probe, recovery_check=None):
    result = {'infrastructure': 'unknown', 'task': 'unknown', 'startup_delivery': 'unknown',
              'production_qualified': False, 'semantic_review': 'required for tool intent; not measured',
              'task_verification': 'Literal artifact values and observed event sequence only.',
              'scope': 'Synthetic source-grounded task; no Room startup or model-consumption claim.'}
    try:
        project, manifest, expected, prompt = validate_fixture(run, probe)
        observed = native_evidence(read(events), read(session), project, prompt)
        result.update(infrastructure='valid', case=manifest['case'], manifest_id=manifest['manifest_id'],
                      comparison_key=manifest['comparison_key'], session_id=observed['session_id'],
                      turn_id=observed['turn_id'], native_id=observed['native_id'],
                      completed_tool_count=len(observed['tools']), elapsed_seconds=observed['elapsed_seconds'])
        if observed['final']['outcome'] == 'partial':
            return {**result, 'task': 'partial', 'reason': observed['final']['reason']}
        actual = read(relative_file(project, 'result.json'))
        if encoded(actual) != encoded(expected['answer']):
            return {**result, 'task': 'fail', 'reason': 'artifact does not answer this fixture correctly'}
        # Without exported command arguments, an earlier source output cannot
        # stand in for the requested final artifact check after later tools.
        readbacks = [tool for tool in observed['tools'][-1:] if matching_output(tool, actual)]
        if not readbacks:
            return {**result, 'reason': 'no completed untruncated command output matches the artifact'}
        if recovery_check:
            recovery_check(project, manifest, expected, observed['tools'], readbacks)
        return {**result, 'task': 'pass', 'artifact_sha256': sha((project / 'result.json').read_bytes()),
                'matching_output_tool_ids': [t['tool_id'] for t in readbacks],
                'readback_limit': 'Control exports command results, not command arguments; matching output is observed, the read operation itself is not attested.'}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return {**result, 'reason': str(exc)}
