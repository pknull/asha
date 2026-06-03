#!/usr/bin/env bash
# Stop hook: block /session:save completion until pre-flight gates pass.
#
# Scoped to save turns by the Work/markers/save-pending marker (dropped by
# save.md at the start of a save). On every non-save turn the marker is absent
# and this hook returns instantly. Loop-safe: at most 3 forced remediation
# attempts, with stop_hook_active as a backstop, so a false positive can never
# permanently wedge the session.
#
# Payload (stdin JSON) fields used: cwd, transcript_path, session_id, stop_hook_active.
set -uo pipefail

INPUT=$(cat)

CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
PROJECT_DIR="${CWD:-${CLAUDE_PROJECT_DIR:-$(pwd)}}"
MARKER="$PROJECT_DIR/Work/markers/save-pending"

# Not a save turn -> allow stop immediately (near-zero overhead).
if [[ ! -f "$MARKER" ]]; then
    echo '{}'
    exit 0
fi

STOP_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null || echo false)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)
SID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
ATTEMPTS=$(jq -r '.attempts // 0' "$MARKER" 2>/dev/null || echo 0)
[[ "$ATTEMPTS" =~ ^[0-9]+$ ]] || ATTEMPTS=0

ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../tools" && pwd)/save_preflight.py"
HOOK_LOG="$PROJECT_DIR/Memory/events/save-preflight-hook.log"
mkdir -p "$(dirname "$HOOK_LOG")" 2>/dev/null || true

RESULT=$(ASHA_TRANSCRIPT_PATH="$TRANSCRIPT" CLAUDE_CODE_SESSION_ID="$SID" \
    python3 "$ENGINE" --mode enforce \
        --project-dir "$PROJECT_DIR" \
        --transcript "$TRANSCRIPT" \
        --session-id "$SID" \
        2>>"$HOOK_LOG" || echo '{"hard_fail":false}')

HARD_FAIL=$(echo "$RESULT" | jq -r '.hard_fail // false' 2>/dev/null || echo false)
REASON=$(echo "$RESULT" | jq -r '.reason // "save pre-flight gate failed"' 2>/dev/null || echo "save pre-flight gate failed")

# All gates pass -> clear marker, allow stop (save completes clean).
if [[ "$HARD_FAIL" != "true" ]]; then
    rm -f "$MARKER"
    echo '{}'
    exit 0
fi

# Hard fail. Escape hatch: bail after 3 attempts or on re-entry backstop.
if [[ "$ATTEMPTS" -ge 3 || "$STOP_ACTIVE" == "true" ]]; then
    rm -f "$MARKER"
    echo "[$(date -u +%FT%TZ)] GATE EXHAUSTED after ${ATTEMPTS} attempt(s); allowing stop with UNRESOLVED failures: ${REASON}" >>"$HOOK_LOG"
    echo '{}'
    exit 0
fi

# Block and force remediation; record the attempt.
NEW=$((ATTEMPTS + 1))
printf '{"created":"%s","attempts":%d}\n' "$(date -u +%FT%TZ)" "$NEW" > "$MARKER"
echo "[$(date -u +%FT%TZ)] BLOCK attempt ${NEW}/3: ${REASON}" >>"$HOOK_LOG"
jq -n --arg r "$REASON" --arg n "$NEW" \
    '{decision:"block", reason:("Session-save pre-flight gate failed (attempt " + $n + "/3). " + $r)}'
exit 0
