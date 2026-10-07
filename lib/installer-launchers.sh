#!/usr/bin/env bash
# Sourced by lib/install.sh after its shared helpers; no source-time writes.

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
#   ~/.local/bin/asha-opencode -> asha
#
# `--default <h>` persists the bare-`asha` default harness to
# ~/.asha/config.json (.default_harness); absent => bin/asha falls back to claude.

install_bin() {
  local choice="$1"
  local user_bin="$HOME/.local/bin"
  local failed=0

  say ""
  say "== bin installer (--bin $choice) =="

  ensure_dir "$user_bin" || return $?

  # The dispatcher binary (absolute symlink into the repo).
  mklink "$MARKET_ROOT/bin/asha" "$user_bin/asha" "dispatcher" || return $?

  # Per-harness shims: relative symlinks to `asha` (bin/asha routes on basename).
  local h
  while IFS= read -r h; do
    case "$choice" in
      "$h"|all) _install_shim_link "$user_bin" "asha-$h" || failed=1 ;;
    esac
  done < <(asha_harnesses)

  # Persist the default harness only when --default was explicitly given (so a
  # first-run `asha codex` auto-config doesn't silently change the default).
  if [[ ${DEFAULT_SET:-0} -eq 1 ]]; then
    _write_default_harness "$BIN_DEFAULT" || failed=1
  fi

  _detect_legacy_asha
  return "$failed"
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
    if [[ ${DRY_RUN:-0} -ne 1 ]]; then rm "$link" || return $?; fi
  elif [[ -e "$link" ]]; then
    info "ERROR: refusing to replace foreign non-symlink launcher: $link"
    return 2
  fi

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  LINK [shim]  asha -> $link"
  else
    ln -s "asha" "$link" || return $?
    say "  shim $name -> asha"
  fi
}

# Persist .default_harness into ~/.asha/config.json. Writes THROUGH the file so
# a symlinked config.json (dotfiles) keeps its symlink and its other keys.
_write_default_harness() {
  local h="$1"
  local cfg="${ASHA_CONFIG:-${ASHA_HOME:-$HOME/.asha}/config.json}"
  [[ "$(jq -r '.default_harness // empty' "$cfg" 2>/dev/null || true)" != "$h" ]] || return 0

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  CONFIG  default_harness=$h -> $cfg"
    return 0
  fi

  ensure_dir "$(dirname "$cfg")" || return $?
  if [[ -f "$cfg" ]]; then
    local tmp
    tmp="$(mktemp)" || return $?
    if jq --arg h "$h" '.default_harness = $h' "$cfg" >"$tmp" 2>/dev/null; then
      # truncate+write through symlink; preserves the link
      cat "$tmp" >"$cfg" || { rm -f "$tmp"; return 2; }
      say "  default_harness -> $h ($cfg)"
    else
      info "ERROR: could not update $cfg (invalid JSON?); leaving as-is"
      rm -f "$tmp"
      return 2
    fi
    rm -f "$tmp"
  else
    printf '{\n  "default_harness": "%s"\n}\n' "$h" >"$cfg" || return $?
    say "  default_harness -> $h ($cfg, created)"
  fi
}

# Persist .asha_root into ~/.asha/config.json so commands and hooks can resolve
# the repo without the `asha` wrapper's exported ASHA_ROOT (bare `claude`/`codex`/
# `copilot`/`opencode` launches). Same write-through-symlink discipline as _write_default_harness.
_write_asha_root() {
  local cfg="${ASHA_CONFIG:-${ASHA_HOME:-$HOME/.asha}/config.json}"
  [[ "$(jq -r '.asha_root // empty' "$cfg" 2>/dev/null || true)" != "$MARKET_ROOT" ]] || return 0

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  CONFIG  asha_root=$MARKET_ROOT -> $cfg"
    return 0
  fi

  ensure_dir "$(dirname "$cfg")" || return $?
  if [[ -f "$cfg" ]]; then
    local tmp
    tmp="$(mktemp)" || return $?
    if jq --arg r "$MARKET_ROOT" '.asha_root = $r' "$cfg" >"$tmp" 2>/dev/null; then
      # truncate+write through symlink; preserves the link
      cat "$tmp" >"$cfg" || { rm -f "$tmp"; return 2; }
      say "  asha_root -> $MARKET_ROOT ($cfg)"
    else
      info "ERROR: could not update $cfg (invalid JSON?); leaving as-is"
      rm -f "$tmp"
      return 2
    fi
    rm -f "$tmp"
  else
    jq -n --arg r "$MARKET_ROOT" '{asha_root: $r}' >"$cfg" || return $?
    say "  asha_root -> $MARKET_ROOT ($cfg, created)"
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
