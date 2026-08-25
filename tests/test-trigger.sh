#!/usr/bin/env bash
# `asha trigger` schedules coordinator launches through systemd user timers.
# All behavior is asserted against a sandbox XDG_CONFIG_HOME and a fake
# systemctl; no real units or timers are touched.
set -euo pipefail
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DISPATCHER="$REPO_ROOT/bin/asha"
WORK="$(mktemp -d)"
trap 'command rm -rf "$WORK"' EXIT
PASS=0
FAIL=0
ok() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }
mkdir -p "$WORK/bin" "$WORK/config" "$WORK/repo"
cat > "$WORK/bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
echo "systemctl $*" >> "${SYSTEMCTL_LOG:?}"
FAKE
cat > "$WORK/bin/systemd-analyze" <<'FAKE'
#!/usr/bin/env bash
[[ "$2" == "bogus schedule" ]] && exit 1
echo "Next elapse: soon"
FAKE
chmod +x "$WORK/bin/systemctl" "$WORK/bin/systemd-analyze"
# Sandbox hermeticity: an operator shell exporting these must not leak in.
unset ASHA_HOME XDG_STATE_HOME XDG_DATA_HOME 2>/dev/null || true
export XDG_CONFIG_HOME="$WORK/config" PATH="$WORK/bin:$PATH" SYSTEMCTL_LOG="$WORK/systemctl.log"
: > "$SYSTEMCTL_LOG"
UNITS="$WORK/config/systemd/user"

echo "--- test 1: add writes marked units and arms the timer ---"
"$DISPATCHER" trigger add morning --schedule "Mon..Fri 07:03" --root "$WORK/repo" \
  --intent 'triage: propose one "small" fix' > "$WORK/add.out"
if [[ -f "$UNITS/asha-trigger-morning.service" && -f "$UNITS/asha-trigger-morning.timer" ]]; then
  ok "both units written"
else
  fail "units missing"
fi
if sed -n 2p "$UNITS/asha-trigger-morning.timer" | grep -q "Managed by asha trigger"; then
  ok "managed marker present"
else
  fail "marker missing"
fi
if grep -q 'ExecStart=.*coordinator launch --root "'"$WORK/repo"'" --harness "claude" --intent "triage: propose one \\"small\\" fix"' "$UNITS/asha-trigger-morning.service"; then
  ok "ExecStart quotes root, harness, and intent"
else
  fail "ExecStart wrong: $(grep ExecStart "$UNITS/asha-trigger-morning.service")"
fi
if grep -q "OnCalendar=Mon..Fri 07:03" "$UNITS/asha-trigger-morning.timer" \
   && grep -q "Persistent=true" "$UNITS/asha-trigger-morning.timer"; then
  ok "timer carries the schedule persistently"
else
  fail "timer body wrong"
fi
if grep -q "daemon-reload" "$SYSTEMCTL_LOG" && grep -q -- "enable --now asha-trigger-morning.timer" "$SYSTEMCTL_LOG"; then
  ok "systemctl reloaded and armed the timer"
else
  fail "systemctl calls missing: $(cat "$SYSTEMCTL_LOG")"
fi
grep -q "wait at plan approval" "$WORK/add.out" && ok "approval boundary stated" || fail "no boundary message"

echo "--- test 2: list names the trigger and its intent ---"
out="$("$DISPATCHER" trigger list)"
grep -q "morning" <<<"$out" && grep -q "Mon..Fri 07:03" <<<"$out" && ok "list shows the trigger" || fail "list wrong: $out"

echo "--- test 3: refusals ---"
rc=0; "$DISPATCHER" trigger add "Bad Name" --schedule x --root "$WORK/repo" --intent y 2>/dev/null || rc=$?
[[ $rc -eq 2 ]] && ok "invalid name refused" || fail "invalid name rc=$rc"
rc=0; "$DISPATCHER" trigger add slug --schedule "bogus schedule" --root "$WORK/repo" --intent y 2>/dev/null || rc=$?
[[ $rc -eq 2 ]] && ok "invalid schedule refused" || fail "invalid schedule rc=$rc"
rc=0; "$DISPATCHER" trigger add slug --schedule "Mon 07:00" --intent y 2>/dev/null || rc=$?
[[ $rc -eq 2 ]] && ok "missing --root refused" || fail "missing root rc=$rc"
printf '[Unit]\nDescription=foreign\n' > "$UNITS/asha-trigger-foreign.timer"
rc=0; "$DISPATCHER" trigger remove foreign 2>/dev/null || rc=$?
[[ $rc -eq 2 && -f "$UNITS/asha-trigger-foreign.timer" ]] && ok "foreign unit is never removed" || fail "foreign removal rc=$rc"
rc=0; "$DISPATCHER" trigger add foreign --schedule "Mon 07:00" --root "$WORK/repo" --intent y 2>/dev/null || rc=$?
[[ $rc -eq 2 ]] && ok "foreign unit is never overwritten" || fail "foreign overwrite rc=$rc"

echo "--- test 4: remove deletes owned units and disables the timer ---"
"$DISPATCHER" trigger remove morning --dry-run | grep -q "would remove" && ok "remove --dry-run plans only" || fail "dry-run wrong"
[[ -f "$UNITS/asha-trigger-morning.timer" ]] || fail "dry-run deleted a unit"
"$DISPATCHER" trigger remove morning > /dev/null
if [[ ! -e "$UNITS/asha-trigger-morning.timer" && ! -e "$UNITS/asha-trigger-morning.service" ]]; then
  ok "owned units removed"
else
  fail "units left behind"
fi
grep -q -- "disable --now asha-trigger-morning.timer" "$SYSTEMCTL_LOG" && ok "timer disabled" || fail "no disable call"

echo ""
echo "test-trigger: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
