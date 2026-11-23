#!/usr/bin/env python3
"""Prepare and score observed failure/spill recovery, without running a model."""
import argparse
import json
from pathlib import Path

import continuation_evidence as evidence

FIXTURE = 'tests/fixtures/tool-recovery/fixture.json'
WORKLOAD = 'tests/fixtures/tool-recovery/workload.py'
SOURCES = [FIXTURE, WORKLOAD, 'tests/probes/tool_recovery.py', 'tests/probes/continuation_evidence.py',
           'plugins/session/tools/memory_v2.py',
           'lib/control/codex_protocol.py', 'lib/control/session_harness.py',
           'lib/control/sessions.py', 'lib/control/session_store.py']


def recovery_check(project, manifest, expected, tools, readbacks):
    if manifest['case'] == 'failed-read':
        triggers = [t for t in tools if t.get('name') == 'commandExecution'
                    and t.get('status') == 'failed' and type(t.get('exit_code')) is int
                    and t['exit_code'] != 0 and isinstance(t.get('output'), str)
                    and 'source/missing.json' in t['output']
                    and ('FileNotFoundError' in t['output'] or 'No such file' in t['output'])]
    elif manifest['case'] == 'spill-readback':
        spill = evidence.relative_file(project, 'spill.log').read_bytes()
        if evidence.sha(spill) != expected.get('spill_sha256'):
            raise ValueError('spill artifact is missing or changed')
        triggers = [t for t in tools if t.get('name') == 'commandExecution'
                    and t.get('status') == 'completed' and type(t.get('exit_code')) is int and t['exit_code'] == 0
                    and t.get('output_truncated') is False and isinstance(t.get('output'), str)
                    and t['output'].startswith('SYNTHETIC_PREVIEW_TRUNCATED full_output=spill.log\n')]
    else:
        raise ValueError('unsupported recovery case')
    if not any(trigger['sequence'] < readback['sequence'] for trigger in triggers for readback in readbacks):
        raise ValueError('no observed trigger followed by successful matching artifact output')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--output', required=True, type=Path)
    prep.add_argument('--case', choices=('failed-read', 'spill-readback'), required=True)
    score = sub.add_parser('score')
    score.add_argument('--run', required=True, type=Path)
    score.add_argument('--events', required=True, type=Path)
    score.add_argument('--session', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            fixture = evidence.read(evidence.ROOT / FIXTURE)
            files = {'source/data.json': evidence.encoded(fixture['answer']),
                     'source/index.json': b'{"replacement":"source/data.json"}\n'}
            extra = {}
            if args.case == 'spill-readback':
                files = {'workload.py': (evidence.ROOT / WORKLOAD).read_bytes()}
                spill = b'{"recovery_key":"quartz","count":37}\n' + b'ordinary log detail\n' * 500
                extra['spill_sha256'] = evidence.sha(spill)
            result = evidence.prepare(args.output, 'tool-recovery', args.case, files,
                                      fixture[args.case], fixture['answer'], SOURCES, extra=extra)
        else:
            result = evidence.score(args.run, args.events, args.session, 'tool-recovery', recovery_check)
        print(json.dumps(result, indent=2))
        return 2 if result.get('task') == 'unknown' else 1 if result.get('task') in ('fail', 'partial') else 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'task': 'unknown', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
