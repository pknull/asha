#!/usr/bin/env bash
# lib/doctor.sh — `asha doctor` verb: thin adapter over bin/asha-drift-check.sh
# (the diagnostic engine; kept at its path for cron/systemd users).
#
# Usage:  asha doctor [claude|codex|copilot|opencode|all] [--fix]
# Exit:   0 clean, 1 one-or-more failures, 2 usage error.
#
# Note: `asha claude doctor` still reaches Claude Code's OWN doctor (launch
# forwarding); this verb is asha's install-health audit.
#
# Does NOT `set -e` at source scope (callers own shell options; bin/asha wraps
# invocations in a `set -euo pipefail` subshell).
#
# Public entry point: asha_doctor_main "$@".

# Resolve repo root from THIS file's location (portable; no GNU readlink -f).
# asha-bootstrap-symlink-walk: resolve our own real path, portable (readlink -f is GNU-only).
# Duplicated across 7 scripts — find all: `grep -rn asha-bootstrap-symlink-walk`. Cannot DRY into
# lib/portable.sh:resolve_path() — this runs *before* portable.sh is locatable. Keep copies in sync.
__eng_src="${BASH_SOURCE[0]}"
while [ -h "$__eng_src" ]; do
  __eng_dir="$(cd -P "$(dirname "$__eng_src")" >/dev/null 2>&1 && pwd)"
  __eng_src="$(readlink "$__eng_src")"
  case "$__eng_src" in /*) ;; *) __eng_src="$__eng_dir/$__eng_src" ;; esac
done
__ASHA_LIB_DIR="$(cd -P "$(dirname "$__eng_src")" >/dev/null 2>&1 && pwd)"
unset __eng_src __eng_dir
MARKET_ROOT="${MARKET_ROOT:-$(dirname "$__ASHA_LIB_DIR")}"
# shellcheck source=../harnesses/registry.sh
source "$MARKET_ROOT/harnesses/registry.sh"

asha_doctor_main() {
  local target="all" fix=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      claude|codex|copilot|opencode|all) target="$1" ;;
      # Value validated BEFORE the shift: with the flag as last arg, the
      # loop-bottom shift would fail under bin/asha's set -e wrap and die
      # silently with rc=1 — doctor's "findings" code (review pass 2).
      --target) [[ -n "${2:-}" ]] || { echo "ERROR: --target requires a value" >&2; return 2; }
                target="$2"; shift ;;
      --target=*) target="${1#--target=}" ;;
      --fix) fix=1 ;;
      -h|--help)
        cat <<'EOF'
asha doctor — audit the asha install for drift.

Usage:
  asha doctor [claude|codex|copilot|opencode|all] [--fix]

Targets default to 'all'. --fix self-heals stale command-skills and drifted
guardrails. Exit: 0 clean, 1 failures, 2 usage error.
(Claude Code's native doctor remains at: asha claude doctor)
EOF
        return 0 ;;
      *) echo "ERROR: unknown arg: $1 (see: asha doctor --help)" >&2; return 2 ;;
    esac
    shift
  done
  { asha_harness_exists "$target" || [[ "$target" == all ]]; } \
    || { echo "ERROR: invalid target '$target'" >&2; return 2; }

  local -a args=(--target "$target")
  [[ $fix -eq 1 ]] && args+=(--fix)
  # Child process, not sourced: drift-check is a standalone set -uo script
  # that exits directly. rc captured with `|| rc=$?` because this runs under
  # bin/asha's set -e wrapper — a bare non-zero here would skip the
  # workspace section below.
  local drift_rc=0
  bash "$MARKET_ROOT/bin/asha-drift-check.sh" "${args[@]}" || drift_rc=$?
  local ws_rc=0
  _asha_doctor_workspace_section "$target" || ws_rc=$?
  [[ $drift_rc -eq 0 && $ws_rc -eq 0 ]]
}

# Workspace v1 (issue #35): report workspace state from CWD. No workspace is
# a silent pass; warnings print but pass; an invalid manifest FAILS doctor —
# fail-closed, a broken workspace definition is install-grade breakage.
_asha_doctor_workspace_section() {
  local tool="$MARKET_ROOT/plugins/session/tools/workspace_status.py"
  [[ -f "$tool" ]] || return 0
  if ! command -v python3 >/dev/null 2>&1; then
    # Visible skip, not silence: an invalid manifest coexisting with a green
    # doctor because python was missing would be a hidden gap (pass-2).
    echo ""
    echo "── Workspace (asha workspace status) ──"
    echo "skipped: python3 unavailable — workspace manifest NOT validated"
    return 0
  fi
  local out rc=0
  out="$(python3 "$tool" 2>/dev/null)" || rc=$?
  # Single-project mode: nothing to report, doctor stays quiet.
  [[ $rc -eq 0 && "$out" == "workspace: none"* ]] && return 0
  echo ""
  echo "── Workspace (asha workspace status) ──"
  printf '%s\n' "$out"
  # A workspace is in play (valid or broken): surface what each harness can
  # actually enforce for it (#39 acceptance criterion). Informational — the
  # support level being `partial` is documented reality, not drift.
  _asha_doctor_workspace_capabilities "${1:-all}"
  return $rc
}

# Print the per-harness `workspace` capability entry (support + limitations)
# from harnesses/capabilities.json, scoped to the doctor target. Never fails
# doctor: capability limitations are attested facts, and file validation is
# owned by the schema tests, not this section.
_asha_doctor_workspace_capabilities() {
  local caps="$MARKET_ROOT/harnesses/capabilities.json" target="${1:-all}"
  [[ -f "$caps" ]] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  python3 - "$caps" "$target" <<'PYEOF' 2>/dev/null || true
import json, sys

path, target = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    print("WARN: harnesses/capabilities.json unreadable — "
          "workspace capability matrix not shown")
    sys.exit(0)

harnesses = data.get("harnesses", {})
names = list(harnesses) if target == "all" else [target]
print("workspace capability (harnesses/capabilities.json):")
for name in names:
    entry = (harnesses.get(name, {}).get("capabilities", {})
             .get("workspace"))
    if not isinstance(entry, dict):
        print(f"  {name}: none declared")
        continue
    support = entry.get("support", "?")
    limitations = entry.get("limitations") or []
    if limitations:
        print(f"  {name}: {support}")
        for item in limitations:
            print(f"    - {item}")
    else:
        print(f"  {name}: {support} — no limitations")
PYEOF
}
