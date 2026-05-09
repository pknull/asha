#!/usr/bin/env bash
# install.sh — symlink-mount installer for the asha repo (multi-harness dispatcher).
#
# Symlinks plugin primitives (skills/agents/commands/output-styles) into the
# native scan directories of one or more agent harnesses, and merges per-plugin
# hooks into the harness's settings/config, tagged with "asha:<ns>" for
# reversible removal by uninstall.sh.
#
# Hook scripts are NOT symlinked — they stay in source. Settings entries point
# at absolute source paths so each script's $(dirname "$0") resolves correctly.
#
# Usage:
#   ./install.sh [--target T] [--bin B] [--default D] [--only ns1,ns2,...]
#                [--dry-run] [--force] [--verbose]
#
# Targets:
#   --target claude           install primitives for Claude Code (default)
#   --target codex            install primitives for OpenAI Codex CLI
#   --target copilot          install primitives for GitHub Copilot CLI
#   --target both             install for claude+codex (back-compat)
#   --target all              install for claude+codex+copilot
#
# Bin:
#   --bin claude              install ~/.local/bin/asha → asha-claude wrapper
#   --bin codex               install ~/.local/bin/asha → asha-codex wrapper
#   --bin copilot             install ~/.local/bin/asha → asha-copilot wrapper
#   --bin all                 install all wrappers; --default picks the symlink target
#   --default {claude,codex,copilot}  default harness when --bin all (default: claude)
#
# Other flags:
#   --only      comma-separated plugin directory names (e.g. "devops,prompt")
#   --dry-run   print the action plan only; no filesystem or settings writes
#   --force     overwrite an existing destination symlink that points elsewhere
#   --verbose   echo each action
#
# Exit codes:
#   0  success (or dry-run completed)
#   1  usage error
#   2  conflict: destination exists and --force not given
#   3  dependency missing (jq)
#   4  merge failure (settings edit)

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve script location (handles invocation via symlink)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
MARKET_ROOT="$SCRIPT_DIR"
PLUGINS_DIR="$MARKET_ROOT/plugins"
NAMESPACES_FILE="$MARKET_ROOT/namespaces.json"
HARNESSES_DIR="$MARKET_ROOT/harnesses"

DRY_RUN=0
FORCE=0
VERBOSE=0
ONLY=""
TARGET="claude"          # default — preserves single-harness back-compat
BIN=""                   # empty = no bin install
BIN_DEFAULT="claude"

# ---------------------------------------------------------------------------
# Shared helpers (used by all harness implementations)
# ---------------------------------------------------------------------------

die()  { echo "ERROR: $*" >&2; exit "${2:-1}"; }
log()  { [[ $VERBOSE -eq 1 ]] && echo "  $*"; return 0; }
say()  { echo "$*"; }
info() { echo "$*" >&2; }

require_jq() {
  command -v jq >/dev/null 2>&1 || die "jq not found in PATH" 3
}

ensure_dir() {
  local d="$1"
  if [[ $DRY_RUN -eq 1 ]]; then
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
  abs_src="$(readlink -f "$src")"

  if [[ -L "$dest" ]]; then
    local existing
    existing="$(readlink -f "$dest" 2>/dev/null || true)"
    if [[ "$existing" == "$abs_src" ]]; then
      log "ok (already linked): $dest"
      return 0
    fi
    if [[ $FORCE -eq 0 ]]; then
      die "refusing to overwrite symlink pointing elsewhere: $dest -> $existing (use --force)" 2
    fi
    log "replacing: $dest -> $abs_src (was: $existing)"
    [[ $DRY_RUN -eq 1 ]] || rm "$dest"
  elif [[ -e "$dest" ]]; then
    if [[ $FORCE -eq 0 ]]; then
      die "refusing to overwrite non-link at destination: $dest (use --force)" 2
    fi
    log "removing non-link at dest: $dest"
    [[ $DRY_RUN -eq 1 ]] || rm -rf "$dest"
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
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
  if [[ -n "$ONLY" ]]; then
    IFS=',' read -ra arr <<<"$ONLY"
    printf '%s\n' "${arr[@]}"
  else
    find "$PLUGINS_DIR" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | sort
  fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

usage() {
  sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'
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
      --default) shift; BIN_DEFAULT="${1:-}" ;;
      --default=*) BIN_DEFAULT="${1#--default=}" ;;
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
# Installs harness-aware wrapper scripts and a default `asha` command. The
# real wrappers live in $MARKET_ROOT/bin/asha-{claude,codex}; this function
# symlinks them into $HOME/.local/bin so they're on PATH (XDG standard).
#
# Layout:
#   ~/.local/bin/asha-claude → $MARKET_ROOT/bin/asha-claude
#   ~/.local/bin/asha-codex  → $MARKET_ROOT/bin/asha-codex
#   ~/.local/bin/asha        → asha-{claude|codex}   (relative; switchable)
#
# The bare `asha` command's target is selected by the user:
#   --bin claude           → asha → asha-claude
#   --bin codex            → asha → asha-codex
#   --bin all              → both wrappers + asha → asha-${BIN_DEFAULT}
#
# Legacy ~/bin/asha (typically dotfile-tracked) is detected and the user is
# informed how to retire it. We never touch the dotfiles repo.

