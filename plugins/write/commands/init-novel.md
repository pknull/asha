---
description: "Initialize novel state directory structure (bible/state/timeline)"
argument-hint: "[project-path]"
allowed-tools: ["Bash"]
---

# Initialize Novel State

Create the standardized directory structure for novel writing with state tracking.

## Usage

```bash
/write:init-novel                    # Initialize in current directory
/write:init-novel /path/to/project   # Initialize in specific directory
```

## Execution

Run the initialization script:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/novel-state/scripts/init_novel_state.py" [project-path]
```

## Created Structure

```
Work/novel/
├── bible/                     # Immutable style reference
│   ├── voice.md               # Authoritative voice/style guide
│   ├── rules.md               # Story constraints
│   ├── characters/            # Character sheets
│   │   └── .gitkeep
│   └── world/                 # Worldbuilding
│       └── .gitkeep
├── state/                     # Per-chapter snapshots
│   └── .gitkeep
├── timeline/
│   ├── master.md              # Canonical timeline
│   └── events.json            # Structured event log
└── story/
    ├── synopsis.md            # Story summary
    └── outline.md             # Chapter-level outline
```

## Next Steps

After initialization:

1. Edit `Work/novel/bible/voice.md` with your style guide
2. Edit `Work/novel/bible/rules.md` with story constraints
3. Edit `Work/novel/story/synopsis.md` with your story summary
4. Edit `Work/novel/story/outline.md` with chapter structure
5. Create character files in `Work/novel/bible/characters/`

## Starting a Chapter

```bash
mkdir -p Work/novel/state/ch01
ln -sfn ch01 Work/novel/state/current
```

## Integration

- Consistency-checker reads from `bible/` for validation
- Perplexity-gate writes metrics to `state/[chapter]/metrics.json`
- Book-export reads from `story/manuscript.md`

## File Purposes

| File | Purpose |
|------|---------|
| `bible/voice.md` | Authoritative voice guide (prohibited/required patterns) |
| `bible/rules.md` | Immutable story constraints |
| `state/current` | Symlink to active chapter |
| `state/[ch]/metrics.json` | Perplexity gate results per draft |
| `timeline/events.json` | Structured timeline for consistency |
