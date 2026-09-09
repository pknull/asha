"""Private, bounded actor-to-owner requests over Unix sockets.

The endpoint is a selector, not a credential. Linux peer credentials and a peer
pidfd bind the caller to a live process; ancestry and retained generation/turn
checks bind it to the owner. Only the trusted owner opens the operational store.
"""
from __future__ import annotations

import json
import os
import select
import socket
import stat
import struct
import threading
import time
import uuid
from contextlib import ExitStack

from .harness import caller_descends_from, process_identity, verify_process
from .database import DatabaseError, DatabaseBusyError, BUSY_TIMEOUT_SECONDS
from .session_store import SessionStore, identifier, text, digest
from .store import StoreError, _directory_fd, _managed_start


PROTOCOL = "asha.session.request.v1"
MAX_FRAME = 128 * 1024
FRAME_TIMEOUT = 3.0
CLIENT_TIMEOUT = 10.0
# Linux UAPI asm-generic/socket.h. Python builds predating this option do not
# expose its name; kernels without it are explicitly unsupported.
SO_PEERPIDFD = getattr(socket, "SO_PEERPIDFD", 77)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StoreError("duplicate request field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise StoreError("invalid JSON constant")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StoreError("session request deadline exceeded")
    return remaining


def _read_exact(connection, count, deadline):
    chunks = []
    while count:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(count)
        if not chunk:
            raise StoreError("session request connection ended before receipt")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _receive(connection, deadline):
    size = struct.unpack("!I", _read_exact(connection, 4, deadline))[0]
    if not 2 <= size <= MAX_FRAME:
        raise StoreError("session request frame exceeds bounds")
    try:
        value = json.loads(_read_exact(connection, size, deadline),
                           object_pairs_hook=_unique, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StoreError("invalid session request JSON") from exc
    if not isinstance(value, dict):
        raise StoreError("session request must be an object")
    return value


def _send(connection, value, deadline, *, maximum=MAX_FRAME):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > maximum:
        raise StoreError("session request frame exceeds bounds")
    connection.settimeout(_remaining(deadline))
    connection.sendall(struct.pack("!I", len(raw)) + raw)


def _name(sid, turn):
    return identifier(sid) + "." + identifier(turn) + ".sock"


def _directory(config, *, create):
    path = config.tasks_dir.parent / "session-ipc"
    return _directory_fd(path, create=create,
                         managed_start=_managed_start(path, ("state", "control", "session-ipc")))


def _address(fd, name):
    # FD-relative proc paths avoid Unix's 108-byte pathname limit while retaining
    # the already validated directory even under a long ASHA_HOME.
    return f"/proc/self/fd/{fd}/{name}"


def _peer(connection):
    pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    try:
        handle = connection.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
    except OSError as exc:
        raise StoreError("managed request IPC requires Linux peer pidfd support") from exc
    try:
        os.set_inheritable(handle, False)
    except OSError:
        os.close(handle)
        raise
    return pid, uid, handle


def _peer_live(handle):
    poll = select.poll()
    poll.register(handle, select.POLLIN)
    return not poll.poll(0)


def capability_probe():
    """Probe the actual kernel contract without opening or changing Control state."""
    try:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with left, right:
            _pid, _uid, handle = _peer(left)
            os.close(handle)
        return {"supported": True, "protocol": PROTOCOL, "peer_identity": "Linux SO_PEERCRED + SO_PEERPIDFD"}
    except (StoreError, OSError, AttributeError) as exc:
        return {"supported": False, "protocol": PROTOCOL, "reason": str(exc)}


class SessionRequestServer:
    """One per-turn endpoint, with a separate bounded I/O thread and DB handle."""

    def __init__(self, config, sid, generation, turn):
        self.config, self.sid, self.turn = config, identifier(sid), identifier(turn)
        if type(generation) is not int or generation < 1:
            raise StoreError("invalid managed owner generation")
        self.generation = generation
        self.name = _name(sid, turn)
        self.path = config.tasks_dir.parent / "session-ipc" / self.name
        self.stack = ExitStack()
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.failure = None
        self.thread = None
        self.bound_identity = None
        self.listener = None
        self.active_connection = None
        self.connection_lock = threading.Lock()

    def __enter__(self):
        try:
            probe = capability_probe()
            if not probe["supported"]:
                raise StoreError(probe["reason"])
            with SessionStore(self.config) as store:
                with store.db.transaction() as c:
                    store._owner(c, self.sid, self.generation)
                    turn = c.execute("SELECT state,generation FROM session_turns WHERE turn_id=? AND session_id=?", (self.turn, self.sid)).fetchone()
                    if turn is None or turn["state"] != "running" or turn["generation"] != self.generation:
                        raise StoreError("request endpoint requires the owned active turn")
            self.fd = self.stack.enter_context(_directory(self.config, create=True))
            self.listener = self.stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            try:
                self.listener.bind(_address(self.fd, self.name))
            except OSError as exc:
                # A previous owner/turn's endpoint is never silently adopted.
                raise StoreError(f"cannot bind owned session request endpoint: {exc}") from exc
            metadata = os.stat(self.name, dir_fd=self.fd, follow_symlinks=False)
            self.bound_identity = (metadata.st_dev, metadata.st_ino)
            os.chmod(self.name, 0o600, dir_fd=self.fd, follow_symlinks=False)
            self.listener.listen(8)
            self.listener.settimeout(0.1)
            self.thread = threading.Thread(target=self._serve, name="asha-session-requests", daemon=True)
            self.thread.start()
            if not self.ready.wait(CLIENT_TIMEOUT):
                raise StoreError("session request owner startup timed out")
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.failure is not None:
            raise StoreError(f"session request channel unavailable: {self.failure}")

    def _serve(self):
        try:
            with SessionStore(self.config) as store:
                self.ready.set()
                while not self.stopping.is_set():
                    try:
                        connection, _ = self.listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        with self.connection_lock:
                            if self.stopping.is_set():
                                continue
                            self.active_connection = connection
                        try:
                            self._handle(store, connection)
                        finally:
                            with self.connection_lock:
                                self.active_connection = None
        except Exception as exc:
            self.failure = str(exc)[:1000]
            self.ready.set()

    def _handle(self, store, connection):
        handle = None
        request_id = None
        try:
            pid, uid, handle = _peer(connection)
            if uid != os.geteuid() or not _peer_live(handle):
                raise StoreError("session request peer is foreign or gone")
            identity = process_identity(pid)
            if not identity or not caller_descends_from(os.getpid(), start_pid=pid):
                raise StoreError("session request peer is outside the owner ancestry")
            request = _receive(connection, time.monotonic() + FRAME_TIMEOUT)
            request_id = request.get("request_id")
            expected = {"protocol", "kind", "session_id", "generation", "turn_id", "request_id", "question"}
            if set(request) != expected or request["protocol"] != PROTOCOL or request["kind"] != "ask":
                raise StoreError("unsupported session request operation or fields")
            if request["session_id"] != self.sid or request["turn_id"] != self.turn:
                raise StoreError("session request selects a different session or turn")
            if type(request["generation"]) is not int or request["generation"] != self.generation:
                raise StoreError("session request generation is stale")
            if not _peer_live(handle) or not verify_process(pid, identity):
                raise StoreError("session request peer changed before acceptance")
            result = store.request(self.sid, self.turn, request["question"],
                                   request_id=request_id, generation=self.generation)
            # The transaction commits before the response acknowledges custody.
            response = {"protocol": PROTOCOL, "request_id": request_id, "ok": True, "result": result}
        except (StoreError, OSError, ValueError) as exc:
            if isinstance(exc, DatabaseError) and not isinstance(exc, DatabaseBusyError):
                self.failure = str(exc)[:1000]
                self.stopping.set()
            response = {"protocol": PROTOCOL, "request_id": request_id, "ok": False,
                        "error": str(exc)[:1000], "retryable": isinstance(exc, DatabaseBusyError)}
        finally:
            if handle is not None:
                os.close(handle)
        try:
            _send(connection, response, time.monotonic() + FRAME_TIMEOUT)
        except (StoreError, OSError):
            # A lost response does not discard custody. Exact request-ID replay
            # retrieves the committed request without duplicating a question.
            pass

    def close(self):
        with self.connection_lock:
            self.stopping.set()
            active = self.active_connection
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            # Withdraw the endpoint before waiting for already accepted work.
            if self.bound_identity is not None:
                metadata = os.stat(self.name, dir_fd=self.fd, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != self.bound_identity or not stat.S_ISSOCK(metadata.st_mode):
                    raise StoreError("session request endpoint changed before cleanup")
                os.unlink(self.name, dir_fd=self.fd)
                self.bound_identity = None
            if self.thread is not None:
                self.thread.join(2 * FRAME_TIMEOUT + BUSY_TIMEOUT_SECONDS + 1)
                if self.thread.is_alive():
                    raise StoreError("session request owner did not drain before shutdown")
                self.thread = None
        finally:
            self.stack.close()

    def __exit__(self, *_args):
        self.close()
        if not _args or _args[0] is None:
            self.check()


def request_question(config, *, session_id, generation, turn_id, question, request_id=None):
    """Actor client: send a typed request without opening the database."""
    identifier(session_id)
    identifier(turn_id)
    text(question, "question")
    if type(generation) is not int or generation < 1:
        raise StoreError("invalid managed owner generation")
    request_id = identifier(request_id) if request_id is not None else str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"asha-question:{session_id}:{turn_id}:{question}"))
    request = {"protocol": PROTOCOL, "kind": "ask", "session_id": session_id,
               "generation": generation, "turn_id": turn_id,
               "request_id": request_id, "question": question}
    deadline = time.monotonic() + CLIENT_TIMEOUT
    for attempt in range(3):
        response = _exchange(config, request, deadline)
        if response.get("protocol") != PROTOCOL:
            raise StoreError("invalid session request response protocol")
        if response.get("ok") is True:
            result = response.get("result")
            if (response.get("request_id") != request_id or not isinstance(result, dict)
                    or result.get("request_id") != request_id or result.get("session_id") != session_id
                    or result.get("turn_id") != turn_id or result.get("kind") != "clarification"
                    or result.get("digest") != digest(question)):
                raise StoreError("session response does not match the request")
            return result
        if response.get("retryable") is not True or response.get("request_id") != request_id or attempt == 2:
            raise StoreError(str(response.get("error", "session request refused")))
        time.sleep(min(0.1 * (attempt + 1), _remaining(deadline)))
    raise StoreError("session request retries exhausted")


def _exchange(config, request, deadline):
    try:
        with _directory(config, create=False) as fd:
            if fd is None:
                raise StoreError("session request endpoint is unavailable")
            name = _name(request["session_id"], request["turn_id"])
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise StoreError("session request endpoint is not private and owned")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(_remaining(deadline))
                connection.connect(_address(fd, name))
                pid, uid, peer_handle = _peer(connection)
                try:
                    if uid != os.geteuid() or not _peer_live(peer_handle) or not caller_descends_from(pid):
                        raise StoreError("session request server is outside the actor ancestry")
                    _send(connection, request, deadline, maximum=MAX_FRAME - 4096)
                    response = _receive(connection, deadline)
                finally:
                    os.close(peer_handle)
    except OSError as exc:
        raise StoreError(f"session request transport unavailable: {exc}") from exc
    return response
