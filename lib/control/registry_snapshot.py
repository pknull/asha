"""Logical SQLite snapshot identity independent of WAL and backup file layout."""
import hashlib
import json
import sqlite3

from .store import StoreError


MIGRATION_DOMAINS = ("registry-backend", "registry-activation-ledger")


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def state_digest(connection, *, ignored_domains=MIGRATION_DOMAINS, ignore_runtime=False):
    """Stream authoritative tables in a bounded read transaction.

    Search indexes are rebuildable. Migration bookkeeping is excluded so a
    preparing marker can coexist with the source-state proof it records.
    """
    digest = hashlib.sha256()
    def include(value):
        value = [({"blob": item.hex()} if isinstance(item, bytes) else item) for item in value]
        digest.update(json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n")
    include(["asha.control-state-digest.v1", connection.execute("PRAGMA user_version").fetchone()[0]])
    tables = []
    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        name = row[0]
        if name.startswith(("records_search", "messages_search")):
            continue
        if name == "control_runtime" and ignore_runtime:
            continue
        tables.append(name)
        columns = [row[1] for row in connection.execute("PRAGMA table_info(" + _quoted(name) + ")")]
        if name == "records":
            columns.remove("search_text")
        include([name, *columns])
        projection = ",".join(_quoted(column) for column in columns)
        where, parameters = "", ()
        if name == "records" and ignored_domains:
            where = " WHERE domain NOT IN (" + ",".join("?" for _ in ignored_domains) + ")"
            parameters = tuple(ignored_domains)
        cursor = connection.execute("SELECT " + projection + " FROM " + _quoted(name)
                                    + where + " ORDER BY " + projection, parameters)
        for row in cursor:
            include(list(row))
    return {"contract": "asha.control-state-digest.v1", "sha256": digest.hexdigest(), "tables": tables}


def backup_state_digest(fd):
    """Read an already authenticated pinned backup without journal writes."""
    try:
        connection = sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=ro&immutable=1", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise StoreError("retained source database snapshot failed integrity verification")
            return state_digest(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StoreError(f"cannot read retained source database snapshot: {exc}") from exc
