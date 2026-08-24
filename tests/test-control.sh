#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_PATH="$PATH"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 0755 "$WORK"

export HOME="$WORK/home"
export XDG_STATE_HOME="$WORK/state"
export XDG_DATA_HOME="$WORK/data"
export XDG_RUNTIME_DIR="$WORK/runtime"
mkdir -p "$HOME/.asha" "$HOME/dotfiles/asha/.asha" "$WORK/reject-bin" "$WORK/repository"
chmod 0750 "$HOME"
chmod 0775 "$HOME/.asha" "$HOME/dotfiles/asha/.asha"
printf '%s\n' '{"control":{"default_harness":"codex"}}' >"$HOME/dotfiles/asha/.asha/config.json"
chmod 0600 "$HOME/dotfiles/asha/.asha/config.json"
ln -s '../dotfiles/asha/.asha/config.json' "$HOME/.asha/config.json"
mkdir -m 0700 "$XDG_STATE_HOME"
mkdir -m 0750 "$XDG_STATE_HOME/asha"

# Neither cwd nor inherited PYTHONPATH may shadow the trusted controller.
mkdir -p "$WORK/repository/control" "$WORK/python-poison/control"
cat >"$WORK/repository/control/__init__.py" <<'PY'
from pathlib import Path
import os
Path(os.environ["POISON_MARKER"]).write_text("cwd import executed")
PY
cat >"$WORK/repository/control/cli.py" <<'PY'
raise SystemExit(96)
PY
cat >"$WORK/python-poison/control/__init__.py" <<'PY'
from pathlib import Path
import os
Path(os.environ["POISON_MARKER"]).write_text("PYTHONPATH import executed")
PY
cat >"$WORK/python-poison/control/cli.py" <<'PY'
raise SystemExit(95)
PY
cat >"$WORK/python-poison/json.py" <<'PY'
from pathlib import Path
import os
Path(os.environ["POISON_MARKER"]).write_text("inherited PYTHONPATH executed")
raise SystemExit(94)
PY
export POISON_MARKER="$WORK/python-imported"
export PYTHONPATH="$WORK/python-poison"

for command in tmux jj git; do
  cat >"$WORK/reject-bin/$command" <<EOF
#!/usr/bin/env bash
printf '%s\n' '$command' >>'$WORK/invoked'
echo "FORBIDDEN: $command invoked" >&2
exit 97
EOF
  chmod +x "$WORK/reject-bin/$command"
done
export PATH="$WORK/reject-bin:$PATH"

before="$(find "$WORK/repository" -mindepth 1 -print | sort)"
state_before="$(find "$XDG_STATE_HOME" -printf '%P %y %m %u %g\n' | sort)"
bytecode_before="$(find "$ROOT/lib/control" \( -type d -name __pycache__ -o -type f -name '*.pyc' \) -printf '%p %s %T@\n' | sort)"
cd "$WORK/repository"

human="$(bash "$ROOT/bin/asha" task list)"
[[ "$human" == 'No Control tasks registered.' ]]
json="$(bash "$ROOT/bin/asha" task list --json)"
python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert d == {"contract":"asha.control-task-list.v1","tasks":[]}' <<<"$json"

reconcile="$(bash "$ROOT/bin/asha" task reconcile --json)"
python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert d == {"contract":"asha.control-reconcile-list.v1","results":[]}' <<<"$reconcile"
# Successful batch reads publish the bounded server summary once after their
# (possibly empty) row set. They may invoke tmux, but never jj or git here.
[[ -z "$(sort -u "$WORK/invoked" | grep -Ev '^(tmux|)$' || true)" ]]

show_before="$(wc -l < "$WORK/invoked")"
set +e
bash "$ROOT/bin/asha" task show missing-task --json >/dev/null 2>&1
show_rc=$?
set -e
[[ $show_rc -eq 2 ]]
[[ "$(wc -l < "$WORK/invoked")" -eq "$show_before" ]]

