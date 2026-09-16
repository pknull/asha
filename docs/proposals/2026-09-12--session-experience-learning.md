# Session experience capture and reviewed learning

Amended by [automatic defaults and a working loop](2026-09-13--session-experience-defaults.md)
(C1–C8). Where the documents conflict, that amendment takes precedence.

Status: worker-ready implementation plan, not implementation or deployment.
Prepared 2026-09-12 against `bd56dc6` and the live working copy. Revalidate the
base and relevant sources before implementation. Preserve concurrent edits,
including the existing LanguageTool skill change. No commit, push, installation,
live policy activation, or termination of user sessions is authorized by this
document. The Keeper launches the implementation worker separately.

## 1. Objective and acceptance boundary

Let the orchestration harness learn from agent harnesses without requiring an
initiative or a retrospective after every conversational turn. Capture useful
observations while the executing agent still has context, especially during an
operator-requested graceful close. Review selectively, preserve evidence and
uncertainty, and adopt guidance only through deliberate publication.

The complete implementation must demonstrate this sequence in isolation:

```text
applicable active guidance -> worker assignment -> execution
    -> bounded experience report at completion or graceful close
    -> durable capture -> selective, bounded review
    -> explicit save disposition -> candidate/corroboration
    -> later applicable guidance delivery and observed outcome
```

Acceptance requires:

1. Capture status, review status, and project-memory status are independent.
2. An otherwise valid close never waits for learning review or successful
   experience extraction. Missing capture is visible, not inferred as empty.
3. Retrying a report, close, review dispatch, or save cannot duplicate evidence
   or inflate corroboration counts.
4. Worker observations are not treated as verified causes or binding rules.
5. Reports and reviews do not activate instructions or edit project code.
6. Applicable guidance actually reaches selected workers; supplying it is not
   recorded as proof of use or effectiveness.
7. Coverage, review yield, delivery, recurrence observations, and overhead are
   queryable with explicit denominators and unknowns.
8. All claimed harness delivery paths are tested at their actual native seams.

## 2. Current machinery and constraints

Authoritative starting points:

| Responsibility | Existing source | Relevant behavior |
| --- | --- | --- |
| Project sessions and retained results | `lib/control/session_hub.py` | SQLite hub records, optional terminal reports, retained structured results, generation-bound actors. |
| Close request and Memory publication | `lib/control/session_closure.py`, `hub_cli.py` | Final-turn request, digest-checked handoff, explicit no-update outcome, retained failure states. |
| Structured utility execution | `lib/control/session_store.py`, `sessions.py`, `session_harness.py`, `session_child.py` | Existing session/turn/request records and native permissions. Trace their actual supervisor dispatch call sites before editing. |
| Scheduling/admission | `lib/control/orchestration/supervisor_daemon.py`, `lib/control/runtime.py` | Existing supervisor owns structured execution; terminal jobs do not require it. |
| Project-memory authority | `plugins/session/tools/memory_v2.py`, `save_none.py` | Coherent reads, validation, locking and recovery journal. Ordinary save publication currently lacks required caller preimages. |
| Learning lifecycle | `plugins/session/tools/learnings_manager.py`, `save_identity.py` | Candidate/active/retired files; evidence deduplication; three sessions across two projects; explicit-save identity. |
| Save procedure | `plugins/session/commands/save.md` | Model-directed publication and up to three candidates, not a measured review of all worker results. |
| Context delivery | `plugins/session/hooks/handlers/session-start.sh` | Plain workers skip automatic Asha context. Rooms receive project context and active learnings. |
| User-facing operation | `plugins/session/skills/operate-control/SKILL.md`, `lib/control/session_tui.py` | Ordinary session operation and retained status without mandatory initiative stages. |

Read `AGENTS.md`, current Memory, `docs/session-hub.md`,
`docs/memory-architecture.md`, and `docs/harness-enforcement.md` before editing.
Inspect the scoped history of `b99863b` (bounded Memory v2 replacing the older
pipeline) and `bd56dc6` (session-first hub and graceful close). Do not recreate
the removed transcript parser, pattern analyzer, recall index, or nudge-metric
pipeline under another name. Historical plans are context, not current behavior.

