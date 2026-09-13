"""Inspection and deliberate operator actions beneath `control session experience`."""
from __future__ import annotations
import argparse
import json
import sys
import time
from .session_experience import Experiences, safe_read, strict_json, canonical, silenced
from .store import StoreError


def dispatch(hub, argv):
    parser = argparse.ArgumentParser(prog='asha control session experience')
    sub = parser.add_subparsers(dest='verb', required=True)
    for name in ('list', 'show', 'packet', 'policy', 'review', 'pending', 'dispose', 'stats', 'guidance'):
        command = sub.add_parser(name)
        command.add_argument('--json', action='store_true')
        if name in {'show', 'packet'}:
            command.add_argument('report_id')
        else:
            command.add_argument('--project', required=True)
        if name in {'list', 'pending', 'guidance'}:
            command.add_argument('--offset', type=int, default=0)
            command.add_argument('--limit', type=int, default=50)
        if name == 'policy':
            command.add_argument('--mode', choices=('off', 'capture', 'review'))
            command.add_argument('--revision', type=int)
        if name == 'review':
            command.add_argument('--report', action='append', required=True)
            command.add_argument('--result-file', help='record a chair-reviewed frozen packet result; never launches inference')
        if name == 'dispose':
            command.add_argument('--decision-file', required=True)
            command.add_argument('--publication-file', required=True)
        if name == 'stats':
            command.add_argument('--since', type=float, default=0)
            command.add_argument('--until', type=float)
            command.add_argument('--revision', type=int)
            command.add_argument('--harness')
            command.add_argument('--model')
    args = parser.parse_args(argv)
    experiences = Experiences(hub)
    try:
        from .rooms import resolve_project
        selected = resolve_project(args.project, env=hub.env) if hasattr(args, 'project') else None
        pid = selected['project_id'] if selected else None
        if args.verb == 'show':
            result = experiences.show(args.report_id)
        elif args.verb == 'packet':
            from .experience_review import packet, packet_digest
            report = experiences.show(args.report_id)
            review_id = report['reviews'][-1]['review_id']
            body = packet(hub, review_id)
            result = {'report_id': args.report_id, 'review_id': review_id,
                      'packet_digest': packet_digest(body), 'packet': body}
        elif args.verb == 'list':
            result = experiences.page(pid, offset=args.offset, limit=args.limit)
        elif args.verb == 'policy':
            if args.mode is None:
                result = experiences.policy(pid)
            else:
                if args.revision is None:
                    raise StoreError('policy changes require the inspected --revision')
                result = experiences.set_policy(args.project, args.mode, expected_revision=args.revision)
        elif args.verb == 'pending':
            from .experience_adoption import pending
            result = pending(hub, pid, offset=args.offset, limit=args.limit)
        elif args.verb == 'stats':
            result = experiences.stats(pid, since=args.since, until=args.until, policy_revision=args.revision,
                                       harness=args.harness, model=args.model)
        elif args.verb == 'guidance':
            if type(args.offset) is not int or args.offset < 0 or not 1 <= args.limit <= 100:
                raise StoreError('invalid guidance page')
            rows = []
            total = 0
            if experiences.available():
                with hub.database() as db, db.transaction() as c:
                    rows = [dict(r) for r in c.execute('SELECT * FROM hub_guidance_exposures WHERE project_id=? ORDER BY created_at,exposure_id LIMIT ? OFFSET ?', (pid, args.limit, args.offset))]
                    total = c.execute('SELECT COUNT(*) FROM hub_guidance_exposures WHERE project_id=?', (pid,)).fetchone()[0]
                for row in rows:
                    row['manifest'] = json.loads(row['manifest'])
            end = args.offset + len(rows)
            result = {'rows': rows, 'total': total, 'complete': end >= total, 'next_offset': end if end < total else None}
        elif args.verb == 'review':
            from .experience_review import backfill
            if args.result_file:
                if len(args.report) != 1:
                    raise StoreError('manual review result binds exactly one inspected report')
                result = manual_review(hub, pid, args.report[0], safe_read(args.result_file, 16 * 1024))
            else:
                result = backfill(hub, pid, args.report)
        else:
            experiences.operator()  # Refuse before reading submitted publication/decision data.
            import save_identity
            from pathlib import Path
            # Environment seams are a local heuristic; the operator guard above
            # supplies authority. Do not issue a save identity from close.
            identity = next((hub.env[name].strip() for name in save_identity.ENV_SEAMS
                             if hub.env.get(name, '').strip() not in {'', 'unknown'}), None)
            if identity is None:
                identity = save_identity.resolve(Path(selected['root']), hub.env.get('ASHA_HARNESS', ''))
            from .experience_adoption import dispose
            decision = strict_json(safe_read(args.decision_file, 16 * 1024))
            publication = strict_json(safe_read(args.publication_file, 16 * 1024))
            if publication.get('contract') == 'asha.managed-none-save.v1':
                publication = publication.get('publication', {})
            result = dispose(hub, args.project, decision, publication, save_session_id=identity)
        print(json.dumps(result, ensure_ascii=True, indent=None if args.json else 2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print('asha experience: ' + str(exc), file=sys.stderr)
        return 2


def manual_review(hub, project_id, report_id, raw):
    """Explicit chair advisory result. No backend enforcement claim or paid launch."""
    from .experience_review import packet, packet_digest, decode_result
    experiences = Experiences(hub); experiences.operator()
    report = experiences.show(report_id)
    project = hub.get(report['session_id'])['project']
    if report['project_id'] != project_id or silenced(project) or experiences.policy(project_id)['mode'] == 'off':
        raise StoreError('manual review scope refused or silenced')
    row = report['reviews'][-1]
    body = packet(hub, row['review_id'])
    result = decode_result(raw, report, packet_digest(body))
    wrapped = canonical({'review': result, 'reviewer': 'operator-advisory', 'cost_usd': None, 'tokens': None})
    with hub.database() as db, db.transaction(write=True) as c:
        current = c.execute('SELECT * FROM hub_experience_reviews WHERE review_id=?', (row['review_id'],)).fetchone()
        if (silenced(project) or experiences.policy_in(c, project_id)['mode'] == 'off' or current['utility_id']
                or c.execute('SELECT 1 FROM hub_experiences WHERE supersedes=?', (report_id,)).fetchone()):
            raise StoreError('manual review cannot replace native custody or superseded evidence')
        if current['result']:
            if current['result'] != wrapped:
                raise StoreError('review receipt already binds another result')
        else:
            c.execute("UPDATE hub_experience_reviews SET status='completed',packet_digest=?,result=?,finished_at=? WHERE review_id=?",
                      (packet_digest(body), wrapped, time.time(), row['review_id']))
    return {'review_id': row['review_id'], 'status': 'completed', 'reviewer': 'operator-advisory', 'launched': False}
