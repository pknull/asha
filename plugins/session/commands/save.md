---
name: session-save
description: "Publish compact Memory v2 from live context, validate, commit, and push"
argument-hint: "[--scope repo|workspace|none] [--no-push] [commit message]"
allowed-tools: ["Bash", "Read", "Write"]
---

# Save Session

This explicit command is the **only semantic publication path**. Never call it
from a lifecycle hook, timer, background process, or transcript parser.

## Contract

1. Resolve the effective plane through `tools/save_scope.py resolve`, passing
   `--scope` only when the user supplied it. The resolver checks a strict
   `.asha/control-task.json` marker before repository or Git discovery. Bare
   save in a valid managed Control workspace becomes effective scope `none`;
   a malformed marker fails closed. Explicit `repo` and `workspace` keep their
   existing meaning. Explicit `none` performs no Git discovery.
2. Read the live conversation and verify every state claim against current
   disk. A recovery snapshot is low-authority orientation only.
3. Draft, from live model context:
   - `activeContext.md`, at most 4,096 UTF-8 bytes, with exactly the level-one
     headings `Objective`, `State`, `Next`, and `Blockers` in that order;
   - `decisions.md`, at most 65,536 UTF-8 bytes, headed only `Decisions`,
     containing current binding decisions—not a history. Remove decisions that
     no longer bind.
   `Next` and `Blockers` each contain at most five items.
4. Write both drafts outside `Memory/`. When effective scope is `none`, run
   the executable managed path below. It publishes through the validator,
   compares the exact before/after Memory bytes, resolves the local save
   identity, and has no Git seam:

   ```bash
   SAVE_NONE_SCOPE=()
   if [[ "${REQUESTED_SCOPE:-}" == "none" ]]; then SAVE_NONE_SCOPE=(--scope none); fi
   python3 "$TOOLS/save_none.py" publish "${SAVE_NONE_SCOPE[@]}" --start "$PLANE_BASE" \
     --active-file "$ACTIVE_DRAFT" --decisions-file "$DECISIONS_DRAFT" || exit
   ```

   For effective `repo` or `workspace`, publish directly through the validator:

   ```bash
   TOOLS="$ASHA_ROOT/plugins/session/tools"
   python3 "$TOOLS/memory_v2.py" publish --project-dir "$PLANE_BASE" \
     --active-file "$ACTIVE_DRAFT" --decisions-file "$DECISIONS_DRAFT" || exit
   ```

   The validator checks both drafts before either replacement and writes by
   same-directory `fsync` + `os.replace`. Never bypass it with direct edits.
5. Inspect and show the resulting change. For effective scope `none`, use only
   the exact before/after Memory hashes and changed-path list emitted by
   `save_none.py`; do not invoke `git diff`, Git discovery, staging, commit, or
   push. For `repo` or `workspace`, show the Git diff. Run relevant verification
   for code/config changed during the session. Correct and republish stale,
   vague, or unverifiable drafts.
6. Propose at most three cross-project learning candidates when the evidence
   warrants it. Resolve the save identity from `ASHA_SESSION_ID`, then
   `CLAUDE_CODE_SESSION_ID`, then `CODEX_THREAD_ID`; Copilot may fall back to
   its current/latest validated recovery snapshot. The manager obtains the
   stable project id from `.asha/config.json`:

   ```bash
   if SAVE_SESSION_ID="$(python3 "$TOOLS/save_identity.py" \
       --project-dir "$PLANE_BASE" --harness "${ASHA_HARNESS:-}")"; then
     python3 "$TOOLS/learnings_manager.py" propose \
       --id ID --trigger TRIGGER --action ACTION --reason REASON \
       --project-dir "$PLANE_BASE" --session-id "$SAVE_SESSION_ID" || true
     python3 "$TOOLS/learnings_manager.py" activate-if-eligible --id ID \
       --project-dir "$PLANE_BASE" || true
   else
     echo "learning evidence skipped: session identity unavailable" >&2
   fi
   ```

   For effective scope `none`, reuse a non-null session identity returned by
   `save_none.py`; a null identity with `identity_status=skipped` is nonfatal
   and skips learning evidence. Do not run `save_identity.py` again. The learning manager is
   a local filesystem operation and must remain no-Git on this path. If a
   future identity or learning implementation requires Git, skip it whenever
   effective scope is `none`.

   A learning activates only after three distinct session ids across two
   project ids. This is a user-controlled corroboration heuristic over local
   save evidence, not a security boundary. Candidates are not SessionStart
   instructions. A learning proposal failure is non-fatal after publication:
   report it and continue to diff, commit, and push.
7. Unless the **effective** scope is `none`, stage only the selected plane's
   `Memory/activeContext.md` and `Memory/decisions.md`, then commit. Do not
   stage unrelated work:

   ```bash
   git -C "$COMMIT_REPO" add -- "$MEMORY_REL/activeContext.md" "$MEMORY_REL/decisions.md"
   git -C "$COMMIT_REPO" commit -m "Session save: ${MESSAGE:-$(date -u '+%Y-%m-%d %H:%M UTC')}"
   ```

8. Unless the effective scope is `none` or `--no-push`, publish the commit through the durable
   push path:

   ```bash
   python3 "$TOOLS/push_retry.py" ensure --project-dir "$COMMIT_REPO"
   ```

## Hard exclusions

Do not read Claude/Codex/Copilot/OpenCode transcript stores. Do not derive or
archive events. Do not run a pattern analyzer, recall index, nudge metric,
calibration extractor, or automatic save. Do not modify identity files.

## Silence

If `Work/markers/silence` exists, stop before publication, learning mutation,
Git commit, or push.
