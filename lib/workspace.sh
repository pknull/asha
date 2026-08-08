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
  asha workspace knowledge init|lint [native options]
  asha workspace promote plan|apply|publish [native options]
  asha workspace worktree create|status|remove [native options]
  asha workspace work-item create|list|show|link|import|preview|lint|index|promote-plan|worktree-seed [native options]

Run any command with --help for its exact Python-core flags.

Safety contracts:
  promote plan writes an explicit review artifact. promote apply accepts only
  that artifact plus its digest and explicit confirmation; it revalidates
  source/evidence/target preimages and never pushes or merges. In pull-request
  mode, promote publish creates a digest-named branch, stages only the reviewed
  write-set, pushes that branch, and opens a draft PR; it never merges. Local
  Git hooks require the separate explicit --run-git-hooks authorization.
  work-item import requires a matching scrubbed preview token.
  work-item worktree-seed is a data-only promotion plan alias and requires
  explicit Git confirmation; it never creates a branch or worktree.

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
    knowledge)
      shift
      _asha_workspace_python "$root" workspace_knowledge.py "$@"
      ;;
    promote)
      shift
      _asha_workspace_python "$root" workspace_knowledge.py promote "$@"
      ;;
    worktree)
      shift
      _asha_workspace_python "$root" workspace_worktree.py "$@"
      ;;
    work-item)
      shift
      if [[ "${1:-}" == "worktree-seed" ]]; then
        shift
        _asha_workspace_python "$root" workspace_workitems.py promote-plan "$@" --worktree-seed
      else
        _asha_workspace_python "$root" workspace_workitems.py "$@"
      fi
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
