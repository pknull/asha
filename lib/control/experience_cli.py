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
    for name in ('list', 'show', 'packet', 'policy', 'review', 'unreviewed', 'pending', 'dispose', 'stats', 'guidance'):
        command = sub.add_parser(name)
        command.add_argument('--json', action='store_true')
        if name in {'show', 'packet'}:
            command.add_argument('report_id')
        else:
            command.add_argument('--project', required=True)
        if name in {'list', 'unreviewed', 'pending', 'guidance'}:
            command.add_argument('--offset', type=int, default=0)
            command.add_argument('--limit', type=int, default=50)
        if name == 'policy':
            policy_action = command.add_mutually_exclusive_group()
            policy_action.add_argument('--mode', choices=('off', 'capture', 'review'))
            policy_action.add_argument('--clear', action='store_true')
            policy_action.add_argument('--read-only', action='store_true', help='read policy; incompatible with mutation flags')
            command.add_argument('--revision', type=int)
        if name == 'review':
            command.add_argument('--report', action='append', required=True)
            review_action = command.add_mutually_exclusive_group()
            review_action.add_argument('--result-file', help='record an advisory frozen packet result; never launches inference')
            review_action.add_argument('--skip-own-lineage', action='store_true')
            command.add_argument('--publication-file', help='explicit-save receipt binding the five-report advisory budget')
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
            from .experience_review import packet_for_report, packet_digest
            report = experiences.show(args.report_id)
            revision = experiences.policy(report['project_id'])['revision']
            current = next((r for r in reversed(report['reviews']) if r['policy_revision'] == revision), None)
            review_id = current['review_id'] if current else None
            body = packet_for_report(hub, args.report_id)
            result = {'report_id': args.report_id, 'review_id': review_id,
                      'packet_digest': packet_digest(body), 'packet': body}
        elif args.verb == 'list':
            result = experiences.page(pid, offset=args.offset, limit=args.limit)
        elif args.verb == 'policy':
            if args.clear:
                result = experiences.clear_policy(args.project, expected_revision=args.revision)
            elif args.mode is None:
                result = experiences.policy(pid)
            else:
                result = experiences.set_policy(args.project, args.mode, expected_revision=args.revision)
        elif args.verb == 'unreviewed':
            from .experience_review import unreviewed
            result = unreviewed(hub, pid, offset=args.offset, limit=args.limit)
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
            if args.result_file or args.skip_own_lineage:
                if len(args.report) != 1:
                    raise StoreError('manual review result binds exactly one inspected report')
                experiences.saving_actor(pid, required=bool(args.publication_file), project=selected['root'])
                publication = strict_json(safe_read(args.publication_file, 16 * 1024)) if args.publication_file else None
                if publication and publication.get('contract') == 'asha.managed-none-save.v1':
                    publication = publication.get('publication', {})
                result = manual_review(hub, pid, args.report[0],
                    safe_read(args.result_file, 16 * 1024) if args.result_file else None,
                    publication=publication, skip_own_lineage=args.skip_own_lineage)
            else:
                if args.publication_file:
                    raise StoreError('publication requires --result-file or --skip-own-lineage')
                result = backfill(hub, pid, args.report)
        else:
            saver = experiences.saving_actor(pid, required=True, project=selected['root'])  # Refuse before reading submitted data.
            identity = saver['session_id']
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


