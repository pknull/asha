#!/usr/bin/env bash
# Re-prove a declared old-value replacement at the end-of-turn seam. Fail open.
set -uo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/common.sh" 2>/dev/null || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/harness-response.sh" 2>/dev/null || { echo '{}'; exit 0; }
command -v jq >/dev/null 2>&1 || { echo '{}'; exit 0; }
# flock(1) is Linux-only until lib/portable.sh grows a BSD fallback: no-op without it.
command -v flock >/dev/null 2>&1 || { echo '{}'; exit 0; }

marker_old_value() {
  jq -r 'select(.schema_version == 1) | (.old // .old_value) | select(type == "string" and length > 0)' \
    "$1" 2>/dev/null || true
}

INPUT="$(cat 2>/dev/null || true)"
if printf '%s' "$INPUT" | jq -e '.stop_hook_active == true' >/dev/null 2>&1; then
  echo '{}'
  exit 0
fi

PROJECT_DIR="$(resolve_hook_project_dir "$INPUT" 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/config.json" ]] || { echo '{}'; exit 0; }
MARKER_DIR="$PROJECT_DIR/Work/markers"
MARKER="$MARKER_DIR/pass-declaration.json"
[[ -d "$MARKER_DIR" ]] || { echo '{}'; exit 0; }
{ exec {LOCK_FD}<"$MARKER_DIR"; } 2>/dev/null || { echo '{}'; exit 0; }
flock -w 1 -x "$LOCK_FD" 2>/dev/null || { echo '{}'; exit 0; }
[[ -f "$MARKER" && ! -L "$MARKER" ]] || { echo '{}'; exit 0; }

OLD_VALUE="$(marker_old_value "$MARKER")"
[[ -n "$OLD_VALUE" ]] || { echo '{}'; exit 0; }
NEW_VALUE="$(jq -r '.new // .new_value // empty | select(type == "string")' \
  "$MARKER" 2>/dev/null || true)"
flock -u "$LOCK_FD" 2>/dev/null || { echo '{}'; exit 0; }

HITS_FILE="$(mktemp "${TMPDIR:-/tmp}/asha-verify-pass.XXXXXX" 2>/dev/null || true)"
[[ -n "$HITS_FILE" ]] || { echo '{}'; exit 0; }
trap 'rm -f "$HITS_FILE"' EXIT
grep -rlF --exclude-dir=.git --exclude-dir=.jj --exclude-dir=Work \
  -- "$OLD_VALUE" "$PROJECT_DIR" >"$HITS_FILE" 2>/dev/null
GREP_RC=$?
if [[ $GREP_RC -eq 1 ]]; then
  if flock -w 1 -x "$LOCK_FD" 2>/dev/null; then
    # Compare content, not inode: ext4 reuses freed inode numbers, so a marker
    # re-declared during the search can inherit the proved marker's identity.
    if [[ -f "$MARKER" && ! -L "$MARKER" ]] \
       && [[ "$(marker_old_value "$MARKER")" == "$OLD_VALUE" ]]; then
      rm -f "$MARKER" 2>/dev/null || true
    fi
    flock -u "$LOCK_FD" 2>/dev/null || true
  fi
  echo '{}'
  exit 0
fi
[[ $GREP_RC -eq 0 ]] || { echo '{}'; exit 0; }

FILES=""
while IFS= read -r hit; do
  [[ -n "$hit" ]] || continue
  rel="${hit#"$PROJECT_DIR"/}"
  FILES="${FILES:+$FILES$'\n'}- $rel"
done < "$HITS_FILE"
[[ -n "$FILES" ]] || { echo '{}'; exit 0; }

FRAGMENT="$(nudge_fragment verify-pass-complete 2>/dev/null || true)"
CONTEXT="${FRAGMENT:+$FRAGMENT$'\n\n'}Old value: $OLD_VALUE"
[[ -z "$NEW_VALUE" ]] || CONTEXT="$CONTEXT
Declared replacement: $NEW_VALUE"
CONTEXT="$CONTEXT

Files still containing the old value:
$FILES"
verify_pass_nudge "$CONTEXT"
exit 0
