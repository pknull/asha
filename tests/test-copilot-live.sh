#!/usr/bin/env bash
# test-copilot-live.sh — authenticated runtime canary for Copilot CLI.
#
# This is deliberately opt-in: it sends one prompt to the authenticated local
# Copilot CLI. It proves the custom-instructions directory seam used by
# `asha copilot`; install/doctor tests cover generated local artifacts.
set -euo pipefail

# Sandbox hermeticity: an operator shell exporting these must not leak in.
unset ASHA_HOME XDG_STATE_HOME XDG_DATA_HOME 2>/dev/null || true

if [[ "${ASHA_LIVE_COPILOT:-0}" != "1" ]]; then
  echo "SKIP: set ASHA_LIVE_COPILOT=1 to run the authenticated Copilot runtime canary"
  exit 0
fi

command -v copilot >/dev/null 2>&1 || {
  echo "FAIL: copilot CLI is not on PATH" >&2
  exit 1
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
INSTRUCTIONS="$WORK/instructions"
SENTINEL="ASHA-COPILOT-INSTRUCTIONS-CANARY"
mkdir -p "$INSTRUCTIONS/.github/instructions"
cat > "$INSTRUCTIONS/.github/instructions/asha-canary.instructions.md" <<EOF
---
applyTo: "**"
---

When asked for the Asha Copilot instruction canary, reply with exactly:
$SENTINEL
EOF

output="$(
  COPILOT_HOME="$WORK/copilot" \
  COPILOT_CUSTOM_INSTRUCTIONS_DIRS="$INSTRUCTIONS" \
  copilot -p "What is the Asha Copilot instruction canary? Reply with the exact sentinel and no other text." \
  </dev/null 2>&1
)" || {
  echo "FAIL: Copilot CLI canary invocation failed" >&2
  printf '%s\n' "$output" >&2
  exit 1
}

if [[ "$output" == *"$SENTINEL"* ]]; then
  echo "PASS: Copilot loaded COPILOT_CUSTOM_INSTRUCTIONS_DIRS"
else
  echo "FAIL: Copilot did not return the instruction sentinel" >&2
  printf '%s\n' "$output" >&2
  exit 1
fi
