#!/usr/bin/env python3
"""Capture declared writing inputs and check immutable artifact receipts.

An unchanged verdict concerns declared bytes only. It proves neither complete
dependency coverage, semantic correctness, nor author approval. No source,
artifact, existing receipt, state pointer or author decision is ever modified.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'inline-review/scripts'))
import inline_review

INPUT_SCHEMA = 'asha-writing-inputs/v1'
RECEIPT_SCHEMA = 'asha-writing-receipt/v1'
COVERAGE = 'declared-inputs-only'
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


class FreshnessError(ValueError):
    pass


def project_root(value) -> Path:
    try:
        root = Path(value).resolve(strict=True)
    except RuntimeError as exc:
        raise FreshnessError(f'cannot resolve project: {exc}') from exc
    if not root.is_dir():
        raise FreshnessError('project must be a directory')
    return root


def project_path(root: Path, relative: str) -> Path:
    """Keep portable logical paths, including current symlinks inside a project."""
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise FreshnessError('paths must be nonempty project-relative POSIX paths')
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != relative or relative == '.':
        raise FreshnessError(f'not a normalized project-relative path: {relative}')
    target = root / path
    try:
        resolved = target.resolve()
    except RuntimeError as exc:
        raise FreshnessError(f'cannot resolve {relative}: {exc}') from exc
    if not resolved.is_relative_to(root):
        raise FreshnessError(f'path leaves project: {relative}')
    return target


def read_bytes(root: Path, relative: str) -> bytes:
    path = project_path(root, relative)
    if not stat.S_ISREG(path.stat().st_mode):
        raise FreshnessError(f'not a regular file: {relative}')
    return path.read_bytes()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manuscript_bytes(data: bytes, path: str) -> bytes:
    return inline_review.project(data.decode('utf-8'), path).text.encode('utf-8')


def fingerprint(root: Path, relative: str, mode: str) -> dict:
    data = read_bytes(root, relative)
    if mode == 'manuscript':
        data = manuscript_bytes(data, relative)
    elif mode != 'raw':
        raise FreshnessError(f'unknown hash mode: {mode}')
    return {'path': relative, 'mode': mode, 'sha256': digest(data)}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FreshnessError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def read_json(root: Path, relative: str):
    return json.loads(read_bytes(root, relative), object_pairs_hook=_unique_object)


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def write_new(root: Path, relative: str, data: bytes) -> None:
    """Publish complete bytes without replacing any existing path."""
    path = project_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.receipt-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # link creates the destination atomically and refuses even dangling links.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_entries(root: Path, entries, *, raw_only: bool = False) -> list[dict]:
    if not isinstance(entries, list) or not entries:
        raise FreshnessError('expected a nonempty file list')
    result, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise FreshnessError('file entry must be an object')
        relative, mode, sha = (entry.get(k) for k in ('path', 'mode', 'sha256'))
        project_path(root, relative)
        if mode not in (('raw',) if raw_only else ('raw', 'manuscript')):
            raise FreshnessError('invalid hash mode')
        if not isinstance(sha, str) or not DIGEST.fullmatch(sha):
            raise FreshnessError('invalid SHA-256')
        key = (relative, mode)
        if key in seen:
            raise FreshnessError(f'duplicate declaration: {relative} ({mode})')
        seen.add(key)
        result.append({'path': relative, 'mode': mode, 'sha256': sha})
    return result


def inspect_entries(root: Path, entries: list[dict]) -> list[dict]:
    results = []
    for entry in entries:
        result = {'path': entry['path'], 'mode': entry['mode']}
        try:
            current = fingerprint(root, entry['path'], entry['mode'])
            result['status'] = 'unchanged' if current['sha256'] == entry['sha256'] else 'changed'
        except FileNotFoundError:
            result.update(status='missing', reason='file is unavailable')
        except (OSError, ValueError) as exc:
            result.update(status='unreadable', reason=str(exc))
        results.append(result)
    return results


def applicability(results: list[dict]) -> str:
    if any(row['status'] == 'changed' for row in results):
        return 'changed'
    if not results or any(row['status'] != 'unchanged' for row in results):
        return 'unknown'
    return 'unchanged'


def capture(root: Path, declarations: list[tuple[str, str]], output: str) -> dict:
    inputs = validate_entries(root, [fingerprint(root, path, mode) for path, mode in declarations])
    snapshot = {'schema': INPUT_SCHEMA, 'coverage': COVERAGE,
                'captured_at': datetime.now(timezone.utc).isoformat(), 'inputs': inputs}
    write_new(root, output, json_bytes(snapshot))
    return snapshot


def record(root: Path, snapshot_path: str, artifacts: list[str], output: str) -> dict:
    snapshot_raw = read_bytes(root, snapshot_path)
    snapshot = json.loads(snapshot_raw, object_pairs_hook=_unique_object)
    if not isinstance(snapshot, dict) or snapshot.get('schema') != INPUT_SCHEMA or snapshot.get('coverage') != COVERAGE:
        raise FreshnessError('invalid input snapshot')
    inputs = validate_entries(root, snapshot.get('inputs'))
    outputs = validate_entries(root, [fingerprint(root, path, 'raw') for path in artifacts], raw_only=True)
    state = applicability(inspect_entries(root, inputs))
    if state != 'unchanged':
        raise FreshnessError(f'inputs {state} since capture; preserve the report as historical and review current sources')
    receipt = {'schema': RECEIPT_SCHEMA, 'coverage': COVERAGE,
               'recorded_at': datetime.now(timezone.utc).isoformat(),
               'snapshot_sha256': digest(snapshot_raw), 'inputs': inputs, 'artifacts': outputs}
    write_new(root, output, json_bytes(receipt))
    return receipt


def check(root: Path, receipt_path: str) -> dict:
    try:
        receipt = read_json(root, receipt_path)
        if not isinstance(receipt, dict) or receipt.get('schema') != RECEIPT_SCHEMA or receipt.get('coverage') != COVERAGE:
            raise FreshnessError('invalid receipt schema or coverage')
        sha = receipt.get('snapshot_sha256')
        if not isinstance(sha, str) or not DIGEST.fullmatch(sha):
            raise FreshnessError('missing input snapshot identity')
        inputs = inspect_entries(root, validate_entries(root, receipt.get('inputs')))
        artifacts = inspect_entries(root, validate_entries(root, receipt.get('artifacts'), raw_only=True))
        return {'status': applicability(inputs + artifacts), 'coverage': COVERAGE,
                'inputs': inputs, 'artifacts': artifacts,
                'meaning': 'Declared bytes only; completeness, conclusions and author approval are not verified.'}
    except (OSError, ValueError) as exc:
        return {'status': 'unknown', 'coverage': COVERAGE, 'reason': str(exc),
                'inputs': [], 'artifacts': []}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture_parser = commands.add_parser('capture', help='snapshot inputs before analysis')
    capture_parser.add_argument('--manuscript', action='append', default=[], help='clean projected prose input, repeatable')
    capture_parser.add_argument('--source', action='append', default=[], help='raw authority/config input, repeatable')
    record_parser = commands.add_parser('record', help='bind unchanged captured inputs to resulting artifacts')
    record_parser.add_argument('--snapshot', required=True)
    record_parser.add_argument('--artifact', required=True, action='append')
    check_parser = commands.add_parser('check', help='read-only applicability check; missing legacy receipt is unknown')
    check_parser.add_argument('--receipt', required=True)
    for command in (capture_parser, record_parser, check_parser):
        command.add_argument('--project', required=True, help='explicit project root; other paths are relative to it')
    capture_parser.add_argument('--output', required=True, help='new snapshot path, never overwritten')
    record_parser.add_argument('--output', required=True, help='new receipt path, never overwritten')
    args = parser.parse_args(argv)
    try:
        root = project_root(args.project)
        if args.command == 'capture':
            result = capture(root, [(p, 'manuscript') for p in args.manuscript] + [(p, 'raw') for p in args.source], args.output)
        elif args.command == 'record':
            result = record(root, args.snapshot, args.artifact, args.output)
        else:
            result = check(root, args.receipt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return {'changed': 1, 'unknown': 2}.get(result.get('status'), 0)
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'unknown', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
