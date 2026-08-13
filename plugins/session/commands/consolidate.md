---
name: session-consolidate
description: "Review and migrate legacy memory into explicit Memory v2 state"
argument-hint: "[legacy paths...]"
allowed-tools: ["Bash", "Read", "Write"]
---

# Consolidate Memory

This is the sole legacy migration path. It is reviewed, non-destructive, and
idempotent.

1. Inventory legacy operational files, events/session archives, old learning
   stores, and operational catalogues without changing them:

   Use the durable ignored/private review path. The plan must survive the
   inventory shell so review and apply can occur in later tool calls:

   ```bash
   umask 077
   python3 "$ASHA_ROOT/plugins/session/tools/memory_v2.py" \
     ensure-private-ignores --project-dir "$PROJECT_DIR" || exit
   MIGRATION_DIR="$PROJECT_DIR/Work/memory-migration"
   MIGRATION_PLAN="$MIGRATION_DIR/review.json"
   python3 "$ASHA_ROOT/plugins/session/tools/learnings_manager.py" migrate-plan \
     --project-dir "$PROJECT_DIR" \
     --output "$MIGRATION_PLAN" \
     "$PROJECT_DIR/Memory/activeContext.md" \
     "$PROJECT_DIR/Memory/decisions.md" \
     "$PROJECT_DIR/Memory/projectbrief.md" \
     "$PROJECT_DIR/Memory/workflowProtocols.md" \
     "$PROJECT_DIR/Memory/techEnvironment.md" \
     "$PROJECT_DIR/Memory/events" "$PROJECT_DIR/Memory/sessions/archive" \
     "$HOME/.asha/learnings" "$HOME/.asha/learnings-archive" \
     "$HOME/.asha/learnings.md" "$HOME/.asha/learnings-archive.md" || exit
   printf 'Migration review plan: %s\n' "$MIGRATION_PLAN"
   ```

2. For each item, propose `accept`, `reject`, or `defer` plus an explicit
   mapping. Nothing defaults to acceptance. Give every accepted row an
   `item_type`: `project-publication`, `learning`, or `legacy-evidence`.
   A publication change requires two accepted typed rows, one with
   `publication_role: activeContext` and one with `publication_role: decisions`.
   When a target already exists, that row's hash-bound `source` must be the
   target itself; an unrelated legacy source cannot authorize overwriting it.
   An absent target requires an explicit `create: true` row whose `target` is
   the exact publication path.
3. Draft both v2 publication files. Add a `publication` object to the plan
   containing `active_context_sha256` and `decisions_sha256` for the exact UTF-8
   draft bytes. Stage decisions in a separate private amended-plan file, then
   bind both drafts and atomically replace the durable review:

   ```bash
   python3 "$ASHA_ROOT/plugins/session/tools/learnings_manager.py" migrate-amend \
     --project-dir "$PROJECT_DIR" --review "$AMENDED_PLAN" \
     --output "$MIGRATION_PLAN" --active-file "$ACTIVE_DRAFT" \
     --decisions-file "$DECISIONS_DRAFT" || exit
   ```

   Show the complete staged plan—including both output digests—and wait for
   review. Do not change either draft after approval.
4. Resolve the explicit save identity with `save_identity.py`, then run the
   single whole-review command:

   ```bash
   SAVE_SESSION_ID="$(python3 "$ASHA_ROOT/plugins/session/tools/save_identity.py" \
     --project-dir "$PROJECT_DIR" --harness "${ASHA_HARNESS:-}")" || exit
   python3 "$ASHA_ROOT/plugins/session/tools/learnings_manager.py" migrate-apply \
     --project-dir "$PROJECT_DIR" --review "$MIGRATION_PLAN" \
     --session-id "$SAVE_SESSION_ID" --active-file "$ACTIVE_DRAFT" \
     --decisions-file "$DECISIONS_DRAFT"
   ```

   It verifies both approved draft digests, preflights the entire hash-bound
   batch before mutation, initializes the stable project id when absent,
   publishes the pair, and applies learning rows. A global per-record journal
   uses preimage hashes and compare-before-rollback recovery; it never snapshots
   or replaces the whole learning corpus.
5. Rejected/deferred sources remain in place. Accepted non-publication sources
   also remain. Publication sources are replaced by the reviewed v2 pair, with
   their original hash-bound bytes retained beneath the ignored timestamped
   `Work/memory-migration/backups/` directory.
6. Re-run the same reviewed plan to verify idempotence, validate published
   files, and report every applied/deferred/rejected item.
7. The plan writer refuses to replace an existing review by default. After
   explicit confirmation, `--replace-plan` permits a new defer-only inventory;
   `migrate-amend` is the only path that atomically replaces it with reviewed
   decisions and bound output digests. Remove the private migration plan only
   after review/application is finished.
   Deferred plans remain at `Work/memory-migration/review.json` for later review.

Canonical workspace `knowledge/` indexes and promotion infrastructure are not
legacy operational memory and must remain untouched.
