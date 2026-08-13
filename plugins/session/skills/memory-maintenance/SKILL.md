---
name: memory-maintenance
description: "Validate or deliberately maintain Asha Memory v2 publication and recovery files. Use for activeContext.md, decisions.md, project_id, recovery snapshots, learning lifecycle, and reviewed legacy migration."
---

# Memory v2 Maintenance

## Authority

Live state and verified disk outrank published Memory. Published Memory outranks
unpublished recovery. Candidate learnings have no SessionStart authority.

## Published semantic memory

`Memory/activeContext.md` is at most 4,096 UTF-8 bytes and has exactly:

```markdown
# Objective
# State
# Next
# Blockers
```

`Next` and `Blockers` contain at most five items each.

`Memory/decisions.md` has only `# Decisions` and current binding decisions. It
is not a log or archive.

Only explicit `/session:save` publishes either file. Draft outside `Memory/`
and call `tools/memory_v2.py publish`; never write around the validator. The
publisher holds a project lock and uses a private recovery journal so the pair
cannot interleave or remain partially replaced. Shipped readers use
`memory_v2.py read --project-dir PROJECT` and acquire the same lock; direct
unlocked reads of one file at a time are not a coherent pair read.

## Recovery

`Work/session-state/<harness>-<session>.json` is ignored, mode `0600`, at most
2,048 bytes, and expires after seven days. It stores bounded prompt hints,
touched paths, the last mechanical action, and a blocker indicator. It is not
semantic memory and must be verified before use. `Work/markers/silence`
disables persistence.

## Learnings

Learnings live under `~/.asha/learnings/{candidate,active,retired}/`. Explicit
save resolves `ASHA_SESSION_ID`, `CLAUDE_CODE_SESSION_ID`, or `CODEX_THREAD_ID`
in that order; Copilot may use its current/latest validated recovery snapshot.
The manager reads the actual project `project_id` from config.
Activation requires three distinct sessions across two projects. Propose at
most three candidates per explicit save. This is a user-controlled heuristic
over local evidence, not a security authority. Contradiction and retirement are
visible state transitions; neither silently deletes a record.

## Legacy migration

Use `/session:consolidate`. Inventory first, present `accept`/`reject`/`defer`
per item, wait for review, apply the typed whole review idempotently, and
use `migrate-amend` to atomically stage decisions plus both exact publication
digests. Existing publication targets must be accepted hash-bound sources;
absent targets require explicit create mappings. Preserve the captured reviewed
bytes in timestamped private backup. Recovery conflicts change nothing and keep
the global transaction journal and private backups for repair. Canonical
workspace `knowledge/` is outside the removed operational-memory catalogue.

## Validation

```bash
python3 "$ASHA_ROOT/plugins/session/tools/memory_v2.py" validate --project-dir "$PROJECT_DIR"
python3 "$ASHA_ROOT/plugins/session/tools/learnings_manager.py" list --state active
```
