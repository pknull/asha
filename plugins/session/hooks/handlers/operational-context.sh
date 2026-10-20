#!/bin/bash
# Claude SessionStart entry for the operational layer: operation.md plus active
# learnings. Claude Code caps each hook output string at 10,000 characters and
# project Memory needs most of one, so this layer rides its own entry. Codex,
# Copilot and OpenCode receive the same layer from their wrapper-managed
# instruction file (identity/operational-merge.sh).
set -uo pipefail
SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/common.sh" 2>/dev/null || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/harness-response.sh" 2>/dev/null || true
source "$SCRIPT_DIR/operational-budget.sh" 2>/dev/null || { echo '{}'; exit 0; }
asha_worker_session && { echo '{}'; exit 0; }

INPUT=""
[[ -t 0 ]] || INPUT="$(cat 2>/dev/null || true)"
PROJECT_DIR="$(resolve_hook_project_dir "$INPUT" 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/config.json" ]] || { echo '{}'; exit 0; }
HARNESS="$(asha_harness 2>/dev/null || echo "${ASHA_HARNESS:-claude}")"
[[ "$HARNESS" == "claude" ]] || { echo '{}'; exit 0; }

# shellcheck disable=SC2034  # read by asha_append_operational_context
PLUGIN_ROOT="$(get_plugin_root 2>/dev/null || true)"
# shellcheck disable=SC2034
PYTHON_CMD="$(get_python_cmd "$PROJECT_DIR" 2>/dev/null || true)"

ASHA_CONTEXT_LABELS=() ASHA_CONTEXT_BLOCKS=()
# shellcheck disable=SC2317  # called by asha_append_operational_context
append_context() { [[ -z "$2" ]] || { ASHA_CONTEXT_LABELS+=("$1"); ASHA_CONTEXT_BLOCKS+=("$2"); }; }
asha_append_operational_context

CONTEXT="$(asha_hook_context_join)"
if [[ -z "$CONTEXT" ]]; then
  echo '{}'
else
  printf '%s\n' "$CONTEXT"
fi
exit 0
