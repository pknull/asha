#!/usr/bin/env bash
# lib/control.sh — thin router for room, control and initiative commands (and the retired task noun).

_ASHA_CONTROL_PROGRAM='import runpy,sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("control.cli", run_name="__main__")'

_asha_control_ready() {
  if [[ -z "${ASHA_ROOT:-}" ]]; then
    echo "ERROR: ASHA_ROOT is not set (lib/control.sh is sourced by bin/asha only)" >&2
    return 2
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required for Control commands" >&2
    return 1
  fi
}

asha_control_main() {
  _asha_control_ready || return
  python3 -B -I -c "$_ASHA_CONTROL_PROGRAM" "$ASHA_ROOT/lib" "$@"
}
