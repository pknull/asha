#!/usr/bin/env bash
# lib/install.sh — asha install engine.
#
# Defines the install logic as functions; runs nothing at source time beyond
# resolving repo-path vars and sourcing portable.sh. Sourced by:
#   - ../install.sh   (thin shim — standalone `./install.sh ...`)
#   - ../bin/asha     (`asha install <harness>` and first-run auto-config)
#
# Deliberately does NOT `set -e` at source scope: bin/asha sources this into a
# non-`-e` shell and wraps each invocation in a `set -euo pipefail` subshell;
# the install.sh shim sets the options itself.
#
# Public entry points: asha_install_main "$@"  and  install_bin <choice>.

# Resolve repo root from THIS file's location (portable; no GNU readlink -f),
# independent of which script sourced us.
__eng_src="${BASH_SOURCE[0]}"
while [ -h "$__eng_src" ]; do
  __eng_dir="$(cd -P "$(dirname "$__eng_src")" >/dev/null 2>&1 && pwd)"
  __eng_src="$(readlink "$__eng_src")"
  case "$__eng_src" in /*) ;; *) __eng_src="$__eng_dir/$__eng_src" ;; esac
done
__ASHA_LIB_DIR="$(cd -P "$(dirname "$__eng_src")" >/dev/null 2>&1 && pwd)"
unset __eng_src __eng_dir
MARKET_ROOT="${MARKET_ROOT:-$(dirname "$__ASHA_LIB_DIR")}"
PLUGINS_DIR="$MARKET_ROOT/plugins"
NAMESPACES_FILE="$MARKET_ROOT/namespaces.json"
HARNESSES_DIR="$MARKET_ROOT/harnesses"

# Cross-platform shims (resolve_path); re-exported to sourced harness scripts.
# shellcheck source=lib/portable.sh
source "$MARKET_ROOT/lib/portable.sh"

# ---------------------------------------------------------------------------
# Shared helpers (used by all harness implementations)
# ---------------------------------------------------------------------------

die()  { echo "ERROR: $*" >&2; exit "${2:-1}"; }
log()  { [[ ${VERBOSE:-0} -eq 1 ]] && echo "  $*"; return 0; }
say()  { echo "$*"; }
info() { echo "$*" >&2; }

require_jq() {
  command -v jq >/dev/null 2>&1 || die "jq not found in PATH" 3
}

ensure_dir() {
  local d="$1"
  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    [[ -d "$d" ]] || log "mkdir -p $d"
  else
    mkdir -p "$d"
  fi
}

# Create one symlink. Idempotent (skip if already correct). Refuses on
# mismatched existing target unless --force.
# Args: SOURCE DEST KIND
mklink() {
  local src="$1" dest="$2" kind="$3"
  local abs_src
  abs_src="$(resolve_path "$src")"

  if [[ -L "$dest" ]]; then
    local existing
    existing="$(resolve_path "$dest" 2>/dev/null || true)"
    if [[ "$existing" == "$abs_src" ]]; then
      log "ok (already linked): $dest"
      return 0
    fi
    if [[ ${FORCE:-0} -eq 0 ]]; then
      die "refusing to overwrite symlink pointing elsewhere: $dest -> $existing (use --force)" 2
    fi
    log "replacing: $dest -> $abs_src (was: $existing)"
    [[ ${DRY_RUN:-0} -eq 1 ]] || rm "$dest"
  elif [[ -e "$dest" ]]; then
    if [[ ${FORCE:-0} -eq 0 ]]; then
      die "refusing to overwrite non-link at destination: $dest (use --force)" 2
    fi
    log "removing non-link at dest: $dest"
    [[ ${DRY_RUN:-0} -eq 1 ]] || rm -rf "$dest"
  fi

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  LINK [$kind]  $abs_src -> $dest"
  else
    ensure_dir "$(dirname "$dest")"
    ln -s "$abs_src" "$dest"
    log "linked [$kind]: $dest -> $abs_src"
  fi
}

# Look up namespace for a plugin dir name. Falls back to dir name if not in map.
ns_for() {
  local plugin_dir="$1"
  local ns
  ns="$(jq -r --arg k "$plugin_dir" '.[$k] // empty' "$NAMESPACES_FILE")"
  [[ -n "$ns" ]] || ns="$plugin_dir"
  echo "$ns"
}

selected_plugins() {
  if [[ -n "${ONLY:-}" ]]; then
    IFS=',' read -ra arr <<<"$ONLY"
    printf '%s\n' "${arr[@]}"
  else
    # Portable plugin-dir enumeration (GNU `find -printf` is unavailable on
    # BSD/macOS). Glob the immediate subdirectories and emit their basenames.
    local d
    for d in "$PLUGINS_DIR"/*/; do
      [[ -d "$d" ]] || continue
      basename "$d"
    done | sort
  fi
}

