"""Read and authenticate an offline stage before any live activation mutation."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import re

from .database import ControlDatabase
from .initiative_migration import InitiativeImport
from .jj import ColocationIntentStore
from .model import canonical_uuid
from .orchestration.authority import validate_authority
from .prune import PruneRecordStore
from .record_registry import RecordRegistry
from .registry_migration import STAGED_DOMAINS, _quiescent
from .registry_snapshot import backup_state_digest
from .registry_tree import validate_entries
from .rooms import RoomStore
from .sqlite_tasks import SQLiteTaskStore
from .stage_ledger import iter_ledger
from .store import StoreError, _directory_fd, _managed_start, _open_existing_file
from .transaction import validate_journal


def projection(domain, value):
    if domain in {"tasks", "rooms"}:
        return value["lifecycle"], value["updated_at"]
    if domain == "initiatives" or domain.startswith("initiative."):
        return value.get("state", value.get("status", "")), value.get("updated_at", value.get("recorded_at", ""))
    state = ("revoked" if value["revoked_at"] else "active") if domain == "authorities" else value.get("phase", value.get("state", "retained"))
    return state, value.get("revoked_at") or value.get("recorded_at", value.get("created_at", ""))


def _record(config, domain, scope, key, raw):
    value = RecordRegistry.decode(raw)
    if domain.startswith("initiative."):
        parts = (scope, domain.split(".", 1)[1], key)
        _, _, value = InitiativeImport._validate(parts, raw)
        path = "initiatives/" + "/".join(parts)
    elif domain == "initiatives":
        if scope != "registry":
            raise StoreError("invalid initiative head registry scope")
        _, _, value = InitiativeImport._validate((key, "initiative.json"), raw)
        path = f"initiatives/{key}/initiative.json"
    else:
        if scope != "registry":
            raise StoreError("invalid staged record scope")
        folder = {"creation-journals": "transactions"}.get(domain, domain)
        path = folder + "/" + key + ".json"
        if domain == "tasks":
            value = SQLiteTaskStore(config)._validated_value(value, key)
        elif domain == "rooms":
            value = RoomStore._validate(value)
            if value["room_id"] != key:
                raise StoreError("staged Room identity differs from key")
        elif domain == "creation-journals":
            value = validate_journal(value, config=config)
            if value["task_id"] != key:
                raise StoreError("staged journal identity differs from key")
        elif domain == "authorities":
            value = validate_authority(value)
            if value["authority_id"] != key:
                raise StoreError("staged authority identity differs from key")
        elif domain == "prunes":
            value = PruneRecordStore._validated_value(value, key)
        elif domain == "repository-inits":
            value = ColocationIntentStore._decode(raw)
            root = value["root"]
            if (not isinstance(root, str) or not root.startswith("/") or "\x00" in root
                    or os.path.normpath(root) != root or ColocationIntentStore._key(Path(root)) != key):
                raise StoreError("staged repository intent identity differs from key")
        else:
            raise StoreError("unexpected staged operational domain")
    return path, value


def _file_hash(parent_fd, name):
    fd = _open_existing_file(parent_fd, name, "retained migration artifact")
    with os.fdopen(fd, "rb") as stream:
        return os.fstat(stream.fileno()).st_size, hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def checked_stage(config, stage_home):
    home = Path(stage_home)
    if (not home.is_absolute() or home != home.resolve() or home.is_relative_to(config.asha_home)
            or config.asha_home.is_relative_to(home)):
        raise StoreError("stage root must be canonical and separate from the live Asha root")
    stage_config = replace(config, asha_home=home, tasks_dir=home / "state/control/tasks")
    with _directory_fd(home, create=False, managed_start=max(0, len(home.parts) - 2)) as home_fd:
        if home_fd is None:
            raise StoreError("stage root is missing")
        with ControlDatabase(stage_config, read_only=True) as db:
            _quiescent(db)
            with db.transaction() as c:
                row = RecordRegistry("registry-migration", scope="control").read(c, "stage")
                if row is None:
                    raise StoreError("stage has no completion manifest")
                manifest = row["value"]
                fields = {"contract", "state", "source_root", "domains", "counts", "source_database_digest",
                          "source_database_state", "source_database_snapshot", "records", "artifacts", "source_tree", "external_rooms"}
                if (set(manifest) != fields or manifest["contract"] != "asha.registry-stage.v3"
                        or manifest["state"] != "staged" or manifest["source_root"] != str(config.asha_home)
                        or manifest["domains"] != list(STAGED_DOMAINS)
                        or not isinstance(manifest["counts"], dict) or set(manifest["counts"]) != set(STAGED_DOMAINS)
                        or any(type(count) is not int or count < 0 for count in manifest["counts"].values())
                        or manifest["source_database_snapshot"] != "source-database.sqlite3"):
                    raise StoreError("stage manifest is incomplete, incompatible or bound to another root")
                if RecordRegistry("registry-backend", scope="control").read(c, "active") is not None:
                    raise StoreError("an active database cannot be used as an offline stage")
                source_tree = list(iter_ledger(c, "source", manifest["source_tree"]))
                source = validate_entries(source_tree)
                entries = list(iter_ledger(c, "records", manifest["records"]))
                artifacts = list(iter_ledger(c, "artifacts", manifest["artifacts"]))
                records, counts, keys, paths = [], dict.fromkeys(STAGED_DOMAINS, 0), set(), set()
                for entry in entries:
                    domain, scope, key = entry.get("domain"), entry.get("scope", "registry"), entry.get("key")
                    if (any(not isinstance(item, str) for item in (domain, scope, key))
                            or domain not in counts or (domain, scope, key) in keys):
                        raise StoreError("invalid or duplicate staged record identity")
                    record = RecordRegistry(domain, scope=scope).read(c, key)
                    if record is None or record["revision"] != 1:
                        raise StoreError("staged record is missing or was modified after import")
                    path, value = _record(config, domain, scope, key, record["raw"])
                    original = source.get(path, {})
                    if (entry.get("path", path) != path or path in paths or original.get("kind") != "file"
                            or original.get("digest") != record["digest"] or entry.get("digest") != record["digest"]
                            or original.get("bytes") != len(record["raw"]) or entry.get("bytes") != len(record["raw"])):
                        raise StoreError("staged record differs from its source tree or ledger")
                    state, updated = projection(domain, value)
                    stored_projection = c.execute("SELECT state,updated_at FROM records WHERE domain=? AND scope=? AND record_key=?",
                                                  (domain, scope, key)).fetchone()
                    if tuple(stored_projection) != (state, updated):
                        raise StoreError("staged record projection changed after import")
                    records.append({"domain": domain, "scope": scope, "key": key, "raw": record["raw"],
                                    "value": value, "state": state, "updated_at": updated})
                    keys.add((domain, scope, key))
                    paths.add(path)
                    counts[domain] += 1
                for domain, count in counts.items():
                    actual = c.execute("SELECT count(*) FROM records WHERE domain=?", (domain,)).fetchone()[0]
                    if count != manifest["counts"][domain] or actual != count:
                        raise StoreError("staged operational row count differs from the complete ledger")
                for artifact in artifacts:
                    path = artifact.get("path")
                    if not isinstance(path, str):
                        raise StoreError("invalid staged artifact path")
                    original = source.get(path, {})
                    if (path in paths or original.get("kind") != "file"
                            or original.get("bytes") != artifact.get("bytes") or original.get("digest") != artifact.get("digest")):
                        raise StoreError("staged artifact differs from its source tree")
                    parts = tuple(path.split("/"))
                    if not ((len(parts) == 2 and parts[0] == "transactions" and parts[1].endswith(".ownership"))
                            or (len(parts) == 4 and parts[0] == "initiatives" and parts[2] in {"assignments", "outputs"})):
                        raise StoreError("invalid staged artifact path")
                    suffix = ".ownership" if parts[0] == "transactions" else ".md" if parts[2] == "assignments" else ".bin"
                    if not parts[-1].endswith(suffix):
                        raise StoreError("invalid staged artifact extension")
                    canonical_uuid(parts[-1][:-len(suffix)])
                    if parts[0] == "initiatives":
                        canonical_uuid(parts[1])
                    target = stage_config.tasks_dir.parent.joinpath(*parts)
                    with _directory_fd(target.parent, create=False,
                            managed_start=_managed_start(target.parent, ("state", "control", *parts[:-1]))) as fd:
                        if fd is None or _file_hash(fd, target.name) != (artifact["bytes"], artifact["digest"]):
                            raise StoreError("staged artifact copy is missing or changed")
                    paths.add(path)
                for path, entry in source.items():
                    if entry["kind"] != "file" or path in paths:
                        continue
                    inert = (re.fullmatch(r"tasks/(?:task|source|repository)-[0-9a-f]{64}\.lock", path)
                             or path == "authorities/.lock"
                             or re.fullmatch(r"initiatives/[0-9a-f-]{36}/locks/(?:initiative|result-ingestion-[0-9a-f-]{36})\.lock", path))
                    if not inert or entry["bytes"] != 0:
                        raise StoreError("source file is absent from the complete stage ledger")
                open_rooms = sum(record["domain"] == "rooms" and record["value"]["lifecycle"] == "open" for record in records)
                if manifest["external_rooms"] != {"open_room_count": open_rooms, "liveness": "not-probed",
                                                  "activation_requires_revalidation": True}:
                    raise StoreError("stage Room revalidation summary differs from records")
                backup_fd = _open_existing_file(home_fd, manifest["source_database_snapshot"], "source database snapshot")
                with os.fdopen(backup_fd, "rb") as backup:
                    if hashlib.file_digest(backup, "sha256").hexdigest() != manifest["source_database_digest"]:
                        raise StoreError("retained source database snapshot bytes changed")
                    if backup_state_digest(backup.fileno()) != manifest["source_database_state"]:
                        raise StoreError("retained source database logical state changed")
                yield {"home": str(home), "digest": row["digest"], "manifest": manifest,
                       "source_tree": source_tree, "records": records}
