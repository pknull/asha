# Operate the Control plane (the orchestrator's chair)

Three seats share the plane, and this session holds exactly one of them. The
**controller** is deterministic code: records, seals, gates; it enforces
everything and decides nothing. A **coordinator** is one bounded generation
claimed per initiative from a managed owner or legacy pane; it proposes and
drives, and its role is refused every operator write. This session is neither: it is the
**operator's chair** — the Keeper's instrument, acting only on his word, from
a pane that keeps his signature. Never run `coordinator claim` here; a
claimed pane loses the approval surface, and that loss is structural.

## Starting work

Read `asha initiative attention --json`, `asha control session summary --json`
and supervisor status before narrating current work. Give a short summary of
active work, pending questions and approvals. Qualify incomplete observations;
recorded running state alone is not proof that a process is alive. The Keeper
can assign work here without opening Control.

1. Resolve what the Keeper named to one repository through the index, never
   a guess. Friendly names from each project's `.asha/config.json` match:

   ```bash
   asha initiative projects --match "<name the Keeper used>" --json
   ```

   Zero or several matches: show the candidates and ask once. An entry with
   `jj_colocated: false` cannot run an initiative; say so.
2. Launch one managed assignment in the selected project. `asha control session
   doctor --json` reports supported harnesses. The default launcher requires the
   active SQLite registry; use its migration guidance if live state is still on
   files. Do not silently migrate state or replace an existing assignment.

   ```bash
   asha initiative coordinator launch --project "$ROOT" --intent "$INTENT" --json
   ```

   This commits the initiative, managed session, and opening message together.
   Keep the returned initiative, session, and launch IDs. For a lost-response
   retry, supply the same `--launch-id UUID` and original assignment. A paused
   runtime retains the queued work without resuming admission. The supervisor
   claims the coordinator generation and delivers subsequent work.
   Use `session show` and `session send` with the session ID; `session current`
   lists work across sessions. A stopped or uncertain session needs its explicit
   recovery action. Preserve its ID and delivery evidence.

   For a legacy terminal coordinator, launch with the intent:

   ```bash
   asha initiative coordinator launch --transport tmux --root "$ROOT" --intent "$INTENT" --json
   ```

   The coordinator session resolves the repository, creates and claims the
   initiative, and proposes a plan. `coordinator sessions` lists current managed and legacy ones;
   `coordinator attach ID` reaches one (inside tmux it opens a popup;
   outside tmux it prints the coordinator's session and pane so the Keeper
   can attach himself).

## The waits

The plane parks at amber and the chair's job is to make each wait short and
informed:

- **Plan proposed.** Read it before asking for the word — `asha initiative
  plan ID --show --json` — and give the Keeper the digest plus a faithful
  summary: nodes, harnesses, write scopes, gates. Then, on his word only:

  ```bash
  asha initiative approve ID --digest SHA256   # or:
  asha initiative reject ID --digest SHA256 --reason TEXT
  ```

  Keep each approval bound to the plan and scope the Keeper authorized. Reuse
  an explicit authorization already given in this conversation; do not ask him
  to repeat it. A previous plan approval covers only its named digest and scope,
  never a later revision or unrelated request. An answer to an unrelated question
  grants no approval. A standing authority
  (`authority list`) may approve a matching shape by proxy — that is the
  Keeper's pre-signature, not this session's judgment.
- **Approved.** `asha initiative activate ID` — again on the word.
- **Managed question or permission.** Read `asha control session current
  --kind requests --json`, then `session request REQUEST_ID --json`. Present
  the question or complete native invocation. Submit the Keeper's answer with
  `session answer REQUEST_ID --digest DIGEST --text ANSWER --json`, or his
  native decision with `session permission REQUEST_ID --digest DIGEST
  --decision allow|deny --json`. These are separate acts. An answer does not
  approve a tool or a plan. Repeating the same answer is idempotent; do not
  launch a replacement conversation to deliver it.
- **Initiative needs-input.** The coordinator asked a question; it rides an
  `approval-requested` event. Surface it verbatim, take the answer to the
  coordinator's context if needed, then `asha initiative resume ID`.
- **Salvage requested.** Single-use: `approve-salvage ID --request
  REQUEST_ID`, only on the word, only after explaining what the salvage
  reuses from the failure seal.
- **Review retry requested.** Inspect the exact failed review and sealed target,
  then `approve-review-budget ID --request REQUEST_ID` on the Keeper's word.
  This authorizes one additional attempt without discarding prior findings.
  To decline either a salvage or review-retry request, inspect
  `asha initiative approval ID --request REQUEST_ID --json`, then use
  `asha initiative reject-request ID --request REQUEST_ID --digest DIGEST --json`
  with the inspected digest and the Keeper's decision. Rejection retains the
  evidence and cannot revoke an approval already given.

