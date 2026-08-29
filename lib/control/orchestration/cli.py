"""Deterministic Orchestration Core operator command surface."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..context import read_published_snapshot
from ..jj import DEFAULT_BASE_REVSET, JjAdapter, JjError, colocated_sync_remediation
from ..prepare import derive_repository_identity
from ..reconcile import LiveAdapters
from ..store import StoreError, TaskStore
from .actions import (
    ActionError, ActionRefused, approve_salvage, build_action_document,
    reconcile_actions, submit_action,
)
from ..tmux import TmuxAdapter, TmuxError
from .config import OrchestrationConfigError, load_config
from .coordinator import (
    CoordinatorError, checkpoint as checkpoint_coordinator, claim as claim_coordinator,
    environment_for, reconcile_coordinator, refuse_coordinator_pane,
    release as release_coordinator, require_anchored_caller, require_live_coordinator,
    show as show_coordinator, wait as wait_for_events,
)
from .doctor import run_orchestration_doctor
from .graph import PlanError, validate_plan
from .integration import record_integration
from .workspace_scope import ScopeError, repository_scope, workspace_scope
from .model import (
    APPROVAL_CONTRACT, EVENT_CONTRACT, FORBIDDEN_ACTION_CLASSES,
    INITIATIVE_CONTRACT, INITIATIVE_CONTRACT_V2, MAX_CRITERION_BYTES, MUTATING_NODE_TYPES,
    NODE_NONTERMINAL_STATES,
    ModelError, new_uuid, record_digest,
    validate_approval, validate_event, validate_initiative, validate_node,
    validate_plan_record, validate_slug,
)
from .reconcile import reconcile_live, reconcile_nodes
from .results import (
    ResultError, ResultRefused, publish_result, read_client_file,
    results_for_task,
)
from .ingestion import IngestionRefused, ingest_result, stage_result
from .scheduler import SchedulerError, validate_goal_capacity
from .seals import SealError, seal_for_task_or_attempt
from .storage import storage_report
from .store import MAX_RECORD_BYTES, InitiativeStore


CREATE_CONTRACT = "asha.orchestration-initiative-create.v1"
BASELINE_CONTRACT = "asha.orchestration-baseline.v1"
APPROVAL_RESULT_CONTRACT = "asha.orchestration-plan-approval.v1"
REJECTION_RESULT_CONTRACT = "asha.orchestration-plan-rejection.v1"
LIST_CONTRACT = "asha.orchestration-initiative-list.v1"
SHOW_CONTRACT = "asha.orchestration-initiative-show.v1"
EVENT_LIST_CONTRACT = "asha.orchestration-event-list.v1"
RECONCILE_LIST_CONTRACT = "asha.orchestration-reconcile-list.v1"
SNAPSHOT_CONTRACT = "asha.orchestration-snapshot.v1"
SALVAGE_APPROVAL_RESULT_CONTRACT = "asha.orchestration-salvage-approval.v1"
COORDINATOR_CLAIM_CONTRACT = "asha.orchestration-coordinator-claim.v1"
COORDINATOR_RELEASE_CONTRACT = "asha.orchestration-coordinator-release.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _usage(stream=sys.stdout) -> None:
    print("""asha initiative: bounded Orchestration Core records

