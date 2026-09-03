#!/usr/bin/env bash
# Declare an old value that must disappear before the current pass is complete.
set -uo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 1
# shellcheck source=project-root.sh
source "$SCRIPT_DIR/project-root.sh" 2>/dev/null || {
  echo "ERROR: project-root resolver is unavailable" >&2
  exit 1
}

if [[ $# -lt 1 || $# -gt 2 || -z "${1:-}" ]]; then
  echo "Usage: declare-pass.sh OLD [NEW]" >&2
  exit 2
fi
command -v jq >/dev/null 2>&1 || {
  echo "ERROR: jq is required to declare a verification pass" >&2
  exit 1
}
command -v flock >/dev/null 2>&1 || {
  echo "ERROR: flock is required to declare a verification pass safely" >&2
  exit 1
}

OLD_VALUE="$1"
if [[ "$OLD_VALUE" == *$'\n'* || "$OLD_VALUE" == *$'\r'* ]]; then
  echo "ERROR: OLD must be one line so the fixed-string grep proof is exact" >&2
  exit 2
fi
HAS_NEW=0
NEW_VALUE=""
if [[ $# -eq 2 ]]; then
  HAS_NEW=1
  NEW_VALUE="$2"
fi

PROJECT_DIR="$(asha_detect_project_root "env,git,walk" 1 2>/dev/null || true)"
if [[ -z "$PROJECT_DIR" || ! -f "$PROJECT_DIR/.asha/config.json" ]]; then
  echo "ERROR: run declare-pass.sh inside an initialized Asha project" >&2
  exit 1
fi

MARKER_DIR="$PROJECT_DIR/Work/markers"
MARKER="$MARKER_DIR/pass-declaration.json"
umask 077
mkdir -p "$MARKER_DIR" || exit 1
exec {LOCK_FD}<"$MARKER_DIR" || exit 1
flock -x "$LOCK_FD" || exit 1
TMP="$(mktemp "$MARKER_DIR/.pass-declaration.XXXXXX")" || exit 1
trap 'rm -f "$TMP"' EXIT
if [[ $HAS_NEW -eq 1 ]]; then
  jq -n --arg old "$OLD_VALUE" --arg new "$NEW_VALUE" \
    '{schema_version:1, old:$old, new:$new}' > "$TMP" || {
      rm -f "$TMP"
      exit 1
    }
else
  jq -n --arg old "$OLD_VALUE" \
    '{schema_version:1, old:$old}' > "$TMP" || {
      rm -f "$TMP"
      exit 1
    }
fi
mv "$TMP" "$MARKER" || { rm -f "$TMP"; exit 1; }
trap - EXIT
flock -u "$LOCK_FD" || true
exec {LOCK_FD}>&-
printf 'Declared verification pass: %s\n' "$MARKER"