install_bin() {
  local choice="$1"
  local user_bin="$HOME/.local/bin"

  say ""
  say "== bin installer (--bin $choice, --default $BIN_DEFAULT) =="

  ensure_dir "$user_bin"

  case "$choice" in
    claude|all)
      mklink "$MARKET_ROOT/bin/asha-claude" "$user_bin/asha-claude" "wrapper"
      ;;
  esac
  case "$choice" in
    codex|all)
      mklink "$MARKET_ROOT/bin/asha-codex"  "$user_bin/asha-codex"  "wrapper"
      ;;
  esac
  case "$choice" in
    copilot|all)
      mklink "$MARKET_ROOT/bin/asha-copilot" "$user_bin/asha-copilot" "wrapper"
      ;;
  esac

  # Pick the bare `asha` command's target.
  local default_target
  case "$choice" in
    claude)  default_target="asha-claude" ;;
    codex)   default_target="asha-codex"  ;;
    copilot) default_target="asha-copilot" ;;
    all)     default_target="asha-${BIN_DEFAULT}" ;;
  esac
  _install_asha_default_link "$user_bin" "$default_target"

  _detect_legacy_asha
}

# Create or retarget ~/.local/bin/asha as a *relative* symlink to the chosen
# wrapper sibling. Idempotent.
_install_asha_default_link() {
  local user_bin="$1" target_name="$2"
  local link="$user_bin/asha"

  if [[ -L "$link" ]]; then
    local existing
    existing="$(readlink "$link" 2>/dev/null || true)"
    if [[ "$existing" == "$target_name" ]]; then
      log "ok: $link -> $target_name"
      return 0
    fi
    if [[ $FORCE -eq 0 ]]; then
      die "refusing to retarget $link (currently -> $existing); use --force" 2
    fi
    log "retargeting: $link ($existing -> $target_name)"
    [[ $DRY_RUN -eq 1 ]] || rm "$link"
  elif [[ -e "$link" ]]; then
    if [[ $FORCE -eq 0 ]]; then
      die "$link exists as a non-symlink; use --force to replace" 2
    fi
    log "removing non-link at $link"
    [[ $DRY_RUN -eq 1 ]] || rm -rf "$link"
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    say "  LINK [asha-default]  $target_name -> $link"
  else
    ln -s "$target_name" "$link"
    say "  asha command -> $target_name"
  fi
}

# Detect a legacy ~/bin/asha (typically dotfile-tracked) and inform the user.
# Does NOT touch dotfiles repos. Skips if the legacy entry already points
# into our managed bin dir.
_detect_legacy_asha() {
  local legacy="$HOME/bin/asha"
  [[ -e "$legacy" ]] || return 0

  if [[ -L "$legacy" ]]; then
    local target
    target="$(readlink -f "$legacy" 2>/dev/null || true)"
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
# Main
# ---------------------------------------------------------------------------

main() {
  parse_args "$@"
  require_jq

  [[ -d "$PLUGINS_DIR" ]]     || die "plugins dir not found: $PLUGINS_DIR"
  [[ -f "$NAMESPACES_FILE" ]] || die "namespaces.json not found: $NAMESPACES_FILE"
  [[ -d "$HARNESSES_DIR" ]]   || die "harnesses dir not found: $HARNESSES_DIR"

  say "install.sh: asha root = $MARKET_ROOT"
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

main "$@"