Usage:
  asha initiative baseline --repo PATH [--revision REVSET] [--json]
  asha initiative create (--repo PATH | --workspace PATH) --slug SLUG --label TEXT --objective TEXT [--acceptance TEXT]...
  asha initiative plan <id> --file PLAN.json
  asha initiative plan <id> --show [--revision N] [--json]
  asha initiative approve <id> --digest SHA256 [--json]
  asha initiative approve-salvage <id> --request REQUEST_ID [--json]
  asha initiative reject <id> --digest SHA256 --reason TEXT [--json]
  asha initiative activate <id> [--json]
  asha initiative action <id> --file ACTION.json --json
  asha initiative dispatch <id> --node NODE [--salvage-request REQUEST_ID] [--json]
  asha initiative pause|resume <id> [--json]
  asha initiative stop <id> --attempt ATTEMPT [--json]
  asha initiative cancel <id> --node NODE [--json]
  asha initiative finalize <id> --outcome partial|failed --reason TEXT [--json]
  asha initiative archive <id> [--json]
  asha initiative unarchive <id> [--json]
  asha initiative compose-verify <id> --bundle BUNDLE_ID [--json]
  asha initiative record-integration <id> --bundle BUNDLE_ID [--composed-verification] [--json]
  asha initiative record-integration <id> --seal SEAL_ID --abandoned --reason TEXT [--json]
  asha initiative list [--all] [--json]
  asha initiative show|events|reconcile|storage|snapshot <id> [options]
  asha initiative doctor [--json]
  asha initiative projects [--root DIR] [--depth N] [--match TEXT] [--json]
  asha initiative attention [--json]
  asha initiative authority add NAME --repo DIR --scope PREFIX... [--max-nodes N]
                                 [--harness H,...] [--max-attempts N] [--require-headless]
                                 [--no-auto-activate] [--json]
  asha initiative authority list [--all] [--json]
  asha initiative authority revoke AUTHORITY_ID [--json]
  asha initiative coordinator claim <id> [--harness H] [--json]   (from the Asha pane)
  asha initiative coordinator launch [--root DIR] --intent TEXT [--harness H] [--json]
  asha initiative coordinator sessions [--json]
  asha initiative coordinator attach ID | --session NAME [--json]
  asha initiative coordinator release|show <id> [--json]
  asha initiative propose-plan <id> --file PLAN.json [--json]     (coordinator actor)
  asha initiative wait <id> --after SEQUENCE [--timeout SECONDS] --json
                               (default: coordinator_wait_seconds; maximum: 3600 seconds)
  asha initiative checkpoint <id> --file CHECKPOINT.json [--json] (coordinator actor)
  asha initiative dispatch|pause|stop <id> ... --as-coordinator  (coordinator actor)""", file=stream)


def _payload(value: Any, json_output: bool) -> None:
    if json_output:
        _json(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in input file: {key}")
        result[key] = value
    return result


def _read_json_file(path: Path, label: str) -> Any:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = -1
    try:
        fd = os.open(path, flags)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} file must be a regular file")
        if metadata.st_size > MAX_RECORD_BYTES:
            raise ValueError(f"{label} file exceeds {MAX_RECORD_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"cannot read {label} file: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError(f"{label} file exceeds {MAX_RECORD_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"cannot read {label} file: {exc}") from exc


def _read_plan_file(path: Path) -> Any:
    return _read_json_file(path, "plan")


def _parse_options(args: list[str], *, repeat: set[str] = set(), flags: set[str] = set()) -> dict[str, Any]:
    values: dict[str, Any] = {name: [] for name in repeat}
    values.update({name: False for name in flags})
    seen: set[str] = set()
    index = 0
    while index < len(args):
        argument = args[index]
        if not argument.startswith("--"):
            raise ValueError(f"unexpected argument: {argument}")
        name = argument[2:].replace("-", "_")
        if name in flags:
            if name in seen:
                raise ValueError(f"{argument} may be specified only once")
            seen.add(name)
            values[name] = True
            index += 1
            continue
        if index + 1 >= len(args):
            raise ValueError(f"{argument} requires a value")
        if name not in repeat and name in seen:
            raise ValueError(f"{argument} may be specified only once")
        seen.add(name)
        if name in repeat:
            values[name].append(args[index + 1])
        else:
            values[name] = args[index + 1]
        index += 2
    return values


def _required(options: Mapping[str, Any], *names: str) -> None:
    missing = [name for name in names if options.get(name) in {None, ""}]
    if missing:
        raise ValueError("missing required option(s): " + ", ".join("--" + name.replace("_", "-") for name in missing))


def _only(options: Mapping[str, Any], allowed: set[str], command: str) -> None:
    unknown = options.keys() - allowed
    if unknown:
        name = sorted(unknown)[0].replace("_", "-")
        raise ValueError(f"{command} does not accept --{name}")


def _positive(value: Any, name: str) -> int:
    if value is None:
        raise ValueError(f"--{name.replace('_', '-')} requires a value")
    if re.fullmatch(r"[1-9][0-9]*", str(value)) is None:
        raise ValueError(f"--{name.replace('_', '-')} must be a positive integer")
    return int(value)


def _resolve(store: InitiativeStore, value: str) -> dict[str, Any]:
    try:
        return store.peek(value)
    except StoreError as identifier_error:
        matches = [item for item in store.list_initiatives() if item["slug"] == value]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise StoreError(f"initiative slug is ambiguous: {value}")
        raise identifier_error


def _event(
    initiative: dict[str, Any], event_type: str, payload: dict[str, Any], at: str,
    *, actor_kind: str = "operator", actor_id: str = "cli",
) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return validate_event({
        "contract": EVENT_CONTRACT,
        "sequence": initiative["last_event_sequence"] + 1,
        "event_id": new_uuid(),
        "initiative_id": initiative["initiative_id"],
        "type": event_type,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "subject_ids": [],
        "payload_digest": hashlib.sha256(raw).hexdigest(),
        "payload": payload,
        "recorded_at": at,
    })


_repository_scope = repository_scope


def _guard_colocated_sync(jj: JjAdapter, root: Path, git_root: Path) -> None:
    working_copy_parent = jj.working_copy_parent(root)
    git_head = jj.git_head_exact(root)
    remediation = colocated_sync_remediation(
        root, git_head, working_copy_parent,
    )
    if remediation is not None:
        raise ValueError(remediation)


def _baseline(
    args: list[str], jj: JjAdapter,
) -> tuple[dict[str, Any], bool]:
    options = _parse_options(args, flags={"json"})
    _only(options, {"repo", "revision", "json"}, "baseline")
    _required(options, "repo")
    revision_omitted = options.get("revision") is None
    revision = DEFAULT_BASE_REVSET if revision_omitted else options["revision"]
    root = Path(options["repo"]).expanduser().resolve()
    facts = jj.preflight(root)
    _guard_colocated_sync(jj, facts.root, facts.git_root)
    resolution = None
    try:
        if revision_omitted:
            resolution = jj.resolve_default_base(facts.root)
            commit_id = resolution.commit_id
            jj.require_visible_commit(facts.root, commit_id)
        else:
            commit_id = jj.resolve_base(facts.root, revision)
    except JjError as exc:
        detail = str(exc)
        if revision_omitted:
            detail = detail.replace(
                "Pass an explicit --base", "Pass an explicit --revision",
            ).replace(
                "pass an explicit --base", "pass an explicit --revision",
            )
        shown = "the default base" if revision_omitted else repr(revision)
        raise JjError(
            f"could not resolve revision {shown} from jj's read-only "
            f"repository view: {detail}; no import was attempted. If Git knows "
            f"the revision or bookmark but jj does not, run `jj status` in "
            f"{facts.root} to import it, then retry"
        ) from exc
    # Advisory only (#81). An omitted revision tracks the bookmark, which an
    # operator's landed-but-unbookmarked chain can sit above; say so loudly and
    # keep the same commit. An explicit --revision is already a deliberate
    # choice, so it is not probed.
    divergence = (
        None if resolution is None
        else jj.detect_baseline_divergence(
            facts.root, commit_id, references=resolution.references,
        )
    )
    tree = jj.immutable_tree(facts.root, commit_id)
    repository = _repository_scope(facts.root, jj)
    return {
        "contract": BASELINE_CONTRACT,
        "repository": {
            "root": repository["root"],
            "control_repository_id": repository["control_repository_id"],
        },
        "jj_commit_id": commit_id,
        "tree_digest": tree.digest,
        "entry_count": len(tree.entries),
        "baseline_divergence": None if divergence is None else divergence.record(),
    }, bool(options["json"])


def _create(args: list[str], config, store: InitiativeStore, jj: JjAdapter) -> dict[str, Any]:
    options = _parse_options(args, repeat={"acceptance"}, flags={"json"})
    allowed = {"repo", "workspace", "slug", "label", "objective", "acceptance", "max_parallel",
               "max_total_tasks", "max_attempts_per_node", "max_repair_cycles", "deadline", "json"}
    unknown = options.keys() - allowed
    if unknown:
        raise ValueError(f"unsupported create option: {next(iter(unknown))}")
    if bool(options.get("repo")) == bool(options.get("workspace")):
        raise ValueError("create requires exactly one of --repo or --workspace")
    _required(options, "slug", "label", "objective")
    validate_slug(options["slug"])
    ceilings = {
        "max_parallel": config.max_parallel_tasks,
        "max_total_tasks": config.max_total_tasks,
        "max_attempts_per_node": config.max_attempts_per_node,
        "max_repair_cycles": config.max_repair_cycles,
    }
    limits: dict[str, Any] = {}
    for name, ceiling in ceilings.items():
        value = ceiling if options.get(name) is None else _positive(options[name], name)
        if value > ceiling:
            raise ValueError(f"--{name.replace('_', '-')} exceeds the configured ceiling")
        limits[name] = value
    limits.update({
        "max_retained_bytes_before_pause": config.max_retained_bytes_before_pause,
        "max_retained_inodes_before_pause": config.max_retained_inodes_before_pause,
        "deadline": options.get("deadline"),
    })
    at = _now()
    acceptance = options["acceptance"]
    if not acceptance:
        if len(options["objective"].encode("utf-8")) > MAX_CRITERION_BYTES:
            raise ValueError(
                "--acceptance is required when --objective exceeds "
                f"{MAX_CRITERION_BYTES} UTF-8 bytes"
            )
        acceptance = [options["objective"]]
    if options.get("workspace"):
        try:
            scope = {"kind": "workspace", "workspace": workspace_scope(Path(options["workspace"]), jj)}
        except ScopeError as exc:
            raise ValueError(str(exc)) from exc
        contract = INITIATIVE_CONTRACT_V2
    else:
        scope = {"kind": "repository", "repository": repository_scope(Path(options["repo"]), jj)}
        contract = INITIATIVE_CONTRACT
    initiative = validate_initiative({
        "contract": contract, "initiative_id": new_uuid(),
        "slug": options["slug"], "label": options["label"], "state": "draft",
        "objective": options["objective"],
        "acceptance_criteria": acceptance,
        "scope": scope,
        "active_plan": None, "limits": limits, "coordinator": None,
        "state_revision": 0, "forbidden_action_classes": list(FORBIDDEN_ACTION_CLASSES),
        "last_event_sequence": 0, "created_at": at, "updated_at": at,
    })
    created_event = _event(initiative, "initiative-created", {"slug": initiative["slug"]}, at)
    store.save_initiative(initiative)
    with store.transaction_lock(initiative["initiative_id"]):
        store.append_event(initiative["initiative_id"], created_event)
    return {"contract": CREATE_CONTRACT, "initiative": store.peek(initiative["initiative_id"]), "json": options["json"]}


def _latest_plan(store: InitiativeStore, initiative_id: str) -> dict[str, Any]:
    plans = store.list_plans_snapshot(initiative_id)
    if not plans:
        raise StoreError("initiative has no proposed plan")
    plan = plans[-1]
    if plan["status"] != "proposed":
        raise StoreError("latest plan is not proposed")
    return plan


def _latest_executable_plan(
    store: InitiativeStore, initiative_id: str,
) -> dict[str, Any]:
    observed = _latest_plan(store, initiative_id)
    return store.read_plan(initiative_id, observed["revision"])


def _has_plan_event(
    events: list[dict[str, Any]], event_type: str, digest: str
) -> bool:
    return any(
        event["type"] == event_type
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("digest") == digest
        for event in events
    )


def _candidate_plan(
    raw: dict[str, Any], revision: int, initiative: dict[str, Any], config
) -> dict[str, Any]:
    expected = {
        "initiative_id": initiative["initiative_id"],
        "revision": revision,
        "status": "proposed",
    }
    for field, value in expected.items():
        supplied = raw.get(field)
        if supplied is not None and supplied != value:
            raise ValueError(
                f"plan file {field} disagrees with target: expected {value!r}"
            )
    supplied_digest = raw.get("digest")
    candidate = copy.deepcopy(raw)
    candidate.update({**expected, "digest": None})
    plan = validate_plan(candidate, config=config, initiative=initiative)
    if supplied_digest is not None and supplied_digest != plan["digest"]:
        raise ValueError("plan file digest disagrees with its canonical target plan")
    return plan


def _baseline_command(root: Path, commit_id: str) -> str:
    return shlex.join([
        "asha", "initiative", "baseline", "--repo", str(root),
        "--revision", commit_id,
    ])


def _verify_approved_baselines(
    nodes: list[dict[str, Any]], plan: dict[str, Any], jj: JjAdapter,
) -> None:
    repositories = {
        repository["repository_id"]: Path(repository["root"])
        for repository in plan["repositories"]
    }
    trees: dict[tuple[Path, str], Any] = {}
    for node in nodes:
        if (
            node["type"] not in MUTATING_NODE_TYPES
            or node["base"]["policy"] != "approved-baseline"
        ):
            continue
        origin = node["base"]["scope_origin"]
        commit_id = origin["jj_commit_id"]
        root = repositories[node["repository_id"]]
        command = _baseline_command(root, commit_id)
        key = (root, commit_id)
        tree = trees.get(key)
        if tree is None:
            try:
                jj.require_visible_commit(root, commit_id)
            except JjError as exc:
                raise ValueError(
                    f"node {node['node_id']} approved-baseline commit "
                    f"{commit_id} is not visible in initiative repository "
                    f"{root}; run `{command}`"
                ) from exc
            try:
                tree = jj.immutable_tree(root, commit_id)
            except JjError as exc:
                raise ValueError(
                    f"node {node['node_id']} approved-baseline commit "
                    f"{commit_id} could not be inspected in initiative "
                    f"repository {root}: {exc}; run `{command}`"
                ) from exc
            trees[key] = tree
        if tree.digest != origin["tree_digest"]:
            raise ValueError(
                f"node {node['node_id']} approved-baseline tree digest "
                f"disagrees with commit {commit_id}: plan declares "
                f"{origin['tree_digest']} but the repository has {tree.digest}; "
                f"run `{command}`"
            )


def _plan(
    args: list[str], store: InitiativeStore, config, *, jj: JjAdapter,
    env: Mapping[str, str] | None = None, tmux: TmuxAdapter | None = None,
) -> tuple[dict[str, Any], bool]:
    if not args:
        raise ValueError("plan requires an initiative ID or exact slug")
    env = {} if env is None else env
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"show", "json"})
    _only(options, {"file", "show", "revision", "json"}, "plan")
    if options.get("show"):
        if options.get("file") is not None:
            raise ValueError("--show and --file are mutually exclusive")
        revision = options.get("revision")
        plan = (
            _latest_plan(store, initiative["initiative_id"])
            if revision is None
            else store.read_plan_snapshot(
                initiative["initiative_id"], _positive(revision, "revision"),
            )
        )
        return plan, bool(options["json"])
    _required(options, "file")
    if options.get("revision") is not None:
        raise ValueError("--revision is valid only with --show")
    refuse_coordinator_pane(store, initiative["initiative_id"], env, tmux or TmuxAdapter())
    raw = _read_plan_file(Path(options["file"]))
    if not isinstance(raw, dict):
        raise ValueError("plan file must contain an object")
    return propose_plan(store, initiative, raw, config=config, jj=jj), bool(options["json"])


def propose_plan(
    store: InitiativeStore,
    initiative: dict[str, Any],
    raw: dict[str, Any],
    *,
    config,
    jj: JjAdapter,
    actor_kind: str = "operator",
    actor_id: str = "cli",
) -> dict[str, Any]:
    """Validate and retain one proposed plan revision; approval stays a separate operator act."""
    if initiative["state"] not in {"draft", "planning", "awaiting-plan-approval"}:
        raise ValueError(
            "initiative must be draft, planning, or awaiting plan approval "
            "to propose or retry a plan"
        )
    existing = store.list_plans_snapshot(initiative["initiative_id"])
    events = store.list_events_snapshot(initiative["initiative_id"])
    retry = False
    plan: dict[str, Any]
    if existing and not _has_plan_event(events, "plan-rejected", existing[-1]["digest"]):
        if (
            initiative["state"] == "awaiting-plan-approval"
            and raw.get("revision") is not None
            and raw["revision"] != existing[-1]["revision"]
        ):
            raise ValueError(
                "reject the pending revision first before proposing a different plan"
            )
        candidate = _candidate_plan(raw, existing[-1]["revision"], initiative, config)
        if candidate["digest"] == existing[-1]["digest"]:
            plan = existing[-1]
            retry = True
        elif initiative["state"] == "awaiting-plan-approval":
            raise ValueError(
                "reject the pending revision first before proposing a different plan"
            )
        elif not _has_plan_event(events, "plan-proposed", existing[-1]["digest"]):
            raise ValueError("plan retry must match the retained latest plan exactly")
        else:
            plan = _candidate_plan(raw, len(existing) + 1, initiative, config)
    else:
        plan = _candidate_plan(raw, len(existing) + 1, initiative, config)
    nodes = [validate_node(node) for node in plan["nodes"]]
    if any(node["state"] != "proposed" for node in nodes):
        raise ValueError("new plan nodes must be proposed")
    _verify_approved_baselines(nodes, plan, jj)
    validate_goal_capacity(config, initiative, nodes)
    retained = {
        node["node_id"]: node
        for node in store.list_nodes_snapshot(initiative["initiative_id"])
    }
    collisions = []
    for node in nodes:
        current = retained.get(node["node_id"])
        if current is not None and not (
            retry and current == node and current["state"] == "proposed"
        ):
            collisions.append(node["node_id"])
    if collisions:
        raise ValueError(
            "new plan node IDs must not reuse retained node IDs: "
            + ", ".join(sorted(collisions))
        )
    at = _now()
    with store.transaction_lock(initiative["initiative_id"]):
        current = store.peek(initiative["initiative_id"])
        if record_digest(current) != record_digest(initiative):
            raise StoreError("initiative changed; reload before proposing the plan")
        if current["state"] == "draft":
            planning = copy.deepcopy(current)
            planning.update({"state": "planning", "state_revision": current["state_revision"] + 1, "updated_at": at})
            store.save_initiative(planning, expected_digest=record_digest(current))
            current = store.peek(current["initiative_id"])
        if not retry:
            store.save_plan(current["initiative_id"], plan)
        for node in nodes:
            if node["node_id"] not in retained:
                store.save_node(current["initiative_id"], node)
        current = store.peek(current["initiative_id"])
        if current["state"] == "planning":
            awaiting = copy.deepcopy(current)
            awaiting.update({"state": "awaiting-plan-approval", "state_revision": current["state_revision"] + 1, "updated_at": at})
            store.save_initiative(awaiting, expected_digest=record_digest(current))
            current = store.peek(current["initiative_id"])
        if not _has_plan_event(
            store.list_events_snapshot(current["initiative_id"]),
            "plan-proposed", plan["digest"],
        ):
            event = _event(
                current, "plan-proposed",
                {"revision": plan["revision"], "digest": plan["digest"]}, at,
                actor_kind=actor_kind, actor_id=actor_id,
            )
            store.append_event(current["initiative_id"], event)
    stored_plan = store.read_plan(initiative["initiative_id"], plan["revision"])
    _apply_standing_authority(store, config, initiative["initiative_id"], stored_plan)
    return stored_plan


def _apply_standing_authority(
    store: InitiativeStore, config, initiative_id: str, plan: dict[str, Any],
) -> None:
    """Execute the operator's pre-signed approval when the plan matches a shape.

    Best-effort by design: an unreadable authority store, an off-shape plan, or
    a refused activation leaves the initiative where the operator can act on it
    and reports why on stderr. Proposing never fails because autonomy could not
    engage. When an authority does fire, the approval record carries its proxy
    actor and the journal carries `approval-decided`; if the event append is
    what fails, the approval still names the authority, so provenance survives.
    """
    from .authority import AuthorityError, find_matching_authority

    try:
        current = store.peek(initiative_id)
        if current["state"] != "awaiting-plan-approval":
            return
        authority, mismatches = find_matching_authority(config, current, plan)
        if authority is None:
            for reason in mismatches:
                print(
                    f"asha initiative: standing authority did not apply: {reason}",
                    file=sys.stderr,
                )
            return
        proxy = f"standing-authority:{authority['authority_id'][:8]}"
        approve_plan(store, current, plan["digest"], actor_id=proxy)
        with store.transaction_lock(initiative_id):
            approved = store.peek(initiative_id)
            event = _event(
                approved, "approval-decided",
                {
                    "decision": "approved",
                    "standing_authority_id": authority["authority_id"],
                    "authority_label": authority["label"],
                    "plan_digest": plan["digest"],
                },
                _now(), actor_kind="controller", actor_id="standing-authority",
            )
            store.append_event(initiative_id, event)
        if not authority["auto_activate"]:
            return
        approved = store.peek(initiative_id)
        document = build_action_document(
            approved, "activate-initiative", {}, actor_id=proxy,
        )
        try:
            submit_action(store, initiative_id, document)
        except (ActionRefused, StoreError, ValueError, OSError) as exc:
            print(
                f"asha initiative: standing authority {authority['label']} approved the plan "
                f"but activation was refused: {exc}",
                file=sys.stderr,
            )
    except (AuthorityError, StoreError, ValueError, OSError) as exc:
        print(f"asha initiative: standing authority not applied: {exc}", file=sys.stderr)


ATTENTION_CONTRACT = "asha.orchestration-attention.v1"


def _refuse_any_coordinator_session(env: Mapping[str, str]) -> None:
    """Authority grants are operator acts; any coordinator session is refused."""
    if env.get("ASHA_ORCHESTRATION_COORDINATOR_ID"):
        raise ValueError(
            "standing authorities are granted and revoked from the operator's own "
            "terminal; a coordinator session cannot hold this verb"
        )


def _authority_command(
    args: list[str], config, env: Mapping[str, str], tmux: TmuxAdapter,
    *, jj: JjAdapter | None = None,
) -> int:
    from .authority import add_authority, list_authorities, revoke_authority

    if not args or args[0] not in {"add", "list", "revoke"}:
        raise ValueError("authority requires add, list, or revoke")
    verb, tail = args[0], args[1:]
    if verb == "list":
        options = _parse_options(tail, flags={"json", "all"})
        _only(options, {"json", "all"}, "authority list")
        records = list_authorities(config, include_revoked=bool(options["all"]))
        payload = {"contract": "asha.orchestration-authority-list.v1", "authorities": records}
        if options["json"]:
            _json(payload)
        else:
            if not records:
                print("No standing authorities.")
            for record in records:
                state = "revoked" if record["revoked_at"] else "active"
                constraints = record["constraints"]
                print(f"{record['label']:<20} {state:<8} {record['authority_id']}")
                print(f"{'':<20} repo {record['repository']['root']}")
                print(
                    f"{'':<20} scope {','.join(constraints['scope_prefixes'])}"
                    f"  nodes<={constraints['max_nodes']}  harnesses {','.join(constraints['harnesses'])}"
                    f"  auto-activate {record['auto_activate']}"
                )
        return 0
    _refuse_any_coordinator_session(env)
    refuse_coordinator_pane_any = getattr(tmux, "pane_option", None)
    pane = env.get("TMUX_PANE")
    if pane and callable(refuse_coordinator_pane_any):
        try:
            if tmux.pane_option(pane, "@asha_coordinator_id"):
                raise ValueError(
                    "standing authorities are granted and revoked from the operator's own "
                    "terminal; the coordinator's pane is refused"
                )
        except TmuxError:
            pass
    if verb == "add":
        if not tail or tail[0].startswith("--"):
            raise ValueError("authority add requires a NAME first")
        positional = [tail[0]]
        options = _parse_options(tail[1:], repeat={"scope"}, flags={"json", "require_headless", "no_auto_activate"})
        _only(options, {"repo", "scope", "max_nodes", "harness", "max_attempts",
                        "json", "require_headless", "no_auto_activate"}, "authority add")
        _required(options, "repo")
        if not options.get("scope"):
            raise ValueError("authority add requires at least one --scope PREFIX")
        harnesses = None
        if options.get("harness"):
            harnesses = [item.strip() for item in options["harness"].split(",") if item.strip()]
        record = add_authority(
            config, root=Path(options["repo"]), label=positional[0],
            scope_prefixes=list(options["scope"]),
            max_nodes=int(options["max_nodes"]) if options.get("max_nodes") else 5,
            harnesses=harnesses,
            max_attempts_per_node=int(options["max_attempts"]) if options.get("max_attempts") else 2,
            require_headless=bool(options["require_headless"]),
            auto_activate=not options["no_auto_activate"],
            jj=jj or JjAdapter(),
        )
        payload = {"contract": "asha.orchestration-authority-grant.v1", "authority": record}
        if options["json"]:
            _json(payload)
        else:
            print(f"Authority {record['label']} granted: {record['authority_id']}")
            print("Matching plans are pre-approved" + (" and activated." if record["auto_activate"] else "."))
        return 0
    if not tail or tail[0].startswith("--"):
        raise ValueError("authority revoke requires an AUTHORITY_ID first")
    options = _parse_options(tail[1:], flags={"json"})
    _only(options, {"json"}, "authority revoke")
    record = revoke_authority(config, tail[0])
    payload = {"contract": "asha.orchestration-authority-revocation.v1", "authority": record}
    if options["json"]:
        _json(payload)
    else:
        print(f"Authority {record['label']} revoked at {record['revoked_at']}.")
    return 0


def _print_project_index(payload: dict[str, Any]) -> None:
    """Grouped by root, orchestration-ready first.

    Only a jj-colocated project can run an initiative, so the distinction leads
    rather than hiding in a flag column: an operator should not pick a project
    and then be refused.
    """
    width = max(
        [len(entry["name"]) for entry in payload["projects"]] + [12]
    ) + 2
    ready_total = 0
    for group in payload["groups"]:
        projects = group["projects"]
        ready = [entry for entry in projects if entry["jj_colocated"]]
        ready_total += len(ready)
        print(f"{_home_relative(group['root'])}   {_count(len(projects), 'project')}"
              f", {len(ready)} orchestration-ready")
        order = sorted(projects, key=lambda item: (not item["jj_colocated"], item["name"].lower()))
        for entry in order:
            mark = "*" if entry["jj_colocated"] else "-"
            note = "" if entry["jj_colocated"] else "  (no jj - cannot run an initiative)"
            renamed = ""
            if entry.get("directory") and entry["directory"] != entry["name"]:
                renamed = f"  [{entry['directory']}]"
            print(f"   {mark} {entry['name']:<{width}}{renamed}{note}")
        print()
    for item in payload.get("skipped", []):
        print(f"skipped {_home_relative(item['root'])}: {item['reason']}")
    total = len(payload["projects"])
    roots = _count(len(payload["groups"]), "root")
    print(f"{_count(total, 'project')} across {roots} - {ready_total} orchestration-ready"
          f"  (roots from {payload.get('roots_from', 'argument')})")


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _home_relative(path: str) -> str:
    home = str(Path.home())
    return f"~{path[len(home):]}" if path.startswith(home) else path


def _attention_payload(env: Mapping[str, str]) -> dict[str, Any]:
    """Everything waiting on a human, from the same assembler the tree uses."""
    from ..cli import _load_rows_for_attention
    from ..tui import _load_initiative_views
    from .tui_model import attention_items

    views = _load_initiative_views(env)
    task_rows = _load_rows_for_attention(env)
    return {
        "contract": ATTENTION_CONTRACT,
        "items": attention_items(views, task_rows),
    }


def _coordinator_command(
    args: list[str], store: InitiativeStore, env: Mapping[str, str], tmux: TmuxAdapter,
    config: Any = None,
) -> tuple[dict[str, Any], bool]:
    if not args or args[0] not in {"claim", "release", "show", "launch", "sessions", "attach"}:
        raise ValueError("coordinator requires claim, release, show, launch, sessions, or attach")
    if args[0] in {"launch", "sessions", "attach"}:
        return _coordinator_session_command(args[0], args[1:], store, config, env, tmux)
    verb, tail = args[0], args[1:]
    if not tail:
        raise ValueError(f"coordinator {verb} requires an initiative ID or exact slug")
    initiative = _resolve(store, tail[0])
    options = _parse_options(tail[1:], flags={"json"})
    if verb == "claim":
        _only(options, {"harness", "json"}, "coordinator claim")
        record = claim_coordinator(
            store, initiative, env=env, tmux=tmux, harness=options.get("harness"),
        )
        return {
            "contract": COORDINATOR_CLAIM_CONTRACT,
            "initiative_id": initiative["initiative_id"],
            "coordinator": record,
            "environment": environment_for(record),
        }, bool(options["json"])
    _only(options, {"json"}, f"coordinator {verb}")
    if verb == "release":
        record = release_coordinator(store, initiative, env=env, tmux=tmux)
        return {
            "contract": COORDINATOR_RELEASE_CONTRACT,
            "initiative_id": initiative["initiative_id"],
            "coordinator": record,
        }, bool(options["json"])
    return show_coordinator(store, initiative, tmux=tmux), bool(options["json"])


def _asha_root_from_env(env: Mapping[str, str]) -> Path:
    raw_root = env.get("ASHA_ROOT")
    root = Path(__file__).resolve().parents[3] if not raw_root else Path(raw_root)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("ASHA_ROOT must be an exact canonical absolute path")
    return root


def _coordinator_session_command(
    verb: str, args: list[str], store: InitiativeStore, config: Any,
    env: Mapping[str, str], tmux: TmuxAdapter,
) -> tuple[dict[str, Any], bool]:
    from .coordinator import attach_target, launch_session, list_coordinator_sessions

    if verb == "launch":
        options = _parse_options(args, flags={"json"})
        _only(options, {"root", "intent", "harness", "json"}, "coordinator launch")
        _required(options, "intent")
        root = Path(options["root"]) if options.get("root") else Path.cwd()
        result = launch_session(
            config.control, root=root, intent=options["intent"], tmux=tmux,
            asha_root=_asha_root_from_env(env), harness=options.get("harness") or "claude",
        )
        return result, bool(options["json"])
    if verb == "sessions":
        options = _parse_options(args, flags={"json"})
        _only(options, {"json"}, "coordinator sessions")
        return list_coordinator_sessions(config.control, store=store, tmux=tmux), bool(options["json"])
    selector = None
    if args and not args[0].startswith("--"):
        selector, args = args[0], args[1:]
    options = _parse_options(args, flags={"json"})
    _only(options, {"session", "json"}, "coordinator attach")
    initiative_id = None if selector is None else _resolve(store, selector)["initiative_id"]
    target = attach_target(store, tmux=tmux, initiative_id=initiative_id, session=options.get("session"))
    if env.get("TMUX") and not options["json"]:
        from ..cli import _run_popup
        refusal = _run_popup(tmux, config.control, target["session"], "coordinator", env)
        if refusal:
            raise ValueError(refusal)
    return target, bool(options["json"])


def _wait_command(
    args: list[str], store: InitiativeStore, env: Mapping[str, str], tmux: TmuxAdapter,
) -> tuple[dict[str, Any], bool]:
    if not args:
        raise ValueError("wait requires an initiative ID or exact slug")
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json"})
    _only(options, {"after", "timeout", "json"}, "wait")
    _required(options, "after")
    if not options["json"]:
        raise ValueError("wait requires --json")
    after = _non_negative(options["after"], "after")
    timeout = (
        store.config.coordinator_wait_seconds
        if options.get("timeout") is None
        else _non_negative(options["timeout"], "timeout")
    )
    return wait_for_events(
        store, initiative, env=env, tmux=tmux, after=after, timeout=timeout,
    ), True


def _propose_plan_command(
    args: list[str], store: InitiativeStore, config, env: Mapping[str, str],
    tmux: TmuxAdapter, *, jj: JjAdapter,
) -> tuple[dict[str, Any], bool]:
    if not args:
        raise ValueError("propose-plan requires an initiative ID or exact slug")
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json"})
    _only(options, {"file", "json"}, "propose-plan")
    _required(options, "file")
    coordinator = require_live_coordinator(store, initiative["initiative_id"])
    require_anchored_caller(coordinator, env, tmux)
    raw = _read_plan_file(Path(options["file"]))
    if not isinstance(raw, dict):
        raise ValueError("plan file must contain an object")
    plan = propose_plan(
        store, initiative, raw, config=config, jj=jj,
        actor_kind="coordinator", actor_id=f"coordinator:{coordinator['coordinator_id']}",
    )
    return plan, bool(options["json"])


def _checkpoint_command(
    args: list[str], store: InitiativeStore, env: Mapping[str, str], tmux: TmuxAdapter,
) -> tuple[dict[str, Any], bool]:
    if not args:
        raise ValueError("checkpoint requires an initiative ID or exact slug")
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json"})
    _only(options, {"file", "json"}, "checkpoint")
    _required(options, "file")
    document = _read_json_file(Path(options["file"]), "checkpoint")
    if not isinstance(document, dict):
        raise ValueError("checkpoint file must contain an object")
    record = checkpoint_coordinator(store, initiative, env=env, tmux=tmux, document=document)
    return record, bool(options["json"])


def _non_negative(value: Any, name: str) -> int:
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", str(value)) is None:
        raise ValueError(f"--{name} must be a non-negative integer")
    return int(value)


def _approve(
    args: list[str], store: InitiativeStore,
    env: Mapping[str, str] | None = None, tmux: TmuxAdapter | None = None,
) -> tuple[dict[str, Any], bool]:
    env = {} if env is None else env
    if not args:
        raise ValueError("approve requires an initiative ID or exact slug")
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json"})
    _only(options, {"digest", "json"}, "approve")
    _required(options, "digest")
    refuse_coordinator_pane(store, initiative["initiative_id"], env, tmux)
    return approve_plan(store, initiative, options["digest"]), bool(options["json"])


def approve_plan(
    store: InitiativeStore, initiative: dict[str, Any], digest: str, *, actor_id: str = "cli",
) -> dict[str, Any]:
    """Operator plan approval core shared by the CLI and the Control TUI."""
    plan = _latest_executable_plan(store, initiative["initiative_id"])
    if initiative["state"] not in {"awaiting-plan-approval", "approved"}:
        raise ValueError("initiative is not awaiting plan approval")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != plan["digest"]:
        raise ValueError("approval digest does not match the latest proposed plan")
    nodes = store.list_nodes_snapshot(initiative["initiative_id"])
    plan_ids = {node["node_id"] for node in plan["nodes"]}
    selected = [node for node in nodes if node["node_id"] in plan_ids]
    if len(selected) != len(plan_ids) or any(
        node["state"] not in {"proposed", "approved"} for node in selected
    ):
        raise StoreError("proposed node records do not exactly match the plan")
    expected_nodes = {node["node_id"]: node for node in plan["nodes"]}
    for node in selected:
        comparable = copy.deepcopy(node)
        comparable["state"] = "proposed"
        if comparable != expected_nodes[node["node_id"]]:
            raise StoreError("retained node content does not exactly match the plan")
    at = _now()
    now_value = datetime.fromisoformat(at[:-1] + "+00:00")
    matching_approvals = [
        item for item in store.list_approvals_snapshot(initiative["initiative_id"])
        if item["binding_digest"] == plan["digest"]
        and item["active_plan_digest"] == plan["digest"]
        and item["action_class"] == "plan-approval"
    ]
    reusable = [
        item for item in matching_approvals
        if item["state"] == "consumed"
        or (
            item["state"] == "approved"
            and datetime.fromisoformat(item["expires_at"][:-1] + "+00:00") > now_value
        )
    ]
    expired_approved = [
        item for item in matching_approvals
        if item["state"] == "approved"
        and datetime.fromisoformat(item["expires_at"][:-1] + "+00:00") <= now_value
    ]
    terminal_expired = [
        item for item in matching_approvals if item["state"] == "expired"
    ]
    unsupported = [
        item for item in matching_approvals
        if item not in reusable and item not in expired_approved and item not in terminal_expired
    ]
    if not reusable and unsupported:
        raise StoreError("retained plan approval cannot be resumed")
    if initiative["state"] == "approved":
        active = initiative["active_plan"]
        if active is None or active["revision"] != plan["revision"] or active["digest"] != plan["digest"]:
            raise StoreError("approved initiative does not match the requested plan")
        reusable = [
            item for item in reusable if item["request_id"] == active["approval_id"]
        ]
    if len(reusable) > 1:
        raise StoreError("multiple retained approvals match the requested plan")
    approved = reusable[0] if reusable else None
    if approved is None:
        if initiative["state"] == "approved":
            raise StoreError("approved initiative is missing its retained approval")
        expires = (now_value + timedelta(days=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        approved = validate_approval({
            "contract": APPROVAL_CONTRACT, "request_id": new_uuid(),
            "initiative_id": initiative["initiative_id"], "action_class": "plan-approval",
            "binding_digest": plan["digest"], "active_plan_digest": plan["digest"],
            "expected_state_revision": initiative["state_revision"], "actor_kind": "operator",
            "actor_id": actor_id, "state": "approved", "expires_at": expires,
            "rationale": None, "created_at": at, "updated_at": at,
        })
    approval_id = approved["request_id"]
    changed_nodes = []
    for node in selected:
        changed = copy.deepcopy(node)
        changed["state"] = "approved"
        validate_node(changed)
        changed_nodes.append((node, changed))
    with store.transaction_lock(initiative["initiative_id"]):
        if record_digest(store.peek(initiative["initiative_id"])) != record_digest(initiative):
            raise StoreError("initiative changed; reload before approving")
        for stale in expired_approved:
            expired = copy.deepcopy(stale)
            expired.update({"state": "expired", "updated_at": at})
            validate_approval(expired)
            store.save_approval(
                initiative["initiative_id"], expired,
                expected_digest=record_digest(stale),
            )
        if not reusable:
            store.save_approval(initiative["initiative_id"], approved)
        for before, after in changed_nodes:
            if before["state"] == "proposed":
                store.save_node(initiative["initiative_id"], after, expected_digest=record_digest(before))
        current = store.peek(initiative["initiative_id"])
        if current["state"] == "awaiting-plan-approval":
            changed_initiative = copy.deepcopy(current)
            changed_initiative.update({
                "state": "approved", "active_plan": {"revision": plan["revision"], "digest": plan["digest"], "approval_id": approval_id},
                "state_revision": current["state_revision"] + 1, "updated_at": at,
            })
            validate_initiative(changed_initiative)
            store.save_initiative(changed_initiative, expected_digest=record_digest(current))
        else:
            expected_active = {
                "revision": plan["revision"], "digest": plan["digest"],
                "approval_id": approval_id,
            }
            if current["active_plan"] != expected_active:
                raise StoreError("approved initiative does not match the requested plan")
        current_approval = store.read_approval(initiative["initiative_id"], approval_id)
        if current_approval["state"] == "approved":
            consumed = copy.deepcopy(current_approval)
            consumed.update({"state": "consumed", "updated_at": at})
            validate_approval(consumed)
            store.save_approval(
                initiative["initiative_id"], consumed,
                expected_digest=record_digest(current_approval),
            )
        current = store.peek(initiative["initiative_id"])
        if not _has_plan_event(
            store.list_events_snapshot(initiative["initiative_id"]),
            "plan-approved", plan["digest"],
        ):
            event = _event(
                current, "plan-approved",
                {"revision": plan["revision"], "digest": plan["digest"], "approval_id": approval_id}, at,
            )
            store.append_event(initiative["initiative_id"], event)
    return {
        "contract": APPROVAL_RESULT_CONTRACT,
        "initiative": store.peek(initiative["initiative_id"]),
        "plan": plan,
        "approval": store.read_approval(initiative["initiative_id"], approval_id),
    }


def _reject(
    args: list[str], store: InitiativeStore,
    env: Mapping[str, str] | None = None, tmux: TmuxAdapter | None = None,
) -> tuple[dict[str, Any], bool]:
    env = {} if env is None else env
    if not args:
        raise ValueError("reject requires an initiative ID or exact slug")
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json"})
    _only(options, {"digest", "reason", "json"}, "reject")
    _required(options, "digest", "reason")
    refuse_coordinator_pane(store, initiative["initiative_id"], env, tmux)
    return reject_plan(store, initiative, options["digest"], options["reason"]), bool(options["json"])


def reject_plan(
    store: InitiativeStore, initiative: dict[str, Any], digest: str, reason: str, *,
    actor_id: str = "cli",
) -> dict[str, Any]:
    """Operator plan rejection core shared by the CLI and the Control TUI."""
    plan = _latest_plan(store, initiative["initiative_id"])
    if initiative["state"] != "awaiting-plan-approval":
        raise ValueError("initiative is not awaiting plan approval")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != plan["digest"]:
        raise ValueError("rejection digest does not match the latest proposed plan")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("rejection requires a reason")
    plan_ids = {node["node_id"] for node in plan["nodes"]}
    retained = {
        node["node_id"]: node
        for node in store.list_nodes_snapshot(initiative["initiative_id"])
        if node["node_id"] in plan_ids
    }
    if set(retained) != plan_ids or any(
        node["state"] not in {"proposed", "approved", "superseded"}
        for node in retained.values()
    ):
        raise StoreError("rejected plan nodes are not all retained as proposed")
    expected_nodes = {node["node_id"]: node for node in plan["nodes"]}
    for node in retained.values():
        comparable = copy.deepcopy(node)
        comparable["state"] = "proposed"
        if comparable != expected_nodes[node["node_id"]]:
            raise StoreError("rejected plan node content does not match the plan")
    at = _now()
    now_value = datetime.fromisoformat(at[:-1] + "+00:00")
    expired_approvals = [
        item for item in store.list_approvals_snapshot(initiative["initiative_id"])
        if item["binding_digest"] == plan["digest"]
        and item["active_plan_digest"] == plan["digest"]
        and item["action_class"] == "plan-approval"
        and item["state"] == "approved"
        and datetime.fromisoformat(item["expires_at"][:-1] + "+00:00") <= now_value
    ]
    changed = copy.deepcopy(initiative)
    changed.update({"state": "planning", "state_revision": initiative["state_revision"] + 1, "updated_at": at})
    validate_initiative(changed)
    event = _event(
        initiative, "plan-rejected",
        {"revision": plan["revision"], "digest": plan["digest"], "reason": reason}, at,
        actor_id=actor_id,
    )
    with store.transaction_lock(initiative["initiative_id"]):
        if record_digest(store.peek(initiative["initiative_id"])) != record_digest(initiative):
            raise StoreError("initiative changed; reload before rejecting")
        for node in retained.values():
            if node["state"] == "superseded":
                continue
            superseded = copy.deepcopy(node)
            superseded["state"] = "superseded"
            validate_node(superseded)
            store.save_node(
                initiative["initiative_id"], superseded,
                expected_digest=record_digest(node),
            )
        for stale in expired_approvals:
            expired = copy.deepcopy(stale)
            expired.update({"state": "expired", "updated_at": at})
            validate_approval(expired)
            store.save_approval(
                initiative["initiative_id"], expired,
                expected_digest=record_digest(stale),
            )
        store.save_initiative(changed, expected_digest=record_digest(initiative))
        store.append_event(initiative["initiative_id"], event)
    return {
        "contract": REJECTION_RESULT_CONTRACT,
        "initiative": store.peek(initiative["initiative_id"]),
        "plan_digest": plan["digest"],
        "reason": reason,
    }


def _partition_nodes(
    initiative: dict[str, Any], plans: list[dict[str, Any]], nodes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    superseded = [node for node in nodes if node["state"] == "superseded"]
    if initiative["active_plan"] is None:
        live = [node for node in nodes if node["state"] in NODE_NONTERMINAL_STATES]
    else:
        active = next(
            (
                plan for plan in plans
                if plan["revision"] == initiative["active_plan"]["revision"]
                and plan["digest"] == initiative["active_plan"]["digest"]
            ),
            None,
        )
        if active is None:
            raise StoreError("active plan revision is missing")
        active_ids = {node["node_id"] for node in active["nodes"]}
        live = [node for node in nodes if node["node_id"] in active_ids]
    return live, superseded


def snapshot(store: InitiativeStore, initiative: dict[str, Any]) -> dict[str, Any]:
    """Lock-free typed read shared by the CLI, reconciliation, and the Control TUI."""
    plans = store.list_plans_snapshot(initiative["initiative_id"])
    active = None
    if initiative["active_plan"] is not None:
        active = next((
            plan for plan in plans
            if plan["revision"] == initiative["active_plan"]["revision"]
            and plan["digest"] == initiative["active_plan"]["digest"]
        ), None)
    nodes, superseded_nodes = _partition_nodes(
        initiative, plans, store.list_nodes_snapshot(initiative["initiative_id"]),
    )
    return {
        "contract": SNAPSHOT_CONTRACT, "initiative": initiative, "active_plan": active,
        "nodes": nodes, "superseded_nodes": superseded_nodes,
        "attempts": store.list_attempts_snapshot(initiative["initiative_id"]),
        "links": store.list_links_snapshot(initiative["initiative_id"]),
        "actions": store.list_actions_snapshot(initiative["initiative_id"]),
        "coordinator": store.current_coordinator(initiative["initiative_id"]),
        "last_event_sequence": initiative["last_event_sequence"],
        "state_revision": initiative["state_revision"],
    }


_snapshot = snapshot


def reconcile_one_initiative(
    store: InitiativeStore,
    initiative_id: str,
    *,
    tmux: TmuxAdapter | None = None,
    control_store: TaskStore | None = None,
    adapters_factory: Callable[[dict[str, Any]], LiveAdapters] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Run the CLI's reconciliation sequence without dispatching ready work."""
    action_result = reconcile_actions(store, initiative_id)
    live_options = {}
    if control_store is not None:
        live_options["control_store"] = control_store
    if adapters_factory is not None:
        live_options["adapters_factory"] = adapters_factory
    if now is not None:
        live_options["now"] = now
    live_result = reconcile_live(store, initiative_id, **live_options)
    coordinator_result = reconcile_coordinator(
        store, initiative_id, tmux=tmux or TmuxAdapter(),
    )
    initiative = store.peek(initiative_id)
    snapshot = _snapshot(store, initiative)
    node_options: dict[str, Any] = {"store": store}
    if control_store is not None:
        node_options["control_store"] = control_store
    if adapters_factory is not None:
        node_options["adapters_factory"] = adapters_factory
    node_results = reconcile_nodes(
        initiative["initiative_id"], snapshot["nodes"], **node_options,
    )
    return {
        "contract": RECONCILE_LIST_CONTRACT,
        "initiative_id": initiative["initiative_id"],
        "action_reconciliation": action_result,
        "live_reconciliation": live_result,
        "coordinator_reconciliation": coordinator_result,
        "results": node_results,
        "superseded_nodes": snapshot["superseded_nodes"],
    }


