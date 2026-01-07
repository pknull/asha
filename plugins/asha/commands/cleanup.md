---
description: "Remove legacy asha/ nested repo installation files"
argument-hint: "Optional: --dry-run (show what would be removed without removing)"
allowed-tools: ["Bash", "Read"]
---

# Cleanup Legacy Asha Installation

Removes files from the old nested-repo installation pattern.

Arguments: $ARGUMENTS

## What This Removes

Legacy files from before Asha was a plugin:
- `asha/` directory (nested repo or submodule)
- `.claude/hooks/hooks.json` (if contains asha references)
- `.claude/commands/*.md` symlinks (if point to asha/)
- `.opencode/` directory (OpenCode support was dropped)
- `.gitmodules` entry for asha (if was a submodule)

## Protocol

### Step 1: Detect Dry Run Mode

```bash
DRY_RUN=false
[[ "$ARGUMENTS" == *"--dry-run"* ]] && DRY_RUN=true
```

### Step 2: Inventory Legacy Files

```bash
echo "Scanning for legacy Asha files..."

# Check for asha/ directory
if [[ -d "${CLAUDE_PROJECT_DIR}/asha" ]]; then
    echo "Found: asha/ directory"
fi

# Check for hooks.json with asha refs
if [[ -f "${CLAUDE_PROJECT_DIR}/.claude/hooks/hooks.json" ]]; then
    if grep -q "asha/" "${CLAUDE_PROJECT_DIR}/.claude/hooks/hooks.json" 2>/dev/null; then
        echo "Found: .claude/hooks/hooks.json (contains asha references)"
    fi
fi

# Check for command symlinks pointing to asha/
for cmd in "${CLAUDE_PROJECT_DIR}/.claude/commands/"*.md; do
    if [[ -L "$cmd" ]] && [[ "$(readlink "$cmd")" == *"asha/"* ]]; then
        echo "Found: $cmd (symlink to asha/)"
    fi
done

# Check for .opencode directory
if [[ -d "${CLAUDE_PROJECT_DIR}/.opencode" ]]; then
    echo "Found: .opencode/ directory"
fi

# Check for .gitmodules with asha
if [[ -f "${CLAUDE_PROJECT_DIR}/.gitmodules" ]]; then
    if grep -q "asha" "${CLAUDE_PROJECT_DIR}/.gitmodules" 2>/dev/null; then
        echo "Found: .gitmodules contains asha entry"
    fi
fi
```

### Step 3: Remove Legacy Files (unless dry-run)

If `--dry-run`, just show what would be removed and exit.

Otherwise:

```bash
# Remove asha/ directory
if [[ -d "${CLAUDE_PROJECT_DIR}/asha" ]]; then
    rm -rf "${CLAUDE_PROJECT_DIR}/asha"
    echo "Removed: asha/"
fi

# Remove hooks.json if it contains asha refs
if [[ -f "${CLAUDE_PROJECT_DIR}/.claude/hooks/hooks.json" ]]; then
    if grep -q "asha/" "${CLAUDE_PROJECT_DIR}/.claude/hooks/hooks.json" 2>/dev/null; then
        rm "${CLAUDE_PROJECT_DIR}/.claude/hooks/hooks.json"
        echo "Removed: .claude/hooks/hooks.json"
    fi
fi

# Remove command symlinks pointing to asha/
for cmd in "${CLAUDE_PROJECT_DIR}/.claude/commands/"*.md; do
    if [[ -L "$cmd" ]] && [[ "$(readlink "$cmd")" == *"asha/"* ]]; then
        rm "$cmd"
        echo "Removed: $cmd"
    fi
done

# Remove .opencode directory
if [[ -d "${CLAUDE_PROJECT_DIR}/.opencode" ]]; then
    rm -rf "${CLAUDE_PROJECT_DIR}/.opencode"
    echo "Removed: .opencode/"
fi
```

### Step 4: Handle .gitmodules

If .gitmodules contains asha entry:

```bash
if [[ -f "${CLAUDE_PROJECT_DIR}/.gitmodules" ]]; then
    if grep -q "asha" "${CLAUDE_PROJECT_DIR}/.gitmodules" 2>/dev/null; then
        echo ""
        echo "WARNING: .gitmodules contains asha entry"
        echo "Manual removal required:"
        echo "  git submodule deinit asha"
        echo "  git rm asha"
        echo "  rm -rf .git/modules/asha"
    fi
fi
```

### Step 5: Report

```bash
echo ""
echo "Cleanup complete!"
echo ""
echo "Next steps:"
echo "  1. Run /asha:init to set up plugin-based Asha"
echo "  2. Commit the cleanup: git add -A && git commit -m 'chore: migrate from nested asha to plugin'"
```

## Safety Notes

- This does NOT touch Memory/ files (those are preserved)
- This does NOT remove .asha/ (that's the new plugin runtime directory)
- Always run `--dry-run` first to see what will be removed
- Git history is preserved; you can recover files if needed
