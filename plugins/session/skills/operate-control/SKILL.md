---
name: session-operate-control
description: "Launch, inspect and steer project harness sessions from Asha's conversational chair. Use for independent jobs, ongoing project Rooms, short result-returning utilities, or session status and input handling."
---

# Operate project sessions

The chair handles conversation, memory and project knowledge. A worker handles
its assignment using its native harness and project instructions. The hub
retains identity, status, messages and results. Ordinary work needs no
initiative, plan, approval summary, or per-turn report.

## Choose the useful scope

Work directly in the chair for small tasks and questions. Read source and use
native subagents when helpful. Launch another harness when independent work,
another project context, or a persistent conversation benefits the Keeper.

First read `asha control session list --json`. Summarize work and requests
briefly; qualify `complete: false`. No record means no observed work, not proof
that no unmanaged harness exists. Resolve a project through
`asha control projects --match NAME --json`, or its known canonical path. Ask
once for ambiguous projects; ordinary sessions do not require jj.

```bash
asha control session launch --project PROJECT --prompt ASSIGNMENT --harness claude --json
asha control session launch --project PROJECT --prompt TOPIC --harness codex --profile room --json
asha control session launch --project PROJECT --prompt QUESTION --harness claude --transport structured --json
```

Add `--model MODEL` and/or `--effort LEVEL` only when the Keeper or the task
calls for a tier; omitted means the harness default. One standing exception: a
**mechanical lookup** may launch on a cheaper model (for example
`--harness claude --model sonnet`). A mechanical lookup locates files, reads
logs or records, or lists and counts, and you check its output against the
source before using it. Anything that draws conclusions does not qualify:
investigation, review, design and fixes stay on the default. Otherwise never
pick, escalate or retry with another model on your own. `show` reports the
requested and effective values with their provenance (docs/session-hub.md,
"Model and effort").

The default **worker** omits Asha's persona, operational injection and automatic
memory work, preserving native permissions, project instructions and installed
skill discovery. **Room** is an interactive Asha project conversation.
**Structured** utilities support Claude and Codex; their backend owner exits
between turns and completed utilities leave the default list. Their results
remain available with `list --all` and `show ID`. Terminal workers also support
Copilot and OpenCode.

Worker launch, resume and send automatically select compatible active learnings
when no `--learning` is supplied: project scope, harness scope, source-session
evidence count descending, then rule ID; at most three rules and 3 KiB.
Repeated `--learning ID` selects explicitly; `--no-learning` supplies none.
Rooms retain their existing context. Supply does not establish use or benefit.

Retain the returned ID. For a lost-response launch retry, reuse
`--session-id UUID` and the original arguments. Never create a replacement
conversation just to deliver an answer. An interrupted launch is retained for
inspection; an error does not prove that no process ran.

## Inspect and communicate

```bash
asha control session list --json
asha control session show ID --json
asha control session attach ID --json
asha control session send ID --text CONTEXT --key MESSAGE_UUID --json
```

Terminal attachment returns an exact verified tmux target. The dashboard opens
it on Enter. Never use screen text to decide permissions or type into native
prompts through tmux. The human can use the attached harness.

Terminal messages remain **queued** until the worker reads them
(`delivery: queued-until-read`); reading is not acknowledgement.
Never type into a pane yourself. Say when attachment is needed. Structured messages are retained for an eligible turn boundary;
do not promise mid-turn steering or consumption. Reuse a message key and
identical body when retrying a send.

Structured questions and permissions use the existing request interface:
`session current --kind requests --json`, then `session request REQUEST_ID
--json`. Answer the inspected question with `session answer REQUEST_ID
--digest DIGEST --text ANSWER --json`. Native tool decisions use `session
permission REQUEST_ID --digest DIGEST --decision allow|deny --json` on the
Keeper's actual decision. Do not add approvals to already authorized work;
do not substitute an assignment or clarification answer for a native tool
decision. Dashboard `a` opens the selected session's input request.

Terminal workers may optionally report meaningful state:

```bash
asha control session report --state needs-input --text QUESTION
asha control session report --state finished --text RESULT
asha control session messages
asha control session ack-message MESSAGE_ID
```

These commands act for the session their environment names, in its current
generation only. Workers need not report each turn. A terminal row keeps two
axes: `observed` from native hooks (`launched` until the first event, then
`working`, `waiting`, `ended` or `unknown` when missing or stale) and `report`
from explicit reports (`needs-input` or `finished`). `idle` means a native turn
ended, and `finished` means an explicit worker report, or a completed
structured utility. A finished report whose observed turn still runs reads
`working` with `reported_activity: finished` (`Working: reported finished`). A
close terminates at once only when a clean Stop emitted after the report ended
that turn; otherwise it asks and waits (always for Copilot, which has no
turn-end event). A later turn with no new report (a background notification
waking the worker) keeps the report and reads finished again when it ends.
Silence proves none of these. Read results
before conveying them as conclusions. Message pages expose `complete` and
`next_offset`; use `messages --offset N` to read subsequent pages.

When effective experience policy is enabled, a finished report without an
assessment returns a bounded request and controller `--key`. Follow up with
`report --state finished --experience-file FILE --key KEY` to attach capture
without replacing the task result. Identical retries reuse the receipt.
An unanswered request followed by exit records `exited-before-capture`.

## Close, stop and resume