def _submit_convenience_action(
    store: InitiativeStore,
    initiative: dict[str, Any],
    action_class: str,
    payload: dict[str, Any],
    *,
    action_id: str | None = None,
) -> dict[str, Any]:
    document = build_action_document(
        initiative, action_class, payload, actor_id="cli", action_id=action_id,
    )
    return submit_action(store, initiative["initiative_id"], document)


def _action_command(
    args: list[str], store: InitiativeStore,
    env: Mapping[str, str] | None = None, tmux: TmuxAdapter | None = None,
) -> tuple[dict[str, Any], bool]:
    env = {} if env is None else env
    if not args:
        raise ValueError("action requires an initiative ID or exact slug")
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json"})
    _only(options, {"file", "json"}, "action")
    _required(options, "file")
    if not options["json"]:
        raise ValueError("action requires --json")
    document = _read_json_file(Path(options["file"]), "action")
    if isinstance(document, dict):
        if document.get("actor_kind") == "coordinator":
            # The pane/process proof lives here; the journal fence lives in submit_action.
            coordinator = require_live_coordinator(store, initiative["initiative_id"])
            require_anchored_caller(coordinator, env, tmux or TmuxAdapter())
        else:
            # Operator documents never come from the coordinator's pane or session.
            refuse_coordinator_pane(store, initiative["initiative_id"], env, tmux or TmuxAdapter())
    result = submit_action(store, initiative["initiative_id"], document)
    return result, True


