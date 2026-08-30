"""Workspace-outbox transport and controller-owned result ingestion.

The producing worker may write only the candidate envelope below its own
workspace.  Every authoritative record, jj snapshot, and verification fact is
created later by this controller module after terminal process evidence.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..harness import HarnessError, caller_descends_from
from ..jj import JjAdapter, JjError
from ..prepare import PreparationError, prepare_materialization
from ..reconcile import LiveAdapters, reconcile_task
from ..store import StoreError, TaskStore
from ..tmux import TmuxAdapter, TmuxError
from .links import control_task_identity_digest
from .model import (
    EVIDENCE_CONTRACT,
    MAX_REFUSAL_BYTES,
    RESULT_CANDIDATE_CONTRACT,
    RESULT_INGESTION_CONTRACT,
    ModelError,
    new_uuid,
    record_digest,
    validate_evidence,
    validate_result,
    validate_result_candidate,
    validate_result_ingestion,
)
from .results import (
    MAX_RESULT_BODY_BYTES,
    ResultError,
    ResultRefused,
    canonical_body_bytes,
    parse_client_body,
    publish_bound_result,
)
from .review import tracked_workspace_status
from .seals import _base_binding, _diff, _in_scope
from .store import InitiativeStore
from .verification import (
    VerificationError,
    _bubblewrap_program,
    _capture_truncated,
    _command_cwd,
    _contained_argv,
    _printable_output_tail,
    _rerun_failure_kind,
    command_denial,
)


RESULT_STAGING_RECEIPT_CONTRACT = (
    "asha.orchestration-result-staging-receipt.v1"
)
RESULT_INGESTION_RECEIPT_CONTRACT = (
    "asha.orchestration-result-ingestion-receipt.v1"
)
MAX_CANDIDATE_BYTES = MAX_RESULT_BODY_BYTES + 16 * 1024
_INGESTION_NAMESPACE = uuid.UUID("89b98b7c-90e6-4a25-8b6f-dd0062989a40")
_VERIFICATION_ATTESTATION_FIELDS = frozenset({
    "argv", "cwd", "exit_code", "finished_at", "output_digest", "summary",
})


class IngestionError(ResultError):
    """A candidate cannot be staged or ingested safely."""


class IngestionRefused(ResultRefused):
    """A deterministic ingestion guard refused the candidate."""


class IngestionUnavailable(ResultError):
    """An environment or infrastructure failure interrupted ingestion.

    Nothing about the candidate has been judged: the ingestion record stays
    non-terminal so the next supervisor pass retries once the environment is
    repaired, instead of minting a permanent refusal (#76).
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def result_ingestion_id(attempt_id: str) -> str:
    """The one reserved ingestion identity for an immutable attempt."""
    try:
        attempt = uuid.UUID(attempt_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise IngestionRefused("attempt_id must be a canonical UUID") from exc
    if str(attempt) != attempt_id:
        raise IngestionRefused("attempt_id must be a canonical UUID")
    return str(uuid.uuid5(_INGESTION_NAMESPACE, attempt_id))


def result_outbox_path(ingestion_id: str) -> str:
    try:
        identifier = uuid.UUID(ingestion_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise IngestionRefused("ingestion_id must be a canonical UUID") from exc
    if str(identifier) != ingestion_id:
        raise IngestionRefused("ingestion_id must be a canonical UUID")
    return f".asha/outbox/{ingestion_id}.json"


def reserve_result_ingestion(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    node: Mapping[str, Any],
    attempt: Mapping[str, Any],
    link: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    staging_token_digest: str,
) -> dict[str, Any]:
    """Persist the controller reservation after the immutable Control link."""
    if (
        not isinstance(staging_token_digest, str)
        or len(staging_token_digest) != 64
        or any(character not in "0123456789abcdef" for character in staging_token_digest)
    ):
        raise IngestionRefused(
            "staging token digest must be 64 lowercase hexadecimal characters"
        )
    ingestion_id = result_ingestion_id(attempt["attempt_id"])
    now = _now()
    record = validate_result_ingestion({
        "contract": RESULT_INGESTION_CONTRACT,
        "ingestion_id": ingestion_id,
        "initiative_id": initiative["initiative_id"],
        "node_id": node["node_id"],
        "attempt_id": attempt["attempt_id"],
        "task_id": task["task_id"],
        "run_id": task["runs"][0]["run_id"],
        "active_plan_digest": link["active_plan_digest"],
        "control_task_identity_digest": link["control_task_identity_digest"],
        "staging_token_digest": staging_token_digest,
        "workspace_path": task["jj"]["workspace_path"],
        "workspace_name": task["jj"]["workspace_name"],
        "change_id": task["jj"]["change_id"],
        "outbox_path": result_outbox_path(ingestion_id),
        "state": "reserved",
        "candidate_digest": None,
        "publication_id": None,
        "result_id": None,
        "claimed_commit_id": None,
        "claimed_tree_digest": None,
        "commit_creator": None,
        "verification_evidence_ids": [],
        "ingester": None,
        "refusal": None,
        "created_at": now,
        "updated_at": now,
    })
    try:
        retained = store.read_result_ingestion(
            initiative["initiative_id"], ingestion_id,
        )
    except StoreError as exc:
        if "not found" not in str(exc):
            raise
        store.save_result_ingestion(initiative["initiative_id"], record)
        return record
    reservation_fields = (
        "contract", "ingestion_id", "initiative_id", "node_id", "attempt_id",
        "task_id", "run_id", "active_plan_digest", "control_task_identity_digest",
        "staging_token_digest", "workspace_path", "workspace_name", "change_id",
        "outbox_path",
    )
    if any(retained[field] != record[field] for field in reservation_fields):
        raise IngestionRefused(
            "retained result ingestion reservation differs from the Control task link"
        )
    return retained


def _workspace_candidate_path(
    task: Mapping[str, Any], ingestion: Mapping[str, Any], raw_outbox: str,
) -> Path:
    workspace = Path(task["jj"]["workspace_path"])
    if (
        not workspace.is_absolute()
        or workspace.is_symlink()
        or not workspace.is_dir()
        or workspace.resolve() != workspace
        or str(workspace) != ingestion["workspace_path"]
    ):
        raise IngestionRefused("linked task workspace is not its exact canonical directory")
    expected = workspace.joinpath(*ingestion["outbox_path"].split("/"))
    supplied = Path(raw_outbox)
    if not supplied.is_absolute() or supplied != expected or supplied.resolve() != expected:
        raise IngestionRefused("managed result outbox differs from its reservation")
    return expected


def _ensure_private_outbox(workspace: Path) -> int:
    """Create/open `.asha/outbox` without following a planted directory symlink."""
    flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    root_fd = os.open(workspace, flags)
    current = root_fd
    try:
        for name in (".asha", "outbox"):
            try:
                os.mkdir(name, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=current)
            metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
            ):
                os.close(child)
                raise IngestionRefused(
                    "result outbox ancestors must be private owner-controlled directories"
                )
            if current != root_fd:
                os.close(current)
            current = child
        result = os.dup(current)
    finally:
        if current != root_fd:
            os.close(current)
        os.close(root_fd)
    return result


def _write_candidate(path: Path, payload: Mapping[str, Any]) -> None:
    raw = _canonical(payload) + b"\n"
    if len(raw) > MAX_CANDIDATE_BYTES:
        raise IngestionRefused(f"result candidate exceeds {MAX_CANDIDATE_BYTES} bytes")
    parent_fd = _ensure_private_outbox(path.parents[2])
    name = path.name
    temporary = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        try:
            existing = _read_candidate(path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != payload:
                raise IngestionRefused(
                    "reserved result outbox already contains different canonical bytes"
                )
            return
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600, dir_fd=parent_fd,
        )
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except FileExistsError:
            retained = _read_candidate(path)
            if retained != payload:
                raise IngestionRefused(
                    "reserved result outbox already contains different canonical bytes"
                )
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _read_candidate(path: Path) -> dict[str, Any]:
    flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or before.st_size > MAX_CANDIDATE_BYTES
        ):
            raise IngestionRefused(
                "result candidate must be one private owner-controlled regular file"
            )
        raw = b""
        while len(raw) <= MAX_CANDIDATE_BYTES:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_CANDIDATE_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        if (
            len(raw) > MAX_CANDIDATE_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (path_after.st_dev, path_after.st_ino)
        ):
            raise IngestionRefused("result candidate changed while it was read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        candidate = validate_result_candidate(value)
        if raw != _canonical(candidate) + b"\n":
            raise IngestionRefused("result candidate bytes are not canonical")
        return candidate
    except (UnicodeError, json.JSONDecodeError, ModelError, RecursionError) as exc:
        raise IngestionRefused(f"result candidate is invalid: {exc}") from exc


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise IngestionRefused(f"result candidate contains duplicate key: {key}")
        value[key] = item
    return value


def _staging_reservation_snapshot(
    config,
    *,
    initiative_id: str,
    ingestion_id: str,
) -> dict[str, Any]:
    """Find a post-launch reservation without locks, writes, or layout upgrades."""
    try:
        store = InitiativeStore(config)
    except StoreError as exc:
        raise IngestionRefused(
            f"staging token reservation cannot be read: {exc}"
        ) from exc
    deadline = time.monotonic() + config.link_grace_seconds
    while True:
        try:
            matches = [
                item for item in store.list_result_ingestions_snapshot(initiative_id)
                if item["ingestion_id"] == ingestion_id
            ]
        except StoreError as exc:
            if "not found" not in str(exc):
                raise IngestionRefused(
                    f"staging token reservation cannot be read: {exc}"
                ) from exc
            matches = []
        if len(matches) > 1:
            raise IngestionRefused(
                "staging token reservation identity is not unique"
            )
        if matches:
            return matches[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IngestionRefused(
                "staging token reservation is not yet available; the report may be retried"
            )
        time.sleep(min(0.25, remaining))


def _staging_token_failure(
    config,
    parsed: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    task_id: str,
    run_id: str,
    ingestion_id: str,
    outbox: str,
) -> str | None:
    token = env.get("ASHA_CONTROL_RESULT_TOKEN")
    if not isinstance(token, str) or not token:
        return "staging token is absent"
    try:
        reservation = _staging_reservation_snapshot(
            config,
            initiative_id=parsed["initiative_id"],
            ingestion_id=ingestion_id,
        )
    except IngestionRefused as exc:
        return str(exc)
    if reservation["staging_token_digest"] is None:
        return "reservation has no staging token digest"
    expected_outbox = str(Path(reservation["workspace_path"]).joinpath(
        *reservation["outbox_path"].split("/")
    ))
    if (
        reservation["attempt_id"] != parsed["attempt_id"]
        or reservation["task_id"] != task_id
        or reservation["run_id"] != run_id
        or expected_outbox != outbox
    ):
        return "staging token reservation binding is foreign"
    presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(presented, reservation["staging_token_digest"]):
        return "staging token is invalid"
    return None


def stage_result(
    config,
    body: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    control_store: TaskStore | None = None,
    caller_pid: int | None = None,
    tmux: TmuxAdapter | None = None,
) -> dict[str, Any]:
    """Authenticate the producer and write only its reserved workspace envelope."""
    if env.get("ASHA_ORCHESTRATION_COORDINATOR_ID"):
        raise IngestionRefused("coordinator sessions cannot impersonate a managed worker")
    if env.get("ASHA_CONTROL_MANAGED") != "1":
        raise IngestionRefused("result staging requires ASHA_CONTROL_MANAGED=1")
    raw = canonical_body_bytes(body)
    parsed = parse_client_body(raw)
    task_id = env.get("ASHA_CONTROL_TASK_ID")
    run_id = env.get("ASHA_CONTROL_RUN_ID")
    ingestion_id = env.get("ASHA_CONTROL_RESULT_INGESTION_ID")
    outbox = env.get("ASHA_CONTROL_RESULT_OUTBOX")
    if not all(isinstance(item, str) and item for item in (
        task_id, run_id, ingestion_id, outbox,
    )):
        raise IngestionRefused("managed result ingestion environment is incomplete")
    del control_store
    if parsed.get("task_id") != task_id or parsed.get("run_id") != run_id:
        raise IngestionRefused("result candidate task/run/attempt binding is foreign")
    if result_ingestion_id(parsed.get("attempt_id")) != ingestion_id:
        raise IngestionRefused("managed result ingestion ID is foreign to this attempt")
    identity_proof = "pane"
    if caller_pid is not None:
        try:
            pane_id = env.get("TMUX_PANE")
            if not pane_id:
                raise IngestionRefused(
                    "managed result staging requires the linked tmux worker pane"
                )
            adapter = tmux or TmuxAdapter()
            facts = adapter.pane_facts(pane_id)
            if facts.dead or facts.pane_pid is None:
                raise IngestionRefused("linked managed worker pane is not live")
            outbox_digest = hashlib.sha256(outbox.encode("utf-8")).hexdigest()
            if (
                adapter.session_option(facts.session, "@asha_managed") != "1"
                or adapter.session_option(facts.session, "@asha_task_id") != task_id
                or adapter.pane_option(pane_id, "@asha_run_id") != run_id
                or adapter.pane_option(pane_id, "@asha_result_ingestion")
                != ingestion_id
                or adapter.pane_option(pane_id, "@asha_result_outbox_digest")
                != outbox_digest
            ):
                raise IngestionRefused(
                    "managed result staging pane reservation does not match"
                )
            descended = caller_descends_from(
                facts.pane_pid, start_pid=caller_pid,
            )
        except (HarnessError, TmuxError) as exc:
            token_failure = _staging_token_failure(
                config, parsed, env,
                task_id=task_id, run_id=run_id,
                ingestion_id=ingestion_id, outbox=outbox,
            )
            if token_failure is not None:
                raise IngestionRefused(
                    "managed worker identity proof failed: "
                    f"pane proof unreachable: {exc}; "
                    f"token proof failed: {token_failure}"
                ) from exc
            identity_proof = "token"
            descended = True
        if not descended:
            raise IngestionRefused(
                "caller does not descend from the linked managed worker process"
            )
    path = Path(outbox)
    if (
        not path.is_absolute()
        or path.name != f"{ingestion_id}.json"
        or path.parent.name != "outbox"
        or path.parent.parent.name != ".asha"
        or path.parents[2].is_symlink()
        or not path.parents[2].is_dir()
        or path.parents[2].resolve() != path.parents[2]
        or path.resolve() != path
    ):
        raise IngestionRefused(
            "managed result outbox is not the exact reserved workspace path"
        )
    digest = hashlib.sha256(raw).hexdigest()
    candidate = validate_result_candidate({
        "contract": RESULT_CANDIDATE_CONTRACT,
        "ingestion_id": ingestion_id,
        "attempt_id": parsed["attempt_id"],
        "task_id": task_id,
        "run_id": run_id,
        "publication_id": parsed["publication_id"],
        "body_digest": digest,
        "body": copy.deepcopy(parsed),
        "staged_at": _now(),
    })
    # staged_at is part of the reserved bytes; a replay returns the retained
    # candidate rather than fabricating a different timestamp.
    try:
        retained = _read_candidate(path)
    except FileNotFoundError:
        _write_candidate(path, candidate)
        retained = _read_candidate(path)
    if (
        retained["ingestion_id"] != ingestion_id
        or retained["body_digest"] != digest
        or retained["body"] != parsed
    ):
        raise IngestionRefused(
            "reserved result outbox already contains different canonical bytes"
        )
    return {
        "contract": RESULT_STAGING_RECEIPT_CONTRACT,
        "ingestion_id": ingestion_id,
        "publication_id": retained["publication_id"],
        "body_digest": retained["body_digest"],
        "outbox_path": str(path),
        "phase": "staged",
        "identity_proof": identity_proof,
    }


def _candidate_digest(candidate: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(candidate)).hexdigest()


def _validate_ingestion_body(body: Mapping[str, Any]) -> None:
    """Validate the closed worker schema before using any body subfields."""
    attestations = body.get("verification_attestations")
    if isinstance(attestations, list):
        for index, attestation in enumerate(attestations):
            if isinstance(attestation, Mapping):
                missing = _VERIFICATION_ATTESTATION_FIELDS - attestation.keys()
                if missing:
                    raise ModelError(
                        f"result verification_attestations[{index}] is missing "
                        f"required field(s): {', '.join(sorted(missing))}"
                    )
    candidate = copy.deepcopy(dict(body))
    result_id = new_uuid()
    while result_id == candidate.get("supersedes_result_id"):
        result_id = new_uuid()
    candidate.update({
        "result_id": result_id,
        "payload_digest": hashlib.sha256(canonical_body_bytes(body)).hexdigest(),
    })
    validate_result(candidate)


def _transition_ingestion(
    store: InitiativeStore,
    record: Mapping[str, Any],
    **changes: Any,
) -> dict[str, Any]:
    changed = copy.deepcopy(dict(record))
    changed.update(changes)
    changed["updated_at"] = _now()
    changed = validate_result_ingestion(changed)
    store.save_result_ingestion(
        record["initiative_id"], changed, expected_digest=record_digest(record),
    )
    return changed


def _refuse(
    store: InitiativeStore, record: Mapping[str, Any], reason: str,
) -> dict[str, Any]:
    if record["state"] == "completed":
        raise IngestionRefused(reason)
    if record["state"] == "refused":
        if record["refusal"] == reason[:2048]:
            return dict(record)
        raise IngestionRefused(
            f"result ingestion is already refused: {record['refusal']}"
        )
    return _transition_ingestion(
        store, record, state="refused", refusal=reason[:2048],
    )


def _save_verification_evidence(
    store: InitiativeStore, initiative_id: str, ingestion_id: str,
    detail: Mapping[str, Any], output: bytes = b"",
) -> str:
    evidence_id = new_uuid()
    output_path = store.save_output(initiative_id, evidence_id, output)
    summary = json.dumps({
        **copy.deepcopy(dict(detail)),
        "output_path": str(output_path),
        "output_digest": hashlib.sha256(output).hexdigest(),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence = validate_evidence({
        "contract": EVIDENCE_CONTRACT,
        "evidence_id": evidence_id,
        "initiative_id": initiative_id,
        "kind": "verification-command",
        "subject_id": ingestion_id,
        "digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "summary": summary,
        "recorded_at": _now(),
    })
    store.save_evidence(initiative_id, evidence)
    return evidence_id


def _controller_rerun_refusal(
    failure_kind: str, output: bytes, *, detail: str | None = None,
) -> str:
    """Keep the actionable captured tail inside the ingestion refusal bound."""
    prefix = (
        f"controller verification {failure_kind} failure while reproducing "
        "the declared command"
    )
    if detail:
        sanitized = _printable_output_tail(
            detail.encode("utf-8", errors="replace"), limit=400,
        )
        prefix += f": {sanitized}"
    marker = "; output tail: "
    tail = _printable_output_tail(output) or "<empty>"
    head = prefix + marker
    available = MAX_REFUSAL_BYTES - len(head.encode("utf-8"))
    if available <= 0:
        return head.encode("utf-8")[:MAX_REFUSAL_BYTES].decode(
            "utf-8", errors="ignore",
        )
    tail_bytes = tail.encode("utf-8")
    if len(tail_bytes) > available:
        tail = tail_bytes[-available:].decode("utf-8", errors="ignore")
    return head + tail


def _save_verification_environment_gap(
    store: InitiativeStore,
    ingestion: Mapping[str, Any],
    attestation: Mapping[str, Any],
    commit_id: str,
    tree_digest: str,
    output: bytes,
    *,
    detail: str | None = None,
) -> str:
    gap = {
        "kind": "snapshot-verification-environment-gap",
        "claimed_commit_id": commit_id,
        "claimed_tree_digest": tree_digest,
        "argv": list(attestation["argv"]),
        "cwd": attestation["cwd"],
        "failure_kind": "invocation/environment",
        "output_tail": _printable_output_tail(output) or "<empty>",
        "status": "unreproducible-environment",
    }
    if detail:
        gap["failure_detail"] = _printable_output_tail(
            detail.encode("utf-8", errors="replace"), limit=400,
        )
    return _save_verification_evidence(
        store, ingestion["initiative_id"], ingestion["ingestion_id"], gap,
    )


def verify_controller_snapshot(
    store: InitiativeStore,
    ingestion: Mapping[str, Any],
    task: Mapping[str, Any],
    body: Mapping[str, Any],
    commit_id: str,
    tree_digest: str,
    *,
    jj: JjAdapter | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    """Rerun every declared attestation in an exact retained materialization."""
    adapter = jj or JjAdapter()
    source = Path(task["repository"]["root"])
    name = f"ingest-{ingestion['ingestion_id'][:8]}"
    try:
        materialization = prepare_materialization(
            store.config.control, source, commit_id, name, jj=adapter,
        )
        path = Path(materialization["workspace_path"])
        identity = adapter.inspect_workspace(
            path, materialization["workspace_name"], require_empty=False,
        )
        tree = adapter.immutable_tree(path, identity.commit_id)
        tracked, _extra, _truncated = tracked_workspace_status(
            adapter, path, source, commit_id,
        )
        if identity.parent_commit_ids != (commit_id,) or tree.digest != tree_digest or not tracked:
            raise IngestionRefused(
                "controller verification materialization differs from the exact snapshot"
            )
    except IngestionRefused:
        raise
    except (PreparationError, VerificationError, JjError, OSError, ValueError) as exc:
        raise IngestionUnavailable(
            f"controller snapshot materialization failed: {exc}"
        ) from exc
    pre = (identity.change_id, identity.commit_id, identity.parent_commit_ids, tree.digest)
    evidence_ids: list[str] = []
    attestations = body["verification_attestations"]
    if not attestations:
        evidence_ids.append(_save_verification_evidence(
            store, ingestion["initiative_id"], ingestion["ingestion_id"], {
                "kind": "snapshot-integrity",
                "claimed_commit_id": commit_id,
                "claimed_tree_digest": tree_digest,
                "materialization_path": str(path),
                "status": "verified-no-declared-command",
            },
        ))
        return evidence_ids
    bubblewrap = _bubblewrap_program()
    command_environment = os.environ if environment is None else environment
    for attestation in attestations:
        denial = command_denial(list(attestation["argv"]))
        if denial is not None:
            raise IngestionRefused(
                f"controller verification refused declared argv: {denial}"
            )
        output_path_id = new_uuid()
        output_path = store.reserve_output(
            ingestion["initiative_id"], output_path_id,
        )
        output = b""
        try:
            cwd = _command_cwd(path, attestation["cwd"])
            returncode, status = _capture_truncated(
                _contained_argv(
                    bubblewrap, command_environment, list(attestation["argv"]),
                    writable_root=path,
                    output_path=output_path, timeout_seconds=600,
                ),
                cwd=cwd, deadline_seconds=605,
            )
            output = store.read_output(ingestion["initiative_id"], output_path_id)
            if (
                not isinstance(status.get("output_truncated"), bool)
                or not isinstance(status.get("output_original_bytes"), int)
                or status["output_original_bytes"] < len(output)
                or not isinstance(status.get("timed_out"), bool)
                or status.get("output_digest")
                != hashlib.sha256(output).hexdigest()
            ):
                raise IngestionRefused(
                    "controller verification output status is invalid"
                )
            store.finalize_reserved_output(
                ingestion["initiative_id"], output_path_id, output,
            )
        except (VerificationError, StoreError, OSError, ValueError) as exc:
            try:
                output = store.read_output(
                    ingestion["initiative_id"], output_path_id,
                )
            except StoreError:
                pass
            evidence_ids.append(_save_verification_environment_gap(
                store, ingestion, attestation, commit_id, tree_digest, output,
                detail=str(exc),
            ))
            # The same containment wall applies to every later attestation.
            break
        child_status = status.get("returncode")
        if (
            returncode != 0 or status.get("timed_out") is not False
            or status.get("invocation_error") is not None
            or child_status != 0 or attestation["exit_code"] != 0
        ):
            failure_kind = _rerun_failure_kind(
                containment_returncode=returncode,
                child_returncode=child_status,
                invocation_error=status.get("invocation_error"),
                timed_out=status.get("timed_out") is True,
                output=output,
            )
            if failure_kind == "invocation/environment":
                evidence_ids.append(_save_verification_environment_gap(
                    store, ingestion, attestation, commit_id, tree_digest, output,
                ))
                # More reruns cannot add reproduction evidence in this environment.
                break
            raise IngestionRefused(_controller_rerun_refusal(
                failure_kind, output,
            ))
        try:
            after_identity = adapter.inspect_workspace(
                path, materialization["workspace_name"], require_empty=False,
            )
            after_tree = adapter.immutable_tree(path, after_identity.commit_id)
            tracked, _extra, _truncated = tracked_workspace_status(
                adapter, path, source, commit_id,
            )
        except (PreparationError, JjError, OSError, ValueError) as exc:
            raise IngestionRefused(
                f"controller verification post-run identity failed: {exc}"
            ) from exc
        post = (
            after_identity.change_id, after_identity.commit_id,
            after_identity.parent_commit_ids, after_tree.digest,
        )
        if post != pre or not tracked:
            raise IngestionRefused(
                "controller verification command modified the exact snapshot materialization"
            )
        # The supervisor-owned output is already immutable. Bind it directly
        # into the evidence instead of duplicating the potentially large bytes.
        summary = json.dumps({
            "kind": "snapshot-verification-command",
            "claimed_commit_id": commit_id,
            "claimed_tree_digest": tree_digest,
            "argv": attestation["argv"],
            "cwd": attestation["cwd"],
            "exit_code": child_status,
            "worker_output_digest": attestation["output_digest"],
            "output_path": str(output_path),
            "output_digest": hashlib.sha256(output).hexdigest(),
            "pre_identity": pre,
            "post_identity": post,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        evidence = validate_evidence({
            "contract": EVIDENCE_CONTRACT,
            "evidence_id": output_path_id,
            "initiative_id": ingestion["initiative_id"],
            "kind": "verification-command",
            "subject_id": ingestion["ingestion_id"],
            "digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            "summary": summary,
            "recorded_at": _now(),
        })
        store.save_evidence(ingestion["initiative_id"], evidence)
        evidence_ids.append(output_path_id)
    return evidence_ids


def _receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract": RESULT_INGESTION_RECEIPT_CONTRACT,
        "ingestion_id": record["ingestion_id"],
        "publication_id": record["publication_id"],
        "result_id": record["result_id"],
        "phase": record["state"],
        "refusal": record["refusal"],
    }


def ingest_result(
    store: InitiativeStore,
    initiative_id: str,
    ingestion_id: str,
    *,
    ingester: Mapping[str, Any] | None = None,
    control_store: TaskStore | None = None,
    jj: JjAdapter | None = None,
    terminal_reconciliation: Mapping[str, Any] | None = None,
    verifier: Callable[..., list[str]] | None = None,
) -> dict[str, Any]:
    """Single-flight one exact staged candidate through controller ingestion."""
    with store.result_ingestion_lock(initiative_id, ingestion_id):
        return _ingest_result(
            store, initiative_id, ingestion_id,
            ingester=ingester, control_store=control_store, jj=jj,
            terminal_reconciliation=terminal_reconciliation, verifier=verifier,
        )


def _ingest_result(
    store: InitiativeStore,
    initiative_id: str,
    ingestion_id: str,
    *,
    ingester: Mapping[str, Any] | None = None,
    control_store: TaskStore | None = None,
    jj: JjAdapter | None = None,
    terminal_reconciliation: Mapping[str, Any] | None = None,
    verifier: Callable[..., list[str]] | None = None,
) -> dict[str, Any]:
    """Accept one exact staged candidate after terminal producer evidence."""
    adapter = jj or JjAdapter()
    control = control_store or TaskStore(store.config.control)
    actor = dict(ingester or {
        "actor_kind": "controller", "actor_id": "result-ingester",
        "coordinator_generation": None,
    })
    with store.transaction_lock(initiative_id):
        try:
            record = store.read_result_ingestion(initiative_id, ingestion_id)
        except StoreError as exc:
            if "not found" in str(exc):
                raise IngestionRefused("result candidate has no controller reservation") from exc
            raise IngestionRefused(f"result ingestion reservation is unreadable: {exc}") from exc
        task = control.peek(record["task_id"])
        if record["state"] == "completed":
            path = _workspace_candidate_path(
                task, record,
                str(Path(record["workspace_path"]).joinpath(
                    *record["outbox_path"].split("/"),
                )),
            )
            try:
                candidate = _read_candidate(path)
            except FileNotFoundError as exc:
                raise IngestionRefused(
                    "completed result ingestion candidate is missing"
                ) from exc
            except (IngestionRefused, OSError) as exc:
                raise IngestionRefused(
                    "completed result ingestion candidate is modified or unreadable: "
                    f"{exc}"
                ) from exc
            if _candidate_digest(candidate) != record["candidate_digest"]:
                raise IngestionRefused(
                    "completed result ingestion candidate was modified"
                )
            return _receipt(record)
        initiative = store.peek(initiative_id)
        attempt = store.read_attempt(initiative_id, record["attempt_id"])
        node = store.read_node(initiative_id, record["node_id"])
        link = store.read_link(initiative_id, record["attempt_id"])
        expected = (
            initiative["active_plan"] is not None
            and initiative["active_plan"]["digest"] == record["active_plan_digest"]
            and link["active_plan_digest"] == record["active_plan_digest"]
            and link["control_task_identity_digest"]
            == record["control_task_identity_digest"]
            and control_task_identity_digest(task)
            == record["control_task_identity_digest"]
            and attempt["task_id"] == record["task_id"]
            and attempt["node_id"] == record["node_id"]
            and attempt["seal_id"] is None
            and task["runs"][0]["run_id"] == record["run_id"]
            and task["jj"]["workspace_path"] == record["workspace_path"]
            and task["jj"]["workspace_name"] == record["workspace_name"]
            and task["jj"]["change_id"] == record["change_id"]
        )
        if not expected:
            refused = _refuse(
                store, record,
                "stale result ingestion binding: plan, attempt, task, or workspace changed",
            )
            return _receipt(refused)
        path = _workspace_candidate_path(
            task, record,
            str(Path(record["workspace_path"]).joinpath(*record["outbox_path"].split("/"))),
        )
        try:
            candidate = _read_candidate(path)
        except FileNotFoundError as exc:
            if record["state"] != "reserved":
                refused = _refuse(store, record, "result candidate disappeared after staging")
                return _receipt(refused)
            raise IngestionRefused("reserved result candidate has not been staged") from exc
        except (IngestionRefused, OSError) as exc:
            reason = f"result candidate is modified or unreadable: {exc}"
            refused = _refuse(store, record, reason)
            return _receipt(refused)
        digest = _candidate_digest(candidate)
        if record["state"] == "refused":
            return _receipt(record)
        if (
            record["candidate_digest"] is not None
            and digest != record["candidate_digest"]
        ):
            refused = _refuse(store, record, "result candidate was modified after reservation")
            return _receipt(refused)
        try:
            body = parse_client_body(canonical_body_bytes(candidate["body"]))
            _validate_ingestion_body(body)
        except (ModelError, ResultError) as exc:
            refused = _refuse(
                store, record, f"result candidate body is invalid: {exc}",
            )
            return _receipt(refused)
        if candidate["body_digest"] != hashlib.sha256(canonical_body_bytes(body)).hexdigest():
            refused = _refuse(store, record, "result candidate body digest changed")
            return _receipt(refused)
        bindings = {
            "ingestion_id": ingestion_id,
            "attempt_id": record["attempt_id"],
            "task_id": record["task_id"],
            "run_id": record["run_id"],
            "publication_id": body["publication_id"],
        }
        if any(candidate[field] != value for field, value in bindings.items()):
            refused = _refuse(store, record, "result candidate is foreign to its reservation")
            return _receipt(refused)
        for field in ("initiative_id", "node_id", "attempt_id", "task_id", "run_id"):
            expected_value = (
                initiative_id if field == "initiative_id" else
                record[field]
            )
            if body.get(field) != expected_value:
                refused = _refuse(
                    store, record, f"result candidate {field} is foreign to its reservation",
                )
                return _receipt(refused)
        for changed_path in body.get("files_changed", []):
            if not _in_scope(changed_path, node["hard_write_scope"]):
                refused = _refuse(
                    store, record,
                    f"result candidate path is outside the node hard scope: {changed_path}",
                )
                return _receipt(refused)
        if record["state"] == "reserved":
            record = _transition_ingestion(
                store, record, state="ingesting", candidate_digest=digest,
                publication_id=body["publication_id"], ingester=actor,
            )
    observed = terminal_reconciliation
    if observed is None:
        socket = task["tmux"]["socket"]
        observed = reconcile_task(task, LiveAdapters(
            config=store.config.control,
            tmux=TmuxAdapter(socket=None if socket == "default" else socket),
            jj=adapter,
        ))
    if observed.get("state") not in {"exited", "failed"}:
        raise IngestionRefused("result ingestion requires terminal producer process evidence")

    try:
        creator = "none"
        claimed_commit: str | None = None
        claimed_tree: str | None = None
        evidence_ids: list[str] = []
        if node["type"] != "review":
            base, _failures, base_commit = _base_binding(
                store, initiative_id, node, attempt,
            )
            if record["claimed_commit_id"] is not None:
                retained_identity = adapter.inspect_workspace(
                    Path(task["jj"]["workspace_path"]),
                    task["jj"]["workspace_name"], snapshot=False,
                    require_empty=False,
                )
                retained_tree = adapter.immutable_tree(
                    Path(task["jj"]["workspace_path"]),
                    retained_identity.commit_id,
                )
                if (
                    retained_identity.commit_id != record["claimed_commit_id"]
                    or retained_tree.digest != record["claimed_tree_digest"]
                ):
                    raise IngestionRefused(
                        "retained controller snapshot changed before publication"
                    )
                creator = record["commit_creator"]
                if creator not in {"worker", "controller"}:
                    raise IngestionRefused(
                        "retained snapshot has no immutable creator provenance"
                    )
                claimed_commit = record["claimed_commit_id"]
                claimed_tree = record["claimed_tree_digest"]
                evidence_ids = list(record["verification_evidence_ids"])
                identity = retained_identity
                final_tree = retained_tree
            else:
                before = adapter.inspect_workspace(
                    Path(task["jj"]["workspace_path"]), task["jj"]["workspace_name"],
                    snapshot=False, require_empty=False,
                )
                identity = adapter.inspect_workspace(
                    Path(task["jj"]["workspace_path"]), task["jj"]["workspace_name"],
                    snapshot=True, require_empty=False,
                    exclude_control_transport=True,
                )
                creator = (
                    "controller"
                    if (
                        identity.commit_id != before.commit_id
                        or before.commit_id == task["jj"]["working_commit_id"]
                    )
                    else "worker"
                )
                final_tree = adapter.immutable_tree(
                    Path(task["jj"]["workspace_path"]), identity.commit_id,
                )
            base_tree = adapter.immutable_tree(
                Path(task["jj"]["workspace_path"]), base_commit,
            )
            origin_tree = adapter.immutable_tree(
                Path(task["jj"]["workspace_path"]),
                attempt["base"]["scope_origin"]["jj_commit_id"],
            )
            changed_paths, _diff_digest = _diff(base_tree, final_tree)
            cumulative, _cumulative_digest = _diff(origin_tree, final_tree)
            if (
                identity.change_id != record["change_id"]
                or identity.parent_commit_ids != (base_commit,)
                or origin_tree.digest != attempt["base"]["scope_origin"]["tree_digest"]
                or (base["kind"] != "composition-inputs" and base_tree.digest != base["tree_digest"])
            ):
                raise IngestionRefused(
                    "controller snapshot workspace/change/base identity changed"
                )
            violations = [
                item for item in cumulative
                if not _in_scope(item, node["hard_write_scope"])
            ]
            if violations:
                raise IngestionRefused(
                    f"controller snapshot contains out-of-scope path: {violations[0]}"
                )
            missing = sorted(set(body["files_changed"]) - set(changed_paths))
            if missing:
                raise IngestionRefused(
                    f"result claimed changed path is absent from the snapshot: {missing[0]}"
                )
            claimed_commit = identity.commit_id
            claimed_tree = final_tree.digest
            if record["claimed_commit_id"] is None:
                if creator == "controller":
                    evidence_ids = [_save_verification_evidence(
                        store, initiative_id, ingestion_id, {
                            "kind": "controller-snapshot-created",
                            "claimed_commit_id": claimed_commit,
                            "claimed_tree_digest": claimed_tree,
                            "status": "captured-before-independent-verification",
                        },
                    )]
                with store.transaction_lock(initiative_id):
                    current_snapshot = store.read_result_ingestion(
                        initiative_id, ingestion_id,
                    )
                    if current_snapshot["candidate_digest"] != record["candidate_digest"]:
                        raise IngestionRefused(
                            "result candidate binding changed during controller snapshot"
                        )
                    record = _transition_ingestion(
                        store, current_snapshot,
                        claimed_commit_id=claimed_commit,
                        claimed_tree_digest=claimed_tree,
                        commit_creator=creator,
                        verification_evidence_ids=evidence_ids,
                    )
            if creator == "controller" and len(evidence_ids) == 1:
                evidence_ids.extend((verifier or verify_controller_snapshot)(
                    store, record, task, body, claimed_commit, claimed_tree,
                    jj=adapter,
                ))
    except IngestionRefused as exc:
        with store.transaction_lock(initiative_id):
            current = store.read_result_ingestion(initiative_id, ingestion_id)
            refused = _refuse(store, current, str(exc))
        return _receipt(refused)
    except IngestionUnavailable:
        raise
    except (JjError, PreparationError, VerificationError, OSError, ValueError) as exc:
        # Only deterministic guards may refuse; an environment-class failure
        # leaves the record retryable for the next supervisor pass (#76).
        raise IngestionUnavailable(
            f"result ingestion was interrupted: {exc}"
        ) from exc

    with store.transaction_lock(initiative_id):
        current = store.read_result_ingestion(initiative_id, ingestion_id)
        try:
            latest_candidate = _read_candidate(path)
        except (FileNotFoundError, IngestionRefused, OSError) as exc:
            refused = _refuse(
                store, current, f"result candidate changed during ingestion: {exc}",
            )
            return _receipt(refused)
        if _candidate_digest(latest_candidate) != current["candidate_digest"]:
            refused = _refuse(store, current, "result candidate was modified during ingestion")
            return _receipt(refused)
        latest_initiative = store.peek(initiative_id)
        latest_task = control.peek(record["task_id"])
        latest_attempt = store.read_attempt(initiative_id, record["attempt_id"])
        if (
            latest_initiative.get("active_plan", {}).get("digest")
            != record["active_plan_digest"]
            or control_task_identity_digest(latest_task)
            != record["control_task_identity_digest"]
            or latest_attempt["seal_id"] is not None
        ):
            refused = _refuse(
                store, current, "result ingestion binding changed after snapshot verification",
            )
            return _receipt(refused)
        if (
            current["claimed_commit_id"] is not None
            and (
                current["claimed_commit_id"] != claimed_commit
                or current["claimed_tree_digest"] != claimed_tree
                or current["commit_creator"] != creator
                or evidence_ids[:len(current["verification_evidence_ids"])]
                != current["verification_evidence_ids"]
            )
        ):
            refused = _refuse(
                store, current, "retained result ingestion snapshot binding conflicts",
            )
            return _receipt(refused)
        if (
            current["claimed_commit_id"] is None
            or current["verification_evidence_ids"] != evidence_ids
            or current["commit_creator"] is None
        ):
            current = _transition_ingestion(
                store, current, claimed_commit_id=claimed_commit,
                claimed_tree_digest=claimed_tree,
                commit_creator=creator,
                verification_evidence_ids=evidence_ids,
            )
        publication_ingester = current["ingester"] or actor
        provenance = {
            "method": "controller-ingestion",
            "producer_run_id": record["run_id"],
            "ingestion_id": ingestion_id,
            "ingester_actor_kind": publication_ingester["actor_kind"],
            "ingester_actor_id": publication_ingester["actor_id"],
            "ingester_coordinator_generation": publication_ingester[
                "coordinator_generation"
            ],
        }
        commit_provenance = {
            "creator": creator,
            "actor_id": (
                None if creator == "none" else
                publication_ingester["actor_id"]
                if creator == "controller" else record["run_id"]
            ),
            "verification_evidence_ids": evidence_ids,
        }
        binding = (
            store, latest_initiative,
            store.read_link(initiative_id, record["attempt_id"]),
            latest_attempt, node, latest_task,
        )
    try:
        receipt = publish_bound_result(
            store, body, binding=binding,
            publication_provenance=provenance,
            claimed_commit_id=claimed_commit,
            commit_provenance=commit_provenance,
            control_store=control, jj=adapter,
        )
    except ResultRefused as exc:
        with store.transaction_lock(initiative_id):
            current = store.read_result_ingestion(initiative_id, ingestion_id)
            refused = _refuse(
                store, current, f"authoritative result publication refused: {exc}",
            )
        return _receipt(refused)
    with store.transaction_lock(initiative_id):
        current = store.read_result_ingestion(initiative_id, ingestion_id)
        if receipt["phase"] != "completed":
            refused = _refuse(
                store, current,
                f"authoritative result publication was {receipt['phase']}: {receipt['refusal']}",
            )
            return _receipt(refused)
        current = store.read_result_ingestion(initiative_id, ingestion_id)
        completed = _transition_ingestion(
            store, current, state="completed", result_id=receipt["result_id"],
        )
        return _receipt(completed)


def _record_ingestion_deferral(
    store: InitiativeStore,
    initiative_id: str,
    record: Mapping[str, Any],
    reason: str,
) -> None:
    """Best-effort durable trace of a retryable ingestion abort, deduplicated."""
    from .actions import append_event

    subject_ids = [record["node_id"], record["attempt_id"], record["ingestion_id"]]
    # The record's own state moves between retries; only the reason carries
    # diagnostic weight, and only a changed reason deserves a new event.
    payload = {"reason": reason[:500]}
    try:
        previous = next((
            event for event in reversed(store.list_events_snapshot(initiative_id))
            if event["type"] == "result-ingestion-deferred"
            and event["subject_ids"] == subject_ids
        ), None)
        if previous is not None and previous["payload"] == payload:
            return
        append_event(
            store, initiative_id, "result-ingestion-deferred", subject_ids,
            payload, actor_kind="controller", actor_id="live-reconciler",
        )
    except (StoreError, OSError, ValueError):
        # Visibility must never break the retry itself.
        pass


def ingest_pending_results(
    store: InitiativeStore,
    initiative_id: str,
    *,
    control_store: TaskStore | None = None,
    adapters_factory: Callable[[dict[str, Any]], LiveAdapters] | None = None,
) -> list[dict[str, Any]]:
    """Supervisor pass: ingest staged candidates whose producers are terminal."""
    receipts: list[dict[str, Any]] = []
    control = control_store or TaskStore(store.config.control)
    # A first locked use upgrades initiatives created before the additive
    # result-ingestions directory existed.
    with store.transaction_lock(initiative_id):
        records = store.list_result_ingestions_snapshot(initiative_id)
    for record in records:
        if record["state"] in {"completed", "refused"}:
            continue
        path = Path(record["workspace_path"]).joinpath(*record["outbox_path"].split("/"))
        if not path.is_file():
            continue
        try:
            task = control.peek(record["task_id"])
            if adapters_factory is None:
                socket = task["tmux"]["socket"]
                adapters = LiveAdapters(
                    config=store.config.control,
                    tmux=TmuxAdapter(socket=None if socket == "default" else socket),
                    jj=JjAdapter(),
                )
            else:
                adapters = adapters_factory(task)
            observed = reconcile_task(task, adapters)
            if observed["state"] not in {"exited", "failed"}:
                continue
            receipts.append(ingest_result(
                store, initiative_id, record["ingestion_id"],
                control_store=control,
                jj=(adapters.jj_adapter if isinstance(adapters, LiveAdapters) else None),
                terminal_reconciliation=observed,
            ))
        except IngestionUnavailable as exc:
            # Retryable, but never silent: the deferral reason is the only
            # trace of why an ingest pass aborted (#77).
            _record_ingestion_deferral(store, initiative_id, record, str(exc))
            continue
        except (ResultError, StoreError, JjError, OSError, ValueError):
            # The authoritative ingestion journal retains precise refusals.
            # Unavailable live evidence is retried by the next supervisor pass.
            continue
    return receipts


__all__ = [
    "IngestionError", "IngestionRefused", "IngestionUnavailable",
    "RESULT_INGESTION_RECEIPT_CONTRACT",
    "RESULT_STAGING_RECEIPT_CONTRACT", "ingest_pending_results", "ingest_result",
    "reserve_result_ingestion", "result_ingestion_id", "result_outbox_path",
    "stage_result", "verify_controller_snapshot",
]
