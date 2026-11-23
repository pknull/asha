#!/usr/bin/env python3
"""Prepare source-bound revision comparisons and summarize supplied preferences.

No inference, prose edits, candidate promotion or author approval. Presentation
labels are anonymized, not an enforced blindness boundary: packet.json retains
the mapping for the operator. Give reviewers only comparison.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'novel-state/scripts'))
import artifact_freshness as freshness

SCHEMA = 'asha-revision-comparison/v1'
ELIGIBILITY = 'declared-by-caller'


def packet_identity(packet: dict) -> str:
    body = {key: value for key, value in packet.items() if key != 'packet_id'}
    return freshness.digest(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode('utf-8'))


def _quoted(text: str) -> str:
    fence = '`' * max(3, max((len(m[0]) + 1 for m in re.finditer(r'`+', text)), default=0))
    body = text if text.endswith('\n') else text + '\n'
    return f'{fence}text\n{body}{fence}\n'


def prepare(root: Path, objective: str, original: str, candidates: list[str],
            mode: str, output: str, seed: int | None = None, sources: list[str] | None = None) -> dict:
    if mode not in ('optional', 'required'):
        raise freshness.FreshnessError('mode must be optional or required')
    if len(candidates) < (1 if mode == 'optional' else 2):
        raise freshness.FreshnessError('comparison needs at least two eligible alternatives; required mode excludes original')
    destination = freshness.project_path(root, output)
    if destination.exists() or destination.is_symlink():
        raise freshness.FreshnessError('comparison output already exists')
    objective_bytes = freshness.read_bytes(root, objective)
    objective_text = objective_bytes.decode('utf-8')
    if not objective_text.strip():
        raise freshness.FreshnessError('objective must state the requested change and evaluation criteria')
    objective_entry = {'path': objective, 'mode': 'raw', 'sha256': freshness.digest(objective_bytes)}
    entries, texts = [], {}
    for path in [original, *candidates]:
        data = freshness.read_bytes(root, path)
        entries.append({'path': path, 'mode': 'raw', 'sha256': freshness.digest(data),
                        'role': 'original' if path == original else 'revision',
                        'eligible': path != original or mode == 'optional'})
        texts[path] = freshness.manuscript_bytes(data, path).decode('utf-8')
    authorities, authority_texts = [], []
    for path in sources or []:
        data = freshness.read_bytes(root, path)
        authorities.append({'path': path, 'mode': 'raw', 'sha256': freshness.digest(data)})
        authority_texts.append(data.decode('utf-8'))
    inputs = [objective_entry, *entries, *authorities]
    freshness.validate_entries(root, inputs, raw_only=True)
    eligible = [row for row in entries if row['eligible']]
    (random.Random(seed) if seed is not None else random.SystemRandom()).shuffle(eligible)
    for number, row in enumerate(eligible, 1):
        row['id'] = f'C{number}'
    references = [row for row in entries if not row['eligible']]
    for row in references:
        row['id'] = 'reference'
    entries = eligible + references
    presentation = '# Revision comparison\n\n## Objective\n\n' + objective_text.rstrip('\n') + '\n\n'
    presentation += ('Rank every eligible label exactly once against this objective and its declared voice/authority. '
                     'A preference is advisory; it does not authorize replacing prose.\n\n')
    for number, authority in enumerate(authority_texts, 1):
        presentation += f'## Declared authority {number}\n\n' + _quoted(authority) + '\n'
    if references:
        presentation += '## Original reference (ineligible)\n\n' + _quoted(texts[original]) + '\n'
    for entry in eligible:
        presentation += f'## Candidate {entry["id"]}\n\n' + _quoted(texts[entry['path']]) + '\n'
    presentation_path = f'{output}/comparison.md'
    packet = {'schema': SCHEMA, 'mode': mode, 'eligibility': ELIGIBILITY,
              'objective': objective_entry, 'entries': entries, 'sources': authorities,
              'presentation': {'path': presentation_path, 'mode': 'raw',
                               'sha256': freshness.digest(presentation.encode('utf-8'))}}
    packet['packet_id'] = packet_identity(packet)
    if freshness.applicability(freshness.inspect_entries(root, inputs)) != 'unchanged':
        raise freshness.FreshnessError('comparison inputs changed during preparation')
    destination.mkdir(parents=True, exist_ok=False)
    freshness.write_new(root, presentation_path, presentation.encode('utf-8'))
    # Publish identity last. An interrupted packet cannot pass check/summarize.
    freshness.write_new(root, f'{output}/packet.json', freshness.json_bytes(packet))
    return {'packet': f'{output}/packet.json', 'packet_id': packet['packet_id'],
            'presentation': presentation_path, 'eligible': [row['id'] for row in eligible]}


def validate_packet(root: Path, packet) -> dict:
    if not isinstance(packet, dict) or packet.get('schema') != SCHEMA or packet.get('eligibility') != ELIGIBILITY:
        raise freshness.FreshnessError('invalid comparison packet')
    if packet.get('packet_id') != packet_identity(packet):
        raise freshness.FreshnessError('comparison identity does not match its contents')
    mode, entries, sources = packet.get('mode'), packet.get('entries'), packet.get('sources')
    if mode not in ('optional', 'required') or not isinstance(entries, list) or not isinstance(sources, list):
        raise freshness.FreshnessError('invalid mode or candidate entries')
    freshness.validate_entries(root, [packet.get('objective'), *entries, *sources], raw_only=True)
    freshness.validate_entries(root, [packet.get('presentation')], raw_only=True)
    labels, original_count, eligible_count = set(), 0, 0
    for row in entries:
        role, label, eligible = row.get('role'), row.get('id'), row.get('eligible')
        if role not in ('original', 'revision') or not isinstance(eligible, bool):
            raise freshness.FreshnessError('invalid candidate role or eligibility')
        expected = role == 'revision' or mode == 'optional'
        if eligible != expected:
            raise freshness.FreshnessError('eligibility conflicts with the requested comparison mode')
        if not isinstance(label, str) or not (re.fullmatch(r'C[1-9][0-9]*', label) if eligible else label == 'reference'):
            raise freshness.FreshnessError('invalid candidate label')
        if label in labels:
            raise freshness.FreshnessError('duplicate candidate label')
        labels.add(label)
        original_count += role == 'original'
        eligible_count += eligible
    if original_count != 1 or eligible_count < 2:
        raise freshness.FreshnessError('comparison requires one original and at least two eligible alternatives')
    return packet


def _inspect_packet(root: Path, packet: dict) -> dict:
    checks = freshness.inspect_entries(root, [packet['objective'], *packet['entries'],
                                              *packet['sources'], packet['presentation']])
    return {'status': freshness.applicability(checks), 'packet_id': packet['packet_id'],
            'inputs': checks, 'eligibility': ELIGIBILITY,
            'meaning': 'Declared inputs and presentation only; compliance and author approval are not verified.'}


def check(root: Path, packet_path: str) -> dict:
    try:
        packet = validate_packet(root, freshness.read_json(root, packet_path))
        return _inspect_packet(root, packet)
    except (OSError, ValueError) as exc:
        return {'status': 'unknown', 'reason': str(exc)}


def summarize(root: Path, packet_path: str, judgments_path: str) -> dict:
    unknown = {'status': 'unknown', 'preferred': None, 'author_decision': None,
               'eligibility': ELIGIBILITY}
    try:
        packet = validate_packet(root, freshness.read_json(root, packet_path))
        checked = _inspect_packet(root, packet)
        if checked['status'] != 'unchanged':
            return {**unknown, 'reason': 'packet inputs are not unchanged', 'check': checked}
        judgments = freshness.read_json(root, judgments_path)
        if not isinstance(judgments, dict) or judgments.get('packet_id') != packet['packet_id']:
            raise freshness.FreshnessError('judgments belong to another comparison')
        rankings = judgments.get('rankings')
        if not isinstance(rankings, list) or not rankings:
            raise freshness.FreshnessError('no valid judgments supplied')
        eligible = [row['id'] for row in packet['entries'] if row['eligible']]
        scores = dict.fromkeys(eligible, 0)
        reviewers = set()
        for ranking in rankings:
            if not isinstance(ranking, dict):
                raise freshness.FreshnessError('ranking must be an object')
            reviewer, order = ranking.get('reviewer'), ranking.get('order')
            if not isinstance(reviewer, str) or not reviewer.strip() or reviewer.strip() in reviewers:
                raise freshness.FreshnessError('missing or duplicate reviewer identity')
            reviewers.add(reviewer.strip())
            if (not isinstance(order, list) or not all(isinstance(value, str) for value in order)
                    or len(order) != len(eligible) or len(set(order)) != len(order) or set(order) != set(eligible)):
                raise freshness.FreshnessError('ranking must contain every eligible label exactly once')
            for rank, label in enumerate(order):
                scores[label] += len(eligible) - rank - 1
        leaders = [label for label, score in scores.items() if score == max(scores.values())]
        return {'status': 'preference' if len(leaders) == 1 else 'tie',
                'preferred': leaders[0] if len(leaders) == 1 else None,
                'scores': scores, 'judgments_supplied': len(rankings),
                'packet_id': packet['packet_id'], 'eligibility': ELIGIBILITY,
                'author_decision': None,
                'meaning': 'Supplied preferences only; no author acceptance or prose replacement.'}
    except (OSError, ValueError) as exc:
        return {**unknown, 'reason': str(exc)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--objective', required=True, help='file containing purpose, voice/authority and acceptance criteria')
    prep.add_argument('--original', required=True)
    prep.add_argument('--candidate', required=True, action='append', help='already-compliant revision path, repeatable')
    prep.add_argument('--source', action='append', default=[], help='voice/canon/config authority text, raw-hashed and shown; repeatable')
    prep.add_argument('--mode', required=True, choices=('optional', 'required'))
    prep.add_argument('--output', required=True, help='new packet directory')
    prep.add_argument('--seed', type=int, help='reproducible presentation order; default uses random order')
    inspect = commands.add_parser('check')
    inspect.add_argument('--packet', required=True)
    summary = commands.add_parser('summarize')
    summary.add_argument('--packet', required=True)
    summary.add_argument('--judgments', required=True)
    for command in (prep, inspect, summary):
        command.add_argument('--project', required=True, help='explicit root; all file paths are relative to it')
    args = parser.parse_args(argv)
    try:
        root = freshness.project_root(args.project)
        if args.command == 'prepare':
            result = prepare(root, args.objective, args.original, args.candidate, args.mode, args.output,
                             args.seed, args.source)
        elif args.command == 'check':
            result = check(root, args.packet)
        else:
            result = summarize(root, args.packet, args.judgments)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return {'unknown': 2, 'changed': 1}.get(result.get('status'), 0)
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'unknown', 'preferred': None, 'author_decision': None, 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
