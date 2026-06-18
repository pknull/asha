---
name: session-save
description: "Manually trigger session synthesis and git commit"
argument-hint: "[--no-push] [commit message]"
allowed-tools: ["Bash", "Read", "Edit"]
---

# Save Session

Trigger synthesis now. Use when you want to checkpoint mid-session or ensure state is captured before exiting.

**Note:** Session-end hook runs synthesis automatically on clean exit. This command is for explicit mid-session saves or when you want to add a custom commit message.

## What It Does

1. **Runs pattern analyzer** — synthesizes activeContext.md from events
2. **Extracts patterns** — updates learnings.md with discovered patterns
3. **Captures calibration** — voice.md and keeper.md signals if detected
4. **Archives events** — rotates old events to archive
5. **Captures baseline sample** — if Asha baseline tooling is present, records a metrics sample for the session (best-effort, non-blocking)
6. **Git commit + push** — commits Memory/ changes

## Usage

```bash
/save                           # Synthesize + commit + push
/save --no-push                 # Synthesize + commit only
/save "Completed auth feature"  # Custom commit message
```

## Execution

First, opportunistically drain any queued (previously unpushed) commits. If a push destination exists they go out now; if not, this is a no-op that reports the backlog instead of failing silently:

```bash
"$ASHA_ROOT/plugins/session/tools/push_retry.py" drain --project-dir "$PROJECT_DIR"
```

Run the synthesis pipeline:

```bash
"$ASHA_ROOT/plugins/session/tools/pattern_analyzer.py" synthesize --days 7
```

Then archive and rotate events:

```bash
"$ASHA_ROOT/plugins/session/tools/save-session.sh" --archive-only
```

Then run the boundary guardrail to strip auto-fallback stub blocks the synthesizer re-appends and dedup re-emitted calibration signals against the existing keeper log. This treats `pattern_analyzer.py`'s output as untrusted input — durable fix at the boundary, no upstream chase required.

```bash
"$ASHA_ROOT/plugins/session/tools/save_guardrail.py" all "$PROJECT_DIR"
```

Surface any non-zero counts in chat output so the user sees what was cleaned. If the guardrail itself errors (missing file, parse failure), report it and continue — the gate must not block save on a guardrail bug.

Then validate the learnings OKF bundle. This is **warn-only** — it never blocks the save. It checks the three OKF hard rules (parseable frontmatter, non-empty `type`, reserved-file structure) over `~/.asha/learnings/`. Set `ASHA_LEARNINGS_VALIDATE=strict` to also surface producer-quality lints (missing title/description, broken links, orphans):

```bash
LEARNINGS_DIR="$HOME/.asha/learnings"
if [[ -d "$LEARNINGS_DIR" ]]; then
    VFLAG=""; [[ "${ASHA_LEARNINGS_VALIDATE:-warn}" == "strict" ]] && VFLAG="--strict"
    "$ASHA_ROOT/plugins/session/tools/validate.py" "$LEARNINGS_DIR" $VFLAG \
        || echo "warn: learnings bundle validation reported issues (non-fatal)" >&2
fi
```

Surface any `ERROR` lines in chat (a malformed concept file), but do not block the commit on them.

Then capture a baseline sample (best-effort, non-blocking — only runs if the Asha baseline tooling is present).

**Step 5a — Determine archetype** from this session's activity. Apply this heuristic in order, pick the FIRST match:

| Archetype | Signal |
|---|---|
| `panel-orchestration` | `/panel` invoked this session OR ≥2 distinct subagents spawned |
| `daily-brief` | `/daily-brief` invoked this session |
| `research-synthesis` | ≥3 `WebSearch`/`WebFetch` calls OR `research-assistant` agent spawned |
| `email-triage` | Any `gws` CLI calls OR `mcp__gemini`/email-related MCP tools used |
| `code-implementation` | ≥10 `Edit`/`Write`/`MultiEdit` on non-`Memory/` paths |
| `unclassified` | None of the above (fallback) |

**Step 5b — Invoke capture.sh** with the archetype:

```bash
CAPTURE=~/life/Work/panels/baseline--2026-04-17/capture.sh
if [[ -x "$CAPTURE" ]]; then
    # ARCHETYPE set per heuristic above; SAVE_DURATION left empty for v1
    "$CAPTURE" "$ARCHETYPE" "" --notes "auto-captured from /session:save" || {
        echo "warn: capture.sh failed (non-fatal); continuing with commit" >&2
    }
fi
```

**Failures are non-fatal.** If the script is missing (other projects, or baseline dir not set up), skip silently. If it exits non-zero, log a one-line warning and continue — baseline accumulation is best-effort, not a gate on save.

