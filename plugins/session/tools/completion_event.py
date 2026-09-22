#!/usr/bin/env python3
"""Classify tool boundaries without retaining commands or granting permissions."""
import hashlib
import json
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[3]


def command_kind(payload):
    if not isinstance(payload, dict) or payload.get('tool_name') != 'Bash':
        return 'work'
    value = payload.get('tool_input')
    command = value.get('command') if isinstance(value, dict) else None
    if not isinstance(command, str) or any(c in command for c in '\n\r$`;&|<>()\\'):
        return 'work'
    try:
        argv = shlex.split(command)
    except ValueError:
        return 'work'
    if not argv:
        return 'work'
    kind = 'work'
    flags = set()
    if argv[0] in {'asha', str(ROOT / 'bin/asha')} and argv[1:3] == ['control', 'session']:
        if len(argv) < 4:
            return 'work'
        verb, tail = argv[3], argv[4:]
        if verb == 'report':
            kind = 'report'
            flags = {'--state', '--text', '--native-id', '--experience-file', '--experience-ref', '--key', '--supersedes'}
        elif verb == 'handoff':
            kind = 'finalizer'
            flags = {'--request', '--attempt', '--outcome', '--detail', '--active-file', '--decisions-file',
                     '--expected-active', '--expected-decisions', '--experience-file', '--experience-ref', '--key', '--supersedes'}
    elif (len(argv) >= 3 and argv[0] in {'python3', '/usr/bin/python3'}
          and argv[1] in {str(ROOT / 'plugins/session/tools/memory_v2.py'), str(ROOT / 'plugins/session/tools/save_none.py')}
          and argv[2] == 'publish'):
        kind, tail = 'finalizer', argv[3:]
        flags = {'--project-dir', '--start', '--scope', '--active-file', '--decisions-file', '--expected-active', '--expected-decisions'}
    if kind == 'work':
        return kind
    options, i = {}, 0
    while i < len(tail):
        flag = tail[i]
        if flag in options:
            return 'work'
        if flag == '--json':
            options[flag] = True
            i += 1
        elif flag in flags and i + 1 < len(tail):
            options[flag] = tail[i + 1]
            i += 2
        else:
            return 'work'
    if kind == 'report' and options.get('--state') != 'finished':
        return 'work'
    return kind


def report_only(payload):
    return command_kind(payload) == 'report'


def metadata(payload):
    if not isinstance(payload, dict):
        return 'work', 'unknown'
    name, value = payload.get('tool_name'), payload.get('tool_input')
    native_id = payload.get('tool_use_id') or payload.get('tool_id')
    # Pre/Post share native ID, or the identical tool name/input. Other payload
    # fields (output, timing, session bodies) never enter the token.
    material = [name, native_id] if native_id else [name, value]
    if not name or (not native_id and value is None):
        return 'work', 'unknown'
    token = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return command_kind(payload), token


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(262145)
        result = metadata(json.loads(raw)) if len(raw) <= 262144 else ('work', 'unknown')
    except (ValueError, UnicodeError):
        result = ('work', 'unknown')
    print(' '.join(result))