### Non-goals and hard boundaries

- No transcript-store scraping, terminal-screen analysis, simulated keystrokes,
  lifecycle-generated semantic summaries, or periodic Memory saves.
- No mandatory initiatives, new database service, new supervisor, or orchestration
  framework. Reuse existing session and runtime machinery.
- No automatic active-learning promotion, model-weight training, autonomous
  harness edits, or changes to identity files.
- No automatic worktree creation or isolation claims for ordinary workers.
- No repository-wide knowledge/retrieval redesign. Start with explicitly selected
  active learning IDs and scoped assignment context.
- No rewrite of legacy initiatives or historical reports. Their existing evidence
  remains intact and is not silently imported as fresh learning evidence.
- No automatic Git actions in capture, close, review, or learning disposition.
  Existing explicit-save Git scope remains separately governed.

## 3. Ownership and policy

### Owners

**Executing agent:** reports observations and evidence from its live context.
Reports may include native-subagent findings, with their source and limits.
The parent harness owns those subagents; Control does not adopt them as sessions.

**Control:** validates actor identity, bounds and retains reports, records factual
delivery/custody state, selects review eligibility, and schedules authorized
utilities. It does not infer a semantic lesson from exit codes or telemetry.

**Reviewer:** assesses only the assigned frozen report and evidence. It returns
an advisory result. It cannot publish Memory, mutate learnings, approve work,
change permissions, or launch additional reviewers.

**Chair/saving agent:** examines reviewed findings and original evidence at an
explicit save, records dispositions, and proposes/corroborates through the existing
learning manager. The Keeper can reject or defer any finding.

### Explicit project policy

Add a small project-scoped policy to existing private Control state, keyed by
stable project ID. Do not require editing a project's tracked files to enable it.

| Mode | Effect |
| --- | --- |
| `off` | Existing behavior; no new experience request or automatic review. Initial default for existing and new projects. |
| `capture` | Request capture during graceful close; accept optional completion reports; retain selection decisions without launching reviews. |
| `review` | Capture plus bounded automatic review of selected reports. Explicit operator opt-in authorizes these utilities, not permissions or publication. |

Policy changes have a revision and do not automatically replay old reports.
Provide an explicit bounded backfill operation over inspected report IDs if needed.
Disabling review stops new dispatch and cancels/defer-marks only owned learning
review utilities, never ordinary workers. Re-enabling does not blindly replay an
uncertain attempt. Record the policy revision used for each decision.

Proposed pilot defaults: one automatic review concurrently, at most five launches
per project per UTC day, one execution turn per report/policy revision, a 300-second
wall-clock limit including permission waits, and a deterministic 10% sample of
routine reports. These are conservative cost settings, not validated effectiveness
thresholds. Runtime admission may further restrict them. If a native token cap is
unavailable, do not pretend to enforce one; record actual usage when supplied and
otherwise unknown. Daily admission must be reserved transactionally.

`Work/markers/silence` suppresses new experience-content capture, review, adoption,
and their learning artifacts for that project. Preserve ordinary operational
session/close behavior and existing data. A queued review encountering silence is
deferred; recheck before dispatch and accepting review/adoption output. Do not use
an alternate persistence path to bypass silence. Read-only status stays available.

## 4. Experience contract and storage

### Report v1

Define a strict JSON object, accepted as UTF-8 from an owned, bounded, regular,
symlink-free file. Suggested public shape:

```json
{
  "contract": "asha.session-experience.v1",
  "assessment": "observations",
  "outcome": "partial",
  "summary": "The concrete result and what remains unresolved.",
  "observations": [
    {
      "key": "stable-local-observation-key",
      "kind": "failure-recovery",
      "observed": "What happened, without inferred causation.",
      "explanation": "Proposed cause, explicitly a hypothesis.",
      "evidence_ids": ["test-excerpt"],
      "lesson": {"trigger": "When this situation applies", "action": "Proposed response"},
      "applicability": {"harnesses": ["codex"], "task_kind": "coding", "limitations": "Known scope limits"},
      "uncertainty": "What has not been demonstrated."
    }
  ],
  "evidence": [
    {"id": "test-excerpt", "kind": "agent-attestation", "text": "Bounded relevant output or source excerpt."}
  ],
  "guidance_feedback": []
}
```

`assessment` is `observations`, `none-observed`, or `insufficient-evidence`.
`outcome` is `succeeded`, `partial`, `failed`, or `unknown`; it is the agent's
attestation, not a replacement for hub lifecycle or independent verification.
Define and validate enums for observation kinds, including correction,
failure-recovery, verification-conflict, context-gap, orchestration-failure, and
unexpected-improvement. A lesson is optional. Do not force a failure into a recipe.

Controller-owned envelope fields include report UUID, session ID, generation,
stable project ID, source (`completion` or `close`), delivery key/close-request ID,
timestamps, policy revision, canonical body digest, supplied-guidance versions,
and known harness/model versions. Agent-supplied identity is never authority.
Unknown versions remain unknown; record whether a version was controller-observed
or merely reported. Strictly reject duplicate JSON keys and unknown contract
versions; define permitted optional fields rather than accepting arbitrary data.

Bounds: report JSON at most 16 KiB, at most three observations and four evidence
items. Optional controller-captured evidence excerpts total at most 32 KiB, with
16 KiB per item. Reviewer input, including assignment and evidence, is at most
64 KiB; reviewer output at most 16 KiB. Explicitly identify excerpts and omitted
material. Never silently truncate a claim or evidence record into apparent validity.

### Evidence and trust

Use existing private SQLite storage for bounded report/evidence payloads and
review records. Large code, manuscripts and logs stay in their original artifact
systems; refer to immutable revision/digest identities. A temporary pathname alone
does not establish durable evidence. Copy only explicitly submitted, relevant,
bounded excerpts before accepting their receipt. Do not crawl directories or follow
URLs. Project-file captures must remain within verified permitted roots; session
scratch captures require an exact owned root, not blanket access to `/tmp` or home.

Capture bytes and digest from the same safe read; reject symlinks, FIFOs/devices,
oversized and undecodable input. Reuse existing path and secret-handling machinery.
Known secret-bearing content is omitted/redacted with the omission recorded;
original rejected bytes are not persisted in errors. Scrubbing is best-effort, not
a guarantee that arbitrary content contains no secrets. Keep private evidence out
of global reusable rule text and published repository documentation.

Label provenance: controller-captured source bytes, independently retained
verification, or agent attestation. Copied test-output prose is still an attestation
unless a verified execution record supports it. Review packets delimit task and
evidence as untrusted data, never executable instructions. If current source differs
from captured source, report drift rather than silently reviewing the new version.

### Minimal additive records

Add indexed tables through existing Control database initialization/migration
conventions, not a second database:

- `hub_experiences`: immutable report/envelope/evidence, unique delivery key scoped
  to session and generation; close-request linkage where applicable.
- `hub_experience_reviews`: report digest + policy revision + attempt, selection
  reason, dispatch identity, timestamps, status and bounded reviewer result.
- `hub_experience_dispositions`: finding identity, explicit save/disposition key,
  original observation provenance, target rule/version and completion receipt.

Policy, guidance manifests, and small exposure/outcome records may extend existing
session payloads where indexed querying does not require separate tables. Keep the
schema small, but retain per-assignment/turn history rather than overwriting the
latest exposure. Foreign keys and indexed pagination are required. No automatic
historical backfill, deletion, retention cleanup, or restoration of old snapshots
over newer writes. Report storage volume in status for later retention decisions.

An identical key/body retry returns the existing receipt. A differing body under
that key refuses without overwrite. Corrections create an explicitly linked
replacement report; they do not create independent corroboration. Reports from
resumed generations remain separate records with the same source-session lineage.

