"""Bounded operator projections for SQLite initiative records."""
from __future__ import annotations

import re

from ..database import ControlDatabase
from ..record_registry import RecordRegistry
from . import model
from .store import StoreError, _INVENTORY_CLASSES, _PRESENTATION_EVENT_FILENAME

# Select actionable heads before quiet retained history consumes a read cap.
# All states remain candidates so All retained keeps its existing meaning.
ACTIVITY_HEAD_STATES = (
    "needs-input", "awaiting-plan-approval", "ready-for-integration", "running",
    "approved", "paused", "planning", "draft", "partial", "failed", "cancelled",
    "integrated", "archived",
)
ACTIVITY_RECORD_STATES = {
    "approvals": ("requested", "approved", "rejected", "revoked-before-use", "expired", "cancelled", "consumed"),
    "actions": ("indeterminate", "dispatching", "received", "validated", "refused", "completed"),
}


class SQLiteInitiativeViews:
    def iter_record_snapshots(self, initiative_id, directory, validator, budget):
        with ControlDatabase(self.database_config) as db, db.transaction() as c:
            self._head(c, initiative_id)
            cap = min(1000, max(1, budget.limit - budget.scanned))
            keys = self._registry(initiative_id, directory).keys(c, limit=cap)
            for key in keys:
                if not budget.ready():
                    break
                budget.scanned += 1
                yield key, self._record(c, initiative_id, directory, key, validator)["value"]
            if len(keys) == cap:
                budget.truncated = True

    def _bounded_values(self, initiative_id, directory, validator, budget, pattern, field, parser=str, *, states=None):
        result = []
        if not budget.ready():
            return result
        if directory in ACTIVITY_RECORD_STATES and states is not None:
            known = model.APPROVAL_STATES if directory == "approvals" else model.ACTION_STATES
            if set(states) != set(known):
                budget.unavailable += 1
                return result
        try:
            with ControlDatabase(self.database_config) as db, db.transaction() as c:
                registry = RecordRegistry("initiatives") if directory == "initiative" else self._registry(initiative_id, directory)
                if directory != "initiative":
                    self._head(c, initiative_id)
                cap = min(1000, max(1, budget.limit - budget.scanned))
                if states is None:
                    keys = registry.keys(c, limit=cap)
                elif directory == "initiative":
                    keys = registry.active_root_keys(c, states, limit=cap)
                else:
                    keys = registry.activity_keys(c, states, limit=cap)
                for key in keys:
                    if not budget.ready():
                        break
                    budget.scanned += 1
                    try:
                        match = pattern.fullmatch(key)
                        if match is None:
                            raise StoreError("invalid record key")
                        iid = key if directory == "initiative" else initiative_id
                        value = self._record(c, iid, directory, key, validator)["value"]
                        if value[field] != parser(match.group(1)):
                            raise StoreError("record identity does not match its key")
                        if states is not None:
                            indexed = c.execute("SELECT state,updated_at FROM records WHERE domain=? AND scope=? AND record_key=?",
                                                (registry.domain, registry.scope, key)).fetchone()
                            if (value["state"] != indexed[0]
                                    or value.get("updated_at", value.get("recorded_at", "")) != indexed[1]):
                                raise StoreError("record metadata disagrees with its activity index")
                        result.append(value)
                    except (ValueError, KeyError):
                        budget.unavailable += 1
                if len(keys) == cap:
                    budget.truncated = True
        except (ValueError, OSError):
            budget.unavailable += 1
        return result

    def bounded_snapshots(self, budget):
        return self._bounded_values(None, "initiative", model.validate_initiative, budget,
                                    re.compile(r"([0-9a-f-]{36})"), "initiative_id")

    def bounded_activity_snapshots(self, budget):
        if set(ACTIVITY_HEAD_STATES) != set(model.INITIATIVE_STATES):
            budget.unavailable += 1
            return []
        return self._bounded_values(None, "initiative", model.validate_initiative, budget,
                                    re.compile(r"([0-9a-f-]{36})"), "initiative_id",
                                    states=ACTIVITY_HEAD_STATES)

    def bounded_records(self, initiative_id, directory, validator, identity_field, budget):
        return self._bounded_values(initiative_id, directory, validator, budget,
                                    re.compile(r"([0-9a-f-]{36})\.json"), identity_field,
                                    states=ACTIVITY_RECORD_STATES.get(directory))

    def bounded_presentation_records(self, initiative_id, directory, budget, *, max_records=None):
        validator, pattern, field, parser = self._presentation_class(directory)
        child = budget.record_budget(initiative_id, max_records=max_records)
        if child is None:
            return []
        try:
            result = self._bounded_values(initiative_id, directory, validator, child, pattern, field, parser,
                                          states=ACTIVITY_RECORD_STATES.get(directory))
            if child.unavailable:
                budget.note(f"{initiative_id}/{directory}", f"{child.unavailable} record(s) unreadable")
            return sorted(result, key=lambda value: value[field])
        finally:
            budget.absorb_records(child, initiative_id)

    def bounded_event_sample(self, initiative_id, budget):
        child = budget.record_budget(initiative_id)
        if child is None:
            return [], False
        result, complete = [], True
        try:
            with ControlDatabase(self.database_config) as db, db.transaction() as c:
                head = self._head(c, initiative_id)["value"]
                registry = self._registry(initiative_id, "events")
                count = c.execute("SELECT count(*) FROM records WHERE domain=? AND scope=?",
                                  (registry.domain, registry.scope)).fetchone()[0]
                cap = min(1000, budget.event_tail, max(0, child.limit - child.scanned))
                keys = [r[0] for r in c.execute("SELECT record_key FROM records WHERE domain=? AND scope=? ORDER BY record_key DESC LIMIT ?",
                                               (registry.domain, registry.scope, cap))]
                complete = count <= cap
                if count != head["last_event_sequence"]:
                    complete = False
                    child.unavailable += 1
                    budget.note(f"{initiative_id}/events", "event count does not match initiative head")
                if count > cap and cap < budget.event_tail:
                    child.truncated = True
                for key in reversed(keys):
                    if not child.ready():
                        complete = False
                        break
                    child.scanned += 1
                    try:
                        match = _PRESENTATION_EVENT_FILENAME.fullmatch(key)
                        if match is None:
                            raise StoreError("invalid event key")
                        value = self._record(c, initiative_id, "events", key, model.validate_event)["value"]
                        if value["sequence"] != int(match[1]) or value["event_id"] != match[2]:
                            raise StoreError("event identity does not match its key")
                        result.append(value)
                    except (ValueError, KeyError) as exc:
                        child.unavailable += 1
                        complete = False
                        budget.note(f"{initiative_id}/events", str(exc))
                if complete and [r["sequence"] for r in result] != list(range(1, count + 1)):
                    complete = False
                    budget.note(f"{initiative_id}/events", "event sequence is not contiguous")
        except (ValueError, OSError) as exc:
            child.unavailable += 1
            complete = False
            budget.note(f"{initiative_id}/events", str(exc))
        finally:
            budget.absorb_records(child, initiative_id)
        return result, complete

    def inventory(self, initiative_id, *, locked=True):
        if locked:
            with self.transaction_lock(initiative_id):
                return self.inventory(initiative_id, locked=False)
        # File inventory remains descriptor-checked for assignment/output bytes.
        # SQLite records contribute logical bytes/rows, not fictitious inodes.
        self.peek(initiative_id)
        result = {name: {"bytes": 0, "inodes": 0} for name in _INVENTORY_CLASSES}
        for files in (self.artifacts, self.retained_artifacts):
            for directory, counts in files.inventory(initiative_id).items():
                for field, count in counts.items():
                    result[directory][field] += count
        with ControlDatabase(self.database_config) as db, db.transaction() as c:
            for directory in _INVENTORY_CLASSES:
                if directory in {"outputs", "assignments", "locks"}:
                    continue
                registry = self._registry(initiative_id, directory)
                clauses = "domain=? AND scope=?"
                args = [registry.domain, registry.scope]
                if directory == "initiative":
                    clauses += " AND record_key=?"
                    args.append(initiative_id)
                count, size = c.execute("SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) FROM records WHERE " + clauses, args).fetchone()
                result[directory]["rows"] = count
                result[directory]["bytes"] += size
        result["totals"] = {field: sum(result[directory].get(field, 0) for directory in _INVENTORY_CLASSES)
                            for field in ("bytes", "inodes", "rows")}
        result["pause_recommended"] = (result["totals"]["bytes"] >= self.config.max_retained_bytes_before_pause
                                        or result["totals"]["inodes"] >= self.config.max_retained_inodes_before_pause)
        return result
