#!/bin/bash
# Wrapper script to run Python tools using the project's virtual environment
# Usage: ./run-python.sh <script.py> [args...]
#
# Looks for venv in order:
# 1. Project's .asha/.venv/ (plugin pattern)
# 2. System python3

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detect project directory
if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
    PROJECT_DIR="$CLAUDE_PROJECT_DIR"
elif command -v git >/dev/null 2>&1; then
    PROJECT_DIR=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
fi

# Use project's .asha venv if available, otherwise system python
if [[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/.venv/bin/python" ]]; then
    PYTHON="$PROJECT_DIR/.asha/.venv/bin/python"
else
    PYTHON="python3"
fi

exec "$PYTHON" "$@"
