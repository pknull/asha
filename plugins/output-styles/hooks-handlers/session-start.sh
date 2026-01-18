#!/bin/bash
set -euo pipefail
# SessionStart hook - Injects active output style into session context

CONFIG_FILE="$HOME/.claude/active-output-style"
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Check if a style is configured
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo '{}'
    exit 0
fi

STYLE_NAME=$(cat "$CONFIG_FILE" 2>/dev/null | tr -d '[:space:]')

# Validate style exists
STYLE_FILE="$PLUGIN_ROOT/styles/${STYLE_NAME}.md"
if [[ ! -f "$STYLE_FILE" ]]; then
    echo '{}'
    exit 0
fi

# Extract style content (skip YAML frontmatter)
STYLE_CONTENT=$(awk '
    /^---$/ { if (in_frontmatter) { in_frontmatter=0; next } else { in_frontmatter=1; next } }
    !in_frontmatter { print }
' "$STYLE_FILE")

# Escape for JSON
ESCAPED_CONTENT=$(echo "$STYLE_CONTENT" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')

# Return hook output with additionalContext
cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": $ESCAPED_CONTENT
  }
}
EOF

exit 0
