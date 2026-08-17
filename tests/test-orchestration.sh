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
  tests.python.test_orchestration_doctor

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 0755 "$WORK"
export HOME="$WORK/home"
export XDG_STATE_HOME="$WORK/state"
export XDG_DATA_HOME="$WORK/data"
export XDG_RUNTIME_DIR="$WORK/runtime"
export ASHA_CONFIG="$WORK/config.json"
mkdir -m 0700 "$HOME" "$XDG_STATE_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR"
printf '%s\n' '{}' >"$ASHA_CONFIG"
chmod 0600 "$ASHA_CONFIG"

list_json="$(bash "$ROOT/bin/asha" initiative list --json)"
python3 -I -c 'import json,sys; value=json.load(sys.stdin); assert value == {"contract":"asha.orchestration-initiative-list.v1","initiatives":[]}' <<<"$list_json"
set +e
task_refusal="$(bash "$ROOT/bin/asha" task report 2>&1)"
task_refusal_rc=$?
set -e
[[ $task_refusal_rc -eq 2 ]]
[[ "$task_refusal" == *"not available before Increment 2"* ]]

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

printf 'ok - initiative and reserved task routes are wired; corrupt orchestration config stays isolated\n'
