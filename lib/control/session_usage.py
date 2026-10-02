"""Per-worker token use, model and effort read from native records (#111).

Read-only and fail-open: a missing, oversized or unparsable record leaves
usage ``unknown`` with a reason, and never refuses a show or a close. Tokens
only; prices go stale and the plans are subscriptions.

Native records (shapes checked against live records, 2026-10-02):

* Claude: ``$CLAUDE_CONFIG_DIR`` (default ``~/.claude``)
  ``/projects/<encoded cwd>/<native_id>.jsonl``. Each assistant API message
  is written once per content block, every copy carrying the same
  ``message.id`` and ``message.usage``; usage is counted once per message.
  ``input_tokens`` excludes the cache reads and writes. A top-level
  ``effort`` names the effective effort. Native subagent transcripts under
  ``<native_id>/subagents/`` are counted in the totals.
* Codex: ``$CODEX_HOME`` (default ``~/.codex``)
  ``/sessions/YYYY/MM/DD/rollout-*-<thread_id>.jsonl`` (or
  ``archived_sessions``). ``token_count`` events carry a cumulative
  ``total_token_usage`` that restarts when the thread is resumed into the
  same file (the restarting event's total equals its ``last_token_usage``),
  so each run's final cumulative value is summed. ``input_tokens`` includes
  ``cached_input_tokens``. ``turn_context`` carries ``model`` and ``effort``.
  Spawned Codex subagent threads live in their own rollouts and are not
  counted.

Normalised totals: ``input`` (uncached), ``cache_read``, ``cache_write``,
``output`` and ``reasoning``, where reasoning is part of output for both
harnesses. ``cache_hit_ratio`` is cache reads over all input.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path

CONTRACT = 'asha.session-usage.v1'
SUPPORTED = ('claude', 'codex')
FIELDS = ('input', 'cache_read', 'cache_write', 'output', 'reasoning')
NATIVE_ID_LIMIT = 16        # native conversations remembered per session
_FILE_LIMIT = 64            # records read per session
_SIZE_LIMIT = 256 << 20     # bytes per record
_READ_SECONDS = 10.0        # wall clock for one refresh
_SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', re.ASCII)


class Unreadable(Exception):
    """The native record exists but cannot be used; usage stays unknown."""


def unknown(harness, reason, native_ids=()):
    return {'contract': CONTRACT, 'status': 'unknown', 'reason': reason, 'harness': harness,
            'native_ids': list(native_ids), 'source': None, 'model': None, 'effort': None,
            'tokens': None, 'cache_hit_ratio': None, 'records': []}


def remember(row, native_id):
    """The session's native conversation IDs with ``native_id`` appended once, newest last."""
    known = list(row.get('native_ids') or ([row['native_id']] if row.get('native_id') else []))
    if native_id and native_id not in known:
        known.append(native_id)
    return known[-NATIVE_ID_LIMIT:]


def _home(env):
    return Path(env.get('HOME') or os.path.expanduser('~'))


def _locate(harness, native_id, env):
    if harness == 'claude':
        base = Path(env.get('CLAUDE_CONFIG_DIR') or _home(env) / '.claude') / 'projects'
        mains = sorted(glob.glob(str(Path(glob.escape(str(base))) / '*' / (native_id + '.jsonl'))))
        subagents = [p for main in mains
                     for p in sorted(glob.glob(glob.escape(main[:-len('.jsonl')]) + '/subagents/agent-*.jsonl'))]
        return mains, subagents
    base = Path(env.get('CODEX_HOME') or _home(env) / '.codex')
    escaped = glob.escape(str(base))
    pattern = 'rollout-*-' + native_id + '.jsonl'
    found = glob.glob(escaped + '/sessions/*/*/*/' + pattern) + glob.glob(escaped + '/archived_sessions/' + pattern)
    return sorted(set(found)), []


