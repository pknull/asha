#!/usr/bin/env bash
# lib/control.sh — thin router for task, room, control, and initiative commands.

asha_control_main() {
  local root="${ASHA_ROOT:-}"
  if [[ -z "$root" ]]; then
    echo "ERROR: ASHA_ROOT is not set (lib/control.sh is sourced by bin/asha only)" >&2
    return 2
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required for Control commands" >&2
    return 1
  fi
  python3 -B -I -c \
    'import runpy,sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("control.cli", run_name="__main__")' \
    "$root/lib" "$@"
}