def _operator_action(
    command: str, args: list[str], store: InitiativeStore,
    env: Mapping[str, str] | None = None, tmux: TmuxAdapter | None = None,
) -> tuple[dict[str, Any], bool]:
    if not args:
        raise ValueError(f"{command} requires an initiative ID or exact slug")
    env = {} if env is None else env
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json", "as_coordinator"})
    allowed = {"json", "as_coordinator"}
    payload: dict[str, Any]
    action_class = command
    if command == "activate":
        action_class, payload = "activate-initiative", {}
    elif command in {"pause", "resume"}:
        payload = {}
    elif command == "dispatch":
        allowed.update({"node", "salvage_request"})
        _required(options, "node")
        action_class, payload = "dispatch-node", {"node_id": options["node"]}
        if options.get("salvage_request") is not None:
            payload["salvage_request_id"] = options["salvage_request"]
    elif command == "stop":
        allowed.add("attempt")
        _required(options, "attempt")
        action_class, payload = "stop-attempt", {"attempt_id": options["attempt"]}
    elif command == "cancel":
        allowed.add("node")
        _required(options, "node")
        action_class, payload = "cancel-node", {"node_id": options["node"]}
    elif command == "finalize":
        allowed.update({"outcome", "reason"})
        _required(options, "outcome", "reason")
        action_class, payload = "finalize", {
            "outcome": options["outcome"], "reason": options["reason"],
        }
    elif command in {"archive", "unarchive"}:
        action_class, payload = command, {}
    else:
        raise ValueError(f"unknown operator action: {command}")
    _only(options, allowed, command)
    if not options["as_coordinator"]:
        refuse_coordinator_pane(store, initiative["initiative_id"], env, tmux or TmuxAdapter())
    if options["as_coordinator"]:
        coordinator = require_live_coordinator(store, initiative["initiative_id"])
        require_anchored_caller(coordinator, env, tmux or TmuxAdapter())
        document = build_action_document(
            initiative, action_class, payload,
            actor_id=f"coordinator:{coordinator['coordinator_id']}", coordinator=coordinator,
        )
        return submit_action(store, initiative["initiative_id"], document), bool(options["json"])
    return (
        _submit_convenience_action(
            store, initiative, action_class, payload,
        ),
        bool(options["json"]),
    )


