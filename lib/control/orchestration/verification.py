"""Controller-owned approved-argv verification in retained materializations."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pwd
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..jj import JjAdapter, JjError
from ..process import capture_bytes
from ..prepare import (
    PreparationError,
    plan_materialization,
    prepare_materialization,
)
from .actions import append_event
from .composition import bundle_composition_digest, bundle_composition_inputs
from .model import (
    EVIDENCE_CONTRACT,
    MAX_SUMMARY_BYTES,
    VERIFICATION_CONTRACT,
    new_uuid,
    record_digest,
    validate_evidence,
    validate_node,
    validate_verification,
)
from .review import specification_digest, tracked_workspace_status
from .store import InitiativeStore, StoreError


MAX_VERIFICATION_OUTPUT_BYTES = 1024 * 1024
COMPOSED_VERIFICATION_KIND = "composed-verification"
COMPOSED_COMMAND_KIND = "composed-verification-command"
_COMPOSED_OUTCOMES = ("failed", "passed", "indeterminate")
_STATUS_TAIL_BYTES = 64 * 1024
_PROCESS_STATUS_PREFIX = b"\nASHA_VERIFICATION_PROCESS_V1:"
_FAILURE_OUTPUT_TAIL_BYTES = 2048
_EMPTY_CAPTURED_OUTPUT = b"\n--- stderr ---\n"


class VerificationError(ValueError):
    """A verification specification, command, or candidate identity failed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _identity_digest(
    workspace_name: str, change_id: str, commit_id: str, tree_digest: str,
) -> str:
    return hashlib.sha256(_canonical([
        workspace_name, change_id, commit_id, tree_digest,
    ])).hexdigest()


