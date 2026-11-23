#!/usr/bin/env python3
"""Prepare and score synthetic Memory retrieval tasks without running inference."""
import argparse
import json
from pathlib import Path

import continuation_evidence as evidence

FIXTURE = 'tests/fixtures/memory-continuation/fixture.json'
SOURCES = [FIXTURE, 'tests/probes/memory_continuation.py', 'tests/probes/continuation_evidence.py',
           'plugins/session/tools/memory_v2.py', 'lib/control/codex_protocol.py',
           'lib/control/session_harness.py', 'lib/control/sessions.py', 'lib/control/session_store.py']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--output', required=True, type=Path)
    prep.add_argument('--case', choices=('facts', 'operation'), required=True)
    score = sub.add_parser('score')
    score.add_argument('--run', required=True, type=Path)
    score.add_argument('--events', required=True, type=Path)
    score.add_argument('--session', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            fixture = evidence.read(evidence.ROOT / FIXTURE)
            result = evidence.prepare(args.output, 'memory-continuation', args.case,
                {'live/status.json': evidence.encoded(fixture['live_status'])},
                fixture[args.case]['task'], fixture[args.case]['answer'], SOURCES,
                memory={'active': fixture['active'], 'decisions': fixture['decisions']})
        else:
            result = evidence.score(args.run, args.events, args.session, 'memory-continuation')
        print(json.dumps(result, indent=2))
        return 2 if result.get('task') == 'unknown' else 1 if result.get('task') in ('fail', 'partial') else 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'task': 'unknown', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