usage() {
  cat <<'EOF'
install.sh / `asha install` — symlink-mount installer (multi-harness).

Usage:
  ./install.sh [--target T] [--bin B] [--default D] [--only ns,...] [--dry-run] [--force] [--verbose]
  asha install <claude|codex|copilot|both|all> [--bin B] [--default D] [--only ...] [--dry-run] [--force]

Targets (--target or positional after `asha install`):
  claude | codex | copilot | both (claude+codex) | all (claude+codex+copilot)

Bin:
  --bin <claude|codex|copilot|all>   install ~/.local/bin/asha dispatcher + harness shims
  --default <claude|codex|copilot>   default harness for bare `asha` (persisted to ~/.asha/config.json)

Other:
  --only ns1,ns2   limit to named plugin dirs
  --dry-run        print the action plan only; no writes
  --force          replace mismatched symlinks
  --verbose        echo each action
EOF
  exit 1
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --force)   FORCE=1 ;;
      --verbose|-v) VERBOSE=1 ;;
      --only)    shift; ONLY="${1:-}" ;;
      --only=*)  ONLY="${1#--only=}" ;;
      --target)  shift; TARGET="${1:-}" ;;
      --target=*) TARGET="${1#--target=}" ;;
      --bin)     shift; BIN="${1:-}" ;;
      --bin=*)   BIN="${1#--bin=}" ;;
      --default) shift; BIN_DEFAULT="${1:-}"; DEFAULT_SET=1 ;;
      --default=*) BIN_DEFAULT="${1#--default=}"; DEFAULT_SET=1 ;;
      -h|--help) usage ;;
      *)         die "unknown argument: $1" 1 ;;
    esac
    shift
  done

  case "$TARGET" in
    claude|codex|copilot|both|all) ;;
    *) die "invalid --target '$TARGET' (expected: claude|codex|copilot|both|all)" 1 ;;
  esac
  if [[ -n "$BIN" ]]; then
    case "$BIN" in
      claude|codex|copilot|all) ;;
      *) die "invalid --bin '$BIN' (expected: claude|codex|copilot|all)" 1 ;;
    esac
  fi
  case "$BIN_DEFAULT" in
    claude|codex|copilot) ;;
    *) die "invalid --default '$BIN_DEFAULT' (expected: claude|codex|copilot)" 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Bin installer
# ---------------------------------------------------------------------------
#
# Installs the `asha` dispatcher and per-harness shims into ~/.local/bin (XDG,
# on PATH). The dispatcher (bin/asha) routes by argv / invocation name.
#
# Layout:
#   ~/.local/bin/asha          -> $MARKET_ROOT/bin/asha          (absolute)
#   ~/.local/bin/asha-claude   -> asha   (relative shim; basename routing)
#   ~/.local/bin/asha-codex    -> asha
#   ~/.local/bin/asha-copilot  -> asha
#
# `--default <h>` persists the bare-`asha` default harness to
# ~/.asha/config.json (.default_harness); absent => bin/asha falls back to claude.

install_bin() {
  local choice="$1"
  local user_bin="$HOME/.local/bin"

  say ""
  say "== bin installer (--bin $choice, --default $BIN_DEFAULT) =="

  ensure_dir "$user_bin"

  # The dispatcher binary (absolute symlink into the repo).
  mklink "$MARKET_ROOT/bin/asha" "$user_bin/asha" "dispatcher"

  # Per-harness shims: relative symlinks to `asha` (bin/asha routes on basename).
  local h
  for h in claude codex copilot; do
    case "$choice" in
      "$h"|all) _install_shim_link "$user_bin" "asha-$h" ;;
    esac
  done

  # Persist the default harness only when --default was explicitly given (so a
  # first-run `asha codex` auto-config doesn't silently change the default).
  [[ ${DEFAULT_SET:-0} -eq 1 ]] && _write_default_harness "$BIN_DEFAULT"

  _detect_legacy_asha
}

