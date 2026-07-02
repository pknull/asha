#!/usr/bin/env bash
# lib/doctor.sh — `asha doctor` verb: thin adapter over bin/asha-drift-check.sh
# (the diagnostic engine; kept at its path for cron/systemd users).
#
# Usage:  asha doctor [claude|codex|copilot|all] [--fix]
# Exit:   0 clean, 1 one-or-more failures, 2 usage error.
#
# Note: `asha claude doctor` still reaches Claude Code's OWN doctor (launch
# forwarding); this verb is asha's install-health audit.
#
# Does NOT `set -e` at source scope (callers own shell options; bin/asha wraps
# invocations in a `set -euo pipefail` subshell).
#
# Public entry point: asha_doctor_main "$@".

# Resolve repo root from THIS file's location (portable; no GNU readlink -f).
# asha-bootstrap-symlink-walk: resolve our own real path, portable (readlink -f is GNU-only).
# Duplicated across 7 scripts — find all: `grep -rn asha-bootstrap-symlink-walk`. Cannot DRY into
# lib/portable.sh:resolve_path() — this runs *before* portable.sh is locatable. Keep copies in sync.
__eng_src="${BASH_SOURCE[0]}"
while [ -h "$__eng_src" ]; do
  __eng_dir="$(cd -P "$(dirname "$__eng_src")" >/dev/null 2>&1 && pwd)"
  __eng_src="$(readlink "$__eng_src")"
  case "$__eng_src" in /*) ;; *) __eng_src="$__eng_dir/$__eng_src" ;; esac
done
__ASHA_LIB_DIR="$(cd -P "$(dirname "$__eng_src")" >/dev/null 2>&1 && pwd)"
unset __eng_src __eng_dir
MARKET_ROOT="${MARKET_ROOT:-$(dirname "$__ASHA_LIB_DIR")}"

asha_doctor_main() {
  local target="all" fix=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      claude|codex|copilot|all) target="$1" ;;
      --target) shift; target="${1:-}" ;;
      --target=*) target="${1#--target=}" ;;
      --fix) fix=1 ;;
      -h|--help)
        cat <<'EOF'
asha doctor — audit the asha install for drift.

Usage:
  asha doctor [claude|codex|copilot|all] [--fix]

Targets default to 'all'. --fix self-heals stale command-skills and drifted
guardrails. Exit: 0 clean, 1 failures, 2 usage error.
(Claude Code's native doctor remains at: asha claude doctor)
EOF
        return 0 ;;
      *) echo "ERROR: unknown arg: $1 (see: asha doctor --help)" >&2; return 2 ;;
    esac
    shift
  done
  case "$target" in
    claude|codex|copilot|all) ;;
    *) echo "ERROR: invalid target '$target'" >&2; return 2 ;;
  esac

  local -a args=(--target "$target")
  [[ $fix -eq 1 ]] && args+=(--fix)
  # Child process, not sourced: drift-check is a standalone set -uo script
  # that exits directly. Its exit code is the doctor contract.
  bash "$MARKET_ROOT/bin/asha-drift-check.sh" "${args[@]}"
}
