"""Codex dynamic tools hosted by the fenced session owner, outside its sandbox.

Only ``ask`` is exposed: it retains a question for the operator. Durable call receipts
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
    'description': 'Ask the operator a question about this Asha session; ask retains it for a human answer.',
    'inputSchema': {'type': 'object', 'required': ['operation'], 'additionalProperties': False,
        'properties': {
            'operation': {'type': 'string', 'enum': ['ask']},
            'question': {'type': 'string'}}},
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
        if not isinstance(args, dict) or not isinstance(args.get('operation'), str):
            raise StoreError('actor operation must be an object with an operation name')
        if args['operation'] != 'ask' or set(args) - {'operation', 'question'}:
            raise StoreError('unsupported actor operation or fields')
        with sessions.db.transaction() as c:
            self._active(sessions, c)
        return sessions.request(self.sid, self.turn, args.get('question'), request_id=key,
                                generation=self.generation)