`session stop ID` stops the owned process now and retains history. `session
close ID` is best-effort: it asks the session to save project Memory, waits a
bounded time (`--wait SECONDS`, default `control.close_wait_seconds`, 60), then
terminates it, attached terminal included. `close ID --force` is a zero wait.
The request reaches a working Claude session as its Stop-hook decision and an
idle or unobserved terminal as one typed pointer line to `session messages`; a
working Codex session gets the pointer once observed idle; a structured session
gets it as its next turn. The close ends at the first save after the request,
at process exit, or at the deadline; the row then reads `Closed, saved HH:MM
UTC` or `Closed, unsaved` (unsaved only when the generation has no save at
all). Dashboard `x` records the request and hands the wait to a detached
waiter; re-running `close` joins a pending request and finalizes an expired
one. A finished report that is still current and settled plus a save for the current
assignment closes at once. The handoff never commits or pushes; landing code
remains a separate explicit decision. Dashboard `q` only exits the UI; it does
not stop workers or the supervisor. `M` filters input requests; `A` includes
retained history.

Rooms that explicitly saved after their latest assignment in the same generation
omit the close experience assessment (`explicit-save-published`); an attestation
does not. Treat `memory_saved_at` and `closure.guidance` as evidence; never infer
a save from prose.

Worker assignments include the project-memory startup/completion contract across
harnesses without chair context. `report --state finished` is never gated; the
row reads finished, saved or unsaved. Publication blockers use `handoff
--outcome blocked`; silence or scope refusal is not a no-update attestation.
Structured paths bind to their managed turn; native delivery and permissions
remain separately qualified.

Experience policy resolves project override, user `session_experience.default_mode`
in `~/.asha/config.json`, then builtin off. `experience policy --read-only --project
PROJECT --json` inspects mode/source/revision; `--mode MODE` changes it in one
transaction, optional `--revision` uses compare-and-set, and `--clear` follows the
default again. Policy mutations remain chair/Keeper-only. No config opens the
native review release gate. Explicit save reviews at most five selected unreviewed
reports after publication, excludes its own lineage and records advisory-save-review
results before disposition. Verified Rooms can review/dispose only their own
project, with a retained explicit-save receipt for disposal. Follow the save skill;
skip experience reads when off or silenced. Writes retain native approvals.

`session resume ID --text CONTINUATION` preserves a terminal session's hub
identity. Claude/Codex reuse a captured native conversation ID when available;
otherwise the response explicitly says it starts with fresh continuation
context. Never describe that fallback as recovered conversation memory.

For failed or stopped structured utilities, inspect `show ID` and use its
`recovery_digest` with `resume ID --digest DIGEST --text CONTINUATION`.
The digest prevents resuming different state than the one inspected. Uncertain
submissions are never automatically replayed. The dashboard shows retained
recovery state before accepting a continuation.

Structured work uses the existing supervisor and runtime admission setting.
A paused runtime retains the assignment; report that fact rather than silently
resuming it. Ordinary terminal sessions run independently of the supervisor
and dashboard. A failed telemetry hook must not block native work.

## Gotchas

- Launch, send and the operator verbs refuse a worker-profile session and a
  Codex Room ("cannot perform session operator actions"). The chair keeps them
  on every harness; on Codex they run through a sandbox escalation the Keeper
  approves.
- Launch refuses a project without initialized Memory v2 ("not exactly one
  initialized Memory v2 Asha project"). For a scratch probe, run `memory_v2.py
  init --project-dir DIR` and commit it before launching.
- A dropped hook report leaves a row stale (lost reports are logged in
  `~/.asha/state/control/hub-lost-events.jsonl`; none is delivered late). After
  a dropped `turn-stopped` a finished report reads `Working: reported finished`,
  then `Finished` once stale, and a close waits its bound instead of closing at
  once. Confirm from the pane before acting on the row.
- Copilot has no turn-end hook: a Copilot row stays working after its turn until
  the next event or the staleness rule, and its close always asks and waits.
- Codex and Copilot ask to trust a new folder before their first turn; the row
  shows no activity (Codex: "Hooks not reporting") until someone answers in the
  pane. Accepting Codex's "remember" choice writes to the live
  `~/.codex/config.toml`.
- A worker running one long command (a full test suite) emits no events, so the
  row drifts to `unknown`/"Inspect session". Check the process before treating
  it as stuck.

## Retired initiatives

The legacy initiative engine and `asha task` were retired on 2026-10-05. Their
records are read-only evidence: `asha initiative list|show [--json]` reads
them and `asha initiative export` writes every Control record as JSON Lines.
Never migrate, resume or delete them; new staged work runs as workers and
Rooms.

## Session experience evidence

Capture and Memory close outcomes are independent. Workers may add
`--experience-file FILE --key UUID` to finished reports, or add an experience
file/reference to a close handoff. Corrections use `--supersedes REPORT_ID --key
NEW_UUID`. Never reconstruct transcripts for missing capture.
`experience list/show/packet/pending/unreviewed/guidance/stats` inspect retained
evidence. Guidance is selected automatically unless `--learning ID[@DIGEST]` or
`--no-learning` overrides selection, within the three-rule and 3 KiB limits.
Check exclusions and delivery manifests; queued context is not supplied context
and supplied guidance is not proof of use.
When `session messages` returns `delivery_digest`, acknowledge that exact body
with `session ack-message MESSAGE_ID --delivery-digest DIGEST`. Ordinary
acknowledgements without the optional digest leave guidance supply unknown.
Review utilities are single-turn and advisory. Native automatic review is gated off
until the outstanding approved enforcement probe; unavailable backends remain
unsupported. Explicit-save disposition is the only adoption path. Read
`docs/session-experience.md` for contracts, silence, budgets and recovery.
