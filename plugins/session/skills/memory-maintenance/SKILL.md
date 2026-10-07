---
name: session-memory-maintenance
description: "Validate or deliberately maintain Asha Memory v2 publication and recovery files. Use for activeContext.md, decisions.md, project_id, recovery snapshots, and learning lifecycle."
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
read `tools/memory_v2.py read --format json` before drafting and retain its coherent
digests. Call `tools/memory_v2.py publish --expected-active DIGEST
--expected-decisions DIGEST`; on conflict reread and merge. Never write around the validator. The
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

## Legacy Memory

The reviewed v1 migration command was retired in session 2.7.0. A project whose `activeContext.md` or `decisions.md` is not valid v2
cannot be initialized until those files are rewritten in the v2 format. Legacy
files beside them stay in place; nothing reads or deletes them. Canonical
workspace `knowledge/` is outside the removed operational-memory catalogue.

## Validation

```bash
python3 "$ASHA_ROOT/plugins/session/tools/memory_v2.py" validate --project-dir "$PROJECT_DIR"
python3 "$ASHA_ROOT/plugins/session/tools/learnings_manager.py" list --state active
```
