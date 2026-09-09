"""Byte-preserving SQLite records for the existing validated registry interfaces.

Callers own domain validation and transaction boundaries. These methods never
open a second writable authority or contact a harness while holding a transaction.
"""
from __future__ import annotations

import hashlib
import json

from .database import ControlDatabase, DatabaseError


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate record field")
        value[key] = item
    return value


def _nonfinite(_value):
    raise ValueError("nonfinite record number")


class RecordRegistry:
    """A single record domain; canonical file bytes and their digests survive import."""

    def __init__(self, domain, *, scope="registry"):
        self.domain = ControlDatabase._label(domain)
        self.scope = ControlDatabase._label(scope)

    @staticmethod
    def decode(raw):
        if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
            raise DatabaseError("registry record exceeds its byte limit")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=_nonfinite)
            # This also catches floating-point exponent overflow.
            json.dumps(value, allow_nan=False)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DatabaseError("invalid registry record JSON") from exc
        if not isinstance(value, dict):
            raise DatabaseError("registry record must be an object")
        return value

    def read(self, c, key):
        key = ControlDatabase._label(key)
        row = c.execute("SELECT payload,digest,revision FROM records WHERE domain=? AND scope=? AND record_key=?",
                        (self.domain, self.scope, key)).fetchone()
        if row is None:
            return None
        raw = row["payload"].encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != row["digest"]:
            raise DatabaseError("registry record content digest mismatch")
        return {"raw": raw, "value": self.decode(raw), "digest": row["digest"], "revision": row["revision"]}

    def put(self, c, key, raw, *, expected_digest=None, state="", updated_at=""):
        key = ControlDatabase._label(key)
        value = self.decode(raw)
        if not isinstance(state, str) or not isinstance(updated_at, str):
            raise DatabaseError("registry projections must be text")
        previous = self.read(c, key)
        if ((previous is None and expected_digest is not None)
                or (previous is not None and previous["digest"] != expected_digest)):
            raise DatabaseError("registry record changed or already exists")
        digest = hashlib.sha256(raw).hexdigest()
        revision = previous["revision"] + 1 if previous else 1
        fields = (raw.decode("utf-8"), digest, revision, state, updated_at,
                  json.dumps(value, ensure_ascii=False, allow_nan=False))
        labels = (self.domain, self.scope, key)
        if previous:
            c.execute("UPDATE records SET payload=?,digest=?,revision=?,state=?,updated_at=?,search_text=? WHERE domain=? AND scope=? AND record_key=?",
                      (*fields, *labels))
        else:
            c.execute("INSERT INTO records(payload,digest,revision,state,updated_at,search_text,domain,scope,record_key) VALUES(?,?,?,?,?,?,?,?,?)",
                      (*fields, *labels))
        return digest

    def keys(self, c, *, after="", limit=100):
        ControlDatabase._limit(limit)
        if not isinstance(after, str):
            raise DatabaseError("invalid registry cursor")
        return [row[0] for row in c.execute("SELECT record_key FROM records WHERE domain=? AND scope=? AND record_key>? ORDER BY record_key LIMIT ?",
                                            (self.domain, self.scope, after, limit))]

    def active_root_keys(self, c, states, *, limit=100):
        """Select root registry candidates through the state index before capping.

        This is for single-scope root domains, not per-initiative child records.
        The caller still validates each selected record and its lifecycle.
        """
        ControlDatabase._limit(limit)
        known = {"tasks": ("creating", "running", "ended", "failed", "archived"),
                 "rooms": ("creating", "open", "ended")}.get(self.domain)
        if self.domain == "initiatives":
            from .orchestration.model import INITIATIVE_STATES
            known = INITIATIVE_STATES
        if (self.scope != "registry" or not isinstance(states, (tuple, list))
                or not 1 <= len(states) <= 32
                or any(not isinstance(state, str) or not state or len(state) > 128 for state in states)
                or len(set(states)) != len(states) or known is None or not set(states) <= set(known)):
            raise DatabaseError("invalid root activity states")
        return self._state_keys(c, states, known, limit)

    def activity_keys(self, c, states, *, limit=100):
        """Per-initiative action records selected by scoped lifecycle index."""
        from .orchestration.model import APPROVAL_STATES, ACTION_STATES
        known = {"initiative.approvals": APPROVAL_STATES, "initiative.actions": ACTION_STATES}.get(self.domain)
        ControlDatabase._limit(limit)
        if (known is None or self.scope == "registry" or not isinstance(states, (tuple, list))
                or not states or any(not isinstance(state, str) for state in states)
                or len(states) != len(set(states)) or not set(states) <= set(known)):
            raise DatabaseError("invalid initiative activity states")
        return self._state_keys(c, states, known, limit)

    def _state_keys(self, c, states, known, limit):
        # A forgotten projection must be unavailable, never known empty. Probe
        # the fixed gaps in the state index without scanning finished history.
        ordered = sorted(known)
        ranges = [("state<?", (ordered[0],))]
        ranges += [("state>? AND state<?", (a, b)) for a, b in zip(ordered, ordered[1:])]
        ranges.append(("state>?", (ordered[-1],)))
        for condition, args in ranges:
            if c.execute("SELECT 1 FROM records WHERE domain=? AND scope=? AND " + condition + " LIMIT 1",
                         (self.domain, self.scope, *args)).fetchone():
                raise DatabaseError("unknown activity state; registry projection needs repair")
        keys = []
        for state in states:
            keys.extend(row[0] for row in c.execute(
                "SELECT record_key FROM records WHERE domain=? AND state=? AND scope=? ORDER BY updated_at DESC LIMIT ?",
                (self.domain, state, self.scope, limit - len(keys))))
            if len(keys) == limit:
                break
        return keys
