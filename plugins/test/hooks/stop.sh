#!/usr/bin/env bash
# Canary Stop-lifecycle hook for the asha-marketplace installer.
# Side effect: touches a marker file with a timestamp when the hook fires.
# Purpose: proves settings.json registration succeeded and the merged
# command path resolves.

if [ "${ASHA_CANARY_MARKER+x}" = x ]; then
  MARKER="$ASHA_CANARY_MARKER"
elif [ -n "${XDG_RUNTIME_DIR:-}" ] \
    && [ -d "$XDG_RUNTIME_DIR" ] \
    && [ -O "$XDG_RUNTIME_DIR" ]; then
  MARKER="$XDG_RUNTIME_DIR/asha-canary-hook-fired"
else
  MARKER="$(mktemp "${TMPDIR:-/tmp}/asha-canary-hook.XXXXXX" 2>/dev/null)" \
    || { echo '{}'; exit 0; }
fi

date -u +"%Y-%m-%dT%H:%M:%SZ fired ${BASH_SOURCE[0]}" >> "$MARKER" 2>/dev/null \
  || { echo '{}'; exit 0; }
# Empty JSON object so Claude Code doesn't misinterpret the hook's stdout.
echo '{}' || exit 0
exit 0
