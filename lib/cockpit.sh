#!/usr/bin/env bash
# asha cockpit: the coordinator's chat pane beside the Initiatives monitor.
# source-scoped library: bin/asha sources it and calls asha_cockpit_main.
#
# One tmux window: left pane runs `asha claude` at DIR (the projects root the
# coordinator resolves intents against); right pane runs
# `asha control --initiatives`. Inside tmux a new window is added to the
# current session; outside tmux a detached session is created and attached.
# Approvals happen in the right pane; the left pane is refused by design.

asha_cockpit_usage() {
  cat <<'USAGE'
Usage: asha cockpit [DIR] [--session NAME] [--check|--no-check] [--dry-run]
  DIR         projects root for the coordinator pane (default: current directory)
  --session   tmux session name when created outside tmux (default: asha-cockpit-<dir>)
  --check     run the preflight only (Claude install health, orchestration doctor, project index)
  --no-check  open without the preflight
  --dry-run   print the tmux plan instead of running it (no preflight)
USAGE
}

# The first dogfood run failed on an uninstalled skill and an unimported jj
# head; neither is visible from the panes. Refuse to open a cockpit that the
# coordinator cannot operate, and name the remediation.
asha_cockpit_preflight() { # asha_bin dir
  local asha="$1" dir="$2" failed=0 out
  if out="$("$asha" doctor claude 2>&1)"; then
    echo "ok    asha doctor claude"
  else
    echo "FAIL  asha doctor claude reports drift; run: asha doctor claude (then asha install claude or --fix)" >&2
    grep -E "^FAIL" <<<"$out" | head -5 >&2
    failed=1
  fi
  if out="$("$asha" initiative doctor 2>&1)"; then
    echo "ok    asha initiative doctor"
  else
    echo "FAIL  asha initiative doctor is not ok:" >&2
    grep -vE "^match" <<<"$out" | head -6 >&2
    failed=1
  fi
  local count
  count="$("$asha" initiative projects --root "$dir" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(sum(1 for p in json.load(sys.stdin)["projects"] if p["jj_colocated"]))' 2>/dev/null || echo 0)"
  if [[ "$count" =~ ^[0-9]+$ ]] && (( count > 0 )); then
    echo "ok    $count jj-colocated Asha project(s) under $dir"
  else
    echo "warn  no jj-colocated Asha project under $dir; the coordinator will ask for a path" >&2
  fi
  return $failed
}

asha_cockpit_main() {
  local root="${ASHA_ROOT:-}"
  if [[ -z "$root" ]]; then
    echo "ERROR: ASHA_ROOT is not set (lib/cockpit.sh is sourced by bin/asha only)" >&2
    return 2
  fi
  local dir="" session="" dry=0 check=1
  while (($#)); do
    case "$1" in
      --dry-run) dry=1 ;;
      --check) check=2 ;;
      --no-check) check=0 ;;
      --session) [[ $# -ge 2 ]] || { echo "asha cockpit: --session needs a value" >&2; return 2; }
                 session="$2"; shift ;;
      -h|--help) asha_cockpit_usage; return 0 ;;
      -*) echo "asha cockpit: unknown option: $1" >&2; asha_cockpit_usage >&2; return 2 ;;
      *) if [[ -n "$dir" ]]; then echo "asha cockpit: at most one DIR" >&2; return 2; fi
         dir="$1" ;;
    esac
    shift
  done
  dir="${dir:-$PWD}"
  if ! dir="$(cd -P -- "$dir" 2>/dev/null && pwd)"; then
    echo "asha cockpit: directory not found: ${1:-$dir}" >&2
    return 2
  fi
  if ! command -v tmux >/dev/null 2>&1; then
    echo "asha cockpit: tmux is required" >&2
    return 1
  fi
  local asha="$root/bin/asha"
  if (( check == 2 )) || (( check == 1 && dry == 0 )); then
    asha_cockpit_preflight "$asha" "$dir" || return $?
    (( check == 2 )) && return 0
  fi
  local base; base="$(basename -- "$dir")"; base="${base//[^A-Za-z0-9_-]/-}"
  session="${session:-asha-cockpit-${base}}"

  step() {
    if ((dry)); then
      local joined="" word
      for word in "$@"; do joined+="$(printf '%q' "$word") "; done
      printf '%s\n' "${joined% }"
    else
      "$@"
    fi
  }

  if [[ -n "${TMUX:-}" ]]; then
    step tmux new-window -c "$dir" -n cockpit -- "$asha" claude
    step tmux split-window -h -c "$dir" -- "$asha" control --initiatives
    step tmux select-pane -L
    return 0
  fi
  if ((dry)) || ! tmux has-session -t "=$session" 2>/dev/null; then
    step tmux new-session -d -s "$session" -c "$dir" -n cockpit -- "$asha" claude
    step tmux split-window -h -t "$session:cockpit" -c "$dir" -- "$asha" control --initiatives
    step tmux select-pane -t "$session:cockpit.0"
  fi
  step tmux attach-session -t "$session"
}