# Assertions below are DELTA-based: each command may only add invocations from
# its own allowed set. A whole-file equality check silently rots as soon as an
# earlier command legitimately probes something new.
allowed_only() { # allowed-egrep-pattern  since-line-count
  local pattern="$1" since="$2"
  local added; added="$(tail -n +$((since + 1)) "$WORK/invoked" 2>/dev/null | sort -u)"
  [[ -z "$(printf '%s\n' "$added" | grep -Ev "$pattern" || true)" ]]
}
invoked_lines() { [[ -e "$WORK/invoked" ]] && wc -l < "$WORK/invoked" || echo 0; }

doctor_before="$(invoked_lines)"
set +e
doctor="$(bash "$ROOT/bin/asha" task doctor --json)"
doctor_rc=$?
set -e
[[ $doctor_rc -eq 1 ]]
python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert d["contract"] == "asha.control-doctor.v1"; assert d["ok"] is False; assert any(p["name"] == "tmux" and p["outcome"] == "unavailable" for p in d["probes"])' <<<"$doctor"
# doctor probes capabilities, so tmux and jj (including `jj git init --help`)
# are expected. The external Git executable remains a read-only classification
# dependency here and must never be invoked by doctor.
allowed_only '^(tmux|jj|)$' "$doctor_before"
! grep -Eq '^git$' "$WORK/invoked"

# The externally reachable event route authorizes against the durable task
# registry before it writes a snapshot. Seed the exact task/run/pane identity
# exercised below; isolated mode plus an explicit trusted path keeps both
# poison import roots out of this setup process.
mkdir -p "$WORK/source" "$XDG_DATA_HOME/asha/workspaces/repo-key/shell-event"
# The namespace predicate rejects writable ancestors and requires 0700
# destination components: privatize the fixture chain like production.
chmod 0755 "$WORK/source"
chmod 0700 "$XDG_DATA_HOME" "$XDG_DATA_HOME/asha" "$XDG_DATA_HOME/asha/workspaces" \
  "$XDG_DATA_HOME/asha/workspaces/repo-key" "$XDG_DATA_HOME/asha/workspaces/repo-key/shell-event"
python3 -I - "$ROOT" "$WORK/source" \
  "$XDG_DATA_HOME/asha/workspaces/repo-key/shell-event" <<'PY'
import os
import sys

sys.path.insert(0, sys.argv[1])
from lib.control.config import load_config
from lib.control.store import TaskStore
from tests.python.test_control_config_model import task_record

task = task_record(
    task_id="11111111-1111-4111-8111-111111111111",
    repository_root=sys.argv[2],
    workspace_path=sys.argv[3],
)
task["runs"][0]["run_id"] = "22222222-2222-4222-8222-222222222222"
task["runs"][0]["pane_id"] = "%9"
TaskStore(load_config(os.environ)).save(task)
PY
state_before="$(find "$XDG_STATE_HOME" -printf '%P %y %m %u %g\n' | sort)"

export ASHA_CONTROL_MANAGED=1
export ASHA_CONTROL_TASK_ID=11111111-1111-4111-8111-111111111111
export ASHA_CONTROL_RUN_ID=22222222-2222-4222-8222-222222222222
export ASHA_CONTROL_STATE_DIR="$XDG_STATE_HOME/asha/control/tasks"
event_before="$(invoked_lines)"
event="$(bash "$ROOT/bin/asha" control event --event prompt-submitted \
  --harness codex --session-id shell-isolation --pane-id %9 --json)"
python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert d["contract"] == "asha.control-event.v1"; assert d["state"] == "working"' <<<"$event"
permission="$(bash "$ROOT/bin/asha" control event --event permission-requested \
  --harness codex --session-id shell-isolation --pane-id %9 --json)"
python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert d["contract"] == "asha.control-event.v1"; assert d["event"] == "permission-requested"; assert d["state"] == "needs-input"' <<<"$permission"
resumed="$(bash "$ROOT/bin/asha" control event --event tool-completed \
  --harness codex --session-id shell-isolation --pane-id %9 --json)"
