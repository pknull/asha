# Explicit Memory v2 Save Pipeline

This page is the manual fallback when the rendered `session-save` workflow is
unavailable. It is never called by hooks.

## 1. Resolve the publication plane

```bash
ASHA_ROOT=/path/to/asha
TOOLS="$ASHA_ROOT/plugins/session/tools"
python3 "$TOOLS/save_scope.py" resolve --scope repo --start "$PWD"
```

For a workspace-owned handoff use `--scope workspace`. The JSON result supplies
`plane_base`, `memory_root`, `memory_rel`, and `commit_repo`. Use `--scope none`
semantics by resolving the repository plane but skipping Git below.

## 2. Author from live context

Verify current repository state, then draft two temporary files:

- `activeContext.md`: exactly `Objective`, `State`, `Next`, `Blockers`, at most
  4,096 UTF-8 bytes, with at most five Next items and five Blockers.
- `decisions.md`: one `Decisions` heading and current binding decisions only.

Do not read a harness transcript or derive semantic state from recovery JSON.

## 3. Validate and publish atomically

```bash
python3 "$TOOLS/memory_v2.py" publish \
  --project-dir "$PLANE_BASE" \
  --active-file "$ACTIVE_DRAFT" \
  --decisions-file "$DECISIONS_DRAFT"
```

The validator checks both drafts before replacing either published file.
After publication, resolve `SAVE_SESSION_ID` only when proposing optional
learning evidence. Missing identity skips that optional step; it never blocks
publication, commit, or push.

## 4. Review and verify

Inspect the exact Memory diff and run the repository's required verification.
Correct stale or unverifiable claims by changing the drafts and publishing
again. Recovery state remains unpublished and ignored.

## 5. Commit and push

Unless the requested scope is `none`:

```bash
git -C "$COMMIT_REPO" add -- \
  "$MEMORY_REL/activeContext.md" "$MEMORY_REL/decisions.md"
git -C "$COMMIT_REPO" commit -m "Session save: $(date -u '+%Y-%m-%d %H:%M UTC')"
```

Unless `--no-push` was requested:

```bash
python3 "$TOOLS/push_retry.py" ensure --project-dir "$COMMIT_REPO"
```

If `Work/markers/silence` exists, stop before all publication, learning, Git,
or push operations.
