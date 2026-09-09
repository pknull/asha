"""Pinned, byte-preserving initiative import into an offline staging database."""
from __future__ import annotations

import hashlib
import os
import re
import stat

from .orchestration import model
from .orchestration.config import from_control
from .orchestration.sqlite_store import SQLiteInitiativeStore
from .orchestration.store import InitiativeStore, _LAYOUT_DIRECTORIES, _open_directory
from .record_registry import RecordRegistry
from .session_store import process_live
from .store import StoreError, _directory_fd, _managed_start, _registry_lock, _task_lock


# The same validators used by the public store. Historical plans retain only
# their observation authority; importing them must not mint execution consent.
_RECORDS = {
    "plans": (InitiativeStore._validate_stored_plan_observation, "revision"),
    "nodes": (model.validate_node, "node_id"),
    "attempts": (model.validate_attempt, "attempt_id"),
    "links": (model.validate_link, "attempt_id"),
    "result-ingestions": (model.validate_result_ingestion, "ingestion_id"),
    "result-publications": (model.validate_result_publication, "publication_id"),
    "results": (model.validate_result, "result_id"),
    "seal-preparations": (model.validate_seal_preparation, "seal_id"),
    "seals": (model.validate_seal, "seal_id"),
    "reviews": (model.validate_review, "review_id"),
    "verifications": (model.validate_verification, "verification_id"),
    "bundles": (model.validate_bundle, "bundle_id"),
    "approvals": (model.validate_approval, "request_id"),
    "actions": (model.validate_action, "action_id"),
    "evidence": (model.validate_evidence, "evidence_id"),
    "events": (model.validate_event, "event_id"),
    "coordinators": (model.validate_coordinator, "coordinator_id"),
    "checkpoints": (model.validate_coordinator_checkpoint, "coordinator_id"),
    "messages": (model.validate_message, "message_id"),
    "message-observations": (model.validate_message_receipt, "message_id"),
    "message-acks": (model.validate_message_receipt, "message_id"),
}
DOMAINS = ("initiatives",) + tuple("initiative." + name for name in _RECORDS)


def coordinator_requires_stop(value):
    # Retired legacy generations cannot act even if their parent terminal
    # survives. This includes stale: every authority gate refuses that state.
    # Managed owners may still hold a provider connection after retirement.
    return (value['anchor'].get('kind') == 'managed-session-v1'
            or value['state'] in model.COORDINATOR_LIVE_STATES)