python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert d["contract"] == "asha.control-event.v1"; assert d["event"] == "tool-completed"; assert d["state"] == "working"' <<<"$resumed"
stopped="$(bash "$ROOT/bin/asha" control event --event turn-stopped \
  --harness codex --session-id shell-isolation --pane-id %9 --json)"
python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert d["contract"] == "asha.control-event.v1"; assert d["event"] == "turn-stopped"; assert d["state"] == "idle"' <<<"$stopped"
# `control event` publishes @asha_state/@asha_summary, so it invokes tmux. The
# guarantee that matters: the event path touches ONLY tmux -- never jj, never
# git -- and a failing tmux (exit 97 here) never fails the hook.
allowed_only '^(tmux|)$' "$event_before"
unset ASHA_CONTROL_MANAGED ASHA_CONTROL_TASK_ID ASHA_CONTROL_RUN_ID ASHA_CONTROL_STATE_DIR

set +e
control_before="$(invoked_lines)"
control_err="$(bash "$ROOT/bin/asha" control 2>&1)"
control_rc=$?
set -e
[[ $control_rc -eq 2 ]]
[[ "$control_err" == *"asha task list --json"* ]]
# The non-TTY degrade path must shell out to nothing at all.
allowed_only '^$' "$control_before"

# Isolate the inherited-PYTHONPATH poison from the cwd-package poison so both
# attack paths are independently exercised.
mv "$WORK/repository/control" "$WORK/repository/control-disabled"
mkdir "$WORK/safe-cwd"
(
  cd "$WORK/safe-cwd"
  bash "$ROOT/bin/asha" task list --json >/dev/null
)
mv "$WORK/repository/control-disabled" "$WORK/repository/control"

after="$(find "$WORK/repository" -mindepth 1 -print | sort)"
state_after="$(find "$XDG_STATE_HOME" -printf '%P %y %m %u %g\n' | sort)"
bytecode_after="$(find "$ROOT/lib/control" \( -type d -name __pycache__ -o -type f -name '*.pyc' \) -printf '%p %s %T@\n' | sort)"
[[ "$before" == "$after" ]]
[[ "$state_before" == "$state_after" ]]
[[ "$bytecode_before" == "$bytecode_after" ]]
[[ ! -e "$POISON_MARKER" ]]

printf 'ok - batch reads publish only tmux summary; doctor probes tmux+jj; Codex prompt/permission/tool/Stop events touch tmux only; git never\n'

# The mutating seam runs only against Python-created disposable repositories;
# restore the real PATH so this section exercises the installed jj 0.38 binary.
PATH="$REAL_PATH" PYTHONPATH="$ROOT" python3 -m unittest \
  tests.python.test_control_finish_increment \
  tests.python.test_control_terminal_actions \
  tests.python.test_control_task_start_smoke_fixes \
  tests.python.test_control_tui_initiatives_mode \
  tests.python.test_control_create_by_id \
  tests.python.test_control_colocated_sync \
  tests.python.test_control_doctor_ok \
  tests.python.test_control_finish_review_repairs \
  tests.python.test_control_goal_viewport_large_tree \
  tests.python.test_control_increment4 \
  tests.python.test_control_increment5 \
  tests.python.test_control_issue60_cleanup \
  tests.python.test_control_live_state \
  tests.python.test_control_pane_death \
  tests.python.test_control_prerequisites \
  tests.python.test_control_reconcile_doctor_cli \
  tests.python.test_control_store \
  tests.python.test_control_workspace_trust \
  tests.python.test_control_increment2.RealJjPreparationTests \
  tests.python.test_control_increment3.RealTmuxLaunchTests \
  tests.python.test_control_popup_client.RealTmuxPopupClientTests \
  tests.python.test_control_prune.RealTmuxPruneTests \
  tests.python.test_control_prune.RealJjForgetTests \
  tests.python.test_control_needs_input \
  tests.python.test_control_increment6.RealGithubSourceTests
printf 'ok - Control real-jj, isolated-tmux, and hermetic GitHub-source integration\n'