## 5. Completion and close integration

Extend existing interfaces; these spellings are the intended public contract:

```text
asha control session report --state finished --text RESULT --experience-file FILE --key UUID
asha control session handoff --request ID [existing memory arguments] --experience-file FILE
```

The report flag is optional and additive. Legacy callers remain valid. A completed
structured utility may supply the same optional envelope through its retained
result path; ordinary free-form results must not become malformed because they lack
it. Select structured envelopes explicitly, not by guessing JSON out of prose.

Support `--experience-ref REPORT_ID` instead of a new file to attach an unchanged
report, with scope/ownership validation. Explicit corrections use a new delivery
key and a validated `supersedes` reference; define their CLI spelling consistently
for completion and close. Existing immutable bytes survive. After close, the old
worker cannot submit a correction; a later operator-reviewed report must identify
itself as a later observation, not impersonate the closed actor.

With policy enabled, graceful close asks for one final bounded assessment. Include
the previously captured report receipt, when present, so the worker can reference
unchanged findings rather than invent a new retrospective. `no-durable-update` for
project Memory is independent of whether a reusable observation exists.

### Transaction and failure ordering

1. Validate the current actor, generation and close request using existing proofs.
2. Validate/capture the optional report and return/persist its receipt under the
   source identity. Capture can survive a later Memory publication failure.
3. Perform the current Memory CAS handoff and record acknowledgement. Do not
   claim an atomic transaction spanning SQLite and the two Memory files.
4. Persist/link review eligibility with capture so a crash cannot lose the need
   for review. A deterministic reconciler can derive missing eligibility records.
5. Permit existing close finalization after valid Memory acknowledgement; review
   may run later, independently of the worker and chair terminals.

Invalid/missing optional capture records `invalid`/`missing` with bounded reason,
but does not invalidate otherwise valid Memory arguments or acknowledgements.
Persistence failure must not be reported as successful capture. If the Control
database itself cannot record the close acknowledgement, existing close failure
behavior still applies; this plan does not bypass that authority boundary.

Keep `capture: captured|none-observed|insufficient-evidence|missing|invalid|disabled`
separate from closure state. A forced/unreachable closure records missing capture
only when capture was requested; it never manufactures a report. Existing reports
can be reviewed with their earlier coverage boundary. Late receipts cannot update
a replacement generation or a different close request.

Report corrections cancel/supersede pending reviews of the old digest. A completed
review remains historical and cannot authorize adopting the replacement report.
On ambiguous partial completion, inspect current receipts and exact digests before
retrying. Never revert later Memory or restore an earlier database snapshot.

## 6. Conditional review and execution

Selection is cheap and deterministic over submitted fields and retained operational
facts. Trigger on corrections, conflicting verification, failure/recovery,
context/handoff problems, or an explicit proposed improvement. Missing capture is
a coverage gap, not automatic permission to reconstruct a transcript. An operator
can request investigation of a consequential gap separately.

Sample routine reports using a controller-issued stable ID and policy revision,
not agent-selected wording; retain sample assignment across retries. `none-observed`
reports are eligible for this sample. Record `not-selected`, `disabled`,
`insufficient-evidence`, `budget-deferred`, and `selected` distinctly. Rule-based
selection will miss some unreported events; the sample estimates that weakness.

Review through an existing Claude structured utility first; Codex may be enabled
only after equivalent contract/permission tests. Reports from all four worker
harnesses use the same reviewer-independent contract. The chair need not be awake.

The review utility is packet-only: bounded frozen evidence supplied by Control,
no project edits, shell/network execution, Memory writes, learning mutations, or
operator authority. Implement the restriction at verified native backend seams,
not merely with a read-only prompt. If a backend cannot enforce the required
tool restriction, mark automatic review unsupported there and retain the report
for chair review; do not silently run a broadly empowered reviewer. Do not add
generic sandbox infrastructure to solve this. Limited evidence can legitimately
produce `insufficient-evidence` for later human/chair inspection.

