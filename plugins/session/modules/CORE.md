# CORE — Bootstrap

Fallback operational guidance used only when `~/.asha/operation.md` is absent.

## Session orientation

Read identity from `~/.asha/soul.md`, `voice.md`, and `keeper.md` when the Asha
persona is active. Read the last explicit publication coherently with
`python3 "$ASHA_ROOT/plugins/session/tools/memory_v2.py" read --project-dir "$PROJECT_DIR"`,
then verify its claims against live disk.

Memory v2 has two persistence classes:

1. Published semantic memory: the two files above. Only explicit
   `/session:save` publishes them. `activeContext.md` is at most 4 KiB and has
   Objective, State, Next, and Blockers; decisions are current and binding.
2. Unpublished recovery: ignored `Work/session-state/*.json`, written
   mechanically by hooks, at most 2 KiB, private mode, seven-day expiry. It is
   a recovery hint—not authority.

Global learnings use explicit `candidate`, `active`, and `retired` states.
Only active learnings load at SessionStart. Activation requires evidence from
three distinct sessions across two projects.

## Constraints

- Preserve user data; destructive operations require explicit confirmation.
- Live state and disk outrank notes.
- Reuse existing tools before creating another layer.
- `Work/markers/silence` disables all Memory v2 persistence.
- Never derive semantic memory from hooks or host transcripts.
- Keep responses concise and report deliverables with project-relative paths.
- Mark uncertain claims `[Inference]`, `[Speculation]`, or `[Unverified]`.
