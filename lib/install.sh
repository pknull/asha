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
PLUGINS_DIR="$MARKET_ROOT/plugins"
NAMESPACES_FILE="$MARKET_ROOT/namespaces.json"
HARNESSES_DIR="$MARKET_ROOT/harnesses"

# Cross-platform shims (resolve_path); re-exported to sourced harness scripts.
# shellcheck source=lib/portable.sh
source "$MARKET_ROOT/lib/portable.sh"
# shellcheck source=../harnesses/registry.sh
source "$HARNESSES_DIR/registry.sh"
# shellcheck source=../harnesses/generated-artifacts.sh
source "$HARNESSES_DIR/generated-artifacts.sh"

# ---------------------------------------------------------------------------
# Shared helpers (used by all harness implementations)
# ---------------------------------------------------------------------------

die()  { echo "ERROR: ${1:-error}" >&2; exit "${2:-1}"; }
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
    all_plugin_dirs
  fi
}

# Enumerate ALL plugin dir basenames, ignoring the --only/$ONLY filter. Portable
# (GNU `find -printf` is unavailable on BSD/macOS): glob immediate subdirectories
# and emit their basenames. Used by register_hooks, which must reconcile the
# COMPLETE asha hook set every run regardless of --only scoping (a scoped install
# must never de-register another plugin's hooks).
all_plugin_dirs() {
  local d
  for d in "$PLUGINS_DIR"/*/; do
    [[ -d "$d" ]] || continue
    basename "$d"
  done | sort
}

# Remove only broken symlinks whose stored target points into this Asha source
# tree. Full installs use this to reconcile primitives removed or renamed since
# the previous install; foreign broken links are preserved.
prune_retired_asha_symlinks() {
  local home="$1" link raw n=0
  [[ -d "$home" ]] || return 0
  while IFS= read -r -d '' link; do
    [[ ! -e "$link" ]] || continue
    raw="$(readlink "$link" 2>/dev/null || true)"
    case "$raw" in
      "$MARKET_ROOT"/plugins/*|"${ABS_MARKET_ROOT:-$MARKET_ROOT}"/plugins/*)
        if [[ ${DRY_RUN:-0} -eq 1 ]]; then
          say "  RM [retired-link]  $link -> $raw"
        else
          rm -f "$link"
          log "removed retired Asha symlink: $link -> $raw"
        fi
        n=$((n + 1))
        ;;
    esac
  done < <(find "$home" -mindepth 1 -maxdepth 4 -type l -print0 2>/dev/null)
  [[ $n -gt 0 ]] && say "[$(basename "$home")] removed $n retired symlink(s)"
  return 0
}

usage() {
  cat <<'EOF'
install.sh / `asha install` — symlink-mount installer (multi-harness).

Usage:
  ./install.sh [--target T] [--bin B] [--default D] [--only ns,...] [--dry-run] [--force] [--verbose]
  asha install <claude|codex|copilot|opencode|both|all> [--bin B] [--default D] [--only ...] [--dry-run] [--force]

Targets (--target or positional after `asha install`):
  claude | codex | copilot | both (claude+codex) | all (claude+codex+copilot)

Bin:
  --bin <claude|codex|copilot|opencode|all> install ~/.local/bin/asha dispatcher + harness shims
  --default <claude|codex|copilot|opencode> default harness for bare `asha` (persisted to ~/.asha/config.json)

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

  asha_target_exists "$TARGET" \
    || die "invalid --target '$TARGET' (expected: $(asha_harness_names_inline)|both|all)" 1
  if [[ -n "$BIN" ]]; then
    { asha_harness_exists "$BIN" || [[ "$BIN" == all ]]; } \
      || die "invalid --bin '$BIN' (expected: $(asha_harness_names_inline)|all)" 1
  fi
  asha_harness_exists "$BIN_DEFAULT" \
    || die "invalid --default '$BIN_DEFAULT' (expected: $(asha_harness_names_inline))" 1
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
  say "== bin installer (--bin $choice) =="

  ensure_dir "$user_bin"

  # The dispatcher binary (absolute symlink into the repo).
  mklink "$MARKET_ROOT/bin/asha" "$user_bin/asha" "dispatcher"

  # Per-harness shims: relative symlinks to `asha` (bin/asha routes on basename).
  local h
  while IFS= read -r h; do
    case "$choice" in
      "$h"|all) _install_shim_link "$user_bin" "asha-$h" ;;
    esac
  done < <(asha_harnesses)

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

# Persist .asha_root into ~/.asha/config.json so commands and hooks can resolve
# the repo without the `asha` wrapper's exported ASHA_ROOT (bare `claude`/`codex`/
# `copilot` launches). Same write-through-symlink discipline as _write_default_harness.
_write_asha_root() {
  local cfg="${ASHA_CONFIG:-$HOME/.asha/config.json}"

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  CONFIG  asha_root=$MARKET_ROOT -> $cfg"
    return 0
  fi

  ensure_dir "$(dirname "$cfg")"
  if [[ -f "$cfg" ]]; then
    local tmp
    tmp="$(mktemp)"
    if jq --arg r "$MARKET_ROOT" '.asha_root = $r' "$cfg" >"$tmp" 2>/dev/null; then
      cat "$tmp" >"$cfg"      # truncate+write through symlink; preserves the link
      say "  asha_root -> $MARKET_ROOT ($cfg)"
    else
      info "warn: could not update $cfg (invalid JSON?); leaving as-is"
    fi
    rm -f "$tmp"
  else
    jq -n --arg r "$MARKET_ROOT" '{asha_root: $r}' >"$cfg"
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

# Detect a legacy flat ~/.asha/learnings.md with no OKF bundle yet and prompt the
# user to run the one-time, non-destructive migration. Does NOT migrate
# automatically (file conversion at install time is least-surprise-violating).
_detect_legacy_learnings() {
  local flat="$HOME/.asha/learnings.md"
  local bundle="$HOME/.asha/learnings"
  [[ -f "$flat" ]] || return 0
  # Already migrated if the bundle dir holds any concept files.
  if [[ -d "$bundle" ]] && compgen -G "$bundle/*.md" >/dev/null 2>&1; then
    return 0
  fi
  local migrator="$PLUGINS_DIR/session/tools/migrate_learnings_to_okf.py"
  say ""
  say "NOTE: legacy flat learnings detected at $flat"
  say "      This version stores learnings as an OKF concept bundle in ~/.asha/learnings/."
  say "      Run the one-time migration (non-destructive — the flat file is kept):"
  say "        python3 $migrator --dry-run   # preview"
  say "        python3 $migrator             # apply"
}

# ---------------------------------------------------------------------------
# Hook registration — the installer OWNS settings.json .hooks for asha
# ---------------------------------------------------------------------------
#
# register_hooks() is the SINGLE authority for asha hook entries in Claude's
# settings.json. It exists to cure the duplicate/canary drift that the older
# per-plugin tagged merge could not: that path only collapsed groups carrying
# the *exact* same "source":"asha:<ns>" tag, so legacy UNTAGGED asha groups
# (and stale duplicates from repeated surgical jq merges) accumulated forever.
#
# Asha-group identification (used to decide what to DROP before re-adding):
#   A hook group counts as an asha group — and is removed — when EITHER
#     (a) any hook's .command starts with "$ASHA_ROOT/plugins/"   (path-prefix), OR
#     (b) any hook's .source matches "asha:*"                      (legacy tag).
#   Either signal is sufficient, so UNTAGGED legacy groups whose command points
#   into the repo are collapsed alongside properly-tagged ones. NON-asha groups
#   (e.g. the user's own hooks under ~/.claude/hooks/: trace-pre.sh, trace-post.sh,
#   console-log-check.sh, lint-file.sh, doc-file-blocker.sh, console-log-audit.sh)
#   match neither test and are preserved byte-for-byte.
#
# The desired asha set is rebuilt from scratch each run: for every selected
# plugin EXCEPT the test plugin (its canary stop.sh must never reach prod),
# read plugins/<p>/hooks/hooks.json, substitute ${CLAUDE_PLUGIN_ROOT} with the
# plugin's absolute path, and tag each hook "source":"asha:<ns>". Re-running on
# an already-clean file is therefore a no-op (drop-then-readd is identity).
#
# Invocation:
#   - Called from asha_install_main() for the claude target after symlinks.
#   - Standalone: `source lib/install.sh; register_hooks` (e.g. to dry-run
#     against a COPY). Target file is $CLAUDE_SETTINGS (default
#     /home/pknull/.claude/settings.json) so a reviewer can point it elsewhere.
#   - Honors DRY_RUN / VERBOSE if already set; defaults them when sourced bare.
#
# Plugins excluded from prod hook registration.
_REGISTER_HOOKS_SKIP=(test)

_register_hooks_is_skip() {
  local p="$1" sp
  for sp in "${_REGISTER_HOOKS_SKIP[@]}"; do [[ "$p" == "$sp" ]] && return 0; done
  return 1
}

register_hooks() {
  # Defaults so the function is safe to call standalone (bare source).
  : "${DRY_RUN:=0}"; : "${VERBOSE:=0}"
  local settings
  settings="$(asha_harness_native_config claude)"
  local asha_root
  asha_root="$(resolve_path "$MARKET_ROOT")"

  # Only act if the file exists; absence is not an error (nothing to own yet).
  [[ -f "$settings" ]] || { log "register_hooks: $settings absent; skipping"; return 0; }

  # Build the DESIRED asha hook set: a single {event: [group,...]} object that
  # concatenates every selected, non-test plugin's tagged groups.
  local desired='{}'
  local plugin_dir ns plugin_root abs_root hooks_json
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || continue
    _register_hooks_is_skip "$plugin_dir" && continue

    plugin_root="$PLUGINS_DIR/$plugin_dir"
    if   [[ -f "$plugin_root/hooks/hooks.json" ]]; then hooks_json="$plugin_root/hooks/hooks.json"
    elif [[ -f "$plugin_root/hooks.json"      ]]; then hooks_json="$plugin_root/hooks.json"
    else continue
    fi

    local lifecycles_count
    lifecycles_count="$(jq -r '.hooks // {} | length' "$hooks_json")"
    [[ "$lifecycles_count" -gt 0 ]] || continue

    abs_root="$(resolve_path "$plugin_root")"
    ns="$(ns_for "$plugin_dir")"

    # Per-plugin tagged groups: ${CLAUDE_PLUGIN_ROOT} -> abs path, +source tag.
    local tagged
    tagged="$(jq \
      --arg root "$abs_root" \
      --arg tag  "asha:$ns" '
        .hooks
        | to_entries
        | map({
            key: .key,
            value: (
              .value
              | map(
                  .hooks |= map(
                    . + {
                      command: (.command | gsub("\\$\\{CLAUDE_PLUGIN_ROOT\\}"; $root)),
                      source: $tag
                    }
                  )
                )
            )
          })
        | from_entries
      ' "$hooks_json")"

    # Fold this plugin's events into the accumulator (concat per event).
    desired="$(jq -n \
      --argjson acc "$desired" \
      --argjson add "$tagged" '
        $acc as $a | $add as $b
        | reduce ($b | to_entries[]) as $e ($a;
            .[$e.key] = (($a[$e.key] // []) + $e.value))
      ')"
  done < <(all_plugin_dirs)   # full asha set, independent of --only (Defect 1)

  if [[ $DRY_RUN -eq 1 ]]; then
    local nd
    nd="$(jq -r '[ .[] | .[]? | .hooks[]? ] | length' <<<"$desired")"
    say "  HOOKS  would re-own $nd asha hook entr$([[ "$nd" == "1" ]] && echo y || echo ies) in $settings"
    return 0
  fi

  # Atomic, validated merge. For every event present in EITHER the existing file
  # or the desired set: strip existing asha hook ENTRIES (path-prefix OR source
  # tag) from each group — dropping a group only when ALL its hooks were asha,
  # but keeping the group (with its surviving non-asha hooks) when it was mixed —
  # then append the freshly-built asha groups. Non-asha hooks stay untouched;
  # events that end up empty are removed.
  local stamp bkp tmp
  # Unique backup name: timestamp + nanoseconds + PID, then a numeric-suffix
  # loop as a final guard so two runs in the same nanosecond never clobber an
  # existing backup (Defect 3).
  stamp="$(date +%Y%m%d-%H%M%S-%N)"
  bkp="$settings.bak-$stamp.$$"
  if [[ -e "$bkp" ]]; then
    local _i=1
    while [[ -e "$bkp.$_i" ]]; do _i=$((_i+1)); done
    bkp="$bkp.$_i"
  fi
  cp -p "$settings" "$bkp"
  say "backed up settings.json -> $bkp"

  tmp="$settings.tmp.$$"
  jq \
    --arg prefix "$asha_root/plugins/" \
    --argjson desired "$desired" '
      def is_asha_hook:
        ((.command // "") | startswith($prefix))
        or ((.source // "") | test("^asha:"));
      # Strip asha hook ENTRIES from a group, keeping co-located non-asha hooks.
      # Emit the slimmed group only if it still carries any non-asha hook; a
      # group whose hooks are ALL asha is dropped entirely (Defect 2).
      def strip_asha_hooks:
        (.hooks // []) as $hs
        | ($hs | map(select(is_asha_hook | not))) as $kept
        | if ($kept | length) > 0 then [ (.hooks = $kept) ] else [] end;
      .hooks = (.hooks // {})
      | ( (.hooks | keys) + ($desired | keys) | unique ) as $events
      | reduce $events[] as $e (.;
          .hooks[$e] = (
            ((.hooks[$e] // []) | map(strip_asha_hooks) | add // [])
            + ($desired[$e] // [])
          )
        )
      | .hooks |= with_entries(select(.value | length > 0))
    ' "$settings" > "$tmp" || { rm -f "$tmp"; die "register_hooks: jq merge failed" 4; }

  jq empty "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "register_hooks: resulting settings.json invalid" 4; }
  # Write THROUGH the file (truncate + cat) rather than `mv`, so a symlinked
  # settings.json (dotfiles) keeps its link instead of being replaced by a
  # regular file — matching _write_default_harness (Defect 4).
  cat "$tmp" > "$settings"
  rm -f "$tmp"

  local n
  n="$(jq -r '[ .hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | test("^asha:")) ] | length' "$settings")"
  say "  registered $n asha hook entr$([[ "$n" == "1" ]] && echo y || echo ies) in $settings"
}

# ---------------------------------------------------------------------------
# Identity bootstrap — ~/.asha/ (folded in from the retired session setup.sh)
# ---------------------------------------------------------------------------
#
# Creates the cross-project identity layer under ~/.asha/. Idempotent: each
# file is guarded by [[ ! -f ]] so existing user data is never clobbered (the
# directory itself, and each of communicationStyle.md / keeper.md / config.json,
# is only created when absent). This is the install-time half of what the old
# plugins/session/hooks/handlers/setup.sh did as a (never-firing) "Setup" hook;
# the per-project Memory/venv init it also carried belongs to /session:init and
# is intentionally NOT reproduced here.
bootstrap_identity() {
  local asha_home="$HOME/.asha"
  local tmpl_dir="$PLUGINS_DIR/session/templates"

  if [[ $DRY_RUN -eq 1 ]]; then
    [[ -d "$asha_home" ]] || say "  IDENTITY  would create $asha_home"
    [[ -f "$asha_home/communicationStyle.md" ]] || [[ ! -f "$tmpl_dir/communicationStyle.md" ]] || say "  IDENTITY  would create $asha_home/communicationStyle.md"
    [[ -f "$asha_home/keeper.md" ]]   || say "  IDENTITY  would create $asha_home/keeper.md"
    [[ -f "$asha_home/config.json" ]] || say "  IDENTITY  would create $asha_home/config.json"
    return 0
  fi

  if [[ ! -d "$asha_home" ]]; then
    mkdir -p "$asha_home"
    say "Created ~/.asha/"
  fi

  # communicationStyle.md — copied from the session plugin template if present.
  if [[ ! -f "$asha_home/communicationStyle.md" ]] && [[ -f "$tmpl_dir/communicationStyle.md" ]]; then
    cp "$tmpl_dir/communicationStyle.md" "$asha_home/communicationStyle.md"
    say "Created ~/.asha/communicationStyle.md"
  fi

  # keeper.md
  if [[ ! -f "$asha_home/keeper.md" ]]; then
    cat > "$asha_home/keeper.md" << 'KEEPER_EOF'
# Keeper Profile

Cross-project user profile. Additive only — signals accumulate with timestamps.

---

## Identity

- **Expertise**: (discovered organically)
- **Context**: (populated via /save)

---

## Voice Calibration

Accumulated signals about communication preferences.

| Date | Signal | Context | Source Project |
|------|--------|---------|----------------|

---

## Working Style

- (populated organically via /save)

---

## Notes

Persistent observations across projects.

---

## Calibration Log

Raw signals captured via `/save`. Synthesis updates sections above.

```
```
KEEPER_EOF
    say "Created ~/.asha/keeper.md"
  fi

  # ~/.asha/config.json
  if [[ ! -f "$asha_home/config.json" ]]; then
    cat > "$asha_home/config.json" << 'CONFIG_EOF'
{
  "version": "1.0",
  "description": "Asha cross-project configuration",
  "capture_calibration": true,
  "keeper_profile": "keeper.md",
  "identity_file": "communicationStyle.md"
}
CONFIG_EOF
    say "Created ~/.asha/config.json"
  fi
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

  local -a targets=()
  while IFS= read -r t; do targets+=("$t"); done < <(asha_expand_target "$TARGET")

  local t
  for t in "${targets[@]}"; do
    local harness_script="$HARNESSES_DIR/$t.sh"
    [[ -f "$harness_script" ]] || die "harness script missing: $harness_script"
    # shellcheck disable=SC1090
    source "$harness_script"
    "${t}_install"
    [[ -z "$ONLY" ]] && prune_retired_asha_symlinks "$(asha_harness_home "$t")"
    # The installer OWNS Claude's settings.json .hooks: after the claude target
    # has mounted its symlinks, rebuild the asha hook set centrally so legacy
    # untagged duplicates are collapsed and the test canary is excluded.
    [[ "$t" == "claude" ]] && register_hooks
  done

  # Cross-project identity layer (~/.asha/). Idempotent; never clobbers user data.
  bootstrap_identity

  # Record the repo root for wrapper-less launches (commands fall back to it).
  _write_asha_root

  [[ -n "$BIN" ]] && install_bin "$BIN"

  _detect_legacy_learnings

  say ""
  say "done."
}