# Create/retarget a relative shim symlink (asha-<h> -> asha). Idempotent.
_install_shim_link() {
  local user_bin="$1" name="$2"
  local link="$user_bin/$name"

  if [[ -L "$link" ]]; then
    local existing
    existing="$(readlink "$link" 2>/dev/null || true)"
    if [[ "$existing" == "asha" ]]; then
      log "ok: $link -> asha"
      return 0
    fi
    if [[ ${FORCE:-0} -eq 0 ]]; then
      die "refusing to retarget $link (currently -> $existing); use --force" 2
    fi
    log "retargeting: $link ($existing -> asha)"
    [[ ${DRY_RUN:-0} -eq 1 ]] || rm "$link"
  elif [[ -e "$link" ]]; then
    if [[ ${FORCE:-0} -eq 0 ]]; then
      die "$link exists as a non-symlink; use --force to replace" 2
    fi
    log "removing non-link at $link"
    [[ ${DRY_RUN:-0} -eq 1 ]] || rm -rf "$link"
  fi

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  LINK [shim]  asha -> $link"
  else
    ln -s "asha" "$link"
    say "  shim $name -> asha"
  fi
}

# Persist .default_harness into ~/.asha/config.json. Writes THROUGH the file so
# a symlinked config.json (dotfiles) keeps its symlink and its other keys.
_write_default_harness() {
  local h="$1"
  local cfg="${ASHA_CONFIG:-$HOME/.asha/config.json}"

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  CONFIG  default_harness=$h -> $cfg"
    return 0
  fi

  ensure_dir "$(dirname "$cfg")"
  if [[ -f "$cfg" ]]; then
    local tmp
    tmp="$(mktemp)"
    if jq --arg h "$h" '.default_harness = $h' "$cfg" >"$tmp" 2>/dev/null; then
      cat "$tmp" >"$cfg"      # truncate+write through symlink; preserves the link
      say "  default_harness -> $h ($cfg)"
    else
      info "warn: could not update $cfg (invalid JSON?); leaving as-is"
    fi
    rm -f "$tmp"
  else
    printf '{\n  "default_harness": "%s"\n}\n' "$h" >"$cfg"
    say "  default_harness -> $h ($cfg, created)"
  fi
}

# Detect a legacy ~/bin/asha (typically dotfile-tracked) and inform the user.
# Does NOT touch dotfiles repos. Skips if it already points into our repo.
_detect_legacy_asha() {
  local legacy="$HOME/bin/asha"
  [[ -e "$legacy" ]] || return 0

  if [[ -L "$legacy" ]]; then
    local target
    target="$(resolve_path "$legacy" 2>/dev/null || true)"
    case "$target" in
      "$MARKET_ROOT"/*) return 0 ;;   # already pointing into asha repo
    esac
  fi

  say ""
  say "NOTE: legacy wrapper detected at $legacy"
  say "      ~/.local/bin precedes ~/bin in your PATH, so the new wrapper takes precedence."
  say "      To retire the old one, in the repo where it's tracked (e.g. dotfiles):"
  say "        git rm bin/asha && git commit -m 'retire bin/asha (replaced by asha installer)'"
}

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

asha_install_main() {
  # Reset runtime state on each call (globals, visible to helpers).
  DRY_RUN=0; FORCE=0; VERBOSE=0; ONLY=""
  TARGET="claude"; BIN=""; BIN_DEFAULT="claude"; DEFAULT_SET=0

  parse_args "$@"
  require_jq

  [[ -d "$PLUGINS_DIR" ]]     || die "plugins dir not found: $PLUGINS_DIR"
  [[ -f "$NAMESPACES_FILE" ]] || die "namespaces.json not found: $NAMESPACES_FILE"
  [[ -d "$HARNESSES_DIR" ]]   || die "harnesses dir not found: $HARNESSES_DIR"

  say "install: asha root = $MARKET_ROOT"
  say "   target = $TARGET"
  [[ $DRY_RUN -eq 1 ]] && say "   (dry-run: no filesystem or settings changes)"
  [[ $FORCE   -eq 1 ]] && say "   (force: will replace mismatched symlinks)"
  [[ -n "$ONLY"     ]] && say "   (only: $ONLY)"

  local targets
  case "$TARGET" in
    claude)  targets=(claude) ;;
    codex)   targets=(codex)  ;;
    copilot) targets=(copilot) ;;
    both)    targets=(claude codex) ;;
    all)     targets=(claude codex copilot) ;;
  esac

  local t
  for t in "${targets[@]}"; do
    local harness_script="$HARNESSES_DIR/$t.sh"
    [[ -f "$harness_script" ]] || die "harness script missing: $harness_script"
    # shellcheck disable=SC1090
    source "$harness_script"
    "${t}_install"
  done

  [[ -n "$BIN" ]] && install_bin "$BIN"

  say ""
  say "done."
}