def _record_integration_command(
    args: list[str], store: InitiativeStore,
    env: Mapping[str, str] | None = None, tmux: TmuxAdapter | None = None,
) -> tuple[dict[str, Any], bool]:
    if not args:
        raise ValueError("record-integration requires an initiative ID or exact slug")
    env = {} if env is None else env
    initiative = _resolve(store, args[0])
    options = _parse_options(
        args[1:], flags={"json", "abandoned", "composed-verification"},
    )
    _only(
        options,
        {"bundle", "seal", "abandoned", "reason", "json", "composed-verification"},
        "record-integration",
    )
    bundle_id, seal_id = options.get("bundle"), options.get("seal")
    if (bundle_id is None) == (seal_id is None):
        raise ValueError("record-integration requires exactly one of --bundle or --seal")
    if bundle_id is not None:
        if options["abandoned"]:
            raise ValueError("record-integration --bundle does not accept --abandoned")
        if options.get("reason") is not None:
            raise ValueError("record-integration --bundle does not accept --reason")
    elif options["composed-verification"]:
        raise ValueError(
            "record-integration --composed-verification applies to --bundle only"
        )
    else:
        if not options["abandoned"]:
            raise ValueError("record-integration --seal requires --abandoned")
        _required(options, "reason")
    refuse_coordinator_pane(
        store, initiative["initiative_id"], env, tmux or TmuxAdapter(),
    )
    event = record_integration(
        store, initiative["initiative_id"], bundle_id=bundle_id, seal_id=seal_id,
        abandoned=bool(options["abandoned"]), reason=options.get("reason"),
        composed_verification=bool(options["composed-verification"]),
    )
    return event, bool(options["json"])


