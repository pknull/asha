#!/usr/bin/env bash
# install.sh — symlink-mount installer for the asha repo.
#
# Symlinks skills / agents / commands / output-styles from plugins/<ns>/…
# into the native ~/.claude/* scan directories, and merges per-plugin
# hooks.json entries into ~/.claude/settings.json, tagged with
# "source": "asha:<ns>" for reversible removal by uninstall.sh.
#
# Hook SCRIPTS are not symlinked — they stay in source. settings.json
# entries point at absolute source paths so each script's internal
# $(dirname "$0") resolves to its real directory (preserves source
# sibling lookups like common.sh).
#
# Usage:
#   ./install.sh [--dry-run] [--only ns1,ns2,...] [--force] [--verbose]
#
# --dry-run  : print the action plan only; no filesystem or JSON writes.
# --only     : comma-separated plugin directory names (e.g. "devops,prompt").
# --force    : overwrite an existing destination symlink that points elsewhere.
# --verbose  : echo each action.
#
# Exit codes:
#   0  success (or dry-run completed)
#   1  usage error
#   2  conflict: destination exists and --force not given
#   3  dependency missing (jq)
#   4  merge failure (settings.json edit)

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve script location (handles invocation via symlink)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
MARKET_ROOT="$SCRIPT_DIR"
PLUGINS_DIR="$MARKET_ROOT/plugins"
NAMESPACES_FILE="$MARKET_ROOT/namespaces.json"

CLAUDE_HOME="$HOME/.claude"
SETTINGS_FILE="$CLAUDE_HOME/settings.json"

DRY_RUN=0
FORCE=0
VERBOSE=0
ONLY=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() { echo "ERROR: $*" >&2; exit "${2:-1}"; }
log() { [[ $VERBOSE -eq 1 ]] && echo "  $*"; return 0; }
say() { echo "$*"; }

require_jq() {
  command -v jq >/dev/null 2>&1 || die "jq not found in PATH" 3
}

# Ensure a directory exists (dry-run safe).
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
#
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

# Atomic-write jq edit to ~/.claude/settings.json.
# First positional arg = jq expression; remaining args forwarded to jq
# (e.g. --argjson add "$tagged_json"). Write-to-temp-then-rename.
settings_update() {
  local jq_expr="$1"
  shift
  local tmp="$SETTINGS_FILE.tmp.$$"

  if [[ $DRY_RUN -eq 1 ]]; then
    log "would apply jq filter to $SETTINGS_FILE"
    return 0
  fi

  jq "$@" "$jq_expr" "$SETTINGS_FILE" > "$tmp" || { rm -f "$tmp"; die "jq filter failed" 4; }

  jq empty "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "resulting settings.json invalid" 4; }

  mv "$tmp" "$SETTINGS_FILE"
}

# Back up settings.json once per run if we're about to mutate it.
_backup_done=0
backup_settings_once() {
  if [[ $DRY_RUN -eq 1 ]]; then return 0; fi
  if [[ $_backup_done -eq 1 ]]; then return 0; fi
  local stamp
  stamp="$(date +%Y%m%d-%H%M%S)"
  local bkp="$SETTINGS_FILE.bak-$stamp"
  cp -p "$SETTINGS_FILE" "$bkp"
  say "backed up settings.json -> $bkp"
  _backup_done=1
}

# ---------------------------------------------------------------------------
# Per-primitive installers
# ---------------------------------------------------------------------------

