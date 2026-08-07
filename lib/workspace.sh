#!/usr/bin/env bash
# lib/workspace.sh — `asha workspace` verb (workspace v1, issue #35).
#
# Usage:  asha workspace status [--json] [--start DIR]
# Exit:   0 no-workspace or valid; 1 invalid manifest; 2 usage error.
#
# Thin shim over plugins/session/tools/workspace_status.py — detection and
# enrichment live there (single source of truth; the dispatcher adds no
# fallback chain of its own, preserving the PR #34 audit invariant).
#
# Does NOT `set -e` at source scope (callers own shell options; bin/asha
# wraps invocations in a `set -euo pipefail` subshell). Sourced ONLY by
# bin/asha, which guarantees ASHA_ROOT — deliberately no bootstrap
# symlink-walk copy here (that duplication exists for engines that must
# also run standalone; this one must not).
#
# Public entry point: asha_workspace_main "$@".

_asha_workspace_usage() {
  cat <<'EOF'
asha workspace — workspace inspection (v1: status only)

Usage:
  asha workspace status [--json] [--start DIR]

Reports the detected workspace (upward walk for .asha/workspace.json),
manifest validity, active child repository, shared_git_root state, and
declared repository health. The manifest is committed in shared_git_root
by convention (ratified 2026-08-08) — status warns when it is untracked.

Exit: 0 = no workspace, or a valid one (warnings do not fail);
      1 = manifest invalid/unreadable (fail-closed, guided repair printed);
      2 = usage error.
EOF
}

asha_workspace_main() {
  local root="${ASHA_ROOT:-${MARKET_ROOT:-}}"
  if [[ -z "$root" ]]; then
    echo "ERROR: ASHA_ROOT is not set (lib/workspace.sh is sourced by bin/asha only)" >&2
    return 2
  fi
  case "${1:-}" in
    status)
      shift
      if ! command -v python3 >/dev/null 2>&1; then
        echo "ERROR: python3 is required for workspace status" >&2
        return 1
      fi
      python3 "$root/plugins/session/tools/workspace_status.py" "$@"
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
      echo "ERROR: unknown workspace subcommand: $1 (see: asha workspace --help)" >&2
      return 2
      ;;
  esac
}
