#!/usr/bin/env bash
# Detached session auto-save.
#
# Spawned by hooks/handlers/session-end.sh via `setsid` so the heavyweight
# synthesis (pattern_analyzer + preflight + git commit) survives harness
# teardown. A SessionEnd hook that runs the save INLINE is cancelled by the
# harness the moment the CLI exits — the synthesis is killed mid-pipeline,
# after it has rewritten activeContext.md but before it can commit, leaving a
# dirty working tree and a "Hook cancelled" report. Running detached in a new
# session/process group lets the save finish after the CLI is gone.
#
# Concurrency: a relaunched session can begin before this save completes, and
# its own save could race this one over Memory/activeContext.md + the git
# index. `flock` serializes the two.
#
# The authoritative transcript identity (ASHA_TRANSCRIPT_PATH /
# CLAUDE_CODE_SESSION_ID) is threaded in via the inherited environment, exactly
# as the old inline `exec` inherited it — so synthesis still reads THIS
# session's transcript, not a concurrent session's newest-by-mtime log.
#
# Args:
#   $1  path to save-session.sh
#   $2  log file (appended; the only on-disk trace of an auto-save)
#   $3  lock file (flock target)
#
# NOTE: deliberately NOT `set -e` — a non-zero synthesis stage must still reach
# the footer log line so failures are diagnosable rather than silent.
set -uo pipefail

SAVE_SCRIPT="${1:?save-session.sh path required}"
LOG_FILE="${2:?log file path required}"
LOCK_FILE="${3:?lock file path required}"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")"

{
  echo "=== $(date -u +%FT%TZ) auto-save START sid=${CLAUDE_CODE_SESSION_ID:-?} transcript=${ASHA_TRANSCRIPT_PATH:-?} ==="
  (
    # Wait up to 10 min for a concurrent save to release; skip rather than
    # corrupt if it never does.
    if ! flock -w 600 9; then
      echo "auto-save SKIP: lock held >600s by another save"
      exit 0
    fi
    "$SAVE_SCRIPT" --automatic
    echo "auto-save synthesis rc=$?"
  ) 9>"$LOCK_FILE"
  echo "=== $(date -u +%FT%TZ) auto-save END ==="
} >>"$LOG_FILE" 2>&1
