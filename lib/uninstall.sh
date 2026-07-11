#!/usr/bin/env bash
# lib/uninstall.sh — asha uninstall engine.
#
# Defines the uninstall logic as functions. Sourced by:
#   - ../uninstall.sh  (thin shim — standalone `./uninstall.sh ...`)
#   - ../bin/asha      (`asha uninstall <harness>`)
#
# Does NOT `set -e` at source scope (callers own shell options; bin/asha wraps
# invocations in a `set -euo pipefail` subshell).
#
# Public entry point: asha_uninstall_main "$@".

# Resolve repo root from THIS file's location (portable; no GNU readlink -f).
# asha-bootstrap-symlink-walk: resolve our own real path, portable (readlink -f is GNU-only).
# Duplicated across 6 scripts — find all: `grep -rn asha-bootstrap-symlink-walk`. Cannot DRY into
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

# shellcheck source=lib/portable.sh
source "$MARKET_ROOT/lib/portable.sh"
ABS_MARKET_ROOT="$(resolve_path "$MARKET_ROOT")"
HARNESSES_DIR="$MARKET_ROOT/harnesses"
PLUGINS_DIR="$MARKET_ROOT/plugins"
NAMESPACES_FILE="$MARKET_ROOT/namespaces.json"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

die()  { echo "ERROR: $*" >&2; exit "${2:-1}"; }
log()  { [[ ${VERBOSE:-0} -eq 1 ]] && echo "  $*" >&2; return 0; }
info() { echo "$*" >&2; }
say()  { echo "$*"; }

ns_for() {
  local plugin_dir="$1"
  local ns
  ns="$(jq -r --arg k "$plugin_dir" '.[$k] // empty' "$NAMESPACES_FILE" 2>/dev/null || true)"
  [[ -n "$ns" ]] || ns="$plugin_dir"
  echo "$ns"
}

