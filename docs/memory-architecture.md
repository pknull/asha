# Memory System v2

Memory v2 separates deliberate semantic publication from bounded mechanical
crash recovery. Nothing infers meaning from host transcripts or hook telemetry.

## Authority order

1. Live system state and verified disk
2. Current explicit publication in `Memory/`
3. Unpublished recovery snapshot
4. Candidate or retired learning evidence

Lower tiers never overwrite higher tiers merely to make notes agree.

## Published semantic memory

Every repository or workspace operational plane has only:

```text
Memory/activeContext.md
Memory/decisions.md
```

`activeContext.md` is no more than 4,096 UTF-8 bytes and has exactly four
level-one headings, in order: Objective, State, Next, Blockers. The latter two
hold at most five items each. `decisions.md` contains only current binding
decisions; resolved/superseded decisions leave the publication rather than
forming a history.

`/session:save` is the sole semantic writer. The live model drafts both files,
checks claims against disk, and calls `memory_v2.py publish`. Both drafts are
validated before publication. A project lock serializes publishers; a private,
ignored recovery journal rolls both files back after a partial replacement and
is replayed before the next validation/publication. The explicit command then
commits and pushes unless `--scope none` or `--no-push` says otherwise.
Every shipped reader acquires the same lock through `memory_v2.py read`, so it
cannot observe a mixed pair during the two sequential file replacements.

No hook, SessionEnd, OpenCode `dispose`, timer, or background process publishes
semantic memory or invokes Git.

## Unpublished recovery

Prompt and post-tool callbacks atomically replace:

```text
Work/session-state/<harness>-<session>.json
```

Each file is mode `0600`, no more than 2,048 bytes, project-local, path-safe,
secret-scrubbed, and isolated by session. Touched paths are deduplicated and
capped at ten. SessionStart removes snapshots older than seven days and may
surface the newest one with an explicit unpublished/verify-first label.
SessionEnd only adds its seal timestamp and prunes. `Work/markers/silence`
disables these writes.

## Learnings

One learning per file lives under:

```text
~/.asha/learnings/candidate/
~/.asha/learnings/active/
~/.asha/learnings/retired/
```

Evidence records date, harness session identity when available, stable project identity
from `.asha/config.json`, kind, and reviewed reason. Explicit save resolves
available native environment seams, with Copilot recovery as a fallback. Duplicate
`(session_id, project_id)` evidence does not count twice. Activation requires
three distinct positive sessions across two projects. This automatic gate is a
user-controlled corroboration heuristic, not a security boundary. Only active learnings are
rendered at SessionStart. SessionStart runs candidate expiry, moving records
older than 90 days to retired; no record
is silently deleted. Contradiction is an explicit transition back to candidate;
retirement is explicit and keeps the record.

## Migration

`/session:consolidate` inventories legacy inputs and presents an itemized
accept/reject/defer plan. It inventories the live root OKF bundle, legacy
archive, and flat files per learning; every item is bound to its source hash.
The private plan persists at `Work/memory-migration/review.json` for later
review and is never replaced without explicit authorization. One typed
whole-review apply preflights the complete batch, publishes project state,
applies learning rows, and binds both exact publication draft digests plus both
typed publication roles into review and receipt identity. Failure recovery uses
a global, durable per-record journal with preimage hashes and
compare-before-rollback. Existing publication targets must themselves be
accepted hash-bound sources; absent targets require explicit create mappings.
Recovery preflights the project pair/config/ignore, learning records, backups,
receipt, silence state, and journal as one unit. A conflict changes nothing and
preserves repair evidence. Migration never snapshots or replaces the global
learning tree. Accepted sources receive exact-byte, hash-bound timestamped private backups;
original, rejected, and deferred material remains in place.

## Separate workspace planes

Canonical workspace `knowledge/` indexes, reviewed promotion infrastructure,
private `memory-local/`, and harness-native memory remain independent. Removing
the operational Memory catalogue does not remove or weaken those systems.

## Harness seams

- Claude reads `hooks.json` directly.
- Codex renders the supported shared hooks to native TOML.
- Copilot installs one `asha-recovery.json` plus its independent guardrails.
- OpenCode generates `plugins/asha.js`, calling the same recovery handlers and
  sealing on dispose.

All four use the same publication validator, recovery writer, and learning
manager.
