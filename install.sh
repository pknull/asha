#!/usr/bin/env bash
# install.sh — thin shim over lib/install.sh (the install engine).
#
# Back-compat: `./install.sh --target codex [...]` behaves exactly as before.
# All logic lives in lib/install.sh so bin/asha can reuse it for
# `asha install <harness>` and first-run auto-configuration.
set -euo pipefail

# Resolve script dir, following symlinks. Portable (no GNU `readlink -f`).
__src="${BASH_SOURCE[0]}"
while [ -h "$__src" ]; do
  __d="$(cd -P "$(dirname "$__src")" >/dev/null 2>&1 && pwd)"
  __src="$(readlink "$__src")"
  case "$__src" in /*) ;; *) __src="$__d/$__src" ;; esac
done
__ROOT="$(cd -P "$(dirname "$__src")" >/dev/null 2>&1 && pwd)"
unset __src __d

# shellcheck source=lib/install.sh
source "$__ROOT/lib/install.sh"
asha_install_main "$@"
