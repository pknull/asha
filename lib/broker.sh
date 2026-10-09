#!/usr/bin/env bash
# source-scoped dispatcher for evidence-backed brokerage (issue #27).

asha_broker_main() {
  local family="${1:-}" action="${2:-}"
  if [[ "$family" == "process" && "$action" == "route" ]]; then
    shift 2
    python3 "$ASHA_ROOT/plugins/session/tools/broker.py" process-route "$@"
    return $?
  fi
  if [[ "$family" == "capabilities" && "$action" == "match" ]]; then
    shift 2
    python3 "$ASHA_ROOT/plugins/session/tools/broker.py" capabilities-match "$@"
    return $?
  fi
  # Read-only dependency plan for one explicitly named capability (#124).
  if [[ "$family" == "capabilities" && "$action" == "plan" ]]; then
    shift 2
    python3 "$ASHA_ROOT/plugins/session/tools/broker.py" capabilities-plan "$@"
    return $?
  fi
  echo "asha: expected one of: process route | capabilities match | capabilities plan" >&2
  return 2
}