def _compose_verify_command(
    args: list[str], store: InitiativeStore,
    env: Mapping[str, str] | None = None, tmux: TmuxAdapter | None = None,
) -> tuple[dict[str, Any], bool]:
    """Run a bundle's declared commands over its members materialized together."""
    if not args:
        raise ValueError("compose-verify requires an initiative ID or exact slug")
    env = {} if env is None else env
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json"})
    _only(options, {"bundle", "json"}, "compose-verify")
    _required(options, "bundle")
    refuse_coordinator_pane(
        store, initiative["initiative_id"], env, tmux or TmuxAdapter(),
    )
    from .verification import run_composed_verification

    return run_composed_verification(
        store, initiative["initiative_id"], options["bundle"],
    ), bool(options["json"])


def _approve_salvage_command(
    args: list[str], store: InitiativeStore,
    env: Mapping[str, str] | None = None, tmux: TmuxAdapter | None = None,
) -> tuple[dict[str, Any], bool]:
    env = {} if env is None else env
    if not args:
        raise ValueError("approve-salvage requires an initiative ID or exact slug")
    initiative = _resolve(store, args[0])
    options = _parse_options(args[1:], flags={"json"})
    _only(options, {"request", "json"}, "approve-salvage")
    _required(options, "request")
    refuse_coordinator_pane(store, initiative["initiative_id"], env, tmux)
    approval = approve_salvage(
        store, initiative["initiative_id"], options["request"], actor_id="cli",
    )
    return {
        "contract": SALVAGE_APPROVAL_RESULT_CONTRACT,
        "initiative_id": initiative["initiative_id"],
        "approval": approval,
    }, bool(options["json"])


