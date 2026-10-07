#!/usr/bin/env bash
# lib/workspace.sh — thin `asha workspace` command router.
#
# Domain behavior and flag parsing remain in the Python cores. This shim only
# selects a core, preserving its stdout/stderr and exit code. Sourced by
# bin/asha, which guarantees ASHA_ROOT and owns shell options.

_asha_workspace_usage() {
  cat <<'EOF'
asha workspace — multi-repository workspace commands

Usage:
  asha workspace status [--json] [--start DIR]
  asha workspace init|discover|doctor [native options]

Run any command with --help for its exact Python-core flags.

Exit codes are passed through unchanged from the selected Python core.
EOF
}

_asha_workspace_python() {
  local root="$1" tool="$2"
  shift 2
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required for workspace commands" >&2
    return 1
  fi
  python3 "$root/plugins/session/tools/$tool" "$@"
}

asha_workspace_main() {
  local root="${ASHA_ROOT:-${MARKET_ROOT:-}}"
  if [[ -z "$root" ]]; then
    echo "ERROR: ASHA_ROOT is not set (lib/workspace.sh is sourced by bin/asha only)" >&2
    return 2
  fi

  local command="${1:-}"
  case "$command" in
    status)
      shift
      _asha_workspace_python "$root" workspace_status.py "$@"
      ;;
    init|discover|doctor)
      shift
      _asha_workspace_python "$root" workspace_init.py "$command" "$@"
      ;;
    knowledge|promote|worktree|work-item)
      # Removed 2026-10-07 (Keeper, subtraction value call N3). Refused by name
      # so the old word says what happened instead of reading as a typo.
      echo "asha workspace $command: the knowledge, promotion, worktree and work-item tools were removed on 2026-10-07; existing knowledge/ folders stay as ordinary Markdown notes" >&2
      return 2
      ;;
    -h|--help)
      _asha_workspace_usage
      return 0
      ;;
    "")
      _asha_workspace_usage >&2
      return 2
      ;;
    *)
      echo "ERROR: unknown workspace subcommand: $command (see: asha workspace --help)" >&2
      return 2
      ;;
  esac
}