Then run the pre-flight verification gate (engine-backed — the enforced version of the manual Verification Gate below). It self-heals `Memory/`, confirms synthesis ran on THIS session's transcript (not a concurrent session's), and blocks a clobbered or foreign-sourced `activeContext.md` from being committed:

```bash
"$ASHA_ROOT/plugins/session/tools/save_preflight.py" --mode guard --skip-push --project-dir "$PROJECT_DIR"
```

If it exits non-zero (a HARD gate failed), STOP — fix the flagged issue (re-run synthesis with the correct transcript, regenerate the affected activeContext section) before committing. Do not commit over a hard failure. The same gates re-run post-commit via the Stop hook as a final net.

Then commit Memory changes. Dropping the `save-pending` marker first arms the Stop hook to run the post-commit verification gate for this turn:

```bash
cd "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/Work/markers"
printf '{"created":"%s","attempts":0}\n' "$(date -u +%FT%TZ)" > "$PROJECT_DIR/Work/markers/save-pending"
git add Memory/
git commit -m "Session save: ${ARGUMENTS:-$(date -u '+%Y-%m-%d %H:%M UTC')}"
```

Push unless `--no-push` specified. This uses the durable push path: if a remote/upstream exists the commit is pushed; otherwise HEAD is recorded to the backoff retry queue (inspect with `push_retry.py status`) instead of failing silently:

```bash
"$ASHA_ROOT/plugins/session/tools/push_retry.py" ensure --project-dir "$PROJECT_DIR"
```

## Verification Gate (run BEFORE commit)

After synthesis writes activeContext.md and BEFORE staging it, verify the file is a usable handoff. Cold-start sessions depend on it. If any of these checks fail, stop and surface the issue to the user — do not paper over it with a commit.

**Check 1 — "What Was Accomplished" is concrete, not auto-generic.**

The synthesizer will write a generic block like `Created N file(s): ...` / `Modified N file(s): ...` when it has nothing better. That block is useless to a future session. If you see it as the LEAD section in activeContext, replace it with a concrete narrative of what actually happened this session (file paths, tool names, decisions, blockers).

```bash
# Quick grep — if this matches and it's near the top, you have the auto-fallback
grep -n "Created [0-9]* file(s)\|Modified [0-9]* file(s)" "$PROJECT_DIR/Memory/activeContext.md" | head -3
```

**Check 2 — "Next Steps" is actionable.**

Per CLAUDE.md "Session Handoff Quality": if Next Steps contains only `Review and plan next session` or similar generic text, replace it with concrete pickups before commit.

**Check 3 — No duplicate "What Was Accomplished" headers.**

After the merge fix landed, this should not happen, but verify defensively. A bare `## What Was Accomplished` co-existing with a `## What Was Accomplished (date — note)` is the synthesizer-clobber bug; report it as a regression.

```bash
grep -c "^## What Was Accomplished" "$PROJECT_DIR/Memory/activeContext.md"
# Expect: 1 (or N matching parenthetical user variants). NOT N+1.
```

**Check 4 — Show me the green for any code/config you wrote this session.**

If this session edited code, hook scripts, or config under version control, run the relevant verification (tests, type check, lint, smoke invocation) and paste the result inline. If you cannot verify, say "unverified" explicitly and list what would need to be checked. Never commit-then-claim-done.

## When to Use

- **Mid-session checkpoint** — long session, want progress saved
- **Before risky operation** — about to do something destructive
- **Custom commit message** — want descriptive message instead of auto-generated
- **Explicit calibration** — want to manually review what gets captured

## Output

Shows synthesis results:

- Events processed
- Patterns found
- Calibration signals captured
- Files updated

<!-- CONSOLIDATION-PASS:START -->
## B1 Consolidation Pass (Appendix)

Run this pass **after** pattern_analyzer synthesis and **before** the git commit step. It is non-destructive — it flags via `superseded_by:` rather than deleting.

### When to run

Always run if any memory files were written or updated this session. Skip only if the session was read-only (no Memory/ writes, no learnings updates).

### Step C1 — Dedup scan

Scan all `~/.asha/*.md` and `Memory/*.md` for near-identical facts (same subject, same conclusion):

1. For each pair of files with overlapping `type:` (e.g., two `type: feedback` files about the same tool), compare their body text.
2. If bodies are >80% similar (same core claim, minor wording difference), mark the **older** file with `superseded_by: <newer-filename>` in its frontmatter.
3. Do not delete. Log the flagged pair in the commit message.

```yaml
# Example: older file gets this added to frontmatter
superseded_by: feedback_gws_over_mcp_v2.md
```

### Step C2 — Contradiction flag

If a session event or new memory directly contradicts an older memory:

