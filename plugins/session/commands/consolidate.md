---
name: session-consolidate
description: "Periodic memory consolidation: merge drift, resolve contradictions, retire concluded records, enforce index budgets"
argument-hint: "[--days N]  (signal window, default 14)"
allowed-tools: ["Bash", "Read", "Edit", "Write", "Grep", "Glob"]
---

# Session Consolidate

A periodic maintenance pass over Asha's memory stores — the counterpart to
`/save`. `/save` *accumulates* (each session appends what it learned);
consolidation *compacts* (merge drifted facts, resolve contradictions, retire
concluded records, keep the injected index inside budget). Modeled on the
four-phase background-consolidation pattern used by harness-native memory
systems, mapped onto Asha's stores.

**When to run:** the session-start learnings index reports omitted concepts
(`[N more concept(s) omitted for budget…]`), `activeContext.md` has drifted
from disk truth, keeper.md's calibration log has grown past its synthesis, or
roughly monthly.

**Scope guard:** operate only on Asha-owned stores — `~/.asha/learnings/`,
`~/.asha/operation.md`, `Memory/` in the current project, and (interactive,
persona sessions only) `~/.asha/keeper.md` + `~/.asha/voice.md`. NEVER write
the harness-native store (`~/.claude/projects/*/memory/`) — it has its own
consolidation and its own writer.

## Protocol

### Phase 1 — Orient

Build the current picture without deep reads:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "$HOME/.asha/config.json" 2>/dev/null)}"
[[ -n "$ASHA_ROOT" ]] || { echo "ERROR: asha_root unresolved" >&2; exit 1; }
T="$ASHA_ROOT/plugins/session/tools"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

python3 "$T/learnings_manager.py" list                       # categories, counts, avg confidence
python3 "$T/learnings_manager.py" render-index --max-bytes 3000 | tail -3   # is the injection truncating?
ls "$PROJECT_DIR/Memory/sessions/archive/" 2>/dev/null | tail -5
```

Read `~/.asha/learnings/index.md`, `Memory/activeContext.md`, and — persona
sessions only — the `## Calibration Log` tail of `~/.asha/keeper.md`.

### Phase 2 — Gather signal

Recent window first (default 14 days; `--days N` overrides):

```bash
python3 "$T/learnings_manager.py" link-candidates --days "${DAYS:-14}"      # recently-touched concepts
python3 "$T/memory_nudge.py" stats --days "${DAYS:-14}"                     # which nudges fired / were acted on
```

Then classify, reading concept files only where the index line is ambiguous:

- **Concluded records** — acceptance records for shipped phases, one-time
  migrations, anything whose trigger can never recur. Candidates for `retire`.
- **Contradicted facts** — a learning or activeContext claim the recent
  sessions disproved. Verify against disk truth before acting (Read the code
  or config it describes; the record is not its own evidence).
- **Near-duplicates** — two concepts one pattern apart. Candidates for merge.
- **Drift** — activeContext references to paths/commands that no longer
  exist, relative dates ("last week"), Next Steps already done.

### Phase 3 — Consolidate

Apply, narrowest tool first — each change individually, not as a bulk sweep:

- Retire concluded records (keeps full text in `~/.asha/learnings-archive/`):

  ```bash
  python3 "$T/learnings_manager.py" retire --id <id> --reason "<why it is concluded>"
  ```

- Contradict disproven-but-live patterns (drops confidence, keeps the record):

  ```bash
  python3 "$T/learnings_manager.py" contradict --id <id> --project <proj> --reason "<evidence>"
  ```

- Merge near-duplicates: fold the weaker concept's unique evidence into the
  stronger via `add` (upsert by id), then `retire` the husk with reason
  "merged into <id>".
- Rewrite drifted `activeContext.md` sections to current truth; convert all
  relative dates to absolute (`YYYY-MM-DD`).
- **keeper.md calibration log** (interactive + `ASHA_PERSONA=1` only): back up
  first (`cp ~/.asha/keeper.md ~/.asha/keeper.md.bak-$(date -u +%Y%m%dT%H%M%SZ)`),
  synthesize log rows into the profile sections above, then truncate the raw
  log to entries newer than the window. Identity files get a confirmation
  before the write — state what will be folded and wait for the Keeper's yes.

### Phase 4 — Prune and index

```bash
python3 "$T/learnings_manager.py" prune-links                # drop links to retired/merged ids
python3 "$T/learnings_manager.py" rebuild-index
python3 "$T/validate.py" ~/.asha/learnings --strict          # structural health (warn-only)
python3 "$T/learnings_manager.py" render-index --max-bytes 3000 | tail -3   # budget check: no omission tail = done
```

### Report

End with a compact accounting: retired (id + reason), contradicted, merged,
activeContext sections rewritten, calibration rows folded, and the
session-start injection size before → after. Synthesis over transcription —
if a store needed nothing, say so and touch nothing.
