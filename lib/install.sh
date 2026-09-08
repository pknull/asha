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
# shellcheck source=lib/imported-skills.sh
source "$MARKET_ROOT/lib/imported-skills.sh"
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

# Return success when the target harness's ownership manifest records a
# generated artifact below DEST. The exact-record lookup remains centralized
# in asha_artifact_manifest_hash_for; this function only discovers the
# descendant path to ask it about.
_asha_managed_artifact_under() {
  local dest="$1" kind="$2" harness manifest artifact
  case "$kind" in
    skill-dir|agent|command) harness=claude ;;
    codex-*) harness=codex ;;
    copilot-*) harness=copilot ;;
    opencode-*) harness=opencode ;;
    *) return 1 ;;
  esac
  manifest="$(asha_artifact_manifest_path "$harness")"
  [[ -f "$manifest" ]] || return 1
  artifact="$(python3 - "$manifest" "$dest" <<'PY'
import json
import os
import sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1) from None
prefix = os.path.abspath(sys.argv[2]).rstrip(os.sep) + os.sep
for item in data.get("artifacts", []):
    candidate = os.path.abspath(item.get("destination", ""))
    if candidate.startswith(prefix):
        print(candidate)
        break
PY
)" || return 1
  [[ -n "$artifact" ]] || return 1
  asha_artifact_manifest_hash_for "$harness" "$artifact" >/dev/null
}

# Create one symlink. Idempotent (skip if already correct). Foreign real files
# and directories are never deleted, including under --force. A real directory
# may be replaced under --force only when the target harness manifest proves it
# contains an Asha-generated artifact.
# Args: SOURCE DEST KIND
mklink() {
  local src="$1" dest="$2" kind="$3"
  local abs_src
  abs_src="$(resolve_path "$src" 2>/dev/null || true)"
  [[ -n "$abs_src" ]] || abs_src="$src"

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
    if [[ ${DRY_RUN:-0} -ne 1 ]]; then rm "$dest" || return $?; fi
  elif [[ -e "$dest" ]]; then
    if [[ ${FORCE:-0} -eq 0 ]]; then
      die "refusing to overwrite non-link at destination: $dest" 2
    fi
    if ! _asha_managed_artifact_under "$dest" "$kind"; then
      die "refusing to delete non-link not recorded as Asha-generated: $dest" 2
    fi
    log "replacing manifest-recorded generated directory: $dest"
    if [[ ${DRY_RUN:-0} -ne 1 ]]; then rm -rf -- "$dest" || return $?; fi
  fi

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  LINK [$kind]  $abs_src -> $dest"
  else
    ensure_dir "$(dirname "$dest")" || return $?
    ln -s "$abs_src" "$dest" || return $?
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
    local -a arr
    IFS=',' read -ra arr <<<"$ONLY"
    local item
    for item in "${arr[@]}"; do
      # `imported` is the user skill source namespace, not a repository plugin.
      # It is enumerated by selected_imported_skill_sources() below.
      [[ "$item" == imported ]] || printf '%s\n' "$item"
    done
  else
    all_plugin_dirs
  fi
}

# Optional plugins are excluded from the complete/default set unless the
# caller explicitly enables canaries or names that plugin with --only.  The
# latter matters to global hook reconciliation, which otherwise ignores ONLY.
_include_plugin_dir() {
  local plugin_dir="$1" item
  [[ "${WITH_CANARY:-0}" == 1 ]] && return 0
  if ! jq -e --arg plugin "$plugin_dir" \
      '((._optional // []) | index($plugin)) != null' \
      "$NAMESPACES_FILE" >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${ONLY:-}" ]]; then
    local -a requested
    IFS=',' read -ra requested <<<"$ONLY"
    for item in "${requested[@]}"; do
      [[ "$item" == "$plugin_dir" ]] && return 0
    done
  fi
  return 1
}

# Enumerate ALL plugin dir basenames, ignoring the --only/$ONLY filter. Portable
# (GNU `find -printf` is unavailable on BSD/macOS): glob immediate subdirectories
# and emit their basenames. Used by register_hooks, which must reconcile the
# COMPLETE asha hook set every run regardless of --only scoping (a scoped install
# must never de-register another plugin's hooks). Optional plugins are the one
# exception: they enter the complete set only when explicitly enabled.
all_plugin_dirs() {
  local d
  for d in "$PLUGINS_DIR"/*/; do
    [[ -d "$d" ]] || continue
    d="${d%/}"
    _include_plugin_dir "${d##*/}" || continue
    printf '%s\n' "${d##*/}"
  done | sort
}