def _verification_gate(plan: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    gates = [
        gate for gate in plan["declared_gates"]
        if gate["kind"] == "verification" and gate["node_id"] == node_id
        and gate["required"] is True
    ]
    if len(gates) != 1:
        raise VerificationError("verify node requires one approved verification specification")
    return gates[0]


def terminal_seals(
    store: InitiativeStore,
    initiative_id: str,
    plan: Mapping[str, Any],
    initiative: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One latest success seal per scope member, in scope order.

    A repository-scope initiative has exactly one terminal candidate; a
    workspace-scope initiative has one per member repository (graph rule).
    """
    from .model import scope_repositories

    record = initiative if initiative is not None else store.peek(initiative_id)
    members = scope_repositories(record)
    candidates = [item for item in plan["nodes"] if item["terminal_candidate"]]
    if len(members) == 1 and len(candidates) != 1:
        raise VerificationError("Core verification requires one terminal candidate producer")
    seals = store.list_seals_snapshot(initiative_id)
    ordered: list[dict[str, Any]] = []
    for member in members:
        producers = [item for item in candidates if item["repository_id"] == member["repository_id"]]
        if len(producers) != 1:
            raise VerificationError(
                f"repository {member['repository_id']} needs exactly one terminal candidate producer"
            )
        success = sorted(
            (
                item for item in seals
                if item["node_id"] == producers[0]["node_id"]
                and item["repository_id"] == member["repository_id"]
                and item["outcome"] == "success"
            ),
            key=lambda item: (item["sealed_at"], item["seal_id"]),
        )
        if not success:
            raise VerificationError("terminal candidate has no success seal")
        ordered.append(success[-1])
    return ordered


def _terminal_seal(
    store: InitiativeStore,
    initiative_id: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """The primary (first-member) terminal seal; kept for single-member callers."""
    return terminal_seals(store, initiative_id, plan)[0]


def candidate_bundle_digest(
    initiative: Mapping[str, Any],
    plan: Mapping[str, Any],
    seal: Mapping[str, Any] | list[Mapping[str, Any]],
    gate: Mapping[str, Any],
) -> str:
    """Bind the exact terminal candidate set to the approved verification gate.

    A single member keeps the Increment 3 canonical shape byte-for-byte; a
    multi-member set binds the ordered member identities instead.
    """
    members = [seal] if isinstance(seal, Mapping) else list(seal)
    if len(members) == 1:
        only = members[0]
        return hashlib.sha256(_canonical({
            "initiative_id": initiative["initiative_id"],
            "active_plan_digest": plan["digest"],
            "aggregate_spec_digest": specification_digest(initiative, plan),
            "repository_id": only["repository_id"],
            "seal_id": only["seal_id"],
            "jj_commit_id": only["jj_commit_id"],
            "tree_digest": only["tree_digest"],
            "diff_digest": only["diff_digest"],
            "verification_spec": gate,
        })).hexdigest()
    return hashlib.sha256(_canonical({
        "initiative_id": initiative["initiative_id"],
        "active_plan_digest": plan["digest"],
        "aggregate_spec_digest": specification_digest(initiative, plan),
        "members": [
            {
                "repository_id": item["repository_id"], "seal_id": item["seal_id"],
                "jj_commit_id": item["jj_commit_id"], "tree_digest": item["tree_digest"],
                "diff_digest": item["diff_digest"],
            }
            for item in members
        ],
        "verification_spec": gate,
    })).hexdigest()


def _member_root(initiative: Mapping[str, Any], seal: Mapping[str, Any]) -> Path:
    from .model import repository_by_id

    return Path(repository_by_id(initiative, seal["repository_id"])["root"])


def _member_materialization_name(initiative_id: str, verification_id: str, index: int) -> str:
    base = f"verify-{initiative_id}-{verification_id[:8]}"
    return base if index == 0 else f"{base}-{index}"


def verification_members(
    store: InitiativeStore, initiative_id: str, verification_id: str,
) -> list[dict[str, Any]]:
    """Per-member bindings recorded as immutable `verification-member` evidence, in scope order."""
    members = []
    for evidence in store.list_evidence_snapshot(initiative_id):
        if evidence["kind"] != "verification-member" or evidence["subject_id"] != verification_id:
            continue
        try:
            members.append(json.loads(evidence["summary"]))
        except (TypeError, ValueError) as exc:
            raise VerificationError("verification member evidence is unreadable") from exc
    return sorted(members, key=lambda item: item["index"])


def prevalidate_verification(
    store: InitiativeStore, initiative_id: str, node_id: str,
    *, exclude_action_id: str | None = None,
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any],
    Path, dict[str, Any], dict[str, Any],
]:
    """Refuse an ineligible controller verification before dispatch is journaled."""
    initiative = store.peek(initiative_id)
    if initiative["state"] != "running" or initiative["active_plan"] is None:
        raise VerificationError("verification requires a running approved initiative")
    plan = store.read_plan(initiative_id, initiative["active_plan"]["revision"])
    if plan["digest"] != initiative["active_plan"]["digest"]:
        raise VerificationError("active plan digest differs from its retained revision")
    node = store.read_node(initiative_id, node_id)
    if node["type"] != "verify" or node["state"] != "ready":
        raise VerificationError("verify node is not deterministically ready")
    from .scheduler import readiness

    if readiness(store, initiative).get(node_id) != "ready":
        raise VerificationError("verify node is blocked by effective readiness")
    for action in store.list_actions_snapshot(initiative_id):
        if (
            action["action_id"] == exclude_action_id
            or action["action_class"] != "dispatch-node"
            or action["state"] in {"completed", "refused"}
        ):
            continue
        try:
            outcome = json.loads(action["outcome"])
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            outcome.get("node_id") == node_id
            or outcome.get("payload", {}).get("node_id") == node_id
        ):
            raise VerificationError("verify node already has an active dispatch action")
    gate = _verification_gate(plan, node_id)
    seals = terminal_seals(store, initiative_id, plan, initiative)
    seal = seals[0]
    all_reviews = store.list_reviews_snapshot(initiative_id)
    reviews: list[dict[str, Any]] = []
    for member_seal in seals:
        matching = [
            item for item in all_reviews
            if item["state"] == "accepted-pass"
            and item["target"]["seal_id"] == member_seal["seal_id"]
            and item["target"]["active_plan_digest"] == plan["digest"]
            and item["target"]["specification_digest"]
            == specification_digest(initiative, plan)
            and item["target"]["repository_id"] == member_seal["repository_id"]
            and item["target"]["jj_commit_id"] == member_seal["jj_commit_id"]
            and item["target"]["base_seal_ids"] == member_seal["base"]["seal_ids"]
            and item["target"]["diff_digest"] == member_seal["diff_digest"]
        ]
        if len(matching) != 1:
            raise VerificationError(
                "verification requires one accepted-pass review on the exact seal"
            )
        reviews.append(matching[0])
    prior = [
        item for item in store.list_verifications_snapshot(initiative_id)
        if item["node_id"] == node_id
        and item["active_plan_digest"] == plan["digest"]
        and item["state"] != "stale"
    ]
    if any(item["state"] != "indeterminate" for item in prior):
        raise VerificationError("verify node already has a retained current execution")
    return initiative, plan, node, gate, _bubblewrap_program(), seal, reviews[0]


def prevalidate_verification_members(
    store: InitiativeStore, initiative_id: str, node_id: str,
    *, exclude_action_id: str | None = None,
) -> list[dict[str, Any]]:
    """The ordered terminal seals a verification binds (after the same prevalidation)."""
    initiative, plan, _node, _gate, _bubblewrap, _seal, _review = prevalidate_verification(
        store, initiative_id, node_id, exclude_action_id=exclude_action_id,
    )
    return terminal_seals(store, initiative_id, plan, initiative)


def command_denial(argv: list[str]) -> str | None:
    """Return the known external-write class before executing an argv."""
    if not argv:
        return "empty argv"
    if argv[0].startswith("-") or "=" in argv[0]:
        return "invalid executable token"
    program = Path(argv[0]).name.lower()
    lowered = [item.lower() for item in argv[1:]]
    if program in {
        "gh", "curl", "wget", "ssh", "scp", "rsync", "docker", "sudo",
        "env", "sh", "bash", "dash", "zsh", "ksh", "fish", "busybox",
        "timeout", "nice", "nohup", "setsid", "xargs", "twine",
    }:
        return program
    if program == "git" and any(item in {"push", "commit", "tag"} for item in lowered):
        return "git external-write subcommand"
    if program == "jj" and "git" in lowered and "push" in lowered:
        return "jj git push"
    if re.fullmatch(r"pip(?:\d+(?:\.\d+)*)?", program) and "install" in lowered:
        return "pip install"
    python_modules: set[str] = set()
    if program.startswith("python"):
        modules = [
            lowered[index + 1]
            for index, item in enumerate(lowered[:-1])
            if item == "-m"
        ]
        modules.extend(
            item[2:] for item in lowered if item.startswith("-m") and len(item) > 2
        )
        python_modules = {item.split(".", 1)[0] for item in modules}
    if program.startswith("python") and "install" in lowered:
        if "pip" in python_modules:
            return "python -m pip install"
        if "uv" in python_modules and "pip" in lowered:
            return "python -m uv pip install"
    if program.startswith("python"):
        if "twine" in python_modules:
            return "python -m twine"
        if "poetry" in python_modules and "publish" in lowered:
            return "python -m poetry publish"
    if program == "npm" and any(item in {"publish", "install", "i"} for item in lowered):
        return "npm publish/install"
    if program == "cargo" and "publish" in lowered:
        return "cargo publish"
    if program == "gem" and "push" in lowered:
        return "gem push"
    if program == "poetry" and "publish" in lowered:
        return "poetry publish"
    if program == "uv" and "pip" in lowered and "install" in lowered:
        return "uv pip install"
    if program == "rm":
        for item in argv[1:]:
            if item == "--recursive" or (
                item.startswith("-") and not item.startswith("--")
                and any(flag in item[1:] for flag in ("r", "R"))
            ):
                return "recursive rm"
    return None


def _command_cwd(materialization: Path, relative: str) -> Path:
    target = materialization if relative == "." else materialization.joinpath(*relative.split("/"))
    current = materialization
    for part in target.relative_to(materialization).parts:
        current /= part
        if current.is_symlink():
            raise VerificationError("verification cwd traverses a symlink")
    if not target.is_dir() or target.resolve() != target:
        raise VerificationError("verification cwd must be an exact directory inside the materialization")
    return target


def _minimal_argv(
    environment: Mapping[str, str], argv: list[str],
) -> list[str]:
    """Clear the inherited environment while retaining bounded streaming."""
    exact = {
        "PATH": environment.get("PATH", "/usr/bin:/bin"),
        "HOME": environment.get("HOME", "/tmp"),
        "LANG": environment.get("LANG", "C.UTF-8"),
    }
    env_program = Path("/usr/bin/env")
    if not env_program.is_file() or not os.access(env_program, os.X_OK):
        raise VerificationError("minimal environment launcher is unavailable")
    return [
        str(env_program), "-i", "--",
        *(f"{name}={value}" for name, value in exact.items()),
        *argv,
    ]


def _printable_output_tail(
    output: bytes, *, limit: int = _FAILURE_OUTPUT_TAIL_BYTES,
) -> str:
    """Render the bounded end of captured bytes safely in evidence or refusals."""
    text = output[-limit:].decode("utf-8", errors="replace")
    return "".join(character if character.isprintable() else "?" for character in text)


def _rerun_failure_kind(
    *, containment_returncode: int, child_returncode: Any,
    invocation_error: Any, timed_out: bool, output: bytes,
) -> str:
    """Separate a likely invocation/environment gap from a command failure."""
    if containment_returncode != 0 or invocation_error is not None:
        return "invocation/environment"
    if (
        isinstance(child_returncode, int)
        and not isinstance(child_returncode, bool)
        and child_returncode > 0
        and not timed_out
        and output in {b"", _EMPTY_CAPTURED_OUTPUT}
    ):
        return "invocation/environment"
    return "command"


def _bubblewrap_program() -> Path:
    for candidate in (Path("/usr/bin/bwrap"), Path("/bin/bwrap")):
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            resolved.is_file()
            and os.access(resolved, os.X_OK)
            and (metadata.st_mode & 0o022) == 0
        ):
            return resolved
    raise VerificationError("trusted bubblewrap PID containment is unavailable")


def _contained_argv(
    bubblewrap: Path,
    environment: Mapping[str, str],
    argv: list[str],
    *,
    writable_root: Path,
    output_path: Path,
    timeout_seconds: int,
) -> list[str]:
    supervisor = Path(__file__).with_name("verification_supervisor.py").resolve()
    if not supervisor.is_file():
        raise VerificationError("verification process supervisor is unavailable")
    writable_root = Path(writable_root)
    output_path = Path(output_path)
    if (
        not writable_root.is_absolute()
        or writable_root.resolve() != writable_root
        or not writable_root.is_dir()
        or not output_path.is_absolute()
        or output_path.resolve() != output_path
        or not output_path.is_file()
    ):
        raise VerificationError("verification containment paths are not exact")
    minimal_environment = dict(environment)
    if "HOME" not in minimal_environment:
        try:
            minimal_environment["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError as exc:
            raise VerificationError("invoking user's home directory is unavailable") from exc
    # The root is already ro-bound, so HOME remains read-only. Toolchains such
    # as rustup and cargo anchor required read-only state there; masking it with
    # the empty /tmp tmpfs makes honest worker results unreproducible.
    return [
        str(bubblewrap), "--unshare-pid", "--die-with-parent",
        "--ro-bind", "/", "/", "--tmpfs", "/tmp",
        "--bind", str(writable_root), str(writable_root),
        "--bind", str(output_path), str(output_path),
        "--proc", "/proc", "--dev-bind", "/dev", "/dev",
        "--", *_minimal_argv(
            minimal_environment,
            [
                sys.executable, str(supervisor),
                "--output", str(output_path),
                "--limit", str(MAX_VERIFICATION_OUTPUT_BYTES),
                "--timeout", str(timeout_seconds),
                "--", *argv,
            ],
        ),
    ]


def _process_status(stderr: bytes) -> tuple[dict[str, Any], bytes]:
    offset = stderr.rfind(_PROCESS_STATUS_PREFIX)
    if offset < 0 or not stderr.endswith(b"\n"):
        raise VerificationError("verification supervisor returned no process status")
    raw = stderr[offset + len(_PROCESS_STATUS_PREFIX):-1]
    try:
        status = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise VerificationError("verification supervisor process status is invalid") from exc
    if not isinstance(status, dict) or set(status) != {
        "pid", "start_ticks", "pid_namespace", "returncode", "invocation_error",
        "timed_out", "output_truncated", "output_original_bytes", "output_digest",
    }:
        raise VerificationError("verification supervisor process status is incomplete")
    return status, stderr[:offset]


def _capture_truncated(
    argv: list[str], *, cwd: Path, deadline_seconds: int,
) -> tuple[int, dict[str, Any]]:
    """Run the bounded orchestration shim through Control's argv-only seam."""
    returncode, stdout, stderr = capture_bytes(
        argv, cwd=cwd, limit=_STATUS_TAIL_BYTES, runner=None,
        error_type=VerificationError,
        # The inner supervisor owns the approved timeout and needs a bounded
        # interval to persist its status before the outer containment deadline.
        deadline_seconds=deadline_seconds + 5,
    )
    status, clean_stderr = _process_status(stderr)
    if stdout or clean_stderr:
        raise VerificationError("verification containment emitted unexpected output")
    return returncode, status


def _save_command_evidence(
    store: InitiativeStore,
    initiative_id: str,
    evidence_id: str,
    verification_id: str,
    detail: Mapping[str, Any],
    output: bytes,
    *,
    output_path: Path | None = None,
    kind: str = "verification-command",
) -> tuple[dict[str, Any], str, str]:
    output_digest = hashlib.sha256(output).hexdigest()
    if output_path is None:
        output_path = store.save_output(initiative_id, evidence_id, output)
    else:
        if store.read_output(initiative_id, evidence_id) != output:
            raise VerificationError("retained command output differs from evidence bytes")
    summary = json.dumps(
        {
            **copy.deepcopy(dict(detail)),
            "output_digest": output_digest,
            "output_path": str(output_path),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    evidence = validate_evidence({
        "contract": EVIDENCE_CONTRACT,
        "evidence_id": evidence_id,
        "initiative_id": initiative_id,
        "kind": kind,
        "subject_id": verification_id,
        "digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "summary": summary,
        "recorded_at": _now(),
    })
    store.save_evidence(initiative_id, evidence)
    return evidence, str(output_path), output_digest


def prepare_verification_intent(
    store: InitiativeStore,
    initiative_id: str,
    node_id: str,
    *,
    jj: JjAdapter | None = None,
    action_id: str | None = None,
    verification_id: str | None = None,
) -> dict[str, Any]:
    """Journal the exact verification/materialization intent under the lock."""
    adapter = jj or JjAdapter()
    initiative, plan, node, gate, bubblewrap, seal, review = prevalidate_verification(
        store, initiative_id, node_id, exclude_action_id=action_id,
    )
    prior = [
        item for item in store.list_verifications_snapshot(initiative_id)
        if item["node_id"] == node_id
        and item["active_plan_digest"] == plan["digest"]
        and item["state"] != "stale"
    ]
    if any(item["state"] != "indeterminate" for item in prior):
        raise VerificationError("verify node already has a retained current execution")
    for item in prior:
        stale = copy.deepcopy(item)
        stale.update({"state": "stale", "outcome": None, "updated_at": _now()})
        store.save_verification(
            initiative_id, stale, expected_digest=record_digest(item),
        )
    del review, bubblewrap
    verification_id = verification_id or new_uuid()
    seals = terminal_seals(store, initiative_id, plan, initiative)
    planned_members: list[dict[str, Any]] = []
    for index, member_seal in enumerate(seals):
        name = _member_materialization_name(initiative_id, verification_id, index)
        source = _member_root(initiative, member_seal)
        try:
            target = plan_materialization(
                store.config.control, source, name, jj=adapter,
            )
        except (PreparationError, JjError, OSError, ValueError) as exc:
            raise VerificationError(f"materialization intent failed without mutation: {exc}") from exc
        planned_members.append({
            "index": index,
            "verification_id": verification_id,
            "repository_id": member_seal["repository_id"],
            "seal_id": member_seal["seal_id"],
            "jj_commit_id": member_seal["jj_commit_id"],
            "tree_digest": member_seal["tree_digest"],
            "materialization_id": new_uuid(),
            "materialization_name": name,
            "materialization_path": str(Path(target["workspace_path"])),
            "source_root": str(source),
        })
    materialization_id = planned_members[0]["materialization_id"]
    materialization_path = Path(planned_members[0]["materialization_path"])
    bundle_digest = candidate_bundle_digest(initiative, plan, seals, gate)
    at = _now()
    record = validate_verification({
        "contract": VERIFICATION_CONTRACT,
        "verification_id": verification_id,
        "initiative_id": initiative_id,
        "node_id": node_id,
        "bundle_digest": bundle_digest,
        "active_plan_digest": plan["digest"],
        "repository_id": seal["repository_id"],
        "seal_id": seal["seal_id"],
        "materialization_id": materialization_id,
        "materialization_path": str(materialization_path),
        "state": "pending",
        "commands": [],
        "evidence_ids": [],
        "outcome": None,
        "created_at": at,
        "updated_at": at,
    })
    store.save_verification(initiative_id, record)
    for member in planned_members:
        summary = json.dumps(member, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        store.save_evidence(initiative_id, {
            "contract": EVIDENCE_CONTRACT,
            "evidence_id": new_uuid(),
            "initiative_id": initiative_id,
            "kind": "verification-member",
            "subject_id": verification_id,
            "digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            "summary": summary,
            "recorded_at": _now(),
        })
    evaluating = copy.deepcopy(node)
    evaluating["state"] = "evaluating"
    validate_node(evaluating)
    store.save_node(initiative_id, evaluating, expected_digest=record_digest(node))
    dispatching = copy.deepcopy(record)
    dispatching.update({"state": "dispatching", "updated_at": _now()})
    store.save_verification(
        initiative_id, dispatching, expected_digest=record_digest(record),
    )
    append_event(
        store, initiative_id, "verification-started",
        [node_id, verification_id, seal["seal_id"]],
        {"bundle_digest": bundle_digest, "materialization_id": materialization_id},
        actor_kind="controller", actor_id="verification-runner",
    )
    return dispatching


def run_verification(
    store: InitiativeStore,
    initiative_id: str,
    node_id: str,
    *,
    jj: JjAdapter | None = None,
    environment: Mapping[str, str] | None = None,
    materializer: Callable[..., dict[str, str]] = prepare_materialization,
    action_id: str | None = None,
    verification_id: str | None = None,
) -> dict[str, Any]:
    """Run one approved verification gate without a worker, harness, or tmux."""
    adapter = jj or JjAdapter()
    if verification_id is None:
        intent = prepare_verification_intent(
            store, initiative_id, node_id, jj=adapter, action_id=action_id,
        )
    else:
        intent = store.read_verification(initiative_id, verification_id)
        if intent["node_id"] != node_id or intent["state"] != "dispatching":
            raise VerificationError("verification intent is not dispatchable for this node")
    initiative = store.peek(initiative_id)
    if initiative["state"] not in {"running", "paused"} or initiative["active_plan"] is None:
        raise VerificationError("verification intent lost its active initiative")
    plan = store.read_plan(initiative_id, initiative["active_plan"]["revision"])
    if (
        plan["digest"] != initiative["active_plan"]["digest"]
        or intent["active_plan_digest"] != plan["digest"]
    ):
        raise VerificationError("verification intent active plan is stale")
    node = store.read_node(initiative_id, node_id)
    if node["state"] != "evaluating" or node["type"] != "verify":
        raise VerificationError("verification intent node is not evaluating")
    gate = _verification_gate(plan, node_id)
    bubblewrap = _bubblewrap_program()
    verification_id = intent["verification_id"]
    members = verification_members(store, initiative_id, verification_id)
    if not members:
        raise VerificationError("verification intent has no retained member bindings")
    member_seals = [store.read_seal(initiative_id, member["seal_id"]) for member in members]
    seal = member_seals[0]
    if (
        intent["bundle_digest"] != candidate_bundle_digest(initiative, plan, member_seals, gate)
        or intent["repository_id"] != seal["repository_id"]
        or intent["seal_id"] != seal["seal_id"]
    ):
        raise VerificationError("verification intent candidate binding is stale")
    planned: list[tuple[dict[str, Any], dict[str, Any], Path, str, Path]] = []
    for member, member_seal in zip(members, member_seals):
        name = member["materialization_name"]
        source = _member_root(initiative, member_seal)
        try:
            target = plan_materialization(
                store.config.control, source, name, jj=adapter,
            )
        except (PreparationError, JjError, OSError, ValueError) as exc:
            raise VerificationError(f"materialization intent cannot be recovered: {exc}") from exc
        if Path(member["materialization_path"]) != Path(target["workspace_path"]):
            raise VerificationError("verification materialization path differs from its intent")
        planned.append((member, member_seal, source, target["workspace_name"], Path(target["workspace_path"])))
    _member0, _seal0, source, planned_workspace_name, planned_path = planned[0]
    if Path(intent["materialization_path"]) != planned_path:
        raise VerificationError("verification materialization path differs from its intent")
    materialization_path = planned_path
    bundle_digest = intent["bundle_digest"]
    materialization_id = intent["materialization_id"]
    running = copy.deepcopy(intent)
    running.update({"state": "running", "updated_at": _now()})
    store.save_verification(
        initiative_id, running, expected_digest=record_digest(intent),
    )

    try:
        materialized: list[dict[str, Any]] = []
        for member, member_seal, member_source, member_workspace_name, member_path in planned:
            item = materializer(
                store.config.control, member_source, member_seal["jj_commit_id"],
                member["materialization_name"], jj=adapter,
            )
            if (
                item["workspace_name"] != member_workspace_name
                or Path(item["workspace_path"]) != member_path
            ):
                raise VerificationError(
                    "materializer returned a path or workspace name outside its durable intent"
                )
            materialized.append(item)
        materialization = materialized[0]
    except (PreparationError, JjError, OSError, ValueError) as exc:
        current = store.read_verification(initiative_id, verification_id)
        failed_record = copy.deepcopy(current)
        failed_record.update({
            "state": "failed", "outcome": "failed", "updated_at": _now(),
        })
        store.save_verification(
            initiative_id, failed_record, expected_digest=record_digest(current),
        )
        current_node = store.read_node(initiative_id, node_id)
        failed_node = copy.deepcopy(current_node)
        failed_node["state"] = "failed"
        validate_node(failed_node)
        store.save_node(
            initiative_id, failed_node,
            expected_digest=record_digest(current_node),
        )
        append_event(
            store, initiative_id, "verification-finished",
            [node_id, verification_id, seal["seal_id"]],
            {
                "outcome": "failed", "bundle_digest": bundle_digest,
                "command_count": 0, "reason": f"materialization failed: {exc}"[:1000],
            },
            actor_kind="controller", actor_id="verification-runner",
        )
        return failed_record

    command_records: list[dict[str, Any]] = []
    evidence_ids: list[str] = []
    failed = False
    command_environment = environment or os.environ
    member_runs = [
        (member, member_seal, member_source, materialized_item, member_path)
        for (member, member_seal, member_source, _name, member_path), materialized_item
        in zip(planned, materialized)
    ]
    for (member, seal, source, materialization, materialization_path), specification in (
        (run, spec) for run in member_runs for spec in gate["commands"]
    ):
        command_id = new_uuid()
        evidence_id = new_uuid()
        process_identity = f"not-executed:{command_id}"
        started_at = _now()
        denial = command_denial(specification["argv"])
        output = b""
        exit_code: int | None = None
        signal: int | None = None
        timed_out = False
        output_truncated = False
        output_original_bytes = 0
        retained_output_path: Path | None = None
        mutation = False
        command_failed = False
        failure_kind: str | None = None
        failure_summary: str | None = None
        identity_error: str | None = None
        pre_identity_status = "observed"
        pre_change_id: str | None = None
        pre_commit_id: str | None = None
        pre_tree_digest: str | None = None
        pre_parent_commit_ids: tuple[str, ...] | None = None
        try:
            pre_identity = adapter.inspect_workspace(
                materialization_path, materialization["workspace_name"],
                require_empty=False,
            )
            pre_tree = adapter.immutable_tree(materialization_path, pre_identity.commit_id)
            tracked_unchanged, _pre_non_tracked, _pre_truncated = tracked_workspace_status(
                adapter, materialization_path, source, seal["jj_commit_id"],
            )
            if (
                pre_identity.change_id != materialization["change_id"]
                or pre_identity.commit_id != materialization["working_commit_id"]
                or pre_identity.parent_commit_ids != (seal["jj_commit_id"],)
                or pre_tree.digest != seal["tree_digest"]
                or not tracked_unchanged
            ):
                raise VerificationError("materialization pre-run identity differs from the target seal")
            pre_change_id = pre_identity.change_id
            pre_commit_id = pre_identity.commit_id
            pre_tree_digest = pre_tree.digest
            pre_parent_commit_ids = pre_identity.parent_commit_ids
        except (PreparationError, JjError, OSError, ValueError) as exc:
            pre_identity_status = "indeterminate"
            identity_error = f"materialization pre-run identity failed: {exc}"
            failed = True
            command_failed = True
            failure_kind = "materialization"
            failure_summary = identity_error
            output = identity_error.encode("utf-8", errors="replace")
        pre_digest = (
            _identity_digest(
                materialization["workspace_name"], pre_change_id,
                pre_commit_id, pre_tree_digest,
            )
            if pre_identity_status == "observed"
            and pre_change_id is not None
            and pre_commit_id is not None
            and pre_tree_digest is not None
            else None
        )
        if identity_error is not None:
            pass
        elif denial is not None:
            output = f"refused before execution: {denial}".encode("utf-8")
            failed = True
            command_failed = True
            failure_kind = "policy"
            failure_summary = "verification policy refused the declared command"
        else:
            try:
                cwd = _command_cwd(materialization_path, specification["cwd"])
                process_identity = f"indeterminate:{command_id}"
                retained_output_path = store.reserve_output(
                    initiative_id, evidence_id,
                )
                returncode, status = _capture_truncated(
                    _contained_argv(
                        bubblewrap, command_environment, list(specification["argv"]),
                        writable_root=materialization_path,
                        output_path=retained_output_path,
                        timeout_seconds=specification["timeout_seconds"],
                    ),
                    cwd=cwd,
                    deadline_seconds=specification["timeout_seconds"],
                )
                output = store.read_output(initiative_id, evidence_id)
                if (
                    not isinstance(status["output_truncated"], bool)
                    or isinstance(status["output_original_bytes"], bool)
                    or not isinstance(status["output_original_bytes"], int)
                    or status["output_original_bytes"] < len(output)
                    or not isinstance(status["timed_out"], bool)
                    or not isinstance(status["output_digest"], str)
                    or status["output_digest"] != hashlib.sha256(output).hexdigest()
                ):
                    raise VerificationError("verification output status is invalid")
                output_truncated = status["output_truncated"]
                output_original_bytes = status["output_original_bytes"]
                timed_out = status["timed_out"]
                if returncode != 0 or status["invocation_error"] is not None:
                    failure_kind = "invocation/environment"
                    failure_summary = (
                        "invocation/environment failure while reproducing the "
                        "declared verification command"
                    )
                    raise VerificationError(
                        "verification process containment or invocation failed: "
                        f"{status['invocation_error'] or returncode}"
                    )
                child_returncode = status["returncode"]
                if (
                    not isinstance(status["pid"], int)
                    or not isinstance(status["start_ticks"], int)
                    or not isinstance(status["pid_namespace"], str)
                    or isinstance(child_returncode, bool)
                    or not isinstance(child_returncode, int)
                ):
                    raise VerificationError("verification process identity is invalid")
                process_identity = (
                    f"pidns:{status['pid_namespace']}:pid:{status['pid']}:"
                    f"start:{status['start_ticks']}"
                )
                if child_returncode < 0:
                    signal = -child_returncode
                else:
                    exit_code = child_returncode
                if child_returncode != 0 or timed_out:
                    failed = True
                    command_failed = True
                    failure_kind = _rerun_failure_kind(
                        containment_returncode=returncode,
                        child_returncode=child_returncode,
                        invocation_error=status["invocation_error"],
                        timed_out=timed_out,
                        output=output,
                    )
                    failure_summary = (
                        f"{failure_kind} failure while reproducing the declared "
                        "verification command"
                    )
            except (VerificationError, StoreError) as exc:
                if retained_output_path is not None:
                    try:
                        output = store.read_output(initiative_id, evidence_id)
                    except StoreError:
                        output = b""
                diagnostic = str(exc).encode("utf-8", errors="replace")
                output = output + b"\n" + diagnostic if output else diagnostic
                timed_out = timed_out or "timed out" in str(exc)
                failed = True
                command_failed = True
                if failure_kind is None:
                    failure_kind = "invocation/environment"
                    failure_summary = (
                        "invocation/environment failure while reproducing the "
                        "declared verification command"
                    )
        post_identity_status = "observed"
        post_change_id: str | None = None
        post_commit_id: str | None = None
        post_tree_digest: str | None = None
        post_parent_commit_ids: tuple[str, ...] | None = None
        non_tracked_paths: list[str] = []
        non_tracked_paths_truncated = False
        tracked_unchanged = False
        try:
            post_identity = adapter.inspect_workspace(
                materialization_path, materialization["workspace_name"],
                require_empty=False,
            )
            post_tree = adapter.immutable_tree(materialization_path, post_identity.commit_id)
            (
                tracked_unchanged,
                non_tracked_paths,
                non_tracked_paths_truncated,
            ) = tracked_workspace_status(
                adapter, materialization_path, source, seal["jj_commit_id"],
            )
            post_change_id = post_identity.change_id
            post_commit_id = post_identity.commit_id
            post_tree_digest = post_tree.digest
            post_parent_commit_ids = post_identity.parent_commit_ids
        except (PreparationError, JjError, OSError, ValueError) as exc:
            post_identity_status = "indeterminate"
            mutation = True
            failed = True
            command_failed = True
            failure_kind = "materialization"
            failure_summary = "post-run materialization identity failure"
            output += (
                "\npost-run materialization identity failed: " + str(exc)
            ).encode("utf-8", errors="replace")
        post_digest = (
            _identity_digest(
                materialization["workspace_name"], post_change_id,
                post_commit_id, post_tree_digest,
            )
            if post_identity_status == "observed"
            and post_change_id is not None
            and post_commit_id is not None
            and post_tree_digest is not None
            else None
        )
        if (
            pre_identity_status != "observed"
            or post_identity_status != "observed"
            or post_digest != pre_digest
            or post_tree_digest != seal["tree_digest"]
            or post_parent_commit_ids != (seal["jj_commit_id"],)
            or not tracked_unchanged
        ):
            mutation = True
            failed = True
            command_failed = True
            if failure_kind is None:
                failure_kind = "materialization"
                failure_summary = "verification materialization identity changed"
        if len(output) > MAX_VERIFICATION_OUTPUT_BYTES:
            original = max(output_original_bytes, len(output))
            marker = (
                f"\n[truncated by Asha verification; original bytes={original}]\n"
            ).encode("ascii")
            output = output[:MAX_VERIFICATION_OUTPUT_BYTES - len(marker)] + marker
            output_truncated = True
            output_original_bytes = original
        output_original_bytes = max(output_original_bytes, len(output))
        if retained_output_path is not None:
            store.finalize_reserved_output(
                initiative_id, evidence_id, output,
            )
        finished_at = _now()
        detail = {
            "verification_id": verification_id,
            "bundle_digest": bundle_digest,
            "repository_id": seal["repository_id"],
            "seal_id": seal["seal_id"],
            "argv": specification["argv"],
            "cwd": specification["cwd"],
            "environment_policy_id": gate["environment_policy"],
            "process_identity": process_identity,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "signal": signal,
            "timed_out": timed_out,
            "denied": denial is not None,
            "mutation": mutation,
            "non_tracked_paths": non_tracked_paths,
            "non_tracked_paths_truncated": non_tracked_paths_truncated,
            "output_truncated": output_truncated,
            "output_original_bytes": output_original_bytes or len(output),
            "pre_identity_status": pre_identity_status,
            "post_identity_status": post_identity_status,
            "pre_jj_commit_id": pre_commit_id,
            "pre_tree_digest": pre_tree_digest,
            "post_jj_commit_id": post_commit_id,
            "post_tree_digest": post_tree_digest,
        }
        if command_failed:
            detail.update({
                "failure_kind": failure_kind or "command",
                "failure_summary": failure_summary or (
                    "command failure while reproducing the declared verification command"
                ),
                "output_tail": _printable_output_tail(output),
            })
        _evidence, output_path, output_digest = _save_command_evidence(
            store, initiative_id, evidence_id, verification_id, detail, output,
            output_path=retained_output_path,
        )
        evidence_ids.append(evidence_id)
        command_records.append({
            "command_id": command_id,
            "argv": list(specification["argv"]),
            "cwd": specification["cwd"],
            "environment_policy_id": gate["environment_policy"],
            "timeout_seconds": specification["timeout_seconds"],
            "process_identity": process_identity,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "signal": signal,
            "timed_out": timed_out,
            "output_path": output_path,
            "output_digest": output_digest,
            "output_truncated": output_truncated,
            "output_original_bytes": output_original_bytes or len(output),
            "pre_identity_status": pre_identity_status,
            "post_identity_status": post_identity_status,
            "pre_identity_digest": pre_digest,
            "post_identity_digest": post_digest,
            "pre_jj_commit_id": pre_commit_id,
            "pre_tree_digest": pre_tree_digest,
            "post_jj_commit_id": post_commit_id,
            "post_tree_digest": post_tree_digest,
        })
        if failed:
            break

    current = store.read_verification(initiative_id, verification_id)
    terminal = copy.deepcopy(current)
    terminal.update({
        "state": "failed" if failed else "passed",
        "commands": command_records,
        "evidence_ids": evidence_ids,
        "outcome": "failed" if failed else "passed",
        "updated_at": _now(),
    })
    store.save_verification(
        initiative_id, terminal, expected_digest=record_digest(current),
    )
    current_node = store.read_node(initiative_id, node_id)
    changed_node = copy.deepcopy(current_node)
    changed_node["state"] = "failed" if failed else "succeeded"
    validate_node(changed_node)
    store.save_node(
        initiative_id, changed_node, expected_digest=record_digest(current_node),
    )
    append_event(
        store, initiative_id, "verification-finished",
        [node_id, verification_id, seal["seal_id"]],
        {
            "outcome": terminal["outcome"],
            "bundle_digest": bundle_digest,
            "command_count": len(command_records),
        },
        actor_kind="controller", actor_id="verification-runner",
    )
    if not failed and store.peek(initiative_id)["state"] == "running":
        from .readiness import bind_readiness

        bind_readiness(store, initiative_id)
    return terminal


def _composed_materialization_name(initiative_id: str, bundle_id: str, index: int) -> str:
    base = f"compose-{initiative_id}-{bundle_id[:8]}"
    return base if index == 0 else f"{base}-{index}"


def composed_roster(
    bundle: Mapping[str, Any], observed: Mapping[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """Every bundle member with the composed tree digest actually observed."""
    return [
        {
            "repository_id": member["repository_id"],
            "seal_id": member["seal_id"],
            "jj_commit_id": member["jj_commit_id"],
            "tree_digest": member["tree_digest"],
            "composed_tree_digest": (observed or {}).get(member["seal_id"]),
        }
        for member in bundle["members"]
    ]


def composed_verdict_evidence(
    initiative_id: str,
    bundle: Mapping[str, Any],
    *,
    outcome: str,
    detail: Mapping[str, Any],
    observed: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """Build the immutable composed verdict bound to one exact composition."""
    if outcome not in _COMPOSED_OUTCOMES:
        raise VerificationError("composed verification outcome is invalid")
    # Descriptive detail may never rebind the identity or the verdict it
    # describes; the gate reads exactly these three fields.
    if {"bundle_id", "composition_digest", "outcome", "members"} & set(detail):
        raise VerificationError("composed verdict detail may not rebind its identity")
    body: dict[str, Any] = {
        "bundle_id": bundle["bundle_id"],
        "composition_digest": bundle_composition_digest(bundle),
        "outcome": outcome,
        "members": composed_roster(bundle, observed),
        "members_elided": False,
        **copy.deepcopy(dict(detail)),
    }

    def encoded() -> str:
        return json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    summary = encoded()
    if len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES:
        # The composition digest already binds every member identity, so a wide
        # bundle drops the readable roster rather than the verdict.
        body["members"] = []
        body["members_elided"] = True
        summary = encoded()
    return validate_evidence({
        "contract": EVIDENCE_CONTRACT,
        "evidence_id": new_uuid(),
        "initiative_id": initiative_id,
        "kind": COMPOSED_VERIFICATION_KIND,
        "subject_id": bundle["bundle_id"],
        "digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "summary": summary,
        "recorded_at": _now(),
    })


def composed_verification_verdict(
    store: InitiativeStore, initiative_id: str, bundle: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """The strongest retained composed verdict for this exact composition.

    A recorded failure outranks a later pass: the composition is immutable, so
    disagreement means the gate did not reproduce and is not permission.
    """
    expected = bundle_composition_digest(bundle)
    found: dict[str, dict[str, Any]] = {}
    for evidence in store.list_evidence_snapshot(initiative_id):
        if (
            evidence["kind"] != COMPOSED_VERIFICATION_KIND
            or evidence["subject_id"] != bundle["bundle_id"]
        ):
            continue
        try:
            summary = json.loads(evidence["summary"])
        except (TypeError, ValueError) as exc:
            raise VerificationError("composed verification evidence is unreadable") from exc
        if (
            not isinstance(summary, dict)
            or summary.get("bundle_id") != bundle["bundle_id"]
            or summary.get("composition_digest") != expected
        ):
            continue
        if summary.get("outcome") in _COMPOSED_OUTCOMES:
            found.setdefault(summary["outcome"], summary)
    for outcome in _COMPOSED_OUTCOMES:
        if outcome in found:
            return outcome, found[outcome]
    return "absent", None


class _ComposedIdentityError(VerificationError):
    """A composed materialization is not the exact member seal tree."""


def _composed_identity(
    adapter: JjAdapter,
    materialization: Mapping[str, str],
    materialization_path: Path,
    source: Path,
    seal: Mapping[str, Any],
) -> tuple[str, str]:
    """The composed workspace's exact commit and tree, or an identity failure."""
    identity = adapter.inspect_workspace(
        materialization_path, materialization["workspace_name"], require_empty=False,
    )
    tree = adapter.immutable_tree(materialization_path, identity.commit_id)
    tracked_unchanged, _paths, _truncated = tracked_workspace_status(
        adapter, materialization_path, source, seal["jj_commit_id"],
    )
    if (
        identity.parent_commit_ids != (seal["jj_commit_id"],)
        or tree.digest != seal["tree_digest"]
        or not tracked_unchanged
    ):
        raise _ComposedIdentityError(
            "composed materialization identity differs from its member seal"
        )
    return identity.commit_id, tree.digest


def _composed_command(
    store: InitiativeStore,
    initiative_id: str,
    bundle_id: str,
    adapter: JjAdapter,
    bubblewrap: Path,
    environment: Mapping[str, str],
    *,
    seal: Mapping[str, Any],
    materialization: Mapping[str, str],
    materialization_path: Path,
    source: Path,
    specification: Mapping[str, Any],
    gate: Mapping[str, Any],
) -> tuple[dict[str, Any], str, str | None]:
    """Run one declared command against the composed tree.

    The returned failure kind is `command` only for a real declared-command
    verdict; every other kind is an environment-class outcome that defers under
    the standing invocation/environment ruling rather than condemning the
    composition.
    """
    evidence_id = new_uuid()
    started_at = _now()
    exit_code: int | None = None
    signal: int | None = None
    timed_out = False
    output = b""
    output_path: Path | None = None
    failure_kind: str | None = None
    pre_tree_digest: str | None = None
    post_tree_digest: str | None = None
    denial = command_denial(specification["argv"])
    try:
        _pre_commit, pre_tree_digest = _composed_identity(
            adapter, materialization, materialization_path, source, seal,
        )
        if denial is not None:
            failure_kind = "policy"
            output = f"refused before execution: {denial}".encode("utf-8")
        else:
            output_path = store.reserve_output(initiative_id, evidence_id)
            returncode, status = _capture_truncated(
                _contained_argv(
                    bubblewrap, environment, list(specification["argv"]),
                    writable_root=materialization_path, output_path=output_path,
                    timeout_seconds=specification["timeout_seconds"],
                ),
                cwd=_command_cwd(materialization_path, specification["cwd"]),
                deadline_seconds=specification["timeout_seconds"],
            )
            output = store.read_output(initiative_id, evidence_id)
            child_returncode = status["returncode"]
            timed_out = status["timed_out"] is True
            if (
                returncode != 0
                or status["invocation_error"] is not None
                or not isinstance(status["timed_out"], bool)
                or isinstance(child_returncode, bool)
                or not isinstance(child_returncode, int)
                or status["output_digest"] != hashlib.sha256(output).hexdigest()
            ):
                failure_kind = "invocation/environment"
            elif child_returncode < 0:
                signal = -child_returncode
                failure_kind = "command"
            else:
                exit_code = child_returncode
                if child_returncode != 0 or timed_out:
                    failure_kind = _rerun_failure_kind(
                        containment_returncode=returncode,
                        child_returncode=child_returncode,
                        invocation_error=status["invocation_error"],
                        timed_out=timed_out, output=output,
                    )
            _post_commit, post_tree_digest = _composed_identity(
                adapter, materialization, materialization_path, source, seal,
            )
    except (PreparationError, JjError, StoreError, VerificationError, OSError, ValueError) as exc:
        diagnostic = str(exc).encode("utf-8", errors="replace")
        output = output + b"\n" + diagnostic if output else diagnostic
        # An untrustworthy run never condemns the composition, so even a
        # command verdict degrades once the tree or the runner misbehaved.
        if failure_kind is None or failure_kind == "command":
            failure_kind = (
                "materialization" if isinstance(exc, _ComposedIdentityError)
                else "invocation/environment"
            )
    output_truncated = len(output) > MAX_VERIFICATION_OUTPUT_BYTES
    if output_truncated:
        output = output[:MAX_VERIFICATION_OUTPUT_BYTES]
    if output_path is not None:
        store.finalize_reserved_output(initiative_id, evidence_id, output)
    detail: dict[str, Any] = {
        "bundle_id": bundle_id,
        "repository_id": seal["repository_id"],
        "seal_id": seal["seal_id"],
        "jj_commit_id": seal["jj_commit_id"],
        "argv": list(specification["argv"]),
        "cwd": specification["cwd"],
        "environment_policy_id": gate["environment_policy"],
        "started_at": started_at,
        "finished_at": _now(),
        "exit_code": exit_code,
        "signal": signal,
        "timed_out": timed_out,
        "denied": denial is not None,
        "output_truncated": output_truncated,
        "pre_tree_digest": pre_tree_digest,
        "post_tree_digest": post_tree_digest,
        "failure_kind": failure_kind,
        "failure_summary": (
            None if failure_kind is None
            else f"{failure_kind} failure while reproducing the declared command "
                 "against the composed tree"
        ),
        "output_tail": _printable_output_tail(output, limit=1024),
    }
    _evidence, _path, _digest = _save_command_evidence(
        store, initiative_id, evidence_id, bundle_id, detail, output,
        output_path=output_path, kind=COMPOSED_COMMAND_KIND,
    )
    return detail, evidence_id, failure_kind


def run_composed_verification(
    store: InitiativeStore,
    initiative_id: str,
    bundle_id: str,
    *,
    jj: JjAdapter | None = None,
    environment: Mapping[str, str] | None = None,
    materializer: Callable[..., dict[str, str]] = prepare_materialization,
) -> dict[str, Any]:
    """Run the approved gate against a compatible bundle's composed members.

    Opt-in by construction: nothing schedules this, and no integration path
    reaches it unless an operator demands the gate.  It appends evidence and
    retained materializations only -- it never advances a lifecycle, mutates a
    bundle, seal or verification record, or removes retained state.

    `failed` is reached only by a real declared-command verdict.  Every other
    non-passing outcome is `indeterminate`: the gate produced no trustworthy
    verdict (invocation/environment, materialization identity, or a policy
    refusal), so it defers to a rerun rather than condemning the composition.
    """
    adapter = jj or JjAdapter()
    initiative = store.peek(initiative_id)
    if (
        initiative["state"] not in {"running", "ready-for-integration"}
        or initiative["active_plan"] is None
    ):
        raise VerificationError("composed verification requires an approved active plan")
    plan = store.read_plan(initiative_id, initiative["active_plan"]["revision"])
    if plan["digest"] != initiative["active_plan"]["digest"]:
        raise VerificationError("active plan digest differs from its retained revision")
    bundle = store.read_bundle(initiative_id, bundle_id)
    if bundle["active_plan_digest"] != plan["digest"]:
        raise VerificationError("bundle was bound under a different active plan")
    members = bundle_composition_inputs(store, initiative_id, bundle)
    gate = _verification_gate(plan, store.read_verification(
        initiative_id, bundle["members"][0]["verification_id"],
    )["node_id"])
    bubblewrap = _bubblewrap_program()
    command_environment = environment or os.environ

    def verdict(
        outcome: str, failure_kind: str | None, failure_summary: str | None,
        evidence_ids: list[str], observed: Mapping[str, str | None],
    ) -> dict[str, Any]:
        evidence = composed_verdict_evidence(
            initiative_id, bundle, outcome=outcome, observed=observed,
            detail={
                "node_id": gate["node_id"],
                "active_plan_digest": plan["digest"],
                "aggregate_spec_digest": specification_digest(initiative, plan),
                "failure_kind": failure_kind,
                "failure_summary": failure_summary,
                "command_evidence_ids": evidence_ids,
            },
        )
        store.save_evidence(initiative_id, evidence)
        return {
            "bundle_id": bundle_id,
            "composition_digest": bundle_composition_digest(bundle),
            "outcome": outcome,
            "failure_kind": failure_kind,
            "failure_summary": failure_summary,
            "evidence_id": evidence["evidence_id"],
            "command_evidence_ids": evidence_ids,
            "members": composed_roster(bundle, observed),
        }

    composed: list[tuple[dict[str, Any], dict[str, str], Path, Path]] = []
    for index, seal in enumerate(members):
        source = _member_root(initiative, seal)
        try:
            item = materializer(
                store.config.control, source, seal["jj_commit_id"],
                _composed_materialization_name(initiative_id, bundle_id, index),
                jj=adapter,
            )
        except (PreparationError, JjError, OSError, ValueError) as exc:
            return verdict(
                "indeterminate", "materialization",
                f"composed materialization failed: {exc}"[:1000], [], {},
            )
        composed.append((seal, item, source, Path(item["workspace_path"])))

    evidence_ids: list[str] = []
    observed: dict[str, str | None] = {}
    failure_kind: str | None = None
    failure_summary: str | None = None
    for seal, item, source, path in composed:
        for specification in gate["commands"]:
            detail, evidence_id, kind = _composed_command(
                store, initiative_id, bundle_id, adapter, bubblewrap,
                command_environment, seal=seal, materialization=item,
                materialization_path=path, source=source,
                specification=specification, gate=gate,
            )
            evidence_ids.append(evidence_id)
            observed[seal["seal_id"]] = (
                detail["post_tree_digest"] or detail["pre_tree_digest"]
            )
            if kind is not None:
                failure_kind, failure_summary = kind, detail["failure_summary"]
                break
        if failure_kind is not None:
            break
    outcome = (
        "passed" if failure_kind is None
        else "failed" if failure_kind == "command"
        else "indeterminate"
    )
    return verdict(outcome, failure_kind, failure_summary, evidence_ids, observed)


__all__ = [
    "COMPOSED_COMMAND_KIND", "COMPOSED_VERIFICATION_KIND",
    "MAX_VERIFICATION_OUTPUT_BYTES", "VerificationError",
    "candidate_bundle_digest", "command_denial", "composed_roster",
    "composed_verdict_evidence", "composed_verification_verdict",
    "run_composed_verification", "run_verification",
]
