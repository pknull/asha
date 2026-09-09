"""SQLite custody for operational Control state.

One embedded database beneath the Control state root holds the transactional
record domains the file registries do not: managed sessions, delivery queues,
outstanding requests, and ordered event cursors.  This module owns the
connection, its durability settings, ownership checks for the file and its
WAL/SHM sidecars, and short fenced transactions.  Record semantics belong to
the domain stores built on top of it.
"""
from __future__ import annotations

import os
import hashlib
import json
import sqlite3
import stat
import threading
import secrets
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import quote

from .config import ControlConfig
from .store import (
    StoreError, _CLOEXEC, _NOFOLLOW, _close_quietly, _directory_fd,
    _managed_start, _validate_open_file,
)


DATABASE_NAME = "control.sqlite3"
_FILE_CREATION_LOCK = threading.RLock()
SIDECAR_SUFFIXES = ("-wal", "-shm")
# ASCII "ASHA"; stamped into the header so a foreign SQLite file is refused.
APPLICATION_ID = 0x41534841
SCHEMA_VERSION = 5
SCHEMA_ID = "asha.control.sqlite.v1"
BUSY_TIMEOUT_SECONDS = 5.0
MAX_BUSY_TIMEOUT_SECONDS = 60.0
MINIMUM_SQLITE_VERSION = (3, 31, 0)
REQUIRED_SETTINGS = {"journal_mode": "wal", "foreign_keys": 1, "synchronous": 2}
_BUSY_CODES = {5, 6}  # SQLITE_BUSY, SQLITE_LOCKED


class DatabaseError(StoreError):
    """The Control database refused an operation."""


class DatabaseBusyError(DatabaseError):
    """Another connection held the lock past the bounded busy timeout."""


def _wrap(exc: sqlite3.Error, action: str) -> DatabaseError:
    code = getattr(exc, "sqlite_errorcode", None)
    busy = (code & 0xFF) in _BUSY_CODES if isinstance(code, int) else False
    message = str(exc).lower()
    if busy or (isinstance(exc, sqlite3.OperationalError)
                and ("locked" in message or "busy" in message)):
        return DatabaseBusyError(f"Control database is busy: {action}: {exc}")
    return DatabaseError(f"Control database {action}: {exc}")