def _initiative_command(
    args: list[str], env: Mapping[str, str], *,
    jj: JjAdapter | None = None, tmux: TmuxAdapter | None = None,
) -> int:
    if not args:
        _usage(sys.stderr)
        return 2
    if args[0] in {"help", "-h", "--help"}:
        _usage()
        return 0
    command, tail = args[0], args[1:]
    tmux = tmux or TmuxAdapter()
    if command == "baseline":
        result, json_output = _baseline(tail, jj or JjAdapter())
        divergence = result["baseline_divergence"]
        if divergence is not None:
            print(f"asha initiative: {divergence['warning']}", file=sys.stderr)
            print(
                "asha initiative: move the bookmark or pass --revision to plan "
                "against the newer tree.",
                file=sys.stderr,
            )
        if json_output:
            _json(result)
        else:
            print(f"Commit: {result['jj_commit_id']}")
            print(f"Tree digest: {result['tree_digest']}")
        return 0
    config = load_config(env)
    store = InitiativeStore(config)
    if command == "create":
        result = _create(tail, config, store, jj or JjAdapter())
        json_output = result.pop("json")
        _payload(result, json_output)
        return 0
    if command == "plan":
        result, json_output = _plan(tail, store, config, jj=jj or JjAdapter(), env=env, tmux=tmux)
        _payload(result, json_output)
        return 0
    if command == "approve":
        result, json_output = _approve(tail, store, env, tmux)
        _payload(result, json_output)
        return 0
    if command == "reject":
        result, json_output = _reject(tail, store, env, tmux)
        _payload(result, json_output)
        return 0
    if command == "approve-salvage":
        result, json_output = _approve_salvage_command(tail, store, env, tmux)
        _payload(result, json_output)
        return 0
    if command == "action":
        result, json_output = _action_command(tail, store, env, tmux)
        _payload(result, json_output)
        return 2 if result["state"] == "refused" else 3 if result["state"] == "indeterminate" else 0
    if command == "coordinator":
        result, json_output = _coordinator_command(tail, store, env, tmux, config=config)
        _payload(result, json_output)
        return 0
    if command == "wait":
        result, json_output = _wait_command(tail, store, env, tmux)
        _payload(result, json_output)
        return 0
    if command == "propose-plan":
        result, json_output = _propose_plan_command(
            tail, store, config, env, tmux, jj=jj or JjAdapter(),
        )
        _payload(result, json_output)
        return 0
    if command in {
        "activate", "dispatch", "pause", "resume", "stop", "cancel",
        "finalize", "archive", "unarchive",
    }:
        result, json_output = _operator_action(command, tail, store, env, tmux)
        _payload(result, json_output)
        return 2 if result["state"] == "refused" else 3 if result["state"] == "indeterminate" else 0
    if command == "record-integration":
        result, json_output = _record_integration_command(tail, store, env, tmux)
        _payload(result, json_output)
        return 0
    if command == "compose-verify":
        result, json_output = _compose_verify_command(tail, store, env, tmux)
        _payload(result, json_output)
        return 0 if result.get("outcome") == "passed" else 3
    if command == "checkpoint":
        result, json_output = _checkpoint_command(tail, store, env, tmux)
        _payload(result, json_output)
        return 0
    options_tail, json_output = [], False
    if command == "list":
        options = _parse_options(tail, flags={"all", "json"})
        _only(options, {"all", "json"}, "list")
        payload = {
            "contract": LIST_CONTRACT,
            "initiatives": [
                item for item in store.list_initiatives()
                if options["all"] or item["state"] != "archived"
            ],
        }
        if store.skipped:
            payload["skipped"] = list(store.skipped)
        _payload(payload, bool(options["json"]))
        return 0
    if command == "authority":
        return _authority_command(tail, config, env, tmux, jj=jj or JjAdapter())
    if command == "attention":
        options = _parse_options(tail, flags={"json"})
        _only(options, {"json"}, "attention")
        payload = _attention_payload(env)
        if options["json"]:
            _json(payload)
        else:
            if not payload["items"]:
                print("Nothing is waiting on a human.")
            for item in payload["items"]:
                where = item.get("slug") or item.get("task_id", "")
                print(f"{item['kind']:<18} {str(where)[:28]:<28} {item['detail'][:70]}")
                print(f"{'':<18} -> {item['resolution'][:90]}")
        return 0
    if command == "projects":
        options = _parse_options(tail, repeat={"root"}, flags={"json"})
        _only(options, {"root", "depth", "match", "json"}, "projects")
        from .projects import list_projects_across, resolve_roots

        depth_option = options.get("depth")
        try:
            depth = 1 if depth_option is None else int(depth_option)
        except ValueError as exc:
            raise ValueError("projects --depth must be an integer") from exc
        roots, roots_from = resolve_roots(list(options.get("root") or []), env=env)
        payload = list_projects_across(
            roots, depth=depth, match=options.get("match"), source_of_roots=roots_from,
        )
        if options["json"]:
            _json(payload)
        else:
            _print_project_index(payload)
        return 0
    if command == "doctor":
        options = _parse_options(tail, flags={"json"})
        _only(options, {"json"}, "doctor")
        payload = run_orchestration_doctor(config)
        if options["json"]:
            _json(payload)
        else:
            for probe in payload["probes"]:
                print(f"{probe['outcome']:<11} {probe['name']}: {probe['detail']}")
        return 0 if payload["ok"] else 1
    if command not in {"show", "events", "reconcile", "storage", "snapshot"}:
        raise ValueError(f"unknown initiative verb: {command}")
    if not tail:
        raise ValueError(f"{command} requires an initiative ID or exact slug")
    initiative = _resolve(store, tail[0])
    flags = {"json"}
    options = _parse_options(tail[1:], flags=flags)
    allowed = {"json", "after"} if command == "events" else {"json"}
    _only(options, allowed, command)
    json_output = bool(options["json"])
    if command == "events":
        raw_after = options.get("after")
        if raw_after is None:
            after = 0
        elif re.fullmatch(r"(?:0|[1-9][0-9]*)", str(raw_after)) is None:
            raise ValueError("--after must be a nonnegative integer")
        else:
            after = int(raw_after)
        payload = {"contract": EVENT_LIST_CONTRACT, "initiative_id": initiative["initiative_id"], "events": store.list_events_snapshot(initiative["initiative_id"], after=after)}
    elif command == "snapshot":
        if not json_output:
            raise ValueError("snapshot requires --json")
        payload = _snapshot(store, initiative)
    elif command == "reconcile":
        payload = reconcile_one_initiative(store, initiative["initiative_id"])
    elif command == "storage":
        payload = storage_report(initiative, store=store)
    else:
        payload = show_payload(store, initiative)
    _payload(payload, json_output)
    return 0


