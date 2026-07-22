#!/usr/bin/env bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
# lib/portable.sh — cross-platform shims for asha install/launch scripts.
#
# Sourced by install.sh and uninstall.sh (which re-export to the harness
# scripts they source) and by the bin/ launch wrappers. Keeps GNU/Linux
# behavior byte-identical while adding a BSD/macOS fallback. Defines a function
# plus one capability flag — no other side effects on source.

# Probe once whether GNU-style `readlink -f` is available. It is on Linux and
# on macOS 12.3+ (which adopted the GNU flag). When present it is authoritative
# and used verbatim, so resolution — including its empty/rc1 result for a path
# whose non-final component is missing — is identical to the pre-port behavior
# that callers depend on (notably uninstall.sh's dangling-symlink detection).
if [ "$(readlink -f / 2>/dev/null)" = "/" ]; then
  __ASHA_HAS_READLINK_F=1
else
  __ASHA_HAS_READLINK_F=
fi

# resolve_path PATH
#   Canonicalize PATH to an absolute, symlink-resolved path: a portable
#   stand-in for GNU `readlink -f`, which older BSD/macOS readlink lacks.
#   Prints the resolved path (rc 0), or prints nothing (rc 1) when PATH cannot
#   be resolved — matching `readlink -f`'s contract, including the callers that
#   wrap it in `... 2>/dev/null || true` and treat empty output as "dangling".
resolve_path() {
  local p="${1:-}"
  [ -n "$p" ] || return 1

  if [ -n "$__ASHA_HAS_READLINK_F" ]; then
    readlink -f -- "$p" 2>/dev/null
    return
  fi

  # Fallback for macOS < 12.3 (no `readlink -f`). python3 is already required
  # by asha's memory pipeline, so it is a safe last resort. The dirname check
  # reproduces GNU `readlink -f`'s rule that all but the final component must
  # exist; otherwise emit nothing so callers see it as unresolved.
  python3 - "$p" 2>/dev/null <<'PY'
import os, sys
p = os.path.realpath(sys.argv[1])
print(p if os.path.isdir(os.path.dirname(p)) else "")
PY
}
