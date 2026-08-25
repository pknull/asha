---
name: operate-control
description: "Run the Asha Control plane from the operator's chair: launch one fenced coordinator per piece of work, read the tree, sign approvals and activations on the Keeper's explicit word, answer needs-input, and prepare — never perform — integration. Use when the Keeper asks this wrapped session to start, steer, or monitor initiatives conversationally instead of through the asha control monitor."
---

# Operate the Control plane (the orchestrator's chair)

Three seats share the plane, and this session holds exactly one of them. The
**controller** is deterministic code: records, seals, gates; it enforces
everything and decides nothing. A **coordinator** is one bounded generation
claimed per initiative from its own pane; it proposes and drives, and its
pane is refused every operator write. This session is neither: it is the
**operator's chair** — the Keeper's instrument, acting only on his word, from
a pane that keeps his signature. Never run `coordinator claim` here; a
claimed pane loses the approval surface, and that loss is structural.

## Starting work

1. Resolve what the Keeper named to one repository through the index, never
   a guess. Friendly names from each project's `.asha/config.json` match:

   ```bash
   asha initiative projects --match "<name the Keeper used>" --json
   ```

   Zero or several matches: show the candidates and ask once. An entry with
   `jj_colocated: false` cannot run an initiative; say so.
2. Launch a fenced coordinator with the intent — one per piece of work:

   ```bash
   asha initiative coordinator launch --root "$ROOT" --intent "$INTENT" --json
   ```

   The coordinator session resolves the repository, creates and claims the
   initiative, and proposes a plan. `coordinator sessions` lists live ones;
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

  One act per word; never batch approvals ahead of it. A standing authority
  (`authority list`) may approve a matching shape by proxy — that is the
  Keeper's pre-signature, not this session's judgment.
- **Approved.** `asha initiative activate ID` — again on the word.
- **Needs-input.** The coordinator asked a question; it rides an
  `approval-requested` event. Surface it verbatim, take the answer to the
  coordinator's context if needed, then `asha initiative resume ID`.
- **Salvage requested.** Single-use: `approve-salvage ID --request
  REQUEST_ID`, only on the word, only after explaining what the salvage
  reuses from the failure seal.

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