class InitiativeImport:
    def __init__(self, config, stack):
        self.config = from_control(config)
        self.path = self.config.initiatives_dir
        self.fd = stack.enter_context(_directory_fd(self.path, create=False,
            managed_start=_managed_start(self.path, ("control", "initiatives"))))
        self.directories, self.files = {}, {}
        if self.fd is None:
            return
        stack.enter_context(_registry_lock(self.fd))
        for iid in sorted(os.listdir(self.fd)):
            try:
                model.canonical_uuid(iid, "initiative directory")
            except model.ModelError as exc:
                raise StoreError(str(exc)) from exc
            initiative_fd = _open_directory(self.fd, iid, create=False)
            if initiative_fd is None:
                raise StoreError("initiative disappeared before migration lock")
            stack.callback(os.close, initiative_fd)
            locks_fd = _open_directory(initiative_fd, "locks", create=False)
            if locks_fd is None:
                raise StoreError("initiative lock directory missing; reconcile before migration")
            stack.callback(os.close, locks_fd)
            # Do not introduce missing lock files into the source snapshot.
            if "initiative.lock" not in os.listdir(locks_fd):
                raise StoreError("initiative lock missing; reconcile before migration")
            stack.enter_context(_task_lock(locks_fd, "initiative"))

    def _walk(self, fd, directories, parts=()):
        from .registry_migration import _read_record
        metadata = os.fstat(fd)
        names = tuple(sorted(os.listdir(fd)))
        directories[parts] = (metadata.st_dev, metadata.st_ino, names)
        for name in names:
            child_parts = (*parts, name)
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                if not parts:
                    model.canonical_uuid(name, "initiative directory")
                elif len(parts) != 1 or name not in _LAYOUT_DIRECTORIES:
                    raise StoreError("unexpected initiative migration directory")
                child = _open_directory(fd, name, create=False)
                try:
                    yield from self._walk(child, directories, child_parts)
                finally:
                    os.close(child)
            else:
                if not parts or len(parts) > 2:
                    raise StoreError("unexpected initiative migration file")
                raw = _read_record(fd, name, 1024 * 1024)
                yield child_parts, raw

    @staticmethod
    def _validate(parts, raw):
        iid = parts[0]
        if len(parts) == 2 and parts[1] == "initiative.json":
            value = model.validate_initiative(RecordRegistry.decode(raw))
            directory, key = "initiative", iid
        elif len(parts) == 3 and parts[1] in _RECORDS:
            directory, key = parts[1:]
            validator, field = _RECORDS[directory]
            value = validator(RecordRegistry.decode(raw))
            if directory == "plans":
                expected = f"{value[field]:04d}.json"
            elif directory == "events":
                expected = f"{value['sequence']:06d}-{value[field]}.json"
            else:
                expected = value[field] + ".json"
            if key != expected:
                raise StoreError("initiative migration record identity differs from filename")
            if directory in {"message-observations", "message-acks"}:
                expected_state = "observed" if directory == "message-observations" else "acknowledged"
                if value["state"] != expected_state:
                    raise StoreError("initiative message receipt class/state mismatch")
            if directory == "coordinators":
                anchor = value["anchor"]
                if coordinator_requires_stop(value) and process_live(anchor.get("owner_pid", anchor.get("pane_pid")), anchor["process_start_identity"]):
                    raise StoreError("stop live initiative coordinators before staging registry migration")
        else:
            raise StoreError("unexpected initiative migration record")
        if value.get("initiative_id", iid) != iid:
            raise StoreError("initiative migration record belongs to another initiative")
        if len(raw) > 256 * 1024:
            raise StoreError("initiative migration record exceeds its byte limit")
        return directory, key, value

    def import_into(self, c, stage_config, after_import=None):
        counts = dict.fromkeys(DOMAINS, 0)
        entries, artifacts, revisions, heads = [], [], {}, set()
        for domain in DOMAINS:
            if c.execute("SELECT 1 FROM records WHERE domain=? LIMIT 1", (domain,)).fetchone():
                raise StoreError("staging source already contains SQLite registry rows; reconcile domain authority before import")
        if self.fd is None:
            return entries, counts, artifacts
        store = SQLiteInitiativeStore(from_control(stage_config))
        for parts, raw in self._walk(self.fd, self.directories):
            relative = "/".join(parts)
            digest = hashlib.sha256(raw).hexdigest()
            self.files[parts] = digest
            if len(parts) == 3 and parts[1] == "locks":
                if not re.fullmatch(r"(?:initiative|result-ingestion-[0-9a-f-]{36})\.lock", parts[2]) or raw:
                    raise StoreError("unexpected initiative lock record")
                continue
            if len(parts) == 3 and parts[1] in {"assignments", "outputs"}:
                suffix = ".md" if parts[1] == "assignments" else ".bin"
                if not parts[2].endswith(suffix):
                    raise StoreError("invalid initiative artifact filename")
                model.canonical_uuid(parts[2][:-len(suffix)], "artifact identity")
                if parts[1] == "assignments":
                    if not raw or len(raw) > 32768:
                        raise StoreError("initiative assignment exceeds its byte limit or is empty")
                    raw.decode("utf-8")
                destination = store.config.initiatives_dir.joinpath(*parts)
                with _directory_fd(destination.parent, create=True,
                        managed_start=store._root_managed_start) as target_fd:
                    store._write_once(target_fd, destination.name, raw)
                artifacts.append({"path": "initiatives/" + relative, "digest": digest, "bytes": len(raw)})
                continue
            directory, key, value = self._validate(parts, raw)
            registry = store._registry(parts[0], directory)
            registry.put(c, key, raw, state=value.get("state", value.get("status", "")),
                         updated_at=value.get("updated_at", value.get("recorded_at", "")))
            if registry.read(c, key)["raw"] != raw:
                raise StoreError("imported initiative record differs from source bytes")
            entries.append({"domain": registry.domain, "scope": registry.scope, "key": key,
                            "digest": digest, "bytes": len(raw), "path": "initiatives/" + relative})
            counts[registry.domain] += 1
            if directory == "initiative":
                heads.add(parts[0])
            if directory == "plans":
                revisions.setdefault(parts[0], []).append(value["revision"])
            if after_import is not None:
                after_import(registry.domain, key)
        if heads != set(self.directories[()][2]):
            raise StoreError("initiative migration source has missing heads")
        for iid in heads:
            store._events(c, iid)
            plans = sorted(revisions.get(iid, []))
            if plans != list(range(1, len(plans) + 1)):
                raise StoreError("stored plan revisions contain a gap")
        for domain, count in counts.items():
            if c.execute("SELECT count(*) FROM records WHERE domain=?", (domain,)).fetchone()[0] != count:
                raise StoreError("staged initiative row count differs from source")
        return entries, counts, artifacts

    def verify_source(self):
        if self.fd is None:
            if os.path.lexists(self.path):
                raise StoreError("initiative migration source appeared during staging")
            return
        current = os.stat(self.path, follow_symlinks=False)
        pinned = os.fstat(self.fd)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise StoreError("initiative migration source changed identity during staging")
        directories = {}
        files = {parts: hashlib.sha256(raw).hexdigest() for parts, raw in self._walk(self.fd, directories)}
        if directories != self.directories or files != self.files:
            raise StoreError("initiative migration source changed during staging")