1. Add `superseded_by: <new-file>` to the **older** file's frontmatter.
2. Add a `# Superseded` comment at the top of the older file's body (after frontmatter).
3. Never delete the older file — it is the audit trail.

```markdown
# Superseded

Superseded by: feedback_gws_over_mcp.md (2026-04-18)
Reason: Updated policy — gws CLI now preferred over MCP.

[original content below]
```

### Step C3 — Learnings promotion/demotion

After updating learnings.md entries this session:

1. Check each entry's `Confidence` value.
2. If any entry has risen **above 0.7** and is in `learnings-archive.md`, move it to `learnings.md` (hot tier).
3. If any entry has fallen **below 0.7** in `learnings.md` and the hot tier has >10 entries, move lowest-confidence entries to `learnings-archive.md`.
4. Hot tier must not exceed 10 entries.

**Promotion command (manual)**:

```bash
# Move entry from archive to hot tier:
# 1. Edit learnings-archive.md, cut the entry block
# 2. Paste into learnings.md under the appropriate ## section
# 3. Verify total hot entries <= 10
```

### Step C4 — Char budget check

After promotion/demotion, verify learnings.md stays under budget:

```bash
wc -c ~/.asha/learnings.md
# Target: under 51,200 bytes (50KB)
```

If over budget, demote the lowest-confidence entries until under threshold.

<!-- CONSOLIDATION-PASS:END -->

<!-- RED-FLAGS:START -->
## Red Flags — Stop and Reconsider

If you catch yourself thinking any of the following while running `/save`, stop. The thought itself is the warning. Do the action in the right column instead.

| Rationalization (the thought) | What it actually means | Do this instead |
|---|---|---|
| "Synthesis is slow and the events look thin — I'll skip pattern_analyzer and just commit Memory/." | The synthesis step IS the value of `/save`. Skipping it commits stale activeContext and silently breaks future cold-starts. | Run synthesis. If it is genuinely too slow for this checkpoint, raise that as a separate issue — do not bypass it silently. |
| "Next Steps in activeContext came out generic ('Review and plan next session') — that's good enough, the user can fill it in." | This is the exact failure mode the project CLAUDE.md "Session Handoff Quality" rule calls out by name. A cold-start session reading this cannot act. | Replace generic Next Steps with concrete file paths, tool names, blocked decisions, and the first thing next session should pick up — per CLAUDE.md rule. |
| "I captured signals to keeper.md/voice.md but the user didn't ask to see them — I'll just commit." | Calibration is two-way. Writing to keeper without surfacing the signal lets miscalibration compound silently across sessions. | Surface the captured signals in chat before committing. Let the user confirm or correct the read. |
| "Ratchet check found a repeated pattern but proposing a skill/hook feels like scope creep — I'll skip it just this once." | The Ratchet on Save rule exists precisely because "just this once" is how guardrails never get built. The pattern will repeat. | Run the ratchet check and surface the proposal. The user decides whether to act — your job is to flag, not to filter. |
| "User said save, push is the default, I'll go ahead even though they mentioned not wanting to push earlier." | You are about to push on autopilot against a `--no-push` intent the user already signaled. Memory commits are remote-visible. | Honor `--no-push`. If intent is ambiguous, ask one line before pushing. |
| "Event log is empty — nothing happened this session, nothing to save." | An empty event log during a real session is itself a signal (silence marker on, hook misfire, watcher dead). Treating it as "no-op" hides the failure. | Investigate why the log is empty before committing. Note the cause in the commit message or activeContext. |
| "Auto-generated commit message is fine, this session was routine." | If the session crossed a milestone or made a load-bearing decision, the auto-message buries it under a timestamp and the next reviewer can't find it. | Ask one line: "Anything specific to flag in the commit message?" — takes seconds, prevents history archaeology later. |
| "The 'What Was Accomplished' block in activeContext is just a generic file count, but the user can fix it later — committing now." | Cold-start sessions read activeContext as authoritative. A `Created N file(s)` lead block teaches the next instance that this is acceptable handoff. The clobber bug recurs through laziness, not through the merge code. | Replace the auto-fallback block with a concrete session narrative BEFORE commit. See Verification Gate Check 1. |
| "I made code changes but ran out of time — I'll skip the test/type-check and let CI catch it." | "Show me the green" exists because completion claims without verification recur 3+ times in this user's history. Skipping is how the pattern stays alive. | Run the verification. If you cannot, mark it `unverified` in activeContext Next Steps with the specific command that needs to be run. |

**General rule**: rationalization that *sounds* reasonable in the moment is the strongest signal. Genuine exceptions are rare; rationalized shortcuts are common.
<!-- RED-FLAGS:END -->