# True when TARGET points into a repository plugin that is optional and not
# selected for this run.  Symlink ownership is established by that target
# location; ordinary files at the same destination remain user-managed.
_disabled_optional_plugin_target() {
  local target="$1" abs_market_root="$2" root relative plugin_dir
  [[ -n "$target" ]] || return 1
  for root in "$MARKET_ROOT" "$abs_market_root"; do
    [[ -n "$root" ]] || continue
    case "$target" in
      "$root"/plugins/*)
        relative="${target#"$root"/plugins/}"
        plugin_dir="${relative%%/*}"
        _include_plugin_dir "$plugin_dir" || return 0
        ;;
    esac
  done
  return 1
}

# Remove broken Asha-owned links and imported mounts no longer recorded in the
# canonical store's lockfile. Full installs also retire links into optional
# plugins that are disabled for this run. Foreign links and canonical imported
# content are preserved.
prune_retired_asha_symlinks() {
  local home="$1" root link raw target n=0 imported_root abs_imported_root imported_name lock
  local abs_market_root
  abs_market_root="$(resolve_path "$MARKET_ROOT" 2>/dev/null || true)"
  imported_root="$(asha_imported_skills_root)"
  abs_imported_root="$(resolve_path "$imported_root" 2>/dev/null || true)"
  lock="$imported_root/imported.lock.json"
  [[ -d "$home" ]] || return 0
  # The harness's primitive roots may themselves be user-managed symlinks
  # (for example ~/.claude/agents -> a dotfiles checkout). Plain `find` does
  # not descend through those nested roots, leaving retired Asha links behind.
  # Follow only the known primitive roots as command-line symlinks (`-H`), not
  # arbitrary links below the harness home.
  for root in "$home" "$home/agents" "$home/commands" "$home/skills"; do
    [[ -d "$root" ]] || continue
    while IFS= read -r -d '' link; do
      raw="$(readlink "$link" 2>/dev/null || true)"
      target="$(resolve_path "$link" 2>/dev/null || true)"
      local disabled_optional=0
      if _disabled_optional_plugin_target "$raw" "$abs_market_root" \
          || _disabled_optional_plugin_target "$target" "$abs_market_root"; then
        disabled_optional=1
      fi
      if [[ -e "$link" && $disabled_optional -eq 0 ]]; then
        if ! imported_name="$(
          imported_skill_name_from_target "$raw" "$imported_root" "$abs_imported_root"
        )"; then
          continue
        fi
        if [[ -f "$lock" ]] && jq -e --arg name "$imported_name" \
          '.skills | has($name)' "$lock" >/dev/null 2>&1; then
          continue
        fi
      fi
      local owned=0
      case "$raw" in
        "$MARKET_ROOT"/plugins/*|"${ABS_MARKET_ROOT:-$MARKET_ROOT}"/plugins/*|"$imported_root"/*) owned=1 ;;
      esac
      if [[ $owned -eq 0 && -n "$abs_market_root" ]]; then
        case "$target" in "$abs_market_root"/plugins/*) owned=1 ;; esac
      fi
      if [[ $owned -eq 0 && -n "$abs_imported_root" ]]; then
        case "$raw" in "$abs_imported_root"/*) owned=1 ;; esac
      fi
      [[ $owned -eq 1 ]] || continue
      if [[ ${DRY_RUN:-0} -eq 1 ]]; then
        say "  RM [retired-link]  $link -> $raw"
      else
        rm -f "$link"
        log "removed retired Asha symlink: $link -> $raw"
      fi
      n=$((n + 1))
    done < <(find -H "$root" -mindepth 1 -maxdepth 4 -type l -print0 2>/dev/null)
  done
  [[ $n -gt 0 ]] && say "[$(basename "$home")] removed $n retired symlink(s)"
  return 0
}

usage() {
  cat <<'EOF'
install.sh / `asha install` — symlink-mount installer (multi-harness).

Usage:
  ./install.sh [--target T] [--bin B] [--default D] [--only ns,...] [--with-canary] [--dry-run] [--force] [--verbose]
  asha install <claude|codex|copilot|opencode|both|all> [--bin B] [--default D] [--only ...] [--with-canary] [--dry-run] [--force]

Targets (--target or positional after `asha install`):
  claude | codex | copilot | opencode | both (claude+codex) | all (all four)

Bin:
  --bin <claude|codex|copilot|opencode|all> install ~/.local/bin/asha dispatcher + harness shims
  --default <claude|codex|copilot|opencode> default harness for bare `asha` (persisted to ~/.asha/config.json)

Other:
  --only ns1,ns2   limit to named plugin dirs
  --with-canary    include opt-in installer canary plugins
  --dry-run        print the action plan only; no writes
  --force          replace mismatched symlinks
  --verbose        echo each action
EOF
  exit 1
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --only|--target|--bin|--default)
        [[ $# -ge 2 ]] || die "missing value for $1" 1 ;;
    esac
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --force)   FORCE=1 ;;
      --verbose|-v) VERBOSE=1 ;;
      --with-canary) WITH_CANARY=1 ;;
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
    if [[ $# -gt 0 ]]; then shift; fi
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

# Launcher helpers depend on the shared functions above (also a public sourced API).
# shellcheck source=lib/installer-launchers.sh
source "$MARKET_ROOT/lib/installer-launchers.sh"

# Detect every shipped legacy learning store. Installation never migrates
# authority: root OKF concepts, the old archive, and the flat file must be
# reviewed item by item through /session:consolidate.
_detect_legacy_learnings() {
  local flat="${ASHA_HOME:-$HOME/.asha}/learnings.md"
  local bundle="${ASHA_HOME:-$HOME/.asha}/learnings"
  local archive="${ASHA_HOME:-$HOME/.asha}/learnings-archive"
  local marker="$bundle/.migration-v2.json"
  # Reviewed migration is deliberately source-preserving. Once the migration
  # manager has committed its global marker, the remaining root/archive files
  # are evidence and rollback material—not an unfinished upgrade.
  if [[ -f "$marker" ]] && jq -e '
      .version == 2 and .status == "reviewed-migration-complete" and
      (.review_sha256 | type == "string" and test("^[0-9a-f]{64}$"))
    ' "$marker" >/dev/null 2>&1; then
    return 0
  fi
  local found=0
  [[ -f "$flat" ]] && found=1
  if [[ -d "$bundle" ]]; then
    local concept
    for concept in "$bundle"/*.md; do
      [[ -f "$concept" && "$(basename "$concept")" != "index.md" ]] || continue
      found=1
      break
    done
  fi
  [[ -d "$archive" ]] && compgen -G "$archive/*.md" >/dev/null 2>&1 && found=1
  [[ $found -eq 1 ]] || return 0
  say ""
  say "NOTE: legacy learning records detected (root OKF bundle, archive, or flat store)."
  say "      They remain untouched and do not acquire v2 authority automatically."
  say "      Run /session:consolidate to inventory and review each item before migration."
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
# The desired asha set is rebuilt from scratch each run: for every plugin in
# all_plugin_dirs (which excludes opt-in plugins unless explicitly enabled),
# read plugins/<p>/hooks/hooks.json, substitute ${CLAUDE_PLUGIN_ROOT} with the
# plugin's absolute path, and tag each hook "source":"asha:<ns>". Re-running on
# an already-clean file is therefore a no-op (drop-then-readd is identity).
#
# Invocation:
#   - Called from asha_install_main() for the claude target after symlinks.
#   - Standalone: `source lib/install.sh; register_hooks` (e.g. to dry-run
#     against a COPY). Target file is $CLAUDE_SETTINGS (default
#     ~/.claude/settings.json) so a reviewer can point it elsewhere.
#   - Honors DRY_RUN / VERBOSE if already set; defaults them when sourced bare.
#
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
  # concatenates every enabled plugin's tagged groups.
  local desired='{}'
  local plugin_dir ns plugin_root abs_root hooks_json
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || continue

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
                  select(((._asha_harnesses // ["claude"]) | index("claude")) != null)
                  | del(._asha_harnesses)
                  |
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
# Creates the compact cross-project identity layer under ~/.asha/. Idempotent:
# existing user files are never clobbered. Extended identity material belongs
# under ~/.asha/reference/ and is loaded only through the asha-reference skill.
bootstrap_identity() {
  local asha_home="${ASHA_HOME:-$HOME/.asha}"
  local tmpl_dir="$PLUGINS_DIR/asha/templates"

  if [[ $DRY_RUN -eq 1 ]]; then
    [[ -d "$asha_home" ]] || say "  IDENTITY  would create $asha_home"
    local identity_name
    for identity_name in soul voice keeper; do
      [[ -f "$asha_home/$identity_name.md" ]] \
        || say "  IDENTITY  would create $asha_home/$identity_name.md"
    done
    [[ -f "$asha_home/config.json" ]] || say "  IDENTITY  would create $asha_home/config.json"
    return 0
  fi

  if [[ ! -d "$asha_home" ]]; then
    mkdir -p "$asha_home"
    say "Created ~/.asha/"
  fi
  local identity_name
  for identity_name in soul voice keeper; do
    if [[ ! -f "$asha_home/$identity_name.md" ]]; then
      [[ -f "$tmpl_dir/$identity_name.md" ]] \
        || die "identity template missing: $tmpl_dir/$identity_name.md"
      cp "$tmpl_dir/$identity_name.md" "$asha_home/$identity_name.md"
      say "Created ~/.asha/$identity_name.md"
    fi
  done

  # ~/.asha/config.json
  if [[ ! -f "$asha_home/config.json" ]]; then
    cat > "$asha_home/config.json" << 'CONFIG_EOF'
{
  "version": "2.0",
  "description": "Asha user configuration"
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
  DRY_RUN=0; FORCE=0; VERBOSE=0; ONLY=""; WITH_CANARY=0
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
  [[ $WITH_CANARY -eq 1 ]] && say "   (with canary plugins)"

  local -a targets=()
  while IFS= read -r t; do targets+=("$t"); done < <(asha_expand_target "$TARGET")

  local t
  local -a results=()
  local -a failed=()
  local _asha_failed_targets="" _asha_requested_targets="${targets[*]}"
  local launcher_failed=0
  # Remember the caller's errexit state so we can toggle around each harness.
  local had_e=0
  case "$-" in *e*) had_e=1 ;; esac
  for t in "${targets[@]}"; do
    local harness_script="$HARNESSES_DIR/$t.sh"
    [[ -f "$harness_script" ]] || die "harness script missing: $harness_script"
    # shellcheck disable=SC1090
    source "$harness_script"
    # Failure isolation: one harness failing must never prevent later harnesses
    # from installing. Run each harness in its own errexit subshell, outside an
    # if/&&/|| condition (where bash would suppress errexit), and capture the
    # status only after restoring control to this loop.
    set +e
    (
      set -e
      "${t}_install"
      # A sourced caller may itself be in an if/|| context, where Bash ignores
      # errexit even in this subshell. Preserve an adapter's explicit refusal
      # rather than letting later successful pruning erase its return status.
      local adapter_rc=$?
      [[ $adapter_rc -eq 0 ]] || exit "$adapter_rc"
      if [[ -z "$ONLY" ]]; then
        prune_retired_asha_symlinks "$(asha_harness_home "$t")"
      fi
      # The installer OWNS Claude's settings.json .hooks: after the claude target
      # has mounted its symlinks, rebuild the asha hook set centrally so legacy
      # untagged duplicates are collapsed and the test canary is excluded.
      if [[ "$t" == "claude" ]]; then
        register_hooks
      fi
    )
    local rc=$?
    [[ $had_e -eq 1 ]] && set -e
    if [[ $rc -eq 0 ]]; then
      results+=("ok")
    else
      results+=("FAILED")
      failed+=("$t")
      _asha_failed_targets+=" $t"
      info "WARN: [$t] install failed (exit $rc); continuing with remaining targets"
    fi
  done

  # Cross-project identity layer (~/.asha/). Idempotent; never clobbers user data.
  bootstrap_identity

  # Record the repo root for wrapper-less launches (commands fall back to it).
  # Routing has its own failure boundary. An attempted adapter failure must
  # not be confused with an independently requested, unattempted --bin target.
  if _launcher_preflight "$BIN" && _write_asha_root; then
    if [[ -n "$BIN" ]]; then
      # Keep mklink/die and unguarded I/O failures in a child, not a condition
      # that suppresses Bash errexit throughout the launcher implementation.
      set +e
      ( set -e; install_bin "$BIN" )
      local launcher_rc=$?
      [[ $had_e -eq 1 ]] && set -e
      [[ $launcher_rc -eq 0 ]] || launcher_failed=1
    fi
  else
    launcher_failed=1
  fi

  _detect_legacy_learnings

  say ""
  say "install summary:"
  local i
  for ((i = 0; i < ${#targets[@]}; i++)); do
    say "  ${targets[$i]}: ${results[$i]}"
  done

  if [[ ${#failed[@]} -gt 0 || $launcher_failed -ne 0 ]]; then
    say "WARNING: install incomplete for: ${failed[*]} — re-run after fixing the errors above"
    return 1
  fi

  say ""
  say "done."
}