Use a controller-owned purpose tag such as `experience-review`, preserved across
resume, to exclude review utilities from experience capture and recursive dispatch.
Strip unrelated Room/coordinator authority as existing worker launch does.

Reuse structured session/turn custody and uncertain-delivery recovery. Persist a
review's reserved utility ID before launch; replay that same launch identity after
a lost reply. Do not call operator-restricted public launch functions by spoofing
an actor; use a narrow controller-owned scheduling seam. Review admission/cap
reservation and identity linkage must survive daemon restarts. Permission requests,
timeouts and unsupported restrictions cause bounded deferral/failure, never
automatic approval or another attempt through a different harness.

Automatic review is at most one model turn. Retry requires explicit inspected
recovery, not another automatic proposal/review loop. Reports cannot choose the
reviewer, budget, policy or permission mode. Paused admission is visible and never
resumed as a side effect of close. Terminal close works without the supervisor.

### Reviewer result

Return `supported`, `unsupported`, `insufficient-evidence`, or `no-action` per
observation, binding each result to the frozen report/evidence digest. Include:

- Evidence IDs actually supporting the finding and any contradictory evidence.
- Root cause as inference, with uncertainty and scope.
- Remedy destination: code/test, project decision, harness-specific guidance,
  reusable candidate, or no change.
- Proposed check and expected benefit, including plausible regressions.

Structured execution success is not semantic validation. Malformed output becomes
`review-failed`, not a supported finding. No review edits code or marks task success.

## 7. Explicit save and learning provenance

At explicit save, inspect pending reviewed findings only for the resolved project
plane using paginated reads. Read their evidence before adopting. Do not require
the chair to ingest worker transcripts. Record even a deliberate no-action review.

Allow dispositions `propose`, `corroborate`, `project-decision`, `code-test-followup`,
`reject`, and `defer`, with reason and referenced finding digest. Do not create
issues, code edits, or unrelated files automatically from a disposition. A project
decision enters the current explicit Memory draft only when it is actually binding.

Keep the existing up-to-three new candidate limit per explicit save and its current
session/project guard; a batch of worker reports must not bypass it by switching
source identities. Excess eligible findings remain pending. Save failure, silence,
or missing save identity cannot yield an adopted receipt. Learning failures remain
nonfatal to an already successful Memory publication and are reported accurately.

Automatic review policy is not publication authority. Operator-facing policy,
manual-review and disposition commands retain existing chair/operator restrictions.
An executing worker can submit only its own evidence, not select another project's
policy or adopt global rules through the experience API. Explicit save remains its
existing deliberate publication operation, not a new credential issued by close.

### Separate publisher identity from observation identity

Extend the manager compatibly with optional source provenance: original stable
session/project, report/observation ID, harness/version applicability, evidence
digest, and explicit adopting save identity. Existing files still parse; legacy
evidence remains legacy, never upgraded to independently verified observation.

For hub-origin evidence, count the original hub session lineage and project for
the existing three-sessions/two-projects gate. Resume, a native subagent, a reviewer,
the chair and repeated saves do not create additional source sessions. Deduplicate
origin observations across copied reports when provenance establishes the link;
unknown relationships remain unknown. Session diversity is a corroboration
heuristic, not proof of causal independence or effectiveness.

New automation only queues reviewed findings. Candidate mutation and the existing
eligibility check remain inside explicit save. Preserve contradiction, retirement,
semantic-change resets, and the current activation thresholds. New evidence must
not overwrite active rule semantics. Test contradictions from a source that already
has positive evidence: deduplication must not discard the contrary observation or
allow immediate reactivation from pre-contradiction evidence.

Use an idempotent disposition receipt to bridge Control SQLite and learning files:
retain intent, apply the existing locked manager operation with an origin key, then
complete the receipt. On interruption, reconcile exact semantic/evidence identity;
never replay as new corroboration and never roll back someone else's changes.
Private excerpts stay in Control; global trigger/action text contains only reviewed
portable guidance and safe provenance references.

