# Parallel Agents Module — Concurrent Agent Coordination

**Applies to**: Multiple agents/Claude sessions working simultaneously, file conflict prevention, version control coordination.

## jj + git (Hybrid Workflow)

When `.jj/` exists in a repo, use jj for local work, git for remote sync.

### Why jj?

jj (Jujutsu) enables parallel agent work without conflicts blocking operations:

- Working copy is always a valid commit (no staging dance)
- Conflicts recorded as data, not blocking errors
- Each agent works on isolated changes, merge later

### Command Reference

| Task | jj Command | Notes |
|------|------------|-------|
| Start new work | `~/bin/jj new -m "Agent: task"` | Creates isolated change |
| Check status | `~/bin/jj status` | Working copy is a commit |
| View log | `~/bin/jj log` | Cleaner visualization |
| Finish work | `~/bin/jj squash` | Fold into parent |
| Push to remote | `git push` or `~/bin/jj git push` | Standard git remote |

### Multi-Agent Pattern

```bash
# Agent A (terminal 1)
~/bin/jj new -m "Agent A: refactor utils"
# ... edits files ...

# Agent B (terminal 2, concurrent)
~/bin/jj new -m "Agent B: add feature X"
# ... edits files ...

# Both complete. See all changes:
~/bin/jj log -r 'heads(all())'

# Merge when ready:
~/bin/jj new <change-a> <change-b>
~/bin/jj squash --into @--
git push
```

### When to Use

- Multiple Claude sessions working on same codebase
- Parallel agent execution (Task tool with `run_in_background`)
- Any scenario where concurrent edits might conflict

If `.jj/` doesn't exist, use standard git workflow.

---

## Module Ownership

Agents declare ownership in their frontmatter to prevent conflicts:

```yaml
ownership:
  owns:
    - "**/*.py"           # Full authority
  shared:
    - "**/*.md": [other-agent]  # Coordinate with named agents
```

### Ownership Semantics

| Ownership | Meaning | Action |
|-----------|---------|--------|
| `owns` | Full authority over these paths | Edit freely |
| `shared` | Multiple agents may touch | Coordinate via jj or claims |
| (unlisted) | No declared ownership | Check before editing |

### Conflict Resolution

1. More specific glob wins (`src/api/*.ts` > `**/*.ts`)
2. If equal specificity, coordinate via jj (parallel changes, merge later)
3. When in doubt, ask user

---

## File Ownership During a Run

Assign concrete path ownership in each worker's task before parallel edits
begin. Ownership is carried by the orchestration contract and agent messages,
not a global event store. Workers must state that they are not alone in the
codebase, avoid reverting other edits, and report every modified path at handoff.

Before touching a shared path, inspect live state (`git status --short -- PATH`)
and coordinate with the owning agent. If concurrent editing is unavoidable,
use isolated worktrees or jj changes and merge deliberately.

---

## TDD-First Coordination

For new features with multiple agents, spawn TDD agent BEFORE implementation agents.

### Why

Tests define interfaces. Implementation agents write code that satisfies those interfaces. This prevents:

- Integration surprises
- Interface mismatches between parallel agents
- Scope creep

### Protocol

```
1. User requests feature
2. Spawn: tdd agent → writes interface tests (RED)
3. Tests define: function signatures, return types, error cases
4. Spawn: implementation agent → writes code to pass tests (GREEN)
5. Implementation agent runs tests, iterates until green
6. Optional: refactor-cleaner cleans up (REFACTOR)
```

Note: on harnesses without subagent spawning, execute each phase inline in sequence — the protocol order is unchanged.

### When to Apply

- New modules or significant features
- When spawning multiple implementation agents in parallel
- When interface clarity matters (APIs, shared utilities)

### When to Skip

- Bug fixes in existing code (tests already exist)
- Single-file changes
- Documentation or config changes

---

## Coordination Stack

```
Static: ownership declarations (who normally owns what)
   ↓
Dynamic: file claims (who's working on what NOW)
   ↓
Fallback: jj (async conflict resolution if claims ignored)
```

Each layer catches what the previous layer misses.