def show_payload(store: InitiativeStore, initiative: dict[str, Any]) -> dict[str, Any]:
    """The `show` composition: typed snapshot, node reconciliation, counts, gates, limits."""
    current = snapshot(store, initiative)
    reconciled = reconcile_nodes(initiative["initiative_id"], current["nodes"], store=store)
    evidence_counts = store.record_counts_snapshot(initiative["initiative_id"])
    return {
        "contract": SHOW_CONTRACT, "initiative": initiative,
        "graph": {
            "plan": current["active_plan"], "nodes": current["nodes"],
            "attempts": current["attempts"], "links": current["links"],
        },
        "action_outcomes": current["actions"],
        "gates": [] if current["active_plan"] is None else current["active_plan"]["declared_gates"],
        "limits": initiative["limits"],
        "evidence_counts": evidence_counts,
        "node_reconciliation": reconciled,
        "superseded_nodes": current["superseded_nodes"],
    }


def _task_2b_command(args: list[str], env: Mapping[str, str]) -> int:
    if not args or args[0] not in {"report", "ingest", "result", "seal"}:
        raise ValueError("task result route requires report, ingest, result, or seal")
    command, tail = args[0], args[1:]
    config = load_config(env)
    if command == "report":
        options = _parse_options(tail, flags={"json"})
        _only(options, {"file", "json"}, "task report")
        _required(options, "file")
        body = read_client_file(Path(options["file"]))
        if env.get("ASHA_CONTROL_RESULT_INGESTION_ID"):
            receipt = stage_result(
                config, body, env, caller_pid=os.getpid(),
            )
        else:
            receipt = publish_result(
                InitiativeStore(config), body, env, caller_pid=os.getpid(),
            )
        if options["json"]:
            _json(receipt)
        else:
            if receipt["phase"] == "staged":
                print(f"Ingestion: {receipt['ingestion_id']}")
                print(f"Reserved outbox: {receipt['outbox_path']}")
            else:
                print(f"Result: {receipt['result_id']}")
            print(f"Publication phase: {receipt['phase']}")
            if receipt.get("refusal") is not None:
                print(f"Refusal: {receipt['refusal']}")
        return (
            0 if receipt["phase"] in {"completed", "staged"}
            else 3 if receipt["phase"] == "indeterminate" else 2
        )
    if command == "ingest":
        if not tail or tail[0].startswith("--"):
            raise ValueError("task ingest requires exactly one ingestion identity")
        identity = tail[0]
        options = _parse_options(tail[1:], flags={"json"})
        _only(options, {"json"}, "task ingest")
        if env.get("ASHA_CONTROL_MANAGED") == "1":
            raise IngestionRefused(
                "managed workers cannot invoke controller-owned result ingestion"
            )
        store = InitiativeStore(config)
        matches = []
        for initiative in store.list_initiatives():
            initiative_id = initiative["initiative_id"]
            try:
                records = store.list_result_ingestions_snapshot(initiative_id)
            except StoreError as exc:
                if (
                    "initiative storage directory is missing: result-ingestions"
                    not in str(exc)
                ):
                    raise
                # A legacy initiative cannot contain a reservation in the
                # absent additive sidecar. Keep identity resolution read-only;
                # caller attribution is checked before ingestion may write.
                records = []
            matches.extend(
                (initiative, record) for record in records
                if record["ingestion_id"] == identity or record["task_id"] == identity
            )
        if len(matches) != 1:
            raise IngestionRefused(
                "result ingestion identity is not uniquely reserved"
            )
        initiative, record = matches[0]
        actor = {
            "actor_kind": "controller", "actor_id": "task-ingest-cli",
            "coordinator_generation": None,
        }
        if env.get("ASHA_ORCHESTRATION_COORDINATOR_ID"):
            coordinator = require_live_coordinator(
                store, initiative["initiative_id"],
            )
            socket = coordinator["anchor"]["tmux_socket"]
            require_anchored_caller(
                coordinator, env,
                TmuxAdapter(socket=None if socket == "default" else socket),
            )
            actor = {
                "actor_kind": "coordinator",
                "actor_id": f"coordinator:{coordinator['coordinator_id']}",
                "coordinator_generation": coordinator["generation"],
            }
        else:
            # An operator-attributed ingest must not be smuggled from the live
            # coordinator pane by merely unsetting its exported identity.
            refuse_coordinator_pane(
                store, initiative["initiative_id"], env, TmuxAdapter(),
            )
        receipt = ingest_result(
            store, initiative["initiative_id"], record["ingestion_id"],
            ingester=actor,
        )
        if options["json"]:
            _json(receipt)
        else:
            print(f"Ingestion: {receipt['ingestion_id']}")
            print(f"Phase: {receipt['phase']}")
            if receipt["result_id"] is not None:
                print(f"Result: {receipt['result_id']}")
            if receipt["refusal"] is not None:
                print(f"Refusal: {receipt['refusal']}")
        return 0 if receipt["phase"] == "completed" else 2
    if not tail or tail[0].startswith("--"):
        raise ValueError(f"task {command} requires exactly one identity")
    identity = tail[0]
    options = _parse_options(tail[1:], flags={"json"})
    _only(options, {"json"}, f"task {command}")
    if command == "result":
        payload = results_for_task(config, identity)
        if options["json"]:
            _json(payload)
        elif not payload["results"]:
            print("No accepted results.")
        else:
            for result in payload["results"]:
                print(
                    f"{result['claim_status']:<14} {result['result_id']}  "
                    f"{result['summary']}"
                )
        return 0
    payload = seal_for_task_or_attempt(config, identity)
    if options["json"]:
        _json(payload)
    else:
        seal = payload["seal"]
        print(f"Seal: {seal['seal_id']}")
        print(f"Outcome: {seal['outcome']}")
        print(f"Commit: {seal['jj_commit_id']}")
    return 0


def task_main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if env is None else env
    try:
        return _task_2b_command(args, values)
    except (CoordinatorError, OrchestrationConfigError, ResultError, ResultRefused,
            SealError, StoreError, TmuxError, JjError, ModelError, OSError,
            ValueError) as exc:
        print(f"asha task: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("asha task: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        detail = "".join(
            character if character.isprintable() else "?" for character in str(exc)
        )[:450]
        print(
            f"asha task: internal error: {detail or type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if env is None else env
    try:
        if not args:
            _usage(sys.stderr)
            return 2
        if args[0] == "initiative":
            return _initiative_command(args[1:], values)
        if args[0] == "task" and len(args) >= 2 and args[1] in {
            "report", "ingest", "result", "seal",
        }:
            return task_main(args[1:], env=values)
        raise ValueError("unknown orchestration route")
    except (ActionError, ActionRefused, OrchestrationConfigError, SchedulerError,
            StoreError, JjError, ModelError, PlanError, OSError, ValueError) as exc:
        print(f"asha initiative: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("asha initiative: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        detail = "".join(character if character.isprintable() else "?" for character in str(exc))[:450]
        print(f"asha initiative: internal error: {detail or type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