class Transaction:
    """One fenced statement surface; refuses use after its transaction ends."""

    __slots__ = ("_connection", "write")

    def __init__(self, connection: sqlite3.Connection, *, write: bool):
        self._connection: sqlite3.Connection | None = connection
        self.write = write

    def _live(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseError("Control database transaction has ended")
        return self._connection

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        try:
            return self._live().execute(sql, parameters)
        except sqlite3.Error as exc:
            raise _wrap(exc, "statement failed") from exc

    def executemany(self, sql: str, parameters: Any) -> sqlite3.Cursor:
        try:
            return self._live().executemany(sql, parameters)
        except sqlite3.Error as exc:
            raise _wrap(exc, "statement failed") from exc

    def close(self) -> None:
        self._connection = None


class ControlDatabase:
    """Owned SQLite connection with private layout and durable settings."""

    def __init__(
        self,
        config: ControlConfig,
        *,
        create: bool = False,
        busy_timeout: float = BUSY_TIMEOUT_SECONDS,
        initialize=None,
        migrate: bool = False,
        allow_legacy_reads: bool = False,
        read_only: bool = False,
    ):
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
            raise DatabaseError(
                f"linked SQLite {sqlite3.sqlite_version} is older than required "
                + ".".join(str(part) for part in MINIMUM_SQLITE_VERSION)
            )
        if (isinstance(busy_timeout, bool) or not isinstance(busy_timeout, (int, float))
                or not 0 < busy_timeout <= MAX_BUSY_TIMEOUT_SECONDS):
            raise DatabaseError("busy timeout must be a bounded positive number of seconds")
        self.config = config
        self.root = config.tasks_dir.parent
        self._managed_start = _managed_start(self.root, ("state", "control"))
        self.path = self.root / DATABASE_NAME
        self.busy_timeout = float(busy_timeout)
        self._initializer = initialize
        self._allow_migration = migrate
        self._allow_legacy_reads = allow_legacy_reads
        if read_only and (create or migrate or initialize is not None):
            raise DatabaseError("read-only inspection cannot create, initialize or migrate a database")
        self._read_only = read_only
        self._connection: sqlite3.Connection | None = self._open(create=create)

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> ControlDatabase:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            self._rollback(connection)
        finally:
            try:
                connection.close()
            except sqlite3.Error as exc:
                raise _wrap(exc, "cannot close") from exc

    def _live(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseError("Control database is closed")
        return self._connection

    # -- opening -----------------------------------------------------------

    def _open(self, *, create: bool) -> sqlite3.Connection:
        with _directory_fd(
            self.root, create=create, managed_start=self._managed_start,
        ) as directory_fd:
            if directory_fd is None:
                raise DatabaseError(f"Control state directory does not exist: {self.root}")
            # The creator's ordinary fd must close before another local opener
            # can acquire SQLite locks on the newly visible inode.
            with _FILE_CREATION_LOCK:
                identity = self._prepare_file(directory_fd, create=create)
            self._inspect_sidecars(directory_fd)
            connection = self._connect()
            try:
                self._verify_identity(identity, directory_fd)
                # Classify before configuring: setting WAL writes page one, and
                # a refused file must be left exactly as it was found.
                fresh = self._classify(connection, create=create, migrate=self._allow_migration or self._allow_legacy_reads)
                self._configure(connection, change_journal=not (self._allow_legacy_reads or self._read_only))
                if fresh:
                    self._initialize(connection, self._initializer)
                elif self._allow_migration:
                    self._migrate(connection)
            except BaseException:
                connection.close()
                raise
            return connection

    def _prepare_file(self, directory_fd: int, *, create: bool) -> tuple[int, int]:
        """Validate or create the 0600 database file; return its dev/inode identity."""
        try:
            metadata = self._inspect_file(directory_fd, DATABASE_NAME, "Control database")
        except FileNotFoundError:
            if not create:
                raise DatabaseError(
                    "Control database does not exist; open it with create=True first"
                ) from None
            try:
                fd = self._create_file(directory_fd)
            except FileExistsError:
                # Another creator won. Inspect without opening its live inode.
                try:
                    metadata = self._inspect_file(directory_fd, DATABASE_NAME, "Control database")
                except FileNotFoundError as exc:
                    raise DatabaseError("Control database disappeared during creation") from exc
            else:
                try:
                    metadata = os.fstat(fd)
                finally:
                    _close_quietly(fd)
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _inspect_file(directory_fd: int, name: str, label: str):
        # Closing ANY ordinary descriptor for a SQLite database or SHM inode
        # drops this process's POSIX locks, including other live connections.
        # Inspect through the validated directory without opening that inode.
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise DatabaseError(f"cannot inspect {label}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise DatabaseError(f"symlinked {label} rejected: {name}")
        if not stat.S_ISREG(metadata.st_mode):
            raise DatabaseError(f"{label} is not a regular file")
        if metadata.st_uid != os.geteuid():
            raise DatabaseError(f"{label} is not owned by the effective user")
        if metadata.st_nlink != 1:
            raise DatabaseError(f"{label} link count must be exactly 1")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise DatabaseError(f"{label} must have mode 0600")
        return metadata

    @staticmethod
    def _create_file(directory_fd: int) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
        try:
            fd = os.open(DATABASE_NAME, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            raise
        except OSError as exc:
            raise DatabaseError(f"cannot create Control database: {exc}") from exc
        try:
            os.fchmod(fd, 0o600)
            _validate_open_file(fd, "Control database")
            os.fsync(fd)
            os.fsync(directory_fd)
        except OSError as exc:
            _close_quietly(fd)
            raise DatabaseError(f"cannot establish Control database durability: {exc}") from exc
        except Exception:
            _close_quietly(fd)
            raise
        return fd

    @staticmethod
    def _inspect_sidecars(directory_fd: int) -> None:
        """SQLite copies the database mode onto WAL/SHM files; refuse tampered ones."""
        for suffix in SIDECAR_SUFFIXES:
            try:
                ControlDatabase._inspect_file(
                    directory_fd, DATABASE_NAME + suffix, "Control database sidecar",
                )
            except FileNotFoundError:
                continue

    def _connect(self) -> sqlite3.Connection:
        # mode=rw never creates a file: the validated inode must already exist.
        mode = "ro" if self._read_only else "rw"
        uri = "file:" + quote(str(self.path), safe="/") + "?mode=" + mode + "&nofollow=1"
        try:
            connection = sqlite3.connect(
                uri, uri=True, timeout=self.busy_timeout, isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise _wrap(exc, "cannot open") from exc
        connection.row_factory = sqlite3.Row
        return connection

    def _verify_identity(self, identity: tuple[int, int], directory_fd: int) -> None:
        try:
            pinned = self._inspect_file(directory_fd, DATABASE_NAME, "Control database")
            metadata = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise DatabaseError(f"cannot inspect Control database: {exc}") from exc
        if (not stat.S_ISREG(metadata.st_mode)
                or (metadata.st_dev, metadata.st_ino) != identity
                or (pinned.st_dev, pinned.st_ino) != identity):
            raise DatabaseError("Control database changed identity while opening")

    @staticmethod
    def _settings(connection: sqlite3.Connection) -> dict[str, Any]:
        settings: dict[str, Any] = {}
        for name in REQUIRED_SETTINGS:
            value = connection.execute(f"PRAGMA {name}").fetchone()[0]
            settings[name] = value.lower() if isinstance(value, str) else value
        return settings

    def _configure(self, connection: sqlite3.Connection, *, change_journal=True) -> None:
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if change_journal:
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            settings = self._settings(connection)
        except sqlite3.Error as exc:
            raise _wrap(exc, "cannot configure") from exc
        for name, expected in REQUIRED_SETTINGS.items():
            # Offline backups use DELETE mode. Inspection must not mutate their
            # journal; writable connections still require WAL.
            if name == "journal_mode" and not change_journal and settings[name] == "delete":
                continue
            if settings[name] != expected:
                raise DatabaseError(
                    f"Control database storage refused required setting "
                    f"{name}={expected!r} (got {settings[name]!r})"
                )

    @staticmethod
    def _classify(connection: sqlite3.Connection, *, create: bool, migrate=False) -> bool:
        """Return True for a fresh file that may be stamped; refuse foreign ones.

        Only reads run here.  A file carrying another application id or any
        schema object is not adopted, whatever ``create`` says.
        """
        try:
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            if application_id == APPLICATION_ID:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in range(1, SCHEMA_VERSION + 1):
                    raise DatabaseError("unsupported Control database schema version")
                if version < SCHEMA_VERSION and not migrate:
                    raise DatabaseError("Control database requires explicit schema migration; run asha control session migrate")
                metadata = connection.execute("SELECT identity FROM control_schema").fetchall()
                if [r[0] for r in metadata] != [SCHEMA_ID]:
                    raise DatabaseError("invalid Control database schema identity")
                return False
            objects = connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        except sqlite3.Error as exc:
            raise _wrap(exc, "cannot verify identity") from exc
        if application_id or objects:
            raise DatabaseError("file is not an Asha Control database")
        if not create:
            raise DatabaseError("Control database is empty; open it with create=True first")
        return True

    @staticmethod
    def _initialize(connection: sqlite3.Connection, initializer=None) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            # A concurrent explicit creator may have initialized after our read.
            if connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID:
                ControlDatabase._classify(connection, create=False, migrate=True)
                connection.execute("COMMIT")
                ControlDatabase._migrate(connection)
                return
            connection.execute("CREATE TABLE control_schema (identity TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO control_schema VALUES(?)", (SCHEMA_ID,))
            connection.execute("""CREATE TABLE records (
                domain TEXT NOT NULL, scope TEXT NOT NULL, record_key TEXT NOT NULL,
                payload TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL,
                state TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(domain,scope,record_key))""")
            connection.execute("CREATE INDEX records_state ON records(domain,state,updated_at,scope,record_key)")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID:d}")
            connection.execute("PRAGMA user_version=1")
            if initializer is not None:
                initializer(connection)
            ControlDatabase._upgrade_v2(connection)
            ControlDatabase._upgrade_v3(connection)
            ControlDatabase._upgrade_v4(connection)
            ControlDatabase._upgrade_v5(connection)
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            ControlDatabase._rollback(connection)
            raise _wrap(exc, "cannot stamp identity") from exc
        except BaseException:
            ControlDatabase._rollback(connection)
            raise

    @staticmethod
    def _migrate(connection):
        """Upgrade under one exclusive writer transaction; failed DDL rolls back."""
        try:
            connection.execute("BEGIN IMMEDIATE")
            ControlDatabase._classify(connection, create=False, migrate=True)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                ControlDatabase._upgrade_v2(connection)
            if version <= 2:
                ControlDatabase._upgrade_v3(connection)
            if version <= 3:
                ControlDatabase._upgrade_v4(connection)
            if version <= 4:
                ControlDatabase._upgrade_v5(connection)
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            ControlDatabase._rollback(connection)
            raise _wrap(exc, "schema migration failed") from exc
        except BaseException:
            ControlDatabase._rollback(connection)
            raise

    @staticmethod
    def _upgrade_v2(connection):
        # Explicit integer identity keeps the FTS content binding stable across
        # VACUUM. Preserve the old rowids and all canonical authority fields.
        connection.execute("ALTER TABLE records RENAME TO records_v1")
        connection.execute("""CREATE TABLE records (
            record_id INTEGER PRIMARY KEY, domain TEXT NOT NULL, scope TEXT NOT NULL,
            record_key TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
            revision INTEGER NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL,
            search_text TEXT NOT NULL DEFAULT '', UNIQUE(domain,scope,record_key))""")
        connection.execute("""INSERT INTO records(record_id,domain,scope,record_key,payload,digest,revision,state,updated_at)
            SELECT rowid,domain,scope,record_key,payload,digest,revision,state,updated_at FROM records_v1""")
        connection.execute("DROP TABLE records_v1")
        connection.execute("CREATE INDEX records_state ON records(domain,state,updated_at,scope,record_key)")
        # Decode JSON for Unicode search without changing any canonical bytes,
        # revisions, timestamps, or authority digests.
        after = None
        while True:
            rows = connection.execute("SELECT rowid,payload FROM records " +
                ("" if after is None else "WHERE rowid>? ") + "ORDER BY rowid LIMIT 100",
                () if after is None else (after,)).fetchall()
            if not rows:
                break
            connection.executemany("UPDATE records SET search_text=? WHERE rowid=?", [
                (json.dumps(json.loads(row[1]), ensure_ascii=False), row[0]) for row in rows])
            after = rows[-1][0]
        ControlDatabase._install_record_search(connection)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='session_messages'").fetchone():
            ControlDatabase.install_message_search(connection)
        connection.execute("""CREATE TABLE control_runtime (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            mode TEXT NOT NULL CHECK(mode IN ('running','paused','draining','stopped')),
            revision INTEGER NOT NULL, reason TEXT NOT NULL)""")
        connection.execute("INSERT INTO control_runtime VALUES(1,'running',0,'')")
        connection.execute("PRAGMA user_version=2")

    @staticmethod
    def _upgrade_v3(c):
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='session_requests'").fetchone():
            from .native_requests import install
            install(c)
        c.execute("PRAGMA user_version=3")

    @staticmethod
    def _upgrade_v4(c):
        # Sequence is derived from the retained filename, never rewritten into
        # the signed payload. Unique positive sequences make count/min/max a
        # proof of contiguous keys without loading the complete journal.
        valid = ("length(record_key)=48 AND substr(record_key,7,1)='-' "
                 "AND substr(record_key,44)='.json' "
                 "AND substr(record_key,1,6) NOT GLOB '*[^0-9]*' "
                 "AND substr(record_key,1,6)!='000000'")
        if c.execute("SELECT 1 FROM records WHERE domain='initiative.events' AND NOT (" + valid + ") LIMIT 1").fetchone():
            raise DatabaseError("invalid initiative event key; repair before schema migration")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS initiative_event_sequences ON records(scope,substr(record_key,1,6)) WHERE domain='initiative.events'")
        for operation in ("INSERT", "UPDATE"):
            c.execute("CREATE TRIGGER IF NOT EXISTS initiative_event_key_" + operation.lower()
                      + " BEFORE " + operation + " ON records WHEN new.domain='initiative.events' AND NOT ("
                      + valid.replace("record_key", "new.record_key")
                      + ") BEGIN SELECT RAISE(ABORT,'invalid initiative event key'); END")
        c.execute("PRAGMA user_version=4")

    @staticmethod
    def _upgrade_v5(c):
        c.execute("CREATE INDEX records_scope_state ON records(domain,scope,state,updated_at,record_key)")
        c.execute("PRAGMA user_version=5")

    @staticmethod
    def _install_record_search(c):
        c.execute("CREATE VIRTUAL TABLE records_search USING fts5(search_text, content='records', content_rowid='rowid')")
        c.execute("""CREATE TRIGGER records_search_insert AFTER INSERT ON records BEGIN
            INSERT INTO records_search(rowid,search_text) VALUES(new.rowid,new.search_text); END""")
        c.execute("""CREATE TRIGGER records_search_delete AFTER DELETE ON records BEGIN
            INSERT INTO records_search(records_search,rowid,search_text) VALUES('delete',old.rowid,old.search_text); END""")
        c.execute("""CREATE TRIGGER records_search_update AFTER UPDATE OF search_text ON records BEGIN
            INSERT INTO records_search(records_search,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
            INSERT INTO records_search(rowid,search_text) VALUES(new.rowid,new.search_text); END""")
        c.execute("INSERT INTO records_search(records_search) VALUES('rebuild')")

    @staticmethod
    def install_message_search(c):
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='messages_search'").fetchone():
            return
        c.execute("CREATE VIRTUAL TABLE messages_search USING fts5(body, content='session_messages', content_rowid='sequence')")
        c.execute("""CREATE TRIGGER messages_search_insert AFTER INSERT ON session_messages BEGIN
            INSERT INTO messages_search(rowid,body) VALUES(new.sequence,new.body); END""")
        c.execute("""CREATE TRIGGER messages_search_delete AFTER DELETE ON session_messages BEGIN
            INSERT INTO messages_search(messages_search,rowid,body) VALUES('delete',old.sequence,old.body); END""")
        c.execute("""CREATE TRIGGER messages_search_update AFTER UPDATE OF body ON session_messages BEGIN
            INSERT INTO messages_search(messages_search,rowid,body) VALUES('delete',old.sequence,old.body);
            INSERT INTO messages_search(rowid,body) VALUES(new.sequence,new.body); END""")
        c.execute("INSERT INTO messages_search(messages_search) VALUES('rebuild')")

    def rebuild_search(self):
        """Reconstruct derived indexes without modifying authority records."""
        with self.transaction(write=True) as c:
            c.execute("INSERT INTO records_search(records_search) VALUES('rebuild')")
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='messages_search'").fetchone():
                c.execute("INSERT INTO messages_search(messages_search) VALUES('rebuild')")

    # -- transactions ------------------------------------------------------

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except sqlite3.Error:
            # The caller is already unwinding; close() surfaces a dead handle.
            pass

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[Transaction]:
        """Run one short transaction; writes take the lock up front.

        Read transactions are query-only, so a stray write fails loudly instead
        of escalating a shared snapshot.  The yielded handle refuses statements
        once the transaction has committed or rolled back.
        """
        connection = self._live()
        if write and (self._allow_legacy_reads or self._read_only):
            raise DatabaseError("inspection connection is read-only")
        if connection.in_transaction:
            raise DatabaseError("Control database transaction is already active")
        handle = Transaction(connection, write=write)
        try:
            connection.execute("PRAGMA query_only=0" if write else "PRAGMA query_only=1")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
        except sqlite3.Error as exc:
            raise _wrap(exc, "cannot begin transaction") from exc
        try:
            yield handle
        except BaseException:
            handle.close()
            self._rollback(connection)
            raise
        handle.close()
        try:
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback(connection)
            raise _wrap(exc, "cannot commit transaction") from exc

    # -- probes ------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Report the live durability settings and an integrity verdict."""
        connection = self._live()
        try:
            settings = self._settings(connection)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
            relationship_error = connection.execute("PRAGMA foreign_key_check").fetchone()
            required_event_schema = {
                "initiative_event_sequences", "initiative_event_key_insert", "initiative_event_key_update",
            }
            installed_event_schema = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN (?,?,?)", tuple(required_event_schema))}
            if installed_event_schema != required_event_schema:
                raise DatabaseError("initiative event sequence index or guards are missing")
            indexes = {r[1]: r for r in connection.execute("PRAGMA index_list(records)")}
            for name, columns, label in (
                ("records_scope_state", ["domain", "scope", "state", "updated_at", "record_key"], "scoped activity"),
                ("records_state", ["domain", "state", "updated_at", "scope", "record_key"], "root activity"),
            ):
                metadata = indexes.get(name)
                keys = [r for r in connection.execute(f"PRAGMA index_xinfo({name})") if r[5]]
                if (metadata is None or metadata[2] or metadata[4]
                        or [r[2] for r in keys] != columns
                        or any(r[3] or r[4] != "BINARY" for r in keys)):
                    raise DatabaseError(f"{label} index is missing or incompatible")
            # Probe the installed virtual table rather than only a compile flag;
            # a missing derived index must be visible to doctor.
            connection.execute("SELECT rowid FROM records_search WHERE records_search MATCH ? LIMIT 1", ('"asha health probe"',)).fetchall()
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='session_messages'").fetchone():
                connection.execute("SELECT rowid FROM messages_search WHERE messages_search MATCH ? LIMIT 1", ('"asha health probe"',)).fetchall()
                # Native decisions are authoritative records, not a rebuildable
                # index. Missing storage or an orphan request must fail doctor.
                invalid_native = connection.execute("""SELECT 1 FROM session_requests r
                    LEFT JOIN session_native_requests n USING(request_id)
                    WHERE (r.kind='native-permission' AND n.request_id IS NULL)
                       OR (r.kind!='native-permission' AND n.request_id IS NOT NULL)
                    LIMIT 1""").fetchone()
                if invalid_native is not None:
                    relationship_error = invalid_native
        except sqlite3.Error as exc:
            raise _wrap(exc, "health probe failed") from exc
        return {
            **settings,
            "integrity": integrity,
            "relationships": "ok" if relationship_error is None else "invalid",
            "page_count": page_count,
            "path": str(self.path),
            "sqlite_version": sqlite3.sqlite_version,
            "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "fts5": True,
        }

    @staticmethod
    def _label(value, *, maximum=512):
        if not isinstance(value, str) or not value or len(value.encode()) > maximum or "\x00" in value:
            raise DatabaseError("invalid record key or query")
        return value

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DatabaseError("record query limit must be between 1 and 1000")
        return limit

    def get(self, domain, scope, key):
        labels = tuple(self._label(v) for v in (domain, scope, key))
        with self.transaction() as c:
            r = c.execute("SELECT payload FROM records WHERE domain=? AND scope=? AND record_key=?", labels).fetchone()
            return json.loads(r[0]) if r else None

    def put(self, domain, scope, key, payload, *, expected_digest=None):
        labels = tuple(self._label(v) for v in (domain, scope, key))
        if not isinstance(payload, dict):
            raise DatabaseError("record payload must be an object")
        try:
            body = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise DatabaseError("invalid record JSON") from exc
        if len(body.encode()) > 1024 * 1024:
            raise DatabaseError("record exceeds size limit")
        digest = hashlib.sha256(body.encode()).hexdigest()
        state, at = (payload.get(k, "") for k in ("state", "updated_at"))
        if not isinstance(state, str) or not isinstance(at, str):
            raise DatabaseError("record state and updated_at must be strings")
        with self.transaction(write=True) as c:
            old = c.execute("SELECT digest,revision FROM records WHERE domain=? AND scope=? AND record_key=?", labels).fetchone()
            if old and old["digest"] == digest:
                return digest
            if (old is None and expected_digest is not None) or (old is not None and old["digest"] != expected_digest):
                raise DatabaseError("record changed or already exists")
            revision = old["revision"] + 1 if old else 1
            searchable = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if old:
                c.execute("UPDATE records SET payload=?,digest=?,revision=?,state=?,updated_at=?,search_text=? WHERE domain=? AND scope=? AND record_key=?",
                          (body, digest, revision, state, at, searchable, *labels))
            else:
                c.execute("INSERT INTO records(domain,scope,record_key,payload,digest,revision,state,updated_at,search_text) VALUES(?,?,?,?,?,?,?,?,?)", (*labels, body, digest, revision, state, at, searchable))
        return digest

    def list(self, domain, *, scope=None, state=None, after=None, limit=100):
        clauses, parameters = ["domain=?"], [self._label(domain)]
        for name, value in (("scope", scope), ("state", state)):
            if value is not None:
                clauses.append(name + "=?")
                parameters.append(self._label(value))
        if after is not None:
            if not isinstance(after, tuple) or len(after) != 2:
                raise DatabaseError("record cursor must be (scope,key)")
            clauses.append("(scope,record_key)>(?,?)")
            parameters.extend(self._label(v) for v in after)
        parameters.append(self._limit(limit))
        with self.transaction() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT payload FROM records WHERE " + " AND ".join(clauses) + " ORDER BY scope,record_key LIMIT ?", parameters)]

    def search(self, text, *, domain=None, limit=50):
        # Treat user input as a literal token phrase, never FTS query syntax.
        clauses, parameters = ["records_search MATCH ?"], [self.search_phrase(text)]
        if domain is not None:
            clauses.append("domain=?")
            parameters.append(self._label(domain))
        parameters.append(self._limit(limit))
        with self.transaction() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT payload FROM records JOIN records_search ON records.rowid=records_search.rowid WHERE " + " AND ".join(clauses) + " ORDER BY domain,scope,record_key LIMIT ?", parameters)]

    def search_phrase(self, text):
        return '"' + self._label(text, maximum=4096).replace('"', '""') + '"'

    def backup(self, destination: Path):
        destination = Path(destination)
        if not destination.is_absolute() or destination != destination.resolve():
            raise DatabaseError("backup destination must be canonical and absolute")
        if destination.parent == self.root and destination.name in {
            DATABASE_NAME, *(DATABASE_NAME + suffix for suffix in (*SIDECAR_SUFFIXES, "-journal"))
        }:
            raise DatabaseError("backup destination is reserved for Control database files")
        if self._live().in_transaction:
            raise DatabaseError("backup cannot run inside a transaction")
        with _directory_fd(destination.parent, create=False, managed_start=max(0, len(destination.parent.parts) - 2)) as fd:
            if fd is None:
                raise DatabaseError("backup directory must already exist and be private")
            try:
                target = os.open(destination.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=fd)
            except OSError as exc:
                raise DatabaseError(f"cannot create new backup: {exc}") from exc
            try:
                identity = os.fstat(target)
                output = sqlite3.connect(str(destination))
                try:
                    current = os.stat(destination, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                        raise DatabaseError("backup destination changed while opening")
                    self._live().backup(output)
                    output.execute("PRAGMA journal_mode=DELETE")
                    if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise DatabaseError("backup integrity check failed")
                finally:
                    output.close()
                os.fsync(target)
                os.fsync(fd)
            except sqlite3.Error as exc:
                raise _wrap(exc, "backup failed (partial destination retained)") from exc
            finally:
                os.close(target)
        return destination

    @classmethod
    def restore(cls, config, source: Path):
        """Publish a validated backup into an empty Control root, with dispatch paused.

        Existing state is never overwritten. The destination is an offline recovery
        root selected through ASHA_HOME; live roots require a separate cutover.
        """
        source = Path(source)
        if not source.is_absolute() or source != source.resolve():
            raise DatabaseError("restore source must be canonical and absolute")
        root = config.tasks_dir.parent
        with _directory_fd(source.parent, create=False,
                           managed_start=max(0, len(source.parent.parts) - 2)) as source_dir:
            if source_dir is None:
                raise DatabaseError("restore source directory does not exist")
            identity = cls._inspect_file(source_dir, source.name, "restore source")
            try:
                for suffix in SIDECAR_SUFFIXES:
                    try:
                        cls._inspect_file(source_dir, source.name + suffix, "restore source sidecar")
                    except FileNotFoundError:
                        continue
                input_db = sqlite3.connect("file:" + quote(str(source), safe="/") + "?mode=ro", uri=True)
                try:
                    current = os.stat(source, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                        raise DatabaseError("restore source changed while opening")
                    cls._classify(input_db, create=False, migrate=True)
                    with _directory_fd(root, create=True, managed_start=_managed_start(root, ("state", "control"))) as target_dir:
                        if os.listdir(target_dir):
                            raise DatabaseError("restore requires an empty Control state directory")
                        temporary = ".restore-" + secrets.token_hex(12) + ".sqlite3"
                        target_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=target_dir)
                        try:
                            output = sqlite3.connect(root / temporary)
                            try:
                                target_identity = os.fstat(target_fd)
                                current = os.stat(temporary, dir_fd=target_dir, follow_symlinks=False)
                                if (current.st_dev, current.st_ino) != (target_identity.st_dev, target_identity.st_ino):
                                    raise DatabaseError("restore destination changed while opening")
                                input_db.backup(output)
                                cls._migrate(output)
                                if output.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                                    raise DatabaseError("restore integrity check failed")
                                if output.execute("PRAGMA foreign_key_check").fetchone() is not None:
                                    raise DatabaseError("restore contains invalid record relationships")
                                changed = output.execute("UPDATE control_runtime SET mode='paused',revision=revision+1,reason='Restored state requires operator reconciliation before dispatch' WHERE singleton=1")
                                if changed.rowcount != 1:
                                    raise DatabaseError("restore runtime policy is missing")
                                output.commit()
                                output.execute("PRAGMA journal_mode=DELETE")
                            finally:
                                output.close()
                            os.fsync(target_fd)
                            # link is an atomic, no-overwrite publication. Failed
                            # validation or interruption cannot expose a partial DB.
                            os.link(temporary, DATABASE_NAME, src_dir_fd=target_dir, dst_dir_fd=target_dir, follow_symlinks=False)
                            os.fsync(target_dir)
                        finally:
                            os.close(target_fd)
                            os.unlink(temporary, dir_fd=target_dir)
                            os.fsync(target_dir)
                finally:
                    input_db.close()
            except sqlite3.Error as exc:
                raise _wrap(exc, "restore failed") from exc
        return root / DATABASE_NAME