install_skills() {
  local plugin_dir="$1" ns="$2"
  local src_dir="$PLUGINS_DIR/$plugin_dir/skills"
  [[ -d "$src_dir" ]] || return 0

  local skill
  for skill in "$src_dir"/*/; do
    [[ -d "$skill" ]] || continue
    local skill_name
    skill_name="$(basename "$skill")"
    [[ -f "$skill/SKILL.md" ]] || { log "skip skill (no SKILL.md): $skill"; continue; }
    mklink "${skill%/}" "$CLAUDE_HOME/skills/${ns}-${skill_name}" "skill-dir"
  done
}

install_agents() {
  local plugin_dir="$1" ns="$2"
  local src_dir="$PLUGINS_DIR/$plugin_dir/agents"
  [[ -d "$src_dir" ]] || return 0

  # Skip creating a per-plugin subdir when there's nothing to install. An empty
  # subdir would just pollute the scan path (and any parent that mirrors it).
  local agent has=0
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] && { has=1; break; }
  done
  [[ $has -eq 1 ]] || return 0

  # Per-plugin subdirectory keeps asha-sourced agents isolated from the
  # user's flat-scan collection (relevant when ~/.claude/agents is itself
  # a symlink into a tracked dotfiles repo).
  ensure_dir "$CLAUDE_HOME/agents/${ns}"
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] || continue
    local agent_name
    agent_name="$(basename "$agent")"
    mklink "$agent" "$CLAUDE_HOME/agents/${ns}/${agent_name}" "agent"
  done
}

install_commands() {
  local plugin_dir="$1" ns="$2"
  local src_dir="$PLUGINS_DIR/$plugin_dir/commands"
  [[ -d "$src_dir" ]] || return 0

  ensure_dir "$CLAUDE_HOME/commands/${ns}"
  local cmd
  for cmd in "$src_dir"/*.md; do
    [[ -f "$cmd" ]] || continue
    local cmd_name
    cmd_name="$(basename "$cmd")"
    mklink "$cmd" "$CLAUDE_HOME/commands/${ns}/${cmd_name}" "command"
  done
}

install_styles() {
  local plugin_dir="$1" ns="$2"
  local src_dir="$PLUGINS_DIR/$plugin_dir/styles"
  [[ -d "$src_dir" ]] || return 0

  local style
  for style in "$src_dir"/*.md; do
    [[ -f "$style" ]] || continue
    local style_name
    style_name="$(basename "$style")"
    mklink "$style" "$CLAUDE_HOME/output-styles/${ns}-${style_name}" "output-style"
  done
}

# Merge hooks.json into settings.json, tagged with "source": "asha:<ns>".
# Rewrites ${CLAUDE_PLUGIN_ROOT} -> absolute plugin path so commands resolve.
# Idempotent: first removes any existing entries tagged asha:<ns>, then
# re-adds from the plugin manifest.
install_hooks() {
  local plugin_dir="$1" ns="$2"
  local plugin_root="$PLUGINS_DIR/$plugin_dir"
  local abs_root
  abs_root="$(readlink -f "$plugin_root")"
  local hooks_json
  # Support both ./hooks/hooks.json and ./hooks.json just in case.
  if   [[ -f "$plugin_root/hooks/hooks.json" ]]; then hooks_json="$plugin_root/hooks/hooks.json"
  elif [[ -f "$plugin_root/hooks.json"      ]]; then hooks_json="$plugin_root/hooks.json"
  else return 0
  fi

  # Skip empty / no-op manifests.
  local lifecycles_count
  lifecycles_count="$(jq -r '.hooks // {} | length' "$hooks_json")"
  [[ "$lifecycles_count" -gt 0 ]] || { log "hooks.json empty for $plugin_dir"; return 0; }

  backup_settings_once

  local source_tag="asha:$ns"

  # Step 1: remove any pre-existing entries with our source tag (idempotent).
  # We walk .hooks.<lifecycle>[].hooks[] and filter out those with .source == tag.
  # Also purge matcher-groups that end up with an empty .hooks array.
  settings_update '
    if .hooks then
      .hooks |= with_entries(
        .value |= (
          map(
            .hooks |= map(select((.source // "") != "'"$source_tag"'"))
          )
          | map(select(.hooks | length > 0))
        )
      )
      | .hooks |= with_entries(select(.value | length > 0))
    else . end
  '

  # Step 2: build tagged hook entries from the plugin manifest.
  # Rewrite ${CLAUDE_PLUGIN_ROOT} -> abs_root inside each command string.
  # Tag each inner hook entry with source=asha:<ns>.
  local tagged
  tagged="$(jq \
    --arg root "$abs_root" \
    --arg tag  "$source_tag" '
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

  # Step 3: merge tagged entries into settings.json. For each lifecycle, append
  # the plugin's matcher-groups to the existing array, creating keys as needed.
  settings_update '
      .hooks = (.hooks // {})
      | reduce ($add | to_entries[]) as $e (
          .;
          .hooks[$e.key] = ((.hooks[$e.key] // []) + $e.value)
        )
    ' \
    --argjson add "$tagged"

  local n
  n="$(jq -r --arg tag "$source_tag" '
      [.hooks // {} | .[] | .[]? | .hooks[]? | select(.source == $tag)] | length
    ' "$SETTINGS_FILE")"
  log "registered $n hook entr$([[ $n -eq 1 ]] && echo y || echo ies) for $ns"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
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
      -h|--help) usage ;;
      *)         die "unknown argument: $1" 1 ;;
    esac
    shift
  done
}

selected_plugins() {
  if [[ -n "$ONLY" ]]; then
    IFS=',' read -ra arr <<<"$ONLY"
    printf '%s\n' "${arr[@]}"
  else
    find "$PLUGINS_DIR" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | sort
  fi
}

main() {
  parse_args "$@"
  require_jq

  [[ -d "$PLUGINS_DIR" ]]    || die "plugins dir not found: $PLUGINS_DIR"
  [[ -f "$NAMESPACES_FILE" ]] || die "namespaces.json not found: $NAMESPACES_FILE"
  [[ -f "$SETTINGS_FILE" ]]   || die "claude settings.json not found: $SETTINGS_FILE"

  ensure_dir "$CLAUDE_HOME/skills"
  ensure_dir "$CLAUDE_HOME/agents"
  ensure_dir "$CLAUDE_HOME/commands"
  ensure_dir "$CLAUDE_HOME/output-styles"

  say "install.sh: asha root = $MARKET_ROOT"
  [[ $DRY_RUN -eq 1 ]] && say "   (dry-run: no filesystem or settings.json changes)"
  [[ $FORCE   -eq 1 ]] && say "   (force: will replace mismatched symlinks)"
  [[ -n "$ONLY"     ]] && say "   (only: $ONLY)"

  local plugin_dir ns
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || { echo "WARN: not a plugin dir: $plugin_dir" >&2; continue; }
    ns="$(ns_for "$plugin_dir")"
    say ""
    say "== $plugin_dir  (ns=$ns) =="
    install_skills   "$plugin_dir" "$ns"
    install_agents   "$plugin_dir" "$ns"
    install_commands "$plugin_dir" "$ns"
    install_styles   "$plugin_dir" "$ns"
    install_hooks    "$plugin_dir" "$ns"
  done < <(selected_plugins)

  say ""
  say "done."
}

main "$@"
