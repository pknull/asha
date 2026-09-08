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

# Outcomes are dynamically scoped locals of asha_install_main, never sticky
# globals. Public sourced install_bin calls have no adapter prerequisite:
# --bin all explicitly authorizes even UNATTEMPTED harnesses, not FAILED ones.
_launcher_authorized() {
  local h="$1" choice="$2"
  case " ${_asha_failed_targets:-} " in *" $h "*) return 1 ;; esac
  case " ${_asha_requested_targets:-} " in *" $h "*) return 0 ;; esac
  [[ "$choice" == all || "$choice" == "$h" ]] && return 0
  [[ -n "$choice" && ${DEFAULT_SET:-0} -eq 1 && "${BIN_DEFAULT:-}" == "$h" ]]
}

# Shared routing has consumers beyond the requested shim. Preflight all shared
# changes before touching any launcher or routing config; --force does not
# authorize redirecting a failed or unrequested existing consumer. Compatible
# routing is reused byte-for-byte, including configs with unusual formatting.
_launcher_preflight() {
  local choice="$1" h protected=0 protected_default=0 home cfg current_root current_default
  local user_bin="$HOME/.local/bin"
  if [[ -n "$choice" ]]; then
    local authorized=0
    while IFS= read -r h; do
      case "$choice" in
        "$h"|all) if _launcher_authorized "$h" "$choice"; then authorized=1; fi ;;
      esac
    done < <(asha_harnesses)
    if [[ ${DEFAULT_SET:-0} -eq 1 ]] && _launcher_authorized "$BIN_DEFAULT" "$choice"; then authorized=1; fi
    if [[ $authorized -eq 0 ]]; then
      info "ERROR: launcher routing refused: no authorized requested launcher remains"
      return 1
    fi
  fi
  cfg="${ASHA_CONFIG:-${ASHA_HOME:-$HOME/.asha}/config.json}"
  if [[ -e "$cfg" || -L "$cfg" ]] && ! jq -e 'type == "object"' "$cfg" >/dev/null 2>&1; then
    info "ERROR: launcher routing refused: existing routing config is not a JSON object"
    return 1
  fi
  current_root="$(jq -r '.asha_root // empty' "$cfg" 2>/dev/null)" || current_root=""
  current_default="$(jq -r '.default_harness // "claude"' "$cfg" 2>/dev/null)" || current_default=claude
  while IFS= read -r h; do
    _launcher_authorized "$h" "$choice" && continue
    home="$(asha_harness_home "$h")" || return 1
    # A pristine native config is not an Asha routing consumer. Existing
    # mounts/manifests, shims, or a failed adapter's home are protected.
    local failed_home=0
    case " ${_asha_failed_targets:-} " in
      *" $h "*) [[ ! -e "$home" && ! -L "$home" ]] || failed_home=1 ;;
    esac
    if [[ $failed_home -eq 1 || -d "$home/skills" || -d "$home/agents" \
        || -f "${ASHA_HOME:-$HOME/.asha}/install-manifests/$h.json" \
        || -e "$user_bin/asha-$h" || -L "$user_bin/asha-$h" ]] \
        || [[ "$current_default" == "$h" && ( -e "$user_bin/asha" || -L "$user_bin/asha" ) ]]; then
      protected=1
    fi
  done < <(asha_harnesses)
  local consumer name raw resolved current_dispatcher
  current_dispatcher="$(resolve_path "$user_bin/asha" 2>/dev/null || true)"
  if [[ -e "$user_bin/asha" || -L "$user_bin/asha" ]] && ! asha_harness_exists "$current_default"; then
    protected=1
    protected_default=1
  fi
  # Include hidden immediate entries without changing the caller's dotglob
  # setting. Neither hidden pattern includes . or .. (also on Bash 3.2).
  for consumer in "$user_bin"/* "$user_bin"/.[!.]* "$user_bin"/..?*; do
    [[ -e "$consumer" || -L "$consumer" ]] || continue
    [[ "$consumer" != "$user_bin/asha" ]] || continue
    name="${consumer##*/}"
    case "$name" in
      asha-*)
        if asha_harness_exists "${name#asha-}"; then continue; fi
        protected=1
        protected_default=1
        ;;
    esac
    if [[ -L "$consumer" ]]; then
      raw="$(readlink "$consumer")"
      resolved="$(resolve_path "$consumer" 2>/dev/null || true)"
      case "$raw" in asha|asha-*|"$user_bin/asha") protected=1; protected_default=1 ;; esac
      if [[ -n "$current_dispatcher" && "$resolved" == "$current_dispatcher" ]]; then
        protected=1
        protected_default=1
      fi
    fi
  done
  [[ $protected -eq 1 ]] || return 0
  if [[ "$current_root" != "$MARKET_ROOT" ]]; then
    info "ERROR: launcher routing refused: asha_root change would redirect a protected consumer"
    return 1
  fi
  if [[ -n "$choice" ]]; then
    resolved="$(resolve_path "$user_bin/asha" 2>/dev/null || true)"
    if [[ ! -L "$user_bin/asha" || "$resolved" != "$(resolve_path "$MARKET_ROOT/bin/asha")" ]]; then
      info "ERROR: launcher routing refused: dispatcher change would redirect a protected consumer"
      return 1
    fi
    # Unknown invocation names route through the default, not through a known
    # harness shim. Requesting that harness never authorizes the unknown name.
    if [[ ${DEFAULT_SET:-0} -eq 1 && "${BIN_DEFAULT:-}" != "$current_default" ]] \
        && { [[ $protected_default -eq 1 ]] || ! _launcher_authorized "$current_default" "$choice"; }; then
      info "ERROR: launcher routing refused: default change would redirect a protected consumer"
      return 1
    fi
  fi
}

install_bin() {
  local choice="$1"
  local user_bin="$HOME/.local/bin"
  local failed=0

  _launcher_preflight "$choice" || return $?

  say ""
  say "== bin installer (--bin $choice) =="

  ensure_dir "$user_bin" || return $?

  # The dispatcher binary (absolute symlink into the repo).
  mklink "$MARKET_ROOT/bin/asha" "$user_bin/asha" "dispatcher" || return $?

  # Per-harness shims: relative symlinks to `asha` (bin/asha routes on basename).
  local h
  while IFS= read -r h; do
    case "$choice" in
      "$h"|all)
        if _launcher_authorized "$h" "$choice"; then
          _install_shim_link "$user_bin" "asha-$h" || failed=1
        else
          info "ERROR: launcher skipped for failed adapter: $h"
          failed=1
        fi
        ;;
    esac
  done < <(asha_harnesses)

  # Persist the default harness only when --default was explicitly given (so a
  # first-run `asha codex` auto-config doesn't silently change the default).
  if [[ ${DEFAULT_SET:-0} -eq 1 ]]; then
    if _launcher_authorized "$BIN_DEFAULT" "$choice"; then
      _write_default_harness "$BIN_DEFAULT" || failed=1
    else
      info "ERROR: default skipped for failed adapter: $BIN_DEFAULT"
      failed=1
    fi
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