## 8. Required stale-save protection

Normal `memory_v2.py publish` and `save_none.py` currently do not require the model's
pre-draft baseline. Implement the protection before wiring multi-session adoption.

- Add coherent snapshot digests to a read interface used before drafting.
- Require `--expected-active` and `--expected-decisions` on ordinary and scope-none
  user-facing publication. Missing/stale digests refuse with re-read/merge guidance.
- Thread them into the existing locked `expected_preimages` comparison. Computing
  a new baseline after the draft exists is not acceptable protection.
- Keep initialization/migration special cases explicit and audited; do not break
  their existing reviewed-preimage and creation contracts.
- Produce the publication receipt from the validated bytes while publishing.
  A later concurrent publication is `superseded`, not proof that the earlier save
  never succeeded. Keep transaction success separate from post-publication reads.
- Update every affected caller, rendered skill, document and test. Preserve
  scope-none's no-Git contract, private ignores, lock/journal recovery, and silence.

Verify with two publishers that draft from the same baseline: the first succeeds,
the second refuses, and a deliberate re-read/merge succeeds without losing either
contribution. Also test a newer publication arriving between commit and receipt
verification. Do not use Git/jj operations as an implicit Memory concurrency lock.

## 9. Guidance delivery and measurement

Extend launch/continuation input with explicit active-learning IDs selected by the
chair. Render a bounded guidance block (maximum three rules and 3 KiB) into the
actual worker assignment. Resolve active versions at delivery, reject stale,
candidate, retired or explicitly incompatible selections, and return exclusions.
Do not inject persona, global Memory, or every active learning into plain workers.

Applicability is typed where reliable (project, harness, version range if known),
with task limitations retained in text. Explicit chair selection handles semantic
task relevance in v1. Unknown version applicability is flagged, not guessed.
Later startup retrieval redesign is out of scope.

The optional `guidance_feedback` report field links to supplied learning
ID/version pairs and records `applied`, `not-applied`, `not-applicable`, or
`unknown`, plus evidence IDs and an optional target-failure observation of
`observed`, `not-observed`, or `unknown`. These remain agent attestations unless
separate evidence verifies them. Absent feedback is unknown, not non-use or success.
Reject feedback that claims a different supplied version without marking the
discrepancy. Never infer recurrence from the mere presence of a learning ID.

Retain manifests per launch/continuation: selected, actually supplied, digest/version,
known runtime identity, and delivery status. Queued terminal messages are not supplied
context until the existing read/delivery seam establishes that. Keep supply, agent
reported use, and verified improvement as distinct facts. A new generation gets a
new exposure manifest; do not rewrite prior ones.

Expose inspection commands beneath the existing `session` CLI (for example
`experience list/show`, `experience policy`, `experience review`, `experience stats`)
and a compact capture/review indicator in the TUI. Final names may follow parser
conventions but must be documented and tested consistently. Reads do not launch
work, adopt findings, or mutate policy. Pagination includes completeness markers.

Metrics, scoped by project/policy/time and harness/model when known:

| Metric | Required interpretation |
| --- | --- |
| Capture coverage | Eligible closes with assessment receipts / closes where capture was requested; split missing, invalid, disabled, forced and unreachable. |
| Completion coverage | Explicit task-completion reports, separately from close reports; never assume every idle turn was a completed task. |
| Review coverage | Selected reports, completed reviews, deferred/failed reviews, and unselected reports. |
| Review yield | Evidence-supported actionable findings per completed review, split by remedy destination; candidate count alone is not success. |
| Routine-sample misses | Actionable issues found in sampled routine reports; this does not estimate completely unobserved worker history. |
| Adoption | Pending/rejected/deferred/adopted findings with origin-deduplicated counts. |
| Delivery/application | Applicable selected guidance, actual supply, reported use and unknown use, separately. |
| Recurrence | Observed repeat failures on comparable exposed tasks, plus unknown outcomes and changing task/model conditions. |
| Cost | Review launches, elapsed time and available native usage; unknown token/dollar data stays unknown. |

