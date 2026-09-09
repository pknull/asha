"""Codex dynamic tools hosted by the fenced session owner, outside its sandbox.

Only existing session/coordinator operations are exposed. Durable call receipts
separate execution from reply transmission; an interrupted execution is inspected,
never automatically repeated. One worker keeps provider I/O responsive.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import uuid

from .record_registry import RecordRegistry
from .session_store import SessionStore
from .store import StoreError

MAX_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
TOOL = {
    'type': 'function', 'name': 'asha_control',
    'description': ('Operate this Asha session and its assigned initiative. ask retains a human question; '
        'inspect reads head or paged records; propose_plan takes a plan object; action takes '
        'action_class and payload. receive_message and ack_message take message_id; ack also '
        'requires the exact digest from receive. Operator approvals and integration are unavailable.'),
    'inputSchema': {'type': 'object', 'required': ['operation'], 'additionalProperties': False,
        'properties': {
            'operation': {'type': 'string', 'enum': ['ask', 'inspect', 'propose_plan', 'action',
                                                   'receive_message', 'ack_message']},
            'question': {'type': 'string'}, 'kind': {'type': 'string'},
            'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            'plan': {'type': 'object'}, 'action_class': {'type': 'string'}, 'payload': {'type': 'object'},
            'message_id': {'type': 'string'}, 'digest': {'type': 'string'}}},
}


def encode(value, *, limit=MAX_BYTES):
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise StoreError('actor call is not bounded JSON') from exc
    if len(raw) > limit:
        raise StoreError('actor result or arguments exceed byte limit; use a smaller inspection page')
    return raw


def response(value, *, success=True):
    return {'success': success, 'contentItems': [{'type': 'inputText', 'text': encode(value).decode()}]}


class CodexActor:
    def __init__(self, config, sid, generation, turn, *, env):
        self.config, self.sid, self.generation, self.turn = config, sid, generation, turn
        self.env = dict(env)
        self.receipts = RecordRegistry('session-native-actor', scope=turn)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asha-codex-actor')
        self.pending = {}
        self.calls = {}
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        cancelled = [self.calls[key] for key, future in self.pending.items() if future.cancel()]
        # Finish any in-flight effect before the owner publishes turn completion.
        self.executor.shutdown(wait=True, cancel_futures=True)
        if cancelled:
            with SessionStore(self.config) as store:
                for call_id in cancelled:
                    store.observe(self.sid, self.generation, self.turn, 'tool', {
                        'tool_id': call_id, 'name': 'asha_control', 'status': 'cancelled',
                        'reason': 'owner ended the turn before execution; no actor effect was started'})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def submit(self, key, call_id, arguments):
        if self.closed:
            raise StoreError('actor is closed')
        if key in self.pending:
            raise StoreError('duplicate actor submission')
        if len(self.pending) >= 8:
            return False  # No custody/effect; protocol sends an explicit refusal.
        # Copy at the transport boundary; the worker never sees mutable wire data.
        arguments = json.loads(encode(arguments))
        self.calls[key] = call_id
        self.pending[key] = self.executor.submit(self.execute, call_id, arguments)
        return True

    def poll(self):
        replies = []
        for key, future in list(self.pending.items()):
            if future.done():
                try:
                    result = future.result()
                except Exception as exc:
                    result = response({'error': str(exc)[:2000],
                        'operation_id': str(uuid.uuid5(uuid.UUID(self.turn), self.calls[key])),
                        'instruction': 'Execution may have committed. Inspect retained state before issuing new work.'},
                        success=False)
                replies.append((key, result))
                del self.pending[key]
                del self.calls[key]
        return replies

    def _active(self, store, c):
        session = store._owner(c, self.sid, self.generation)
        if session['harness'] != 'codex' or session['stop_requested']:
            raise StoreError('actor belongs to a foreign or stopping session')
        turn = c.execute('SELECT state,generation FROM session_turns WHERE turn_id=? AND session_id=?',
                         (self.turn, self.sid)).fetchone()
        if turn is None or turn['state'] != 'running' or turn['generation'] != self.generation:
            raise StoreError('actor requires the current running turn')
        return session

    def execute(self, call_id, arguments):
        if not isinstance(call_id, str) or not call_id or len(call_id.encode()) > 512:
            raise StoreError('invalid actor call ID')
        raw = encode(arguments)
        digest = hashlib.sha256(raw).hexdigest()
        key = str(uuid.uuid5(uuid.UUID(self.turn), call_id))
        with SessionStore(self.config) as store:
            with store.db.transaction(write=True) as c:
                session = self._active(store, c)
                old = self.receipts.read(c, key)
                if old:
                    if old['value']['arguments_digest'] != digest:
                        raise StoreError('actor call ID arguments changed')
                    if 'response' in old['value']:
                        return old['value']['response']
                    return response({'error': 'execution uncertain; inspect retained state before issuing new work',
                                     'operation_id': key}, success=False)
                receipt = {'session_id': self.sid, 'generation': self.generation, 'turn_id': self.turn,
                    'call_id': call_id, 'operation_id': key, 'arguments_digest': digest,
                    'arguments': arguments, 'state': 'started'}
                previous = self.receipts.put(c, key, encode(receipt, limit=MAX_RECEIPT_BYTES), state='started')
            try:
                result = response({'operation_id': key, 'result': self._perform(store, session, key, arguments)})
            except Exception as exc:
                # A core operation may have committed before raising. Keep its
                # stable ID visible and prohibit automatic replay of this call.
                result = response({'operation_id': key, 'error': str(exc)[:2000],
                                   'instruction': 'Inspect retained state before issuing new work.'}, success=False)
            with store.db.transaction(write=True) as c:
                # Result custody is permitted even if stop arrived during an
                # effect. It does not authorize another effect or revive a turn.
                receipt.update(state='finished', response=result)
                # The receipt contains both bounded arguments and a JSON text
                # response (escaped again). Their combined bound is distinct
                # from the per-call wire bound and matches RecordRegistry.
                self.receipts.put(c, key, encode(receipt, limit=MAX_RECEIPT_BYTES),
                                  expected_digest=previous, state='finished')
            return result

    def _perform(self, sessions, session, key, args):
        fields = {'ask': {'question'}, 'inspect': {'kind', 'offset', 'limit'}, 'propose_plan': {'plan'},
                  'action': {'action_class', 'payload'}, 'receive_message': {'message_id'},
                  'ack_message': {'message_id', 'digest'}}
        if not isinstance(args, dict) or not isinstance(args.get('operation'), str):
            raise StoreError('actor operation must be an object with an operation name')
        operation = args['operation']
        if operation not in fields or set(args) - fields[operation] - {'operation'}:
            raise StoreError('unsupported actor operation or fields')
        with sessions.db.transaction() as c:
            self._active(sessions, c)
        if operation == 'ask':
            return sessions.request(self.sid, self.turn, args.get('question'), request_id=key,
                                    generation=self.generation)
        iid = session['initiative_id']
        if iid is None:
            raise StoreError('session has no assigned initiative')
        from .orchestration.config import from_control
        from .orchestration.store import InitiativeStore
        from .orchestration import coordinator, messages
        from .tmux import TmuxAdapter
        store = InitiativeStore(from_control(self.config))
        current = coordinator.require_live_coordinator(store, iid)
        anchor = current['anchor']
        if (anchor.get('kind') != 'managed-session-v1' or anchor.get('session_id') != self.sid
                or anchor.get('generation') != self.generation):
            raise StoreError('actor does not own this coordinator generation')
        coordinator.require_anchored_caller(current, self.env, TmuxAdapter())
        if operation == 'inspect':
            return self._inspect(store, iid, current, args)
        if operation in {'receive_message', 'ack_message'}:
            fn = messages.receive if operation == 'receive_message' else messages.ack
            extra = {'digest': args.get('digest')} if operation == 'ack_message' else {}
            return fn(store, iid, args.get('message_id'), env=self.env, tmux=TmuxAdapter(),
                      coordinator_id=current['coordinator_id'], generation=current['generation'], **extra)
        if operation == 'propose_plan':
            from .orchestration.cli import propose_plan
            from .jj import JjAdapter
            if not isinstance(args.get('plan'), dict):
                raise StoreError('plan must be an object')
            return propose_plan(store, store.peek(iid), args['plan'], config=store.config, jj=JjAdapter(),
                                actor_kind='coordinator', actor_id=coordinator.actor_id(current))
        from .orchestration.actions import COORDINATOR_ACTION_KINDS, build_action_document, submit_action
        kind = args.get('action_class')
        if not isinstance(kind, str) or kind not in COORDINATOR_ACTION_KINDS or not isinstance(args.get('payload'), dict):
            raise StoreError('only coordinator action classes with object payloads are allowed')
        document = build_action_document(store.peek(iid), kind, args['payload'], action_id=key,
            actor_id=coordinator.actor_id(current), coordinator=current)
        return submit_action(store, iid, document)

    @staticmethod
    def _inspect(store, iid, current, args):
        from .orchestration import messages
        kind = args.get('kind', 'head')
        head = store.peek(iid)
        if kind == 'head':
            return {'initiative': head, 'coordinator': current,
                    'active_plan': store.read_plan(iid, head['active_plan']['revision']) if head['active_plan'] else None}
        readers = {'plans': store.list_plans_snapshot, 'nodes': store.list_nodes_snapshot,
            'attempts': store.list_attempts_snapshot, 'actions': store.list_actions_snapshot,
            'seals': store.list_seals_snapshot, 'reviews': store.list_reviews_snapshot,
            'verifications': store.list_verifications_snapshot, 'events': store.list_events_snapshot,
            'approvals': store.list_approvals_snapshot, 'evidence': store.list_evidence_snapshot,
            'bundles': store.list_bundles_snapshot, 'results': store.list_results_snapshot}
        if not isinstance(kind, str) or kind not in {*readers, 'messages'}:
            raise StoreError('unsupported inspection kind')
        offset, limit = args.get('offset', 0), args.get('limit', 20)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise StoreError('invalid inspection page')
        rows = messages.pending(store, iid, current=current)['messages'] if kind == 'messages' else readers[kind](iid)
        return {'state_revision': head['state_revision'], 'records': rows[offset:offset + limit],
                'next_offset': offset + limit if offset + limit < len(rows) else None}
