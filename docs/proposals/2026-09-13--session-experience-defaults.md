# Session experience: automatic defaults and a working loop

Status: worker-ready amendment, not implementation or deployment. Prepared
2026-09-13 by the chair against master `1d1cda2` (dormant session experience
learning) and the live working copy. Amends
`docs/proposals/2026-09-12--session-experience-learning.md`; where the two
conflict, this document wins. Revalidate every cited line before editing.
No commit, push, installation, live policy change, or paid native probe is
authorized by this document.

## 1. Problem

The shipped feature is correct in custody and bounds, but its loop does not run
without operator ceremony, and with the native review gate closed it does not
run at all. Findings from reading `1d1cda2`:

| Gap | Evidence | Effect |
| --- | --- | --- |
| G1. Per-project opt-in only | `lib/control/session_experience.py:317-319` returns `off`/revision 0 for any project without a row; no user-config default. | Every project must be switched on by CLI with a compare-and-set revision, for a single operator. |
| G2. Workers are never told about completion capture | Worker brief at `lib/control/session_hub.py:149-151` names `report --state finished` but not `--experience-file`. Only the close request asks (`session_closure.py:155`). | A worker that finishes and exits without a Control graceful close yields no capture. Session `543c5c1b` (2026-09-13) exited this way. |
| G3. Capture-only reports never reach save | `experience_adoption.py:16-43` `pending()` lists only `hub_experience_reviews` with `status='completed'`. Native review is hard-gated (`experience_review.py:24`). | In `capture` mode, and in `review` mode until the paid probe passes, `/session:save` always sees zero findings. |
| G4. Save inside a Control Room cannot adopt | `experience dispose` and `review --result-file` call `Experiences.operator()` (`experience_adoption.py:71`, `experience_review.py:228`), which refuses any `ASHA_HUB_SESSION_ID` (`session_experience.py:297`). Control Rooms carry that variable (`rooms.py:767`). | `save.md` step 6 fails in every Control Room; adoption works only from the chair or a non-hub session. Inferred from code; confirm with a failing test. |
| G5. Plain workers receive guidance only by chair selection | `session_guidance.resolve` uses `learning_ids` from launch; worker SessionStart exits early (`session-start.sh:9`). Rooms already receive active learnings (Claude SessionStart `session-start.sh:101-107`; wrapper layer `bin/asha:195,301,399`). | The sessions least attended by the Keeper are the only ones that never get learned guidance by default. |
| G6. Rooms that saved are still asked for a close assessment | Close request text appends the experience request whenever policy is on. Publication receipts carry no hub session identity (`plugins/session/tools/memory_v2.py:370-373`). | Duplicate reflection for Rooms that already ran `/session:save`. |

Observed in live use on 2026-09-14 (thorne Room `af8ed095`, Codex):

| Gap | Evidence | Effect |
| --- | --- | --- |
| G7. Graceful close cannot reach an idle non-Claude terminal session | `session_hub.py` `_deliver` queues the request as a hub message; only Claude has the Stop-hook channel. `_resume` already reuses the captured native conversation ID for Claude and Codex. | "Save and close" from the chair stalls until the Keeper attaches. The chair completed thorne's save only by `session stop` then `session resume --text`. |
| G8. Every Codex save prompts for the experience read | The `save.md` steps added in `1d1cda2` run `experience pending`; the Control database lies outside Codex's sandbox. With policy `off` the result is always empty. | Three native approvals for one save (experience read, Memory commit, `session report`); the first carries no information. |

## 2. Changes

Implement in this order. Test failures first for each change.

### C1. User-level default policy (G1)

- Add an optional user config key in `~/.asha/config.json`, read through the
  existing user-config loader (`lib/control/orchestration/projects.py:200-208`):
  `"session_experience": {"default_mode": "off"|"capture"|"review"}`.
  Absent or invalid key means `off`; invalid values are reported by
  `asha doctor`, never silently coerced to an enabled mode.
- Resolution: project row, else user default, else `off`. `policy` reads return
  `source: "project"|"default"|"builtin"` alongside `mode` and `revision`.
- `review` from any source still resolves through the native release gate;
  config cannot open it.
- `policy --mode M` without `--revision` performs read-and-set in one write
  transaction. `--revision N` keeps compare-and-set. Add `--clear` to delete the
  project row so the project follows the default again.
- Operator restrictions for policy changes are unchanged (chair or Keeper shell).

### C2. Ask at completion (G2)

- When effective policy is not `off`, the worker brief names the experience
  option and the report contract in one bounded paragraph.
- When a session reports `--state finished` without experience and effective
  policy is not `off`, the command's synchronous output includes a bounded
  experience request (contract, limits, the exact follow-up command, and a
  controller-issued `--key`). This reaches the agent in-turn through its own
  tool result; no idle terminal is poked.
- A follow-up `report --state finished --experience-file FILE --key KEY` with
  the issued key attaches capture without replacing the recorded result text.
  Retrying the same key and body returns the existing receipt.
- When a session exits after an unanswered completion request, record capture
  `missing` with reason `exited-before-capture`. Never manufacture a report.
- Structured utilities and `experience-review` utilities are out of scope.

### C3. Save-time advisory review (G3)

Make the loop complete without the native gate, using the saving agent that
already holds adoption authority.

- Add a paginated read, `experience unreviewed --project P`, listing selected
  reports without a completed review for the current policy revision, with
  selection reason.
- In `/session:save`, when effective policy is not `off` and the project is not
  silenced: after publication, review at most five unreviewed selected reports
  (oldest first) by reading `experience packet REPORT_ID` and recording the
  result with the existing `experience review --report REPORT_ID --result-file
  FILE`. Then run the existing pending/dispose step. Excess reports remain for
  the next save.
