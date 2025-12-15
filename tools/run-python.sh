#!/bin/bash
# Wrapper script to run Python tools using the Asha virtual environment
# Usage: ./run-python.sh <script.py> [args...]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASHA_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$ASHA_DIR/.venv"

# Use venv python if available, otherwise system python
if [[ -f "$VENV_DIR/bin/python" ]]; then
    PYTHON="$VENV_DIR/bin/python"
else
    PYTHON="python3"
fi

exec "$PYTHON" "$@"
