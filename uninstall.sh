#!/usr/bin/env bash
# uninstall.sh — reverse the symlink-mount install (multi-harness dispatcher).
#
# Scans the harness's scan directories for symlinks whose realpath resolves
# inside this asha tree and removes them. Strips harness settings/config hook
# entries tagged "source": "asha:*" (and legacy "marketplace:*" for
# migration cleanup).
#
# Source tree is never touched.
#
# Usage:
#   ./uninstall.sh [--target T] [--dry-run] [--verbose]
#
# Targets:
#   --target claude           uninstall from Claude Code (default)
#   --target codex            uninstall from OpenAI Codex CLI
#   --target copilot          uninstall from GitHub Copilot CLI
#   --target both             uninstall from claude+codex (back-compat)
#   --target all              uninstall from claude+codex+copilot
#
# Exit codes:
#   0  success (or dry-run)
#   1  usage error
#   3  dependency missing (jq)
#   4  settings edit failure

set -euo pipefail

# Resolve script dir, following symlinks. Portable (no GNU `readlink -f`).
__asha_src="${BASH_SOURCE[0]}"
while [ -h "$__asha_src" ]; do
  __asha_dir="$(cd -P "$(dirname "$__asha_src")" >/dev/null 2>&1 && pwd)"
  __asha_src="$(readlink "$__asha_src")"
  case "$__asha_src" in /*) ;; *) __asha_src="$__asha_dir/$__asha_src" ;; esac
done
SCRIPT_DIR="$(cd -P "$(dirname "$__asha_src")" >/dev/null 2>&1 && pwd)"
unset __asha_src __asha_dir
MARKET_ROOT="$SCRIPT_DIR"
# Cross-platform shims (resolve_path); re-exported to sourced harness scripts.
# shellcheck source=lib/portable.sh
source "$MARKET_ROOT/lib/portable.sh"
ABS_MARKET_ROOT="$(resolve_path "$MARKET_ROOT")"
HARNESSES_DIR="$MARKET_ROOT/harnesses"
PLUGINS_DIR="$MARKET_ROOT/plugins"

DRY_RUN=0
VERBOSE=0
TARGET="claude"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

die()  { echo "ERROR: $*" >&2; exit "${2:-1}"; }
log()  { [[ $VERBOSE -eq 1 ]] && echo "  $*" >&2; return 0; }
info() { echo "$*" >&2; }
say()  { echo "$*"; }

# Remove symlinks under $1 whose realpath starts with $ABS_MARKET_ROOT.
# Echoes count to stdout (everything else goes to stderr).
remove_symlinks_under() {
  local dir="$1" maxdepth="${2:-2}" count=0
  [[ -d "$dir" ]] || { echo "0"; return 0; }

  while IFS= read -r -d '' link; do
    local target
    target="$(resolve_path "$link" 2>/dev/null || true)"
    [[ -z "$target" ]] && { log "dangling: $link (removing)"; rm -f "$link"; count=$((count+1)); continue; }
    case "$target" in
      "$ABS_MARKET_ROOT"|"$ABS_MARKET_ROOT"/*)
        if [[ $DRY_RUN -eq 1 ]]; then
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

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --verbose|-v) VERBOSE=1 ;;
      --target) shift; TARGET="${1:-}" ;;
      --target=*) TARGET="${1#--target=}" ;;
      -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "unknown arg: $1" 1 ;;
    esac
    shift
  done

  case "$TARGET" in
    claude|codex|copilot|both|all) ;;
    *) die "invalid --target '$TARGET' (expected: claude|codex|copilot|both|all)" 1 ;;
  esac
}

main() {
  parse_args "$@"
  command -v jq >/dev/null 2>&1 || die "jq not found" 3

  [[ -d "$HARNESSES_DIR" ]] || die "harnesses dir not found: $HARNESSES_DIR"

  say "uninstall.sh: asha root = $ABS_MARKET_ROOT"
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
  for t in "${targets[@]}"; do
    local harness_script="$HARNESSES_DIR/$t.sh"
    [[ -f "$harness_script" ]] || die "harness script missing: $harness_script"
    # shellcheck disable=SC1090
    source "$harness_script"
    # Each harness exports <T>_UNINSTALL_TOTAL.
    local var="${t^^}_UNINSTALL_TOTAL"
    "${t}_uninstall"
    total=$((total + ${!var:-0}))
  done

  # Clean asha-rooted bin wrappers from ~/.local/bin/. The bare `asha` symlink
  # is relative (asha-claude or asha-codex), so removing the wrapper makes it
  # dangling — remove_symlinks_under cleans dangling links automatically.
  local user_bin="$HOME/.local/bin"
  if [[ -d "$user_bin" ]]; then
    local n; n="$(remove_symlinks_under "$user_bin" 1)"
    [[ "$n" -gt 0 ]] && say "removed $n bin wrapper(s) from $user_bin"
    total=$((total + n))
  fi

  say ""
  say "done. total symlinks removed: $total"
}

main "$@"