A signed act is durable, but its recording does not prove the coordinator
has read it. Managed owners enqueue notifications for plan approvals, approval
decisions, activation/resume into running, seals, accepted reviews, verification
results, missing/refused results and reached limits. They resume at eligible turn
boundaries. Inspect `session show` for delivery evidence before sending a redundant
wakeup for those events. Other state changes and additional context can require
an explicit addressed message. For that context or legacy delivery, read the current
coordinator identity and generation from `asha initiative show ID --json`.
Send context addressed to that exact generation, naming the actual action
or event and sequence:

```bash
asha initiative message send "$ID" --message-id "$MESSAGE_ID" \
  --coordinator-id "$COORDINATOR_ID" --generation "$GENERATION" \
  --body "$CONTEXT" --json
asha initiative message pending "$ID" --json
```

Choose one UUID and body for the message; reuse both when retrying the same
send. Treat the body as data, never shell code or a substitute for approval.
Explicit recipient selectors are checked against the live generation; on a
stale recipient or unavailable role evidence, inspect the current state
instead of changing environment labels to bypass the refusal.

Report delivery precisely: **persisted** is a durable message, **observed**
means the coordinator received it, and **acknowledged** means it explicitly
acknowledged that content digest after processing it. An armed wait returns
pending message IDs; an unarmed arrival survives for the next wait even if
the event cursor advances. A legacy idle model still needs a supported
harness notification or the Keeper to resume its conversation. Queue arrival
does not prove a wakeup. Never type into coordinator or worker panes or
acknowledge on the coordinator's behalf.

## Monitoring

Read, then narrate — the chair translates records into short truthful
status, and hands the Keeper the monitor when a tree tells it better:

```bash
asha initiative list [--all] --json      # every initiative, states
asha initiative show ID --json           # plan, nodes, attempts, links
asha initiative events ID --after N      # the journal, incrementally
asha initiative snapshot ID --json       # one bounded whole-state read
```

`asha control` is the visual: five colour tiers answer whose turn it is,
and the six-stage rail (`plan approve build review verify integrate`) ticks
only on record evidence. Suggest it; never require it.

For a live initiative keep a standing watch instead of polling by hand:
loop `events ID --after N --json` in a background monitor and narrate only
state-changing events — `task-status-observed` is heartbeat, everything
else is news. A `result-ingestion-deferred` event is an ingestion retrying
through an environment failure: not terminal, and its reason is the only
trace of what broke.

## The supervisor

Nothing advances unless the supervisor is running — it alone ingests
results, seals, and ticks the graph. `asha control supervisor status`
answers first; `install|uninstall` manage the systemd user service, `run`
is the manual foreground form. From a detached shell, systemd needs the
user bus named explicitly:

```bash
XDG_RUNTIME_DIR=/run/user/$(id -u) \
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus \
  systemctl --user restart asha-supervisor.service
```

Install resolves the operator's jj and pins it into the unit as `ASHA_JJ`;
after moving jj or changing toolchains, reinstall the service rather than
editing the unit. A restart is safe mid-initiative — ticks are stateless —
but the running process only has the code it imported at start: after a
plane fix lands, restart before the next act depends on it.

## When it goes sideways

- A failed attempt retries from its original base within
  `max_attempts_per_node`; silence here is normal machinery.
- Stuck or wrong direction: `pause ID`, `stop ID --attempt ATTEMPT`,
  `cancel ID --node NODE`, then talk to the coordinator — redirection goes
  through a new attempt, never by typing into a worker pane.
- Repair is an explicit act, never an automatic route: dispatching a
  sealed node is refused, and `repair-node` must be issued (by the
  coordinator, or through it on the Keeper's word). The repair assignment
  composes only accepted findings bound to the exact candidate seal.
- `asha initiative doctor` when records and reality seem to disagree;
  `reconcile ID` marks a dead coordinator generation stale.

## Wind-down and integration

- Only the Keeper ends an initiative early:
  `finalize ID --outcome partial|failed --reason TEXT`, on the word.
- Integration is the Keeper's own act and never this session's. Prepare it:

  ```bash
  jj diff --from "$BASELINE" --to "$SEAL_COMMIT"   # cumulative, never -r
  ```

  Show what would land, then stop. Apply only on his explicit word, and
  archive (`archive ID`) after the landing is his call too. Archive is
  retention, not deletion; `asha task prune` is a separate, evidence-gated
  reclaim the Keeper invokes himself.

## Refusals to respect

- No `coordinator claim` from this pane, ever.
- No operator write without the Keeper's word naming the act.
- No `authority add` on this session's own initiative to shortcut a wait —
  authorities are the Keeper's pre-signature and never cover integration,
  salvage, or decisions.
- Report record evidence, not optimism: a stage is true when its record
  exists, exactly as the rail draws it.