# Remove symlinks under $1 whose realpath starts with $ABS_MARKET_ROOT, plus
# our own broken bin shims. Echoes count to stdout (everything else to stderr).
remove_symlinks_under() {
  local dir="$1" maxdepth="${2:-2}" count=0
  [[ -d "$dir" ]] || { echo "0"; return 0; }

  while IFS= read -r -d '' link; do
    # Broken symlink (referent missing). GNU `readlink -f` returns the missing
    # FINAL-component path (non-empty) here, so resolve_path can't flag it as
    # dangling — check existence directly. Only remove our own shims
    # (relative `asha`/`asha-*`) or asha-rooted links; never foreign ones. This
    # is what lets a relative shim (asha-codex -> asha) be cleaned even when the
    # `asha` dispatcher link was removed earlier in the same sweep.
    if [[ ! -e "$link" ]]; then
      local raw
      raw="$(readlink "$link" 2>/dev/null || true)"
      case "$raw" in
        asha|asha-claude|asha-codex|asha-copilot|"$ABS_MARKET_ROOT"|"$ABS_MARKET_ROOT"/*|"$MARKET_ROOT"|"$MARKET_ROOT"/*)
          if [[ ${DRY_RUN:-0} -eq 1 ]]; then
            info "  RM  $link (broken -> $raw)"
          else
            rm -f "$link"; log "removed broken: $link"
          fi
          count=$((count+1))
          ;;
        *) log "skip (foreign broken): $link -> $raw" ;;
      esac
      continue
    fi

    local target
    target="$(resolve_path "$link" 2>/dev/null || true)"
    [[ -z "$target" ]] && { log "dangling: $link (removing)"; rm -f "$link"; count=$((count+1)); continue; }
    case "$target" in
      "$ABS_MARKET_ROOT"|"$ABS_MARKET_ROOT"/*)
        if [[ ${DRY_RUN:-0} -eq 1 ]]; then
          info "  RM  $link (-> $target)"
        else
          rm -f "$link"
          log "removed: $link"
        fi
        count=$((count+1))
        ;;
      *) log "skip (foreign target): $link -> $target" ;;
    esac
  done < <(find "$dir/" -mindepth 1 -maxdepth "$maxdepth" -type l -print0)

  echo "$count"
}

usage() {
  cat <<'EOF'
uninstall.sh / `asha uninstall` — reverse the symlink-mount install.

Usage:
  ./uninstall.sh [--target T] [--dry-run] [--verbose]
  asha uninstall <claude|codex|copilot|both|all> [--dry-run] [--verbose]

Targets:
  claude | codex | copilot | both (claude+codex) | all (claude+codex+copilot)
EOF
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --verbose|-v) VERBOSE=1 ;;
      --target) shift; TARGET="${1:-}" ;;
      --target=*) TARGET="${1#--target=}" ;;
      -h|--help) usage ;;
      *) die "unknown arg: $1" 1 ;;
    esac
    shift
  done

  case "$TARGET" in
    claude|codex|copilot|both|all) ;;
    *) die "invalid --target '$TARGET' (expected: claude|codex|copilot|both|all)" 1 ;;
  esac
}

asha_uninstall_main() {
  DRY_RUN=0; VERBOSE=0; TARGET="claude"
  parse_args "$@"
  command -v jq >/dev/null 2>&1 || die "jq not found" 3

  [[ -d "$HARNESSES_DIR" ]] || die "harnesses dir not found: $HARNESSES_DIR"

  say "uninstall: asha root = $ABS_MARKET_ROOT"
  say "   target = $TARGET"
  [[ $DRY_RUN -eq 1 ]] && say "   (dry-run)"

  local targets
  case "$TARGET" in
    claude)  targets=(claude) ;;
    codex)   targets=(codex)  ;;
    copilot) targets=(copilot) ;;
    both)    targets=(claude codex) ;;
    all)     targets=(claude codex copilot) ;;
  esac

  local t total=0
  local -a failed=()
  # Remember the caller's errexit state so we can toggle around each harness.
  local had_e=0
  case "$-" in *e*) had_e=1 ;; esac
  for t in "${targets[@]}"; do
    local harness_script="$HARNESSES_DIR/$t.sh"
    [[ -f "$harness_script" ]] || die "harness script missing: $harness_script"
    # shellcheck disable=SC1090
    source "$harness_script"
    # Each harness exports <T>_UNINSTALL_TOTAL.
    local var="${t^^}_UNINSTALL_TOTAL"
    local total_file="${TMPDIR:-/tmp}/.asha-uninstall-total.$$"
    # Failure isolation: one harness failing must never silently strand the
    # ones after it (issue #4: codex died mid-uninstall under set -e and
    # copilot was never swept). Each harness runs in its own subshell with
    # errexit ON, so ANY failure — including die()'s exit and unguarded
    # command failures — aborts only that harness, loudly. The subshell must
    # NOT sit in an if/&&/|| condition: bash ignores `set -e` inside a
    # condition context even when re-set explicitly, which would silently
    # mask failures instead of isolating them. Hence the set +e/-e toggle.
    # The count handoff is cosmetic and must never fail the harness: without
    # `|| true`, an unwritable TMPDIR would abort the subshell AFTER a fully
    # successful uninstall and report a false failure (review pass 2).
    set +e
    ( set -e; "${t}_uninstall"; echo "${!var:-0}" > "$total_file" 2>/dev/null || true )
    local rc=$?
    [[ $had_e -eq 1 ]] && set -e
    if [[ $rc -ne 0 ]]; then
      failed+=("$t")
      info "WARN: [$t] uninstall failed (exit $rc); continuing with remaining targets"
    fi
    local n_harness
    n_harness="$(cat "$total_file" 2>/dev/null || true)"
    rm -f "$total_file"
    total=$((total + ${n_harness:-0}))
  done

  # Clean asha bin entries from ~/.local/bin/. The dispatcher `asha` resolves
  # into the repo (removed by realpath match); the relative shims (asha-* ->
  # asha) go broken once `asha` is gone and are caught by the broken-link branch
  # in remove_symlinks_under regardless of sweep order.
  local user_bin="$HOME/.local/bin"
  if [[ -d "$user_bin" ]]; then
    local n; n="$(remove_symlinks_under "$user_bin" 1)"
    [[ "$n" -gt 0 ]] && say "removed $n bin entr(y/ies) from $user_bin"
    total=$((total + n))
  fi

  say ""
  say "done. total symlinks removed: $total"
  if [[ ${#failed[@]} -gt 0 ]]; then
    say "WARNING: uninstall incomplete for: ${failed[*]} — re-run after fixing the errors above"
    return 1
  fi
}