- The saving agent must not review a report whose source session is its own
  session lineage; such reports are skipped with a recorded reason.
- The advisory result carries the existing reviewer-result contract and is
  labelled `advisory-save-review`, distinct from native automatic review, in
  stats and in adopted provenance.
- When the native gate later opens, automatic review takes precedence; save
  reviews only what remains unreviewed.

### C4. Adoption from Rooms (G4)

- Permit `experience review --result-file` and `experience dispose` from a
  terminal hub session whose profile is `room`, scoped to that Room's own
  project, with a valid explicit-save publication receipt for dispose.
- Workers, structured utilities, managed actors, coordinators, and worker
  ancestry remain refused exactly as today.
- Policy changes remain chair/Keeper only.
- Test that a Room cannot review or dispose findings from its own session
  lineage, cannot act on another project, and that the worker-ancestry refusal
  still holds.

### C5. Automatic guidance for workers (G5)

This reverses the earlier plan's "explicit chair selection only in v1."

- When a worker launch, resume, or send has no `--learning`, resolve active
  learnings compatible with the project and harness through the existing
  `session_guidance` path and its exclusions. Unscoped rules are compatible.
- Deterministic order when more than three match: project-scoped, then
  harness-scoped, then source-session evidence count descending, then rule id.
  Existing limits hold: three rules, 3 KiB.
- `--learning ID` selects explicitly and disables automatic selection.
  `--no-learning` supplies none.
- Manifests record `selection: automatic|explicit|none`. Supply is still not use.
- Rooms are unchanged.

### C6. No duplicate close assessment for Rooms that saved (G6)

- Publication receipts gain optional controller-derived `hub_session_id` and
  `hub_generation` when publishing inside a hub session, taken from the
  verified actor, never agent input.
- Control records explicit-save publication receipts per session generation.
- The close request omits the experience request when that generation has an
  explicit-save publication after its latest assignment. Capture records
  `disabled` with reason `explicit-save-published`.

### C7. Wake idle sessions for graceful close (G7)

Coordinate with issue #92, which specifies a verified completion receipt that lets
an already-finalized idle worker close without another model turn. Where #92's
receipt exists, it supersedes C6's publication linkage and no wake is needed. C7
covers idle sessions without a qualifying receipt. Dashboard hints for these
states are issue #93.

- When a close request targets a terminal session that is observed idle, has no
  Stop-hook channel, and supports native resume, Control stops the owned process
  and resumes the same native conversation with the close request text as the
  continuation. The close record keeps one request identity across the resume.
- Never type into a pane or read screen text. Unknown activity, harnesses
  without native resume, or a failed resume leave the request `unanswered` and
  say that attachment is required.
- Tests: idle Codex close resumes with the request and reaches `acknowledged`;
  a working session is not stopped; Copilot/OpenCode report attachment required.

### C8. No experience read on saves that cannot use it (G8)

- `save.md` gates both experience steps on a single cheap policy read, skipped
  entirely when effective policy is `off` or the project is silenced.
- The Codex rules rendered by the installer pre-approve the read-only
  `asha control session experience policy|pending|show` commands, so an enabled
  project does not add an approval to every save. Writes (`dispose`, `review
  --result-file`, `report`) keep native approval.

## 3. Workflow after this amendment

```text
Keeper, once:   ~/.asha/config.json session_experience.default_mode = capture

Chair launch:   worker assignment + up to 3 matching active learnings (C5)
Worker:         does the task
                report --state finished  -> reply asks for assessment (C2)
                report --experience-file -> captured, selected or not
   or Keeper:   session close            -> close request asks (Rooms that saved: skipped, C6)
   or exit:     capture recorded missing

Supervisor:     native review, only after the gate opens

Next /session:save in that project (chair or Room, C4):
                publish Memory
                review up to 5 unreviewed selected reports (C3)
                dispose pending findings: propose/corroborate/.../reject/defer
                at most 3 new candidates

Learnings:      candidate -> active after 3 sessions across 2 projects
Delivery:       Rooms at start (existing); matching workers at launch (C5)
```

Keeper-facing surface: set the default once, run `/session:save` as today,
optionally read `experience stats`. No per-project switching, no manual review.

## 4. Verification and constraints

- Failing tests first for C1-C8, including: default resolution and `--clear`;
  config cannot open the native gate; completion request and key reuse;
  `exited-before-capture`; unreviewed listing and five-report cap; own-lineage
  refusal; Room adoption scoped and worker refusal intact; automatic selection
  ordering and `--no-learning`; receipt linkage and close omission.
- Update `save.md`, `operate-control/SKILL.md`, `docs/session-experience.md`,
  and doctor capability text. Keep the earlier proposal as history; add a
  pointer to this amendment at its top.
- Environment hazard (2026-09-10 incident): run every test, installer, drift
  check, or probe under an explicit scrub, e.g. `env -u ASHA_HOME -u ASHA_CONFIG
  -u XDG_CONFIG_HOME -u CODEX_HOME -u CLAUDE_CONFIG_DIR HOME="$TMPHOME" ...`,
  with Asha and native harness homes in a throwaway directory. If any step would
  touch `~/.asha`, `~/.claude`, `~/.codex`, `~/.copilot`, or
  `~/.config/opencode`, stop and report.
- Preserve uncommitted working-copy changes that are not yours. No jj/git
  shaping, commit, push, install, or live config edit. Run git and jj from the
  repository root in the same command.
- Return a completion matrix per change with exact test evidence and any
  outstanding items.
