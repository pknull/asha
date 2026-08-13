#!/usr/bin/env bash
# Direct RP priced-stakes prompt safeguard. Advisory; never blocks a turn.
set -uo pipefail

INPUT="$(cat 2>/dev/null || true)"
PROJECT_DIR="${1:-}"
SELF_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
RULE="$SELF_DIR/../priced-stakes/rule.json"
FRAGMENT="$SELF_DIR/../priced-stakes/canon-priced-stakes.md"

[[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/config.json" ]] || exit 0
MARKERS="$PROJECT_DIR/Work/markers"
[[ -f "$MARKERS/rp-active" && ! -f "$MARKERS/silence" ]] || exit 0
[[ ! -f "$MARKERS/nudge-rp-priced-stakes-off" \
   && ! -f "$MARKERS/rp-priced-stakes-off" ]] || exit 0
[[ -f "$RULE" && -f "$FRAGMENT" ]] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // .initialPrompt // empty | select(type == "string")' 2>/dev/null || true)"
REGEX="$(jq -r '.match_regex // empty' "$RULE" 2>/dev/null || true)"
[[ -n "$PROMPT" && -n "$REGEX" ]] || exit 0
printf '%s' "$PROMPT" | grep -Eiq -- "$REGEX" 2>/dev/null || exit 0

COOLDOWN="$(jq -r '.cooldown_hours // 1' "$RULE" 2>/dev/null || echo 1)"
[[ "$COOLDOWN" =~ ^[0-9]+$ ]] || COOLDOWN=1
COOLDOWN_MARKER="$MARKERS/nudge-rp-priced-stakes-cooldown"
if [[ -f "$COOLDOWN_MARKER" ]]; then
  LAST="$(cat "$COOLDOWN_MARKER" 2>/dev/null || echo 0)"
  [[ "$LAST" =~ ^[0-9]+$ ]] || LAST=0
  NOW="$(date +%s)"
  (( NOW - LAST < COOLDOWN * 3600 )) && exit 0
fi

cat "$FRAGMENT"
mkdir -p "$MARKERS" 2>/dev/null || exit 0
date +%s > "$COOLDOWN_MARKER" 2>/dev/null || true
exit 0
