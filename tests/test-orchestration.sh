#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHONPATH="$ROOT" python3 -m unittest \
  tests.python.test_orchestration_config \
  tests.python.test_orchestration_model \
  tests.python.test_orchestration_store \
  tests.python.test_orchestration_graph \
  tests.python.test_orchestration_cli \
  tests.python.test_orchestration_reconcile \
  tests.python.test_orchestration_tui_model \
  tests.python.test_orchestration_doctor \
  tests.python.test_orchestration_actions \
  tests.python.test_orchestration_scheduler \
  tests.python.test_orchestration_dispatch \
  tests.python.test_orchestration_reconcile_live \
  tests.python.test_orchestration_results \
  tests.python.test_orchestration_seals \
  tests.python.test_orchestration_salvage \
  tests.python.test_orchestration_composition \
  tests.python.test_orchestration_review \
  tests.python.test_orchestration_verification \
  tests.python.test_orchestration_readiness \
  tests.python.test_orchestration_coordinator_records \
  tests.python.test_orchestration_coordinator_claim \
  tests.python.test_orchestration_coordinator_loop \
  tests.python.test_orchestration_coordinator_active \
  tests.python.test_orchestration_revision_rule \
  tests.python.test_orchestration_workspace_scope \
  tests.python.test_orchestration_multi_repo_readiness \
  tests.python.test_orchestration_projects \
  tests.python.test_orchestration_coordinator_sessions \
  tests.python.test_orchestration_authority \
  tests.python.test_orchestration_real_execution

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 0755 "$WORK"
export HOME="$WORK/home"
export ASHA_HOME="$WORK/asha-home"
export XDG_RUNTIME_DIR="$WORK/runtime"
export ASHA_CONFIG="$WORK/config.json"
# Retired from the toolkit; unset so an operator shell cannot leak real
# legacy-state detection into the fixtures.
unset XDG_STATE_HOME XDG_DATA_HOME
mkdir -m 0700 "$HOME" "$ASHA_HOME" "$XDG_RUNTIME_DIR"
printf '%s\n' '{}' >"$ASHA_CONFIG"
chmod 0600 "$ASHA_CONFIG"

list_json="$(bash "$ROOT/bin/asha" initiative list --json)"
python3 -I -c 'import json,sys; value=json.load(sys.stdin); assert value == {"contract":"asha.orchestration-initiative-list.v1","initiatives":[]}' <<<"$list_json"
set +e
task_refusal="$(bash "$ROOT/bin/asha" task report 2>&1)"
task_refusal_rc=$?
set -e
[[ $task_refusal_rc -eq 2 ]]
[[ "$task_refusal" == *"missing required option(s): --file"* ]]

printf '%s\n' '{"orchestration":{"contract":"asha.orchestration-config.v99"}}' >"$ASHA_CONFIG"

set +e
bash "$ROOT/bin/asha" initiative doctor --json >"$WORK/initiative.out" 2>"$WORK/initiative.err"
initiative_rc=$?
bash "$ROOT/bin/asha" task doctor --json >"$WORK/task.out" 2>"$WORK/task.err"
task_rc=$?
set -e
[[ $initiative_rc -eq 2 ]]
[[ $task_rc -ne 2 ]]
python3 -I -c 'import json,sys; value=json.load(open(sys.argv[1])); assert value["contract"] == "asha.control-doctor.v1"' "$WORK/task.out"

printf 'ok - orchestration execution, publication, seals, recovery, and task routes are wired; corrupt orchestration config stays isolated\n'
