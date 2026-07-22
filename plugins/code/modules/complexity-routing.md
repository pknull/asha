# Complexity Routing Module — Tier-Based Model Selection

**Applies to**: `/code:orchestrate` Phase 0. Determines which model implements a task and whether Opus plan-review is required first.

## Why routing exists

A trivial typo fix doesn't need Opus reasoning. A change to install.sh — code that mutates the session you're running in — does. Routing by complexity lets the cheap path stay cheap and forces deliberation on changes that warrant it.

## Tier definitions

| Tier | Default model | Criteria |
|------|---------------|----------|
| **Trivial** | Haiku | ≤2 files; mechanical changes only — typos, version bumps, single-line corrections, README example updates, comment fixes. No logic changes. |
| **Low** | Sonnet | Single skill, command, hook, or agent definition. Bounded scope, no cross-file coupling. |
| **Medium** | Sonnet | Multi-file feature within one plugin, or refactor under ~10 files. Logic changes but no architectural decisions. |
| **High** | Opus plan-review → Sonnet impl | New plugin, multi-plugin refactor, install/lifecycle changes, or any path matched by `high_complexity_paths` in project rules. |

## High-tier triggers

Tier escalates to **High** if **any** of these hold:

1. **Path match**: any modified path matches a glob in `.claude/orchestrate-rules.json` → `high_complexity_paths`
2. **New plugin**: a new directory is created under `plugins/`
3. **Cross-plugin scope**: changes span ≥2 plugin directories
4. **Self-edit risk**: changes affect installer, hooks, namespace registry, or anything that loads at session start
5. **User override**: invocation includes `--tier=high`

## Trivial-tier guards

Tier downgrades to **Trivial** only if **all** hold:

1. ≤2 files modified
2. No file in `high_complexity_paths`
3. No file is a new file in `plugins/*/commands/` or `plugins/*/agents/` (those are Low minimum — they introduce new behavior)
4. Diff has no control flow or function-signature changes

## Routing preflight protocol

Phase 0 of `/code:orchestrate`. Runs once, before any implementation phase:

```
PHASE 0 — ROUTING
1. Load .claude/orchestrate-rules.json (if present at repo root)
2. Determine change scope:
   - If task is pre-implementation: infer from task description
   - If task is on existing diff: read `git diff --name-only`
3. Apply tier rules above
4. Emit declaration:
   Tier: {Trivial|Low|Medium|High}
   Model: {Haiku|Sonnet|Opus+Sonnet}
   Reason: {one-line justification}
5. If High: insert `general-purpose` (Opus, design charge) as first phase before implementation
6. If user provides --tier=X, use that tier and note "(user override)" in reason
```

On harnesses without subagent spawning, execute the plan-review phase inline (same design charge); the tier declaration and handoff format are unchanged.

## Override syntax

```
/code:orchestrate --tier=high feature "Refactor namespace registry"
/code:orchestrate --tier=trivial bugfix "Fix typo in panel README"
```

User overrides bypass inference. Useful when:

- Task description is ambiguous about scope
- Pre-implementation tasks where diff doesn't exist yet
- You know something the routing doesn't (e.g., "this looks small but touches a load-bearing assumption")

## Self-review calibration

The implementing agent **must** end its handoff with a `claimed_status` declaration. The review phase records the actual outcome. Both are appended to `~/.asha/metrics/orchestrate.jsonl`.

### Implementer handoff requirement

Every implementing agent's final message must include:

```
claimed_status: ready | needs-work | blocked
```

- **ready** — implementer believes all gates will pass
- **needs-work** — known issues remain (lists them)
- **blocked** — cannot proceed without external input

### Review phase recording

After review/gate phase completes, append one line to `~/.asha/metrics/orchestrate.jsonl`:

```json
{"ts":"<ISO-8601>","workflow":"<type>","tier":"<tier>","model":"<model>","claimed":"<status>","review":"pass|fail","gates_failed":["<gate>",...],"task":"<one-line summary>"}
```

The `mkdir -p ~/.asha/metrics` runs defensively before append. If write fails (disk full, permissions), log to stderr and continue — calibration is observability, not a blocker.

### Reading calibration

```bash
asha calibration            # last 30 runs summary
asha calibration --tier=high   # filter to high-tier
asha calibration --tail=10  # last 10 runs detail
```

False-positive rate = `claimed=ready AND review=fail` / `total claimed=ready`. Sustained rates above ~25% mean implementer self-review is miscalibrated and the agent prompt needs tightening.

## Routing examples

| Task | Tier | Reason |
|------|------|--------|
| Fix typo in `panel/README.md` | Trivial | 1 file, README, mechanical |
| Bump `plugin.json` version | Trivial | 1 file, mechanical |
| Add new command `/code:lint` | Low | Single command file, bounded |
| Add new skill with 3-file structure | Low | Bounded to one skill dir |
| Refactor 5 hook scripts | Medium | Multi-file, single concern |
| Edit `install.sh` to add new namespace | **High** | Path match: install.sh |
| Add new plugin `auto-merge` | **High** | New plugin trigger |
| Change hook lifecycle in 2 plugins | **High** | Cross-plugin + hooks |
| Change `namespaces.json` registry | **High** | Path match: namespaces.json |

## Project rules file

`.claude/orchestrate-rules.json` at the repo root. Optional — absent file means no overrides, all tier inference happens from defaults above.

```json
{
  "high_complexity_paths": ["glob", ...],
  "trivial_complexity_paths": ["glob", ...]
}
```

Globs are matched against repo-relative paths from `git diff --name-only`. Standard glob syntax — `**` for recursive match.
