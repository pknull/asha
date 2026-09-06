#!/usr/bin/env python3
"""Strict asha-inline-review/v1 parser, prose projection and explicit thread edits.

Text APIs take decoded UTF-8 and preserve newline bytes. Mutation APIs take paths.
No third-party dependencies. See ../references/grammar.md for the public contract.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

START = re.compile(r'<!-- REVIEW (C[0-9]+) START -->')
END = re.compile(r'<!-- REVIEW (C[0-9]+) END')
SPEAKER = re.compile(r'(?:LLM(?:\([A-Za-z0-9_-]+\))?|AUTHOR)')
SPEECH = re.compile(r'(?:LLM(?:\([A-Za-z0-9_-]+\))?|AUTHOR): .+')
STATUS = re.compile(r'STATUS: (open|resolved|dismissed)')
FENCE = re.compile(r' {0,3}(`{3,}|~{3,})(.*)')
RESERVED = re.compile(r'<!--\s*REVIEW\b')


@dataclass(frozen=True)
class Diagnostic:
    file: str
    line: int
    message: str

    def __str__(self):
        return f'{self.file}:{self.line}: {self.message}'


class ReviewError(ValueError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__('\n'.join(map(str, diagnostics)))


@dataclass
class Thread:
    id: str
    start_line: int
    end_line: int
    close_line: int
    status_line: int
    status: str
    note: str
    excerpt: str


@dataclass
class Document:
    text: str
    file: str
    lines: list[str]
    threads: list[Thread] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    protected: set[int] = field(default_factory=set)
    frontmatter_end: int = 0
    fenced_lines: set[int] = field(default_factory=set)  # Zero-based, including delimiters.
    comment_spans: list[tuple[int, int]] = field(default_factory=list)  # Half-open text offsets.

    def strict(self):
        if self.diagnostics:
            raise ReviewError(self.diagnostics)
        return self


@dataclass
class Projection:
    text: str
    # Index zero describes projected line one; values are one-based source lines.
    line_map: list[int]
    file: str

    def source_line(self, projected_line: int) -> int:
        if not 1 <= projected_line <= len(self.line_map):
            raise ValueError('projected line out of range')
        return self.line_map[projected_line - 1]


def parse(text: str, file: str = '<text>') -> Document:
    """Parse without writing; diagnostics are recoverable, results are not editable."""
    # Only LF (optionally preceded by CR) ends a source line. splitlines()
    # also splits Unicode/control separators, shifting editor line numbers.
    parts = text.split('\n')
    lines = [part + '\n' for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    doc = Document(text, str(file), lines)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def error(index, message):
        doc.diagnostics.append(Diagnostic(str(file), index + 1, message))

    i = 0
    if lines and lines[0].rstrip('\r\n') == '---':
        i = 1
        while i < len(lines) and lines[i].rstrip('\r\n') not in ('---', '...'):
            i += 1
        if i == len(lines):
            error(0, 'unterminated frontmatter')
        else:
            i += 1
        doc.frontmatter_end = i
        doc.protected.update(range(i))
    active = None
    seen = set()
    fence = None
    comment = False
    comment_start = 0
    declaration = False
    while i < len(lines):
        raw = lines[i].rstrip('\r\n')
        if fence:
            doc.protected.add(i)
            doc.fenced_lines.add(i)
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}[ \t]*', raw):
                fence = None
            i += 1
            continue
        # Legacy comments are opaque to the grammar, including fenced examples.
        if not comment:
            fm = FENCE.fullmatch(raw)
            if fm and not (fm[1][0] == '`' and '`' in fm[2]):
                fence = (fm[1][0], len(fm[1]))
                doc.protected.add(i)
                doc.fenced_lines.add(i)
                if active:
                    error(i, 'review span cannot cross fenced code')
                i += 1
                continue
            sm, em = START.fullmatch(raw), END.fullmatch(raw)
            if sm:
                doc.protected.add(i)
                doc.comment_spans.append((offsets[i], offsets[i] + len(raw)))
                if active:
                    error(i, 'nested or overlapping review span')
                else:
                    active = (sm[1], i)
                if sm[1] in seen:
                    error(i, f'duplicate ID {sm[1]}')
                seen.add(sm[1])
                i += 1
                continue
            if em:
                end = i
                if not active:
                    error(i, f'{em[1]} END has no START')
                elif active[0] != em[1]:
                    error(i, f'mismatched END {em[1]}, expected {active[0]}')
                elif active[1] + 1 == i:
                    error(i, 'empty span')
                statuses = []
                speeches = 0
                can_continue = False
                i += 1
                while i < len(lines) and lines[i].rstrip('\r\n') != '-->':
                    note = lines[i].rstrip('\r\n')
                    if '-->' in note or '<!--' in note:
                        error(i, 'comment delimiters forbidden in note text')
                    status_match = STATUS.fullmatch(note)
                    if status_match:
                        statuses.append((i, status_match[1]))
                        can_continue = False
                    elif SPEECH.fullmatch(note):
                        speeches += 1
                        can_continue = True
                    elif note.startswith('  ') and can_continue:
                        pass
                    else:
                        error(i, 'expected explicit speaker, two-space continuation, or STATUS')
                    i += 1
                if i == len(lines):
                    error(end, 'unterminated review note')
                if len(statuses) != 1:
                    error(end, 'note requires exactly one STATUS: open|resolved|dismissed')
                if not speeches:
                    error(end, 'note requires an explicit speaker line')
                doc.protected.update(range(end, min(i + 1, len(lines))))
                close_offset = offsets[i] + 3 if i < len(lines) else len(text)
                doc.comment_spans.append((offsets[end], close_offset))
                if active and len(statuses) == 1 and i < len(lines):
                    doc.threads.append(Thread(active[0], active[1] + 1, end + 1, i + 1,
                                              statuses[0][0] + 1, statuses[0][1],
                                              ''.join(lines[end + 1:i]),
                                              ''.join(lines[active[1] + 1:end])))
                active = None
                i += 1
                continue
            if raw == '<!-- REVIEW-GRAMMAR v1 -->':
                doc.protected.add(i)
                doc.comment_spans.append((offsets[i], offsets[i] + len(raw)))
                if declaration:
                    error(i, 'duplicate grammar declaration')
                if active:
                    error(i, 'grammar declaration inside span')
                declaration = True
                i += 1
                continue
        # Scan legacy HTML comments, retaining source and marking unsafe edit lines.
        pos = 0
        while pos < len(raw):
            if comment:
                doc.protected.add(i)
                close = raw.find('-->', pos)
                if close < 0:
                    break
                comment = False
                pos = close + 3
                doc.comment_spans.append((comment_start, offsets[i] + pos))
            else:
                opening = raw.find('<!--', pos)
                if opening < 0:
                    break
                if RESERVED.match(raw, opening):
                    error(i, 'malformed/non-whole-line marker or unknown grammar version')
                doc.protected.add(i)
                comment = True
                comment_start = offsets[i] + opening
                pos = opening + 4
        i += 1
    if active:
        error(active[1], f'missing END for {active[0]}')
    if comment:
        doc.comment_spans.append((comment_start, len(text)))
        error(max(0, len(lines) - 1), 'unterminated HTML comment')
    return doc


def check(text: str, file: str = '<text>') -> list[Diagnostic]:
    return parse(text, file).diagnostics


def _without_reviews(doc: Document) -> str:
    """Strong mutation invariant: retain *all* non-review bytes, not just prose."""
    remove = set()
    for thread in doc.threads:
        remove.add(thread.start_line - 1)
        remove.update(range(thread.end_line - 1, thread.close_line))
    return ''.join(line for i, line in enumerate(doc.lines) if i not in remove)


def project(text: str, file: str = '<text>', *, headings: bool = True) -> Projection:
    """Strict clean view: remove frontmatter and parsed comments, preserve fences."""
    doc = parse(text, file).strict()
    # Mask comment bytes rather than joining separated paragraphs. A comment-only
    # line disappears; original blank lines and newlines beside prose survive.
    # Reuse the parser's classification: fenced bytes are opaque, while a
    # comment opened outside a fence consumes any fence-looking lines within it.
    mask = bytearray(len(text))
    for start, end in doc.comment_spans:
        mask[start:end] = b'\1' * (end - start)
    output, mapping = [], []
    offset = sum(map(len, doc.lines[:doc.frontmatter_end]))
    for source, line in enumerate(doc.lines[doc.frontmatter_end:], doc.frontmatter_end + 1):
        flags = mask[offset:offset + len(line)]
        # Only the trailing CR in a CRLF ending is a newline byte; a bare
        # carriage return inside a comment is content and must be removed.
        crlf_cr = len(line) - 2 if line.endswith('\r\n') else -1
        kept = ''.join(c for j, c in enumerate(line) if not flags[j] or c == '\n' or j == crlf_cr)
        drop_heading = False
        if not headings and source - 1 not in doc.fenced_lines:
            drop_heading = bool(re.match(r' {0,3}#{1,6}(?:[ \t]|$)', kept.rstrip('\r\n')))
        if not drop_heading and (not any(flags) or kept.strip()):
            output.append(kept)
            mapping.append(source)
        offset += len(line)
    return Projection(''.join(output), mapping, str(file))


def read_source(path) -> str:
    return Path(path).read_bytes().decode('utf-8')


def archive_path(path) -> Path:
    return Path(path).with_name(Path(path).name + '.review-archive.json')


def _fail(path, line, message):
    raise ReviewError([Diagnostic(str(path), line, message)])


def _archive_records(path):
    dest = archive_path(path)
    if dest.is_symlink():
        _fail(dest, 1, 'archive symlinks are refused')
    if not dest.exists():
        return [], None
    try:
        snapshot = dest.read_bytes()
        data = json.loads(snapshot.decode('utf-8'))
        if not isinstance(data, dict) or set(data) != {'grammar', 'threads'} or data['grammar'] != 'asha-inline-review/v1':
            raise ValueError('invalid archive header')
        records = data['threads']
        if not isinstance(records, list):
            raise ValueError('threads must be a list')
        ids = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {'file', 'id', 'line', 'excerpt', 'thread', 'provenance'}:
                raise ValueError('invalid archive record')
            if not isinstance(record['id'], str) or not re.fullmatch(r'C[0-9]+', record['id']) or record['id'] in ids:
                raise ValueError('invalid or duplicate retired ID')
            ids.add(record['id'])
            if not all(isinstance(record[k], str) for k in ('file', 'excerpt', 'thread')) or type(record['line']) is not int or record['line'] < 1:
                raise ValueError('invalid archive source fields')
            provenance = record['provenance']
            if not isinstance(provenance, dict) or set(provenance) != {'source_sha256', 'archived_at', 'operation'}:
                raise ValueError('invalid archive provenance')
            if not isinstance(provenance['source_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', provenance['source_sha256']) or provenance['operation'] != 'explicit archive':
                raise ValueError('invalid archive provenance values')
            if not isinstance(provenance['archived_at'], str):
                raise ValueError('invalid archive timestamp')
            saved = parse(record['thread'], str(dest)).strict()
            if len(saved.threads) != 1 or saved.threads[0].id != record['id'] or saved.threads[0].excerpt != record['excerpt']:
                raise ValueError('archive thread does not match record')
        return records, snapshot
    except (ValueError, TypeError, KeyError) as exc:
        _fail(dest, 1, f'invalid archive: {exc}')


@contextmanager
def _locked(path):
    path = Path(path)
    # Lock the directory inode, not the replaced file inode; no persistent lock
    # sidecar. Serializes cooperating mutations for all manuscripts in the folder.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.is_symlink():
            _fail(path, 1, 'mutation requires a regular non-symlink file')
        # stat raises an I/O error for missing/inaccessible inputs (CLI exit 2),
        # rather than misclassifying missing data as a grammar diagnostic.
        if not stat.S_ISREG(path.stat().st_mode):
            _fail(path, 1, 'mutation requires a regular non-symlink file')
        yield path
    finally:
        os.close(fd)


def _atomic(path, text, expected):
    path = Path(path)
    current = path.read_bytes() if path.exists() else None
    if path.is_symlink() or current != expected:
        _fail(path, 1, 'file changed during operation; retry after reading it')
    mode = stat.S_IMODE(path.stat().st_mode) if current is not None else 0o600
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink() or (path.read_bytes() if path.exists() else None) != expected:
            _fail(path, 1, 'file changed during operation; retry after reading it')
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_edit(before, text):
    after = parse(text, before.file).strict()
    if _without_reviews(before) != _without_reviews(after):
        _fail(before.file, 1, 'edit would change non-review bytes')
    if project(before.text, before.file).text != project(text, before.file).text:
        _fail(before.file, 1, 'edit would change clean prose')
    return after


def _message(path, speaker, text, newline):
    if not isinstance(speaker, str) or not SPEAKER.fullmatch(speaker):
        _fail(path, 1, 'explicit speaker must be AUTHOR, LLM, or LLM(agent-name)')
    if not isinstance(text, str) or not text.strip() or '<!--' in text or '-->' in text:
        _fail(path, 1, 'note text must be nonempty and contain no comment delimiters')
    parts = text.splitlines()
    if not parts[0].strip() or any(not p.startswith('  ') for p in parts[1:]):
        _fail(path, 1, 'continuation lines must start with two spaces')
    return speaker + ': ' + newline.join(parts) + newline


def _newline(doc):
    return '\r\n' if doc.lines and doc.lines[0].endswith('\r\n') else '\n'


def annotate(path, start_line: int, end_line: int, *, speaker: str, text: str, id: str | None = None) -> Document:
    """Wrap an inclusive one-based source-line range; never change its bytes."""
    with _locked(path) as path:
        before = parse(read_source(path), str(path)).strict()
        records, archive_snapshot = _archive_records(path)
        if not 1 <= start_line <= end_line <= len(before.lines):
            _fail(path, max(1, start_line), 'invalid inclusive line range')
        occupied = set(before.protected)
        for thread in before.threads:
            occupied.update(range(thread.start_line - 1, thread.close_line))
        if occupied.intersection(range(start_line - 1, end_line)):
            _fail(path, start_line, 'span intersects existing review, frontmatter, comment or fenced code')
        if not before.lines[end_line - 1].endswith('\n'):
            _fail(path, end_line, 'selected final line needs an author-supplied newline')
        used = {t.id for t in before.threads} | {r['id'] for r in records}
        if id is None:
            id = 'C' + str(max([int(value[1:]) for value in used], default=0) + 1)
        if not re.fullmatch(r'C[0-9]+', id) or id in used:
            _fail(path, start_line, 'ID must be C<digits>, unused and not archived')
        nl = _newline(before)
        note = _message(path, speaker, text, nl)
        lines = before.lines
        updated = (''.join(lines[:start_line - 1]) + f'<!-- REVIEW {id} START -->{nl}'
                   + ''.join(lines[start_line - 1:end_line]) + f'<!-- REVIEW {id} END{nl}'
                   + note + f'STATUS: open{nl}-->{nl}' + ''.join(lines[end_line:]))
        after = _validate_edit(before, updated)
        _atomic(path, updated, before.text.encode('utf-8'))
        return after


def _edit_thread(path, id, operation, *, speaker=None, text=None, status=None):
    with _locked(path) as path:
        before = parse(read_source(path), str(path)).strict()
        records, archive_snapshot = _archive_records(path)
        thread = next((t for t in before.threads if t.id == id), None)
        if thread is None:
            _fail(path, 1, f'unknown thread {id}')
        lines = before.lines.copy()
        nl = _newline(before)
        if operation == 'reply':
            # Append after every existing reply, immediately before the terminator.
            lines.insert(thread.close_line - 1, _message(path, speaker, text, nl))
        elif operation == 'status':
            if status not in ('open', 'resolved', 'dismissed'):
                _fail(path, thread.status_line, 'invalid status')
            old = lines[thread.status_line - 1]
            ending = old[len(old.rstrip('\r\n')):]
            lines[thread.status_line - 1] = f'STATUS: {status}' + ending
        elif operation == 'archive':
            del lines[thread.end_line - 1:thread.close_line]
            del lines[thread.start_line - 1]
        after = _validate_edit(before, ''.join(lines))
        if operation == 'archive':
            saved = ''.join(before.lines[thread.start_line - 1:thread.close_line])
            existing = next((r for r in records if r['id'] == id), None)
            if existing and existing['thread'] != saved:
                _fail(path, thread.start_line, 'retired ID conflicts with archived thread; preserve both and recover manually')
            if not existing:
                records.append({'file': path.name, 'id': id, 'line': thread.start_line,
                                'excerpt': thread.excerpt, 'thread': saved,
                                'provenance': {'source_sha256': hashlib.sha256(before.text.encode('utf-8')).hexdigest(),
                                               'archived_at': datetime.now(timezone.utc).isoformat(),
                                               'operation': 'explicit archive'}})
                dest = archive_path(path)
                # Durable archive first: a crash can duplicate a thread, never lose it.
                _atomic(dest, json.dumps({'grammar': 'asha-inline-review/v1', 'threads': records}, indent=2) + '\n', archive_snapshot)
        _atomic(path, after.text, before.text.encode('utf-8'))
        return after


def reply(path, id: str, *, speaker: str, text: str) -> Document:
    return _edit_thread(path, id, 'reply', speaker=speaker, text=text)


def set_status(path, id: str, status: str) -> Document:
    """Explicit author-directed status change; replies never close threads."""
    return _edit_thread(path, id, 'status', status=status)


def archive(path, id: str) -> Document:
    """Explicitly retire a thread, retaining prose and a durable sibling record."""
    return _edit_thread(path, id, 'archive')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('check', 'list', 'show', 'project', 'annotate', 'reply', 'status', 'archive'):
        sub = commands.add_parser(command)
        sub.add_argument('file')
        sub.add_argument('--json', action='store_true')
        if command in ('show', 'reply', 'status', 'archive'):
            sub.add_argument('id')
        if command in ('annotate', 'reply'):
            sub.add_argument('--speaker', required=True)
            sub.add_argument('--text', required=True)
        if command == 'annotate':
            sub.add_argument('start_line', type=int)
            sub.add_argument('end_line', type=int)
            sub.add_argument('--id')
        if command == 'project':
            sub.add_argument('--no-headings', action='store_true',
                             help='Drop ATX heading lines outside fenced code')
        if command == 'status':
            sub.add_argument('status', choices=('open', 'resolved', 'dismissed'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'annotate':
            doc = annotate(args.file, args.start_line, args.end_line, speaker=args.speaker, text=args.text, id=args.id)
        elif args.command == 'reply':
            doc = reply(args.file, args.id, speaker=args.speaker, text=args.text)
        elif args.command == 'status':
            doc = set_status(args.file, args.id, args.status)
        elif args.command == 'archive':
            doc = archive(args.file, args.id)
        else:
            doc = parse(read_source(args.file), args.file)
        if args.command == 'check':
            if args.json:
                print(json.dumps([asdict(d) for d in doc.diagnostics]))
            else:
                for diagnostic in doc.diagnostics:
                    print(diagnostic)
            return 1 if doc.diagnostics else 0
        doc.strict()
        if args.command == 'project':
            projection = project(doc.text, args.file, headings=not args.no_headings)
            print(json.dumps(asdict(projection)) if args.json else projection.text, end='\n' if args.json else '')
        else:
            threads = doc.threads
            if args.command == 'show':
                threads = [t for t in threads if t.id == args.id]
                if not threads:
                    _fail(args.file, 1, f'unknown thread {args.id}')
            if args.json:
                print(json.dumps([asdict(t) for t in threads]))
            else:
                for thread in threads:
                    print(f'{args.file}#{thread.id}:{thread.start_line}: {thread.status}')
                    if args.command == 'show':
                        print(thread.excerpt + thread.note, end='')
        return 0
    except ReviewError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as exc:
        print(f'{args.file}:1: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