Do not infer the absence of recurrence from silence. Metrics describe observation
coverage, not causal improvement. Establish a capture-only baseline, then enable
bounded review on selected projects. Use comparable task/harness/version groups
and selected replay cases to evaluate guidance effects; do not build a general
benchmark platform or claim statistical significance from a small pilot.

## 10. Work packages and ownership

Implement in dependency order. A worker can use read-only specialists when the
governing workflow calls for them, but shared-file edits must have one owner. All
workers must preserve concurrent changes. Do not stop midway through a package
without an exact remaining-work and verification report.

| Package | Scope / likely files | Completion proof |
| --- | --- | --- |
| A: Baseline and contracts | Read sources/history; define strict contracts and policy; new focused `lib/control/session_experience.py` is appropriate if needed. | Failure fixtures for bounds, provenance, retries, absent capture and state separation exist before implementation. |
| B: Storage and capture | Hub schema, `session_experience.py`, `session_hub.py`, `hub_cli.py`; close prompt and handoff linkage. | Durable capture survives close/restart; invalid optional reports never change valid Memory acknowledgement semantics. |
| C: Review selection/execution | Experience module, existing structured-session/supervisor seam, narrow backend restriction, policy. | At-most-once reserved dispatch, enforced tool restriction, budgets/admission, nonrecursive review and malformed-output tests. |
| D: Save concurrency | `memory_v2.py`, `save_none.py`, all publish callers, save command. Can follow B independently of C. | Lost-update reproduction and CAS/receipt tests pass; no-Git/silence/migration regression tests pass. |
| E: Disposition/adoption | Learning manager, save identity integration, save command, experience receipts. Depends on B, C and D. | Replay/cross-store interruption tests; source-session dedup; candidate limits and contradiction/legacy parsing tests. |
| F: Guidance and visibility | Hub launch/continuation, operate-control skill, CLI/TUI, metrics. Depends on B and E for end-to-end proof. | Recorded supplied guidance; no blanket worker context; metrics match fixture denominators; narrow-screen rendering works. |
| G: Cross-harness release verification | Canonical instructions, adapters if affected, capabilities, doctor/drift checks, docs, changelog. | Full suites and isolated native probes; independent diff review, followed by review of any fixes. |

Do not move implementation into generated files under a user's home. Update the
canonical corpus and regenerate through existing installers only inside fixtures.
If a primitive changes, update installer, doctor checks and tests for every affected
harness. Do not claim a prompt-only read-only instruction is enforcement.

## 11. Acceptance scenarios and verification

Required scenarios:

1. Claude terminal close with report and published Memory; report survives process
   termination and is reviewed without another chair turn.
2. Close with `no-durable-update` but a reusable observation; capture/review proceed
   independently of the unchanged Memory digests.
3. Routine close with `none-observed`; sampled/not-selected state is explicit.
4. Missing, oversized, secret-bearing, malformed, path-unsafe or invalid reports;
   valid close still completes and the capture gap is visible.
5. Memory CAS conflict after durable capture; corrected Memory retry does not
   duplicate the report, review or original observation.
6. Repeated close, stale attempt/generation, late report, concurrent SessionEnd,
   forced close, unavailable worker and supervisor outage preserve honest states.
7. Codex/Copilot/OpenCode terminal reports use their real queued/native seams;
   idle terminals are not simulated into receiving a final turn.
8. Structured completion and close retain optional reports; plain results and
   existing structured requests/permissions remain compatible.
9. Lost launch reply, daemon restart, daily-cap contention, denied permission,
   timeout and uncertain review submission cause no duplicate automatic execution.
10. Reviewer tool-use attempts are refused by the supported native configuration;
    unsupported backends defer. Review outputs cannot invoke publication or recursion.
11. Policy disabled/silence before capture, after queueing, during review and before
    adoption prevents new learning-content persistence without stopping user work.
