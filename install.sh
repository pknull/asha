#!/bin/bash
# Asha Install Script - Sets up Asha in a project
# Run from project root: ./Asha/install.sh

set -euo pipefail

# Detect project root
if [[ -d "Asha" ]]; then
    PROJECT_ROOT="$(pwd)"
elif [[ -d "../Asha" ]]; then
    PROJECT_ROOT="$(cd .. && pwd)"
else
    echo "Error: Run from project root (where Asha/ directory is located)"
    exit 1
fi

ASHA_DIR="$PROJECT_ROOT/Asha"

echo "Installing Asha in: $PROJECT_ROOT"
echo ""

# 1. Create directory structure
echo "Creating directory structure..."
mkdir -p "$PROJECT_ROOT/Memory/sessions/archive"
mkdir -p "$PROJECT_ROOT/Memory/reasoning_bank"
mkdir -p "$PROJECT_ROOT/Work/markers"
mkdir -p "$PROJECT_ROOT/.claude/hooks"

# 2. Symlink hooks into .claude/hooks/
echo "Installing hooks..."
for hook in "$ASHA_DIR/hooks/"*; do
    if [[ -f "$hook" && "$(basename "$hook")" != "common.sh" ]]; then
        hook_name=$(basename "$hook")
        target="$PROJECT_ROOT/.claude/hooks/$hook_name"
        if [[ -L "$target" ]]; then
            rm "$target"
        fi
        ln -sf "$ASHA_DIR/hooks/$hook_name" "$target"
        chmod +x "$target"
        echo "  → $hook_name"
    fi
done

# Also symlink common.sh (needed by hooks)
ln -sf "$ASHA_DIR/hooks/common.sh" "$PROJECT_ROOT/.claude/hooks/common.sh"

# 3. Make tools executable
echo "Setting permissions..."
chmod +x "$ASHA_DIR/tools/"*.sh 2>/dev/null || true
chmod +x "$ASHA_DIR/tools/"*.py 2>/dev/null || true

# 4. Copy templates if Memory files don't exist
echo "Checking Memory files..."
for tmpl in "$ASHA_DIR/templates/"*.md; do
    if [[ -f "$tmpl" ]]; then
        filename=$(basename "$tmpl")
        target="$PROJECT_ROOT/Memory/$filename"
        if [[ ! -f "$target" ]]; then
            cp "$tmpl" "$target"
            echo "  → Created Memory/$filename (from template)"
        else
            echo "  → Memory/$filename exists (skipped)"
        fi
    fi
done

# 5. Initialize ReasoningBank database
echo "Initializing ReasoningBank..."
if command -v python3 >/dev/null 2>&1; then
    python3 "$ASHA_DIR/tools/reasoning_bank.py" stats >/dev/null 2>&1 && \
        echo "  → Database initialized" || \
        echo "  → Warning: Could not initialize database"
else
    echo "  → Warning: python3 not found, skipping database init"
fi

echo ""
echo "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Edit Memory/communicationStyle.md to define your assistant's voice"
echo "  2. Edit Memory/activeContext.md to describe your project"
echo "  3. Run: Asha/tools/save-session.sh --interactive"
echo ""
