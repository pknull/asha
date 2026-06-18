#!/usr/bin/env bash
# uninstall.sh — thin shim over lib/uninstall.sh (the uninstall engine).
#
# Back-compat: `./uninstall.sh --target codex [...]` behaves exactly as before.
set -euo pipefail

# Resolve script dir, following symlinks. Portable (no GNU `readlink -f`).
# asha-bootstrap-symlink-walk: resolve our own real path, portable (readlink -f is GNU-only).
# Duplicated across 6 scripts — find all: `grep -rn asha-bootstrap-symlink-walk`. Cannot DRY into
# lib/portable.sh:resolve_path() — this runs *before* portable.sh is locatable. Keep copies in sync.
__src="${BASH_SOURCE[0]}"
while [ -h "$__src" ]; do
  __d="$(cd -P "$(dirname "$__src")" >/dev/null 2>&1 && pwd)"
  __src="$(readlink "$__src")"
  case "$__src" in /*) ;; *) __src="$__d/$__src" ;; esac
done
__ROOT="$(cd -P "$(dirname "$__src")" >/dev/null 2>&1 && pwd)"
unset __src __d

# shellcheck source=lib/uninstall.sh
source "$__ROOT/lib/uninstall.sh"
asha_uninstall_main "$@"