def manual_review(hub, project_id, report_id, raw, *, publication=None, skip_own_lineage=False):
    """Advisory review under saving authority; native custody always takes precedence."""
    from .experience_review import packet_for_report, packet_digest, decode_result
    from .experience_adoption import validate_publication
    from .registry_guards import mutation_guard
    import uuid
    experiences = Experiences(hub)
    saver = experiences.saving_actor(project_id)
    report = experiences.show(report_id)
    project = hub.get(report['session_id'])['project']
    if publication is not None:
        saver = experiences.saving_actor(project_id, required=True, project=project)
    if report['project_id'] != project_id or silenced(project) or experiences.policy(project_id)['mode'] == 'off':
        raise StoreError('manual review scope refused or silenced')
    if publication is not None:
        validate_publication(publication, project_id)
        if saver['hub_actor']:
            from .session_publication import verify_publication
            verify_publication(hub, saver['hub_actor'], publication)
    if skip_own_lineage and publication is None:
        raise StoreError('recorded save-review skip requires an explicit-save publication')
    own = experiences.own_lineage(report, saver)
    if own and publication is None:
        raise StoreError('own session lineage cannot review its source report')
    if skip_own_lineage and not own:
        raise StoreError('report is not from the saving session lineage')
    reviewer = 'advisory-save-review' if publication is not None else 'operator-advisory'
    body = packet_for_report(hub, report_id) if not own else None
    result = decode_result(raw, report, packet_digest(body)) if not own else None
    wrapped = {'review': result, 'reviewer': reviewer, 'cost_usd': None, 'tokens': None}
    if publication is not None:
        wrapped.update(publication_id=publication['publication_id'], save_session_id=saver['session_id'])
    encoded = canonical(wrapped)
    hub.initialize()  # Additive save ledger migration only after authority/scope checks.
    with mutation_guard(hub.config), hub.database() as db, db.transaction(write=True) as c:
        if saver['hub_actor']:
            experiences.current(c, saver['hub_actor'])
        policy = experiences.policy_in(c, project_id)
        if (silenced(project) or policy['mode'] == 'off'
                or c.execute('SELECT 1 FROM hub_experiences WHERE supersedes=?', (report_id,)).fetchone()):
            raise StoreError('manual review cannot replace superseded evidence or bypass policy/silence')
        current = c.execute('SELECT * FROM hub_experience_reviews WHERE report_id=? AND policy_revision=? '
                            'ORDER BY attempt DESC LIMIT 1', (report_id, policy['revision'])).fetchone()
        if current and current['utility_id']:
            raise StoreError('manual review cannot replace native custody')
        if own:
            c.execute("INSERT OR IGNORE INTO hub_experience_save_reviews VALUES(?,?,?,?,?,?,?)",
                      (project_id, publication['publication_id'], report_id, saver['session_id'],
                       'skipped', 'own-session-lineage', time.time()))
            return {'report_id': report_id, 'status': 'skipped', 'reason': 'own-session-lineage', 'launched': False}
        if current and current['result']:
            if current['result'] != encoded:
                raise StoreError('review receipt already binds another result')
            return {'review_id': current['review_id'], 'status': 'completed', 'reviewer': reviewer, 'launched': False}
        status, reason = experiences.selection(report['body'], report_id, policy['revision'])
        if publication is not None:
            if status != 'selected':
                raise StoreError('save review requires a selected report at the current policy revision')
            count = c.execute("""SELECT COUNT(*) FROM hub_experience_reviews r JOIN hub_experiences e USING(report_id)
                WHERE e.project_id=? AND r.status='completed' AND json_extract(r.result,'$.publication_id')=?""",
                (project_id, publication['publication_id'])).fetchone()[0]
            if count >= 5:
                raise StoreError('at most five reports may be reviewed per explicit save publication')
        if current is None:
            review_id = str(uuid.uuid4())
            c.execute("INSERT INTO hub_experience_reviews(review_id,report_id,report_digest,policy_revision,reason,status,created_at) "
                      "VALUES(?,?,?,?,?,?,?)", (review_id, report_id, report['digest'], policy['revision'], reason, status, time.time()))
        else:
            review_id = current['review_id']
        c.execute("UPDATE hub_experience_reviews SET status='completed',packet_digest=?,result=?,finished_at=? WHERE review_id=?",
                  (packet_digest(body), encoded, time.time(), review_id))
    return {'review_id': review_id, 'status': 'completed', 'reviewer': reviewer, 'launched': False}