12. A worker observation reviewed by another harness and adopted twice still counts
    as one source observation/session. Distinct resumes/subagents cannot satisfy
    activation. Genuine cross-project evidence, contradictions and retirement work.
13. Save without any findings, save with deferred findings, the three-candidate cap,
    legacy files, manager failure and interrupted disposition receipts remain valid.
14. Applicable guidance is supplied on a later worker assignment; incompatible or
    no-longer-active guidance is excluded and reported. Supply is not claimed use.
15. State/metrics remain accurate with pagination, unknown telemetry, superseded
    reports, multiple completions per session and narrow TUI dimensions.

Start with the relevant existing tests and new focused tests. Likely suites:

```bash
cd ROOT && python3 -m unittest discover -s tests/python -p 'test_control_session_closure.py' -v
cd ROOT && python3 -m unittest discover -s tests/python -p 'test_control_session_hub.py' -v
cd ROOT && python3 -m unittest discover -s tests/python -p 'test_memory_v2.py' -v
cd ROOT && python3 -m unittest discover -s tests/python -p 'test_learnings_manager_v2.py' -v
cd ROOT && ./tests/test-session-hub-hooks.sh
```

Also run the actual affected managed-save, structured-session, admission, actor,
database, TUI, installer and doctor tests discovered in this checkout. Before
declaring cross-harness work complete:

```bash
cd ROOT && ./tests/run-tests.sh
cd ROOT && ./bin/asha-drift-check.sh --target codex
cd ROOT && ./bin/asha-drift-check.sh --target opencode
```

Use isolated `ASHA_HOME`, native harness homes and throwaway initialized projects
for fixtures and live probes. Test scripts may invoke installers: inspect their
fixture isolation first. Native probes that spend inference or need new permissions
are a separate explicit approval; unavailable probes are outstanding acceptance,
not passing evidence. Do not install into live home to make drift checks green.
Report installed-source drift separately from candidate fixture results.

For changed public names/contracts, run a scoped-to-repository search for superseded
invocations and show command/output, classifying intentional compatibility/negative
test references. No stale executable caller may remain. Run Git and jj from the
repository root in the same command. No commits or pushes without a later directive.

## 12. Rollout and worker handoff

All packages are in implementation scope, even though activation is staged:

1. Ship dormant functionality with policy `off` and complete tests.
2. On the Keeper's later word, enable `capture` for selected projects and collect
   baseline receipts without review inference or automatic adoption.
3. Explicitly enable `review` for the pilot; inspect sampling, useful findings,
   latency, cost and deferrals before expanding.
4. Adopt through explicit save and verify later guidance delivery. Revisit sampling
   and budget defaults from evidence, not candidate volume.

Rollback is policy disablement plus stopping only owned review utilities. Retain
reports, reviews, learning files and user sessions. No destructive schema downgrade,
bulk deletion or database snapshot restore. Existing close semantics remain usable.

The implementation worker's final handoff must include:

- Source revision, preserved pre-existing changes and actual changed-file list.
- Package completion matrix, exact tests/results, and remaining native probes.
- End-to-end fixture evidence for capture -> review -> explicit adoption -> supply.
- Evidence that close does not wait for review, scope-none/close invoked no Git,
  and no live home or user session was modified.
- Security/enforcement claims with their tested limits; reviewer findings and any
  follow-up fix-review results, not merely an overall green summary.
- Unresolved decisions or deviations from this plan, with reasons.

Worker prompt:

> Implement `docs/proposals/2026-09-12--session-experience-learning.md` in this
> repository. Read current AGENTS.md and the authoritative sources named in the
> plan; verify the current working copy before editing. Preserve others' edits.
> Follow the dependency order, test failures before fixes, and independently review
> lifecycle/security changes and subsequent fixes. Implement all packages but leave
> live policy off. Do not commit, push, install into live home, launch paid native
> probes, or alter user sessions without separate approval. Return the required
> completion matrix and exact evidence, with unsupported probes left outstanding.
