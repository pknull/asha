---
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

Run the synthesis pipeline:

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/pattern_analyzer.py" synthesize --days 7
```

Then archive and rotate events:

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/save-session.sh" --archive-only
```

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

Then commit Memory changes:

```bash
cd "$PROJECT_DIR"
git add Memory/
git commit -m "Session save: ${ARGUMENTS:-$(date -u '+%Y-%m-%d %H:%M UTC')}"
```

Push unless `--no-push` specified:

```bash
git push
```

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

**General rule**: rationalization that *sounds* reasonable in the moment is the strongest signal. Genuine exceptions are rare; rationalized shortcuts are common.
<!-- RED-FLAGS:END -->
