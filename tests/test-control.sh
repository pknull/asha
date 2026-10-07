#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_PATH="$PATH"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 0755 "$WORK"

export HOME="$WORK/home"
export ASHA_HOME="$HOME/.asha"
export XDG_RUNTIME_DIR="$WORK/runtime"
# The toolkit no longer consumes these; unset so an operator shell that
# exports them cannot leak real legacy-state detection into the fixtures.
unset XDG_STATE_HOME XDG_DATA_HOME
mkdir -p "$HOME/.asha" "$HOME/dotfiles/asha/.asha" "$WORK/reject-bin" "$WORK/repository"
chmod 0750 "$HOME"
# 0755: the old group-writable .asha tolerance is retired by ruling — the
# state tree now lives beneath it, and a writable .asha refuses everywhere.
chmod 0755 "$HOME/.asha" "$HOME/dotfiles/asha/.asha"
printf '%s\n' '{"control":{"default_harness":"codex"}}' >"$HOME/dotfiles/asha/.asha/config.json"
chmod 0600 "$HOME/dotfiles/asha/.asha/config.json"
ln -s '../dotfiles/asha/.asha/config.json' "$HOME/.asha/config.json"
mkdir -m 0750 "$ASHA_HOME/state"

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
state_before="$(find "$ASHA_HOME/state" -printf '%P %y %m %u %g\n' | sort)"
bytecode_before="$(find "$ROOT/lib/control" \( -type d -name __pycache__ -o -type f -name '*.pyc' \) -printf '%p %s %T@\n' | sort)"
cd "$WORK/repository"

# The retired-initiative evidence reader is the Control route most like the
# old task reads: an empty home lists nothing and shells out to nothing.
json="$(bash "$ROOT/bin/asha" initiative list --json)"
python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert d == {"contract":"asha.initiative-evidence-list.v1","initiatives":[]}' <<<"$json"
[[ ! -e "$WORK/invoked" ]]

set +e
control_err="$(bash "$ROOT/bin/asha" control 2>&1)"
control_rc=$?
set -e
[[ $control_rc -eq 2 ]]
[[ "$control_err" == *"asha control session list --json"* ]]
# The non-TTY degrade path must shell out to nothing at all.
[[ ! -e "$WORK/invoked" ]]

# Isolate the inherited-PYTHONPATH poison from the cwd-package poison so both
# attack paths are independently exercised.
mv "$WORK/repository/control" "$WORK/repository/control-disabled"
mkdir "$WORK/safe-cwd"
(
  cd "$WORK/safe-cwd"
  bash "$ROOT/bin/asha" initiative list --json >/dev/null
)
mv "$WORK/repository/control-disabled" "$WORK/repository/control"

after="$(find "$WORK/repository" -mindepth 1 -print | sort)"
state_after="$(find "$ASHA_HOME/state" -printf '%P %y %m %u %g\n' | sort)"
bytecode_after="$(find "$ROOT/lib/control" \( -type d -name __pycache__ -o -type f -name '*.pyc' \) -printf '%p %s %T@\n' | sort)"
[[ "$before" == "$after" ]]
[[ "$state_before" == "$state_after" ]]
[[ "$bytecode_before" == "$bytecode_after" ]]
[[ ! -e "$POISON_MARKER" ]]

printf 'ok - Control routes import only the trusted controller and touch no tool or state\n'
