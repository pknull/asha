#!/bin/bash
# state.sh — ephemeral per-session state (counters/flags) for Asha hooks.
#
# This is *working memory*, NOT durable Memory: small mechanical counters keyed
# by the harness session id, living in ~/.asha/session-state/<sid>.json. It is
# cleared at session end (and swept on a TTL). A counter from a past session
# must never affect a new one — that ephemerality is the whole point and the
# reason it is separate from Memory/learnings. See README "State model".
#
# Sourced by hooks (e.g. policy-guard.sh). Every op FAILS OPEN: on any error it
# returns a safe default (0 / no-op) so a guardrail is never bricked by state.
#
# API:  state_get <sid> <key>            -> echoes integer (0 if absent)
#       state_incr <sid> <key> [delta=1] -> echoes new integer value
#       state_clear <sid>                -> remove this session's state
#       state_sweep [days=2]             -> delete state files older than N days

ASHA_STATE_DIR="${ASHA_STATE_DIR:-$HOME/.asha/session-state}"

_state_file() {
  local sid="${1:-}"
  [[ -n "$sid" ]] || sid="nosession"
  sid="$(printf '%s' "$sid" | tr -c 'A-Za-z0-9_.-' '_')"
  printf '%s/%s.json' "$ASHA_STATE_DIR" "$sid"
}

state_get() {
  local f v
  command -v jq >/dev/null 2>&1 || { echo 0; return 0; }
  f="$(_state_file "${1:-}")"
  [[ -f "$f" ]] || { echo 0; return 0; }
  v="$(jq -r --arg k "${2:-}" '.[$k] // 0' "$f" 2>/dev/null || echo 0)"
  [[ "$v" =~ ^-?[0-9]+$ ]] && echo "$v" || echo 0
}

state_incr() {
  local sid="${1:-}" key="${2:-}" delta="${3:-1}" f cur new tmp
  command -v jq >/dev/null 2>&1 || { echo 0; return 0; }
  [[ -n "$key" ]] || { echo 0; return 0; }
  f="$(_state_file "$sid")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || { echo 0; return 0; }
  # Best-effort advisory lock; if flock is missing the race only risks an
  # off-by-one on a counter, which is acceptable for rate-limiting.
  exec 9>>"${f}.lock" 2>/dev/null || true
  flock 9 2>/dev/null || true
  cur="$(state_get "$sid" "$key")"
  new=$((cur + delta))
  tmp="$(mktemp 2>/dev/null)" || { flock -u 9 2>/dev/null || true; echo "$new"; return 0; }
  if [[ -f "$f" ]]; then
    jq --arg k "$key" --argjson v "$new" '.[$k]=$v' "$f" >"$tmp" 2>/dev/null && mv "$tmp" "$f" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  else
    jq -n --arg k "$key" --argjson v "$new" '{($k):$v}' >"$f" 2>/dev/null || true
    rm -f "$tmp" 2>/dev/null
  fi
  flock -u 9 2>/dev/null || true
  echo "$new"
}

state_clear() {
  local f
  f="$(_state_file "${1:-}")"
  rm -f "$f" "${f}.lock" 2>/dev/null || true
}

state_sweep() {
  local days="${1:-2}"
  [[ -d "$ASHA_STATE_DIR" ]] || return 0
  find "$ASHA_STATE_DIR" -type f -mtime "+${days}" -delete 2>/dev/null || true
}
