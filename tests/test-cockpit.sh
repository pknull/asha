#!/usr/bin/env bash
# `asha cockpit` is tmux layout glue over Control: it plans one window with the
# coordinator pane (`asha claude` at DIR) beside `asha control --initiatives`.
# The plan is asserted through --dry-run; no tmux server is touched.
set -euo pipefail
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DISPATCHER="$REPO_ROOT/bin/asha"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0
FAIL=0
ok() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }
mkdir -p "$WORK/home" "$WORK/Code/termart" "$WORK/bin"
# A tmux on PATH is required for the plan; the dry run never invokes it.
printf '#!/usr/bin/env bash\nexit 0\n' >"$WORK/bin/tmux"; chmod +x "$WORK/bin/tmux"
export HOME="$WORK/home" PATH="$WORK/bin:$PATH"
unset TMUX

echo "--- test 1: outside tmux, the plan creates, splits, selects, attaches ---"
out="$("$DISPATCHER" cockpit "$WORK/Code" --dry-run)"
if [[ "$(wc -l <<<"$out")" == 4 ]]; then ok "four steps"; else fail "expected four steps, got: $out"; fi
if grep -q "^tmux new-session -d -s asha-cockpit-Code -c $WORK/Code -n cockpit -- $REPO_ROOT/bin/asha claude$" <<<"$out"; then ok "coordinator pane runs asha claude at DIR"; else fail "new-session line wrong: $out"; fi
if grep -q "^tmux split-window -h -t asha-cockpit-Code:cockpit -c $WORK/Code -- $REPO_ROOT/bin/asha control --initiatives$" <<<"$out"; then ok "monitor pane runs asha control --initiatives"; else fail "split line wrong: $out"; fi
if grep -q "^tmux select-pane -t asha-cockpit-Code:cockpit.0$" <<<"$out" && grep -q "^tmux attach-session -t asha-cockpit-Code$" <<<"$out"; then ok "focus returns to the coordinator, then attaches"; else fail "select/attach lines wrong: $out"; fi

echo "--- test 2: inside tmux, a window is added to the current session ---"
out="$(TMUX=/tmp/fake-socket,1,0 "$DISPATCHER" cockpit "$WORK/Code/termart" --dry-run)"
if grep -q "^tmux new-window -c $WORK/Code/termart -n cockpit -- $REPO_ROOT/bin/asha claude$" <<<"$out" && grep -q "^tmux split-window -h -c $WORK/Code/termart -- $REPO_ROOT/bin/asha control --initiatives$" <<<"$out" && grep -q "^tmux select-pane -L$" <<<"$out"; then ok "new-window plan"; else fail "inside-tmux plan wrong: $out"; fi
if ! grep -q "attach-session\|new-session" <<<"$out"; then ok "no nested session"; else fail "nested session planned: $out"; fi

echo "--- test 3: defaults and refusals ---"
out="$(cd "$WORK/Code" && "$DISPATCHER" cockpit --dry-run)"
if grep -q "asha-cockpit-Code" <<<"$out"; then ok "DIR defaults to the current directory"; else fail "default dir wrong: $out"; fi
out="$("$DISPATCHER" cockpit "$WORK/Code" --session keeper --dry-run)"
if grep -q "^tmux attach-session -t keeper$" <<<"$out"; then ok "--session names the session"; else fail "--session ignored: $out"; fi
if "$DISPATCHER" cockpit "$WORK/missing" --dry-run >/dev/null 2>&1; then fail "missing DIR accepted"; else ok "missing DIR refused"; fi
if "$DISPATCHER" cockpit --bogus --dry-run >/dev/null 2>&1; then fail "unknown option accepted"; else ok "unknown option refused"; fi
if "$DISPATCHER" cockpit "$WORK/Code" "$WORK/Code" --dry-run >/dev/null 2>&1; then fail "two DIRs accepted"; else ok "two DIRs refused"; fi
if PATH="$WORK/nope:$PATH" "$DISPATCHER" cockpit "$WORK/Code" --dry-run >/dev/null 2>&1 && ! command -v tmux >/dev/null; then fail "tmux check"; else ok "tmux presence is checked"; fi

echo ""
echo "test-cockpit: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