def _fingerprint(path, native_id, role):
    stat = os.stat(path)
    if stat.st_size > _SIZE_LIMIT:
        raise Unreadable(f'native record exceeds {_SIZE_LIMIT >> 20} MiB')
    return {'native_id': native_id, 'role': role, 'path': str(path), 'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns}


def _lines(path, needles, deadline):
    with open(path, 'rb') as handle:
        for number, line in enumerate(handle):
            if number % 4096 == 0 and time.monotonic() > deadline:
                raise Unreadable('native record read timed out')
            if any(needle in line for needle in needles):
                try:
                    value = json.loads(line)
                except ValueError:
                    continue    # a torn final line while the harness is writing
                if isinstance(value, dict):
                    yield value


def _label(value, current):
    """A model or effort name as displayable data, else the value already held."""
    if isinstance(value, str) and value and len(value.encode('utf-8')) <= 256 and value.isprintable():
        return value
    return current


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _claude(records, deadline):
    messages, model, effort = {}, None, None
    for record in records:
        for entry in _lines(record['path'], (b'"assistant"',), deadline):
            message = entry.get('message')
            if entry.get('type') != 'assistant' or not isinstance(message, dict):
                continue
            usage = message.get('usage')
            key = message.get('id') or entry.get('requestId') or entry.get('uuid')
            if isinstance(usage, dict) and isinstance(key, str):
                details = usage.get('output_tokens_details')
                messages[key] = {
                    'input': _count(usage.get('input_tokens')),
                    'cache_read': _count(usage.get('cache_read_input_tokens')),
                    'cache_write': _count(usage.get('cache_creation_input_tokens')),
                    'output': _count(usage.get('output_tokens')),
                    'reasoning': _count(details.get('thinking_tokens')) if isinstance(details, dict) else 0}
            if record['role'] == 'main' and message.get('model') != '<synthetic>' and _label(message.get('model'), None):
                model = message['model']
                effort = _label(entry.get('effort'), effort)
    totals = dict.fromkeys(FIELDS, 0)
    for usage in messages.values():
        for field in FIELDS:
            totals[field] += usage[field]
    return totals, model, effort


def _codex_vector(usage):
    total_input = _count(usage.get('input_tokens'))
    cached = min(_count(usage.get('cached_input_tokens')), total_input)
    return {'input': total_input - cached, 'cache_read': cached,
            'cache_write': _count(usage.get('cache_write_input_tokens')),
            'output': _count(usage.get('output_tokens')),
            'reasoning': _count(usage.get('reasoning_output_tokens'))}


def _codex(records, deadline):
    totals, model, effort = dict.fromkeys(FIELDS, 0), None, None
    for record in records:
        run = None      # (cumulative total_tokens, normalised vector) of the current run
        for entry in _lines(record['path'], (b'"token_count"', b'"turn_context"'), deadline):
            payload = entry.get('payload')
            if not isinstance(payload, dict):
                continue
            if entry.get('type') == 'turn_context':
                model = _label(payload.get('model'), model)
                effort = _label(payload.get('effort'), effort)
                continue
            info = payload.get('info')
            if payload.get('type') != 'token_count' or not isinstance(info, dict):
                continue
            cumulative, last = info.get('total_token_usage'), info.get('last_token_usage')
            if not isinstance(cumulative, dict):
                continue
            total = _count(cumulative.get('total_tokens'))
            last_total = _count(last.get('total_tokens')) if isinstance(last, dict) else None
            if run is not None and total != run[0] and (total == last_total or total < run[0]):
                # A resumed thread restarts its cumulative count: keep the finished run.
                for field in FIELDS:
                    totals[field] += run[1][field]
            run = (total, _codex_vector(cumulative))
        if run is not None:
            for field in FIELDS:
                totals[field] += run[1][field]
    return totals, model, effort


def _same_records(previous, records):
    keys = ('native_id', 'role', 'path', 'size', 'mtime_ns')
    return [[r.get(k) for k in keys] for r in previous] == [[r[k] for k in keys] for r in records]


def read(harness, native_ids, *, env, previous=None):
    """Usage across ``native_ids`` (oldest first); ``previous`` is reused while its records are unchanged."""
    native_ids = [n for n in (native_ids or []) if isinstance(n, str) and n][-NATIVE_ID_LIMIT:]
    if harness not in SUPPORTED:
        return unknown(harness, f'{harness} native usage records are not checked yet', native_ids)
    if not native_ids:
        return unknown(harness, 'no native conversation observed', native_ids)
    try:
        records, missing = [], []
        for native_id in native_ids:
            if _SAFE_ID.fullmatch(native_id) is None:
                missing.append(native_id)
                continue
            mains, subagents = _locate(harness, native_id, env)
            if not mains:
                missing.append(native_id)
            records += [_fingerprint(p, native_id, 'main') for p in mains]
            records += [_fingerprint(p, native_id, 'subagent') for p in subagents]
        if len(records) > _FILE_LIMIT:
            raise Unreadable(f'more than {_FILE_LIMIT} native records')
        if not records:
            return unknown(harness, 'native record not found', native_ids)
        if (previous and previous.get('status') == 'known' and previous.get('native_ids') == native_ids
                and previous.get('missing', []) == missing and _same_records(previous.get('records') or [], records)):
            return previous
        deadline = time.monotonic() + _READ_SECONDS
        totals, model, effort = (_claude if harness == 'claude' else _codex)(records, deadline)
    except (OSError, ValueError, Unreadable) as exc:
        return unknown(harness, 'native record unreadable: ' + str(exc)[:200], native_ids)
    inputs = totals['input'] + totals['cache_read'] + totals['cache_write']
    return {'contract': CONTRACT, 'status': 'known', 'reason': None, 'harness': harness, 'native_ids': native_ids,
            'missing': missing, 'source': 'claude-transcript' if harness == 'claude' else 'codex-rollout',
            'model': model, 'effort': effort, 'tokens': {**totals, 'total': inputs + totals['output']},
            'cache_hit_ratio': round(totals['cache_read'] / inputs, 4) if inputs else None,
            'records': records, 'read_at': time.time()}


def compact(count):
    """``999``, ``12k``, ``1.2M``: a token count in at most five cells."""
    if count < 1000:
        return str(count)
    for divisor, suffix in ((10 ** 9, 'G'), (10 ** 6, 'M'), (10 ** 3, 'k')):
        if count >= divisor:
            value = count / divisor
            return (f'{value:.1f}' if value < 10 else f'{value:.0f}') + suffix
    return str(count)


def _known_tokens(usage):
    """The token totals of a well-formed known reading, else ``None`` (stored rows are not trusted)."""
    if not isinstance(usage, dict) or usage.get('status') != 'known' or not isinstance(usage.get('tokens'), dict):
        return None
    tokens = usage['tokens']
    if not all(type(tokens.get(k)) is int and tokens[k] >= 0 for k in (*FIELDS, 'total')):
        return None
    return tokens


def tokens_label(usage):
    """The list column: total tokens, or empty while unknown."""
    tokens = _known_tokens(usage)
    return '' if tokens is None else compact(tokens['total'])


def line(usage):
    """The compact ``show`` line; unknown says why."""
    tokens = _known_tokens(usage)
    if tokens is None:
        reason = usage.get('reason') if isinstance(usage, dict) else None
        return 'tokens unknown: ' + (reason if isinstance(reason, str) and reason else 'not read')
    parts = [f"tokens {compact(tokens['total'])}: in {compact(tokens['input'])}",
             f"cache read {compact(tokens['cache_read'])}", f"cache write {compact(tokens['cache_write'])}",
             f"out {compact(tokens['output'])} (reasoning {compact(tokens['reasoning'])})"]
    if isinstance(usage.get('cache_hit_ratio'), (int, float)):
        parts.append(f"cache hit {usage['cache_hit_ratio'] * 100:.0f}%")
    parts += [f'{name} {usage[name]}' for name in ('model', 'effort') if _label(usage.get(name), None)]
    if isinstance(usage.get('missing'), list) and usage['missing']:
        parts.append(f"{len(usage['missing'])} conversation(s) without a record")
    return ' · '.join(parts)


__all__ = ['CONTRACT', 'FIELDS', 'SUPPORTED', 'compact', 'line', 'read', 'remember', 'tokens_label', 'unknown']
