---
name: session-init
description: "Initialize the compact Memory v2 project contract"
argument-hint: ""
allowed-tools: ["Bash", "Read"]
---

# Initialize Memory v2

Resolve the repository root from the harness payload/current Git worktree and
run:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
python3 "$ASHA_ROOT/plugins/session/tools/memory_v2.py" init --project-dir "$PROJECT_DIR"
```

This creates only:

```text
.asha/config.json                    # stable project_id + memory_version: 2
Memory/activeContext.md              # four-section, 4 KiB publication
Memory/decisions.md                  # current binding decisions
Work/session-state/                  # ignored unpublished recovery snapshots
.gitignore                           # managed Memory + Control private rules
```

For repositories that also use workspace init/repair, the optional project config
setting `"memory_visibility": "private"` keeps the workspace's `Memory/`, `Work/`,
`knowledge/`, `memory-local/` and `.asha/workspace*.json` ignored. Preserve existing
config keys and the project identity when adding it. `/session:init` preserves
this setting; apply its workspace rules with `asha workspace init` or, for an
existing workspace, `asha workspace doctor --root . --fix`. Omitted or `"tracked"`
retains the existing trackable workspace publications. This setting neither
untracks existing files nor changes save scope; Git cleanup is a separate step.

Initialization preserves an existing `project_id`, existing published v2
files, and all legacy material. If either published path contains a legacy or
invalid v2 handoff, initialization fails before changing the config and routes
that material to explicit `/session:consolidate`; it never deletes or silently
republishes it. It adds narrow ignores for `/Work/session-state/`, the durable
private `/Work/memory-migration/` review plan, and
`/.asha/control-task.json`, `/.asha/result.json`, and `/.asha/outbox/`.
These fixed Control transport paths do not ignore the rest of `.asha/`.
Initialization upgrades legacy marker-only blocks idempotently and preserves
user comments and rules, reasserting the managed suffix after later negations.
The rules must be committed before an immutable task base gains that authority;
changing only the working tree or global excludes does not authorize an older
selected commit. A typed prerequisite offers only its listed missing Control
rules: applying it changes `.gitignore`, never commits or starts a task. Commit
the reviewed patch and explicitly select the new base before retrying.
