#!/usr/bin/env bash
set -euo pipefail

# Post-edit linting hook - runs formatter/linter based on file extension
# Async hook - warnings go to stderr, doesn't block tool execution

# Read JSON input from stdin
INPUT=$(cat)

# Extract file path from tool input
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [[ -z "$FILE_PATH" || ! -f "$FILE_PATH" ]]; then
    echo "$INPUT"
    exit 0
fi

# Get extension (lowercase)
EXT="${FILE_PATH##*.}"
EXT="${EXT,,}"

# Track if we ran anything
RAN_TOOL=false
TOOL_OUTPUT=""

case "$EXT" in
    # TypeScript / JavaScript
    ts|tsx|js|jsx|mjs|cjs)
        if command -v biome &>/dev/null; then
            TOOL_OUTPUT=$(biome format --write "$FILE_PATH" 2>&1) && RAN_TOOL=true
        elif command -v prettier &>/dev/null; then
            TOOL_OUTPUT=$(prettier --write "$FILE_PATH" 2>&1) && RAN_TOOL=true
        fi
        # Type check (non-blocking, runs in background)
        if [[ "$EXT" == "ts" || "$EXT" == "tsx" ]] && command -v tsc &>/dev/null; then
            # Find tsconfig by walking up
            DIR=$(dirname "$FILE_PATH")
            while [[ "$DIR" != "/" ]]; do
                if [[ -f "$DIR/tsconfig.json" ]]; then
                    (cd "$DIR" && tsc --noEmit 2>&1 | head -20) &
                    break
                fi
                DIR=$(dirname "$DIR")
            done
        fi
        ;;

    # Python
    py|pyi)
        if command -v ruff &>/dev/null; then
            ruff format "$FILE_PATH" 2>/dev/null || true
            TOOL_OUTPUT=$(ruff check "$FILE_PATH" 2>&1) || true
            RAN_TOOL=true
        elif command -v black &>/dev/null; then
            TOOL_OUTPUT=$(black --quiet "$FILE_PATH" 2>&1) && RAN_TOOL=true
        fi
        # Type check (non-blocking, runs in background)
        if command -v mypy &>/dev/null; then
            (mypy "$FILE_PATH" --ignore-missing-imports 2>&1 | head -20) &
        fi
        ;;

    # Go
    go)
        if command -v gofmt &>/dev/null; then
            gofmt -w "$FILE_PATH" 2>/dev/null || true
            RAN_TOOL=true
        fi
        if command -v go &>/dev/null; then
            TOOL_OUTPUT=$(go vet "$FILE_PATH" 2>&1) || true
        fi
        ;;

    # Rust
    rs)
        if command -v rustfmt &>/dev/null; then
            TOOL_OUTPUT=$(rustfmt "$FILE_PATH" 2>&1) && RAN_TOOL=true
        fi
        ;;

    # Markdown
    md)
        if command -v prettier &>/dev/null; then
            TOOL_OUTPUT=$(prettier --write "$FILE_PATH" 2>&1) && RAN_TOOL=true
        elif command -v markdownlint &>/dev/null; then
            TOOL_OUTPUT=$(markdownlint "$FILE_PATH" 2>&1) || true
            RAN_TOOL=true
        fi
        ;;

    # JSON
    json)
        if command -v prettier &>/dev/null; then
            TOOL_OUTPUT=$(prettier --write "$FILE_PATH" 2>&1) && RAN_TOOL=true
        elif command -v jq &>/dev/null; then
            # Format in place using jq
            TMP=$(mktemp)
            if jq '.' "$FILE_PATH" > "$TMP" 2>/dev/null; then
                mv "$TMP" "$FILE_PATH"
                RAN_TOOL=true
            else
                rm -f "$TMP"
            fi
        fi
        ;;

    # YAML
    yaml|yml)
        if command -v prettier &>/dev/null; then
            TOOL_OUTPUT=$(prettier --write "$FILE_PATH" 2>&1) && RAN_TOOL=true
        fi
        ;;

    # Shell
    sh|bash)
        if command -v shfmt &>/dev/null; then
            shfmt -w "$FILE_PATH" 2>/dev/null && RAN_TOOL=true
        fi
        if command -v shellcheck &>/dev/null; then
            TOOL_OUTPUT=$(shellcheck "$FILE_PATH" 2>&1) || true
        fi
        ;;
esac

# Output warnings if tool produced any
if [[ -n "$TOOL_OUTPUT" && "$TOOL_OUTPUT" != *"unchanged"* ]]; then
    echo "[post-edit-lint] $EXT: $TOOL_OUTPUT" >&2
fi

# Pass through original input
echo "$INPUT"
