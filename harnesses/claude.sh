#!/usr/bin/env bash
# harnesses/claude.sh — Claude Code install/uninstall logic.
#
# Sourced by ../install.sh and ../uninstall.sh. Expects these globals to be
# set by the caller:
#   MARKET_ROOT      — repo root (absolute, readlink-resolved)
#   PLUGINS_DIR      — $MARKET_ROOT/plugins
#   NAMESPACES_FILE  — $MARKET_ROOT/namespaces.json
#   DRY_RUN, FORCE, VERBOSE, ONLY  — flag state from CLI
#
# And these helpers (defined in the dispatcher):
#   die, log, say, ensure_dir, mklink, ns_for, selected_plugins
#
# Symlinks skills / agents / commands / output-styles into ~/.claude/* and
# merges per-plugin hooks.json entries into ~/.claude/settings.json,
# tagged with "source": "asha:<ns>" for reversible removal.
#
# Hook SCRIPTS are not symlinked — they stay in source. settings.json
# entries point at absolute source paths so each script's $(dirname "$0")
# resolves to its real directory.

CLAUDE_HOME="$HOME/.claude"
CLAUDE_SETTINGS_FILE="$CLAUDE_HOME/settings.json"

# ---------------------------------------------------------------------------
# Claude-specific helpers
# ---------------------------------------------------------------------------

# Atomic-write jq edit to ~/.claude/settings.json. First arg = jq expression;
# remaining args forwarded to jq (e.g. --argjson add "$tagged_json").
claude_settings_update() {
  local jq_expr="$1"
  shift
  local tmp="$CLAUDE_SETTINGS_FILE.tmp.$$"

  if [[ $DRY_RUN -eq 1 ]]; then
    log "would apply jq filter to $CLAUDE_SETTINGS_FILE"
    return 0
  fi

  jq "$@" "$jq_expr" "$CLAUDE_SETTINGS_FILE" > "$tmp" || { rm -f "$tmp"; die "jq filter failed" 4; }
  jq empty "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "resulting settings.json invalid" 4; }
  mv "$tmp" "$CLAUDE_SETTINGS_FILE"
}

# Back up settings.json once per run if we're about to mutate it.
_claude_backup_done=0
claude_backup_settings_once() {
  [[ $DRY_RUN -eq 1 ]] && return 0
  [[ $_claude_backup_done -eq 1 ]] && return 0
  local stamp
  stamp="$(date +%Y%m%d-%H%M%S)"
  local bkp="$CLAUDE_SETTINGS_FILE.bak-$stamp"
  cp -p "$CLAUDE_SETTINGS_FILE" "$bkp"
  say "backed up settings.json -> $bkp"
  _claude_backup_done=1
}

# ---------------------------------------------------------------------------
# Per-primitive installers (Claude)
# ---------------------------------------------------------------------------

claude_install_skills() {
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

claude_install_agents() {
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

  ensure_dir "$CLAUDE_HOME/agents/${ns}"
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] || continue
    local agent_name
    agent_name="$(basename "$agent")"
    mklink "$agent" "$CLAUDE_HOME/agents/${ns}/${agent_name}" "agent"
  done
}

claude_install_commands() {
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

claude_install_styles() {
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
# Idempotent: removes any existing entries tagged asha:<ns>, then re-adds.
claude_install_hooks() {
  local plugin_dir="$1" ns="$2"
  local plugin_root="$PLUGINS_DIR/$plugin_dir"
  local abs_root
  abs_root="$(resolve_path "$plugin_root")"
  local hooks_json
  if   [[ -f "$plugin_root/hooks/hooks.json" ]]; then hooks_json="$plugin_root/hooks/hooks.json"
  elif [[ -f "$plugin_root/hooks.json"      ]]; then hooks_json="$plugin_root/hooks.json"
  else return 0
  fi

  local lifecycles_count
  lifecycles_count="$(jq -r '.hooks // {} | length' "$hooks_json")"
  [[ "$lifecycles_count" -gt 0 ]] || { log "hooks.json empty for $plugin_dir"; return 0; }

  claude_backup_settings_once

  local source_tag="asha:$ns"

  # Step 1: remove pre-existing entries with our source tag (idempotent).
  claude_settings_update '
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

  # Step 3: merge tagged entries into settings.json.
  claude_settings_update '
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
    ' "$CLAUDE_SETTINGS_FILE")"
  log "registered $n hook entr$([[ $n -eq 1 ]] && echo y || echo ies) for $ns"
}

# ---------------------------------------------------------------------------
# Entry point: claude_install
# ---------------------------------------------------------------------------

claude_install() {
  [[ -f "$CLAUDE_SETTINGS_FILE" ]] || die "claude settings.json not found: $CLAUDE_SETTINGS_FILE"

  ensure_dir "$CLAUDE_HOME/skills"
  ensure_dir "$CLAUDE_HOME/agents"
  ensure_dir "$CLAUDE_HOME/commands"
  ensure_dir "$CLAUDE_HOME/output-styles"

  say "[claude] target = $CLAUDE_HOME"

  local plugin_dir ns
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || { echo "WARN: not a plugin dir: $plugin_dir" >&2; continue; }
    ns="$(ns_for "$plugin_dir")"
    say ""
    say "== [claude] $plugin_dir  (ns=$ns) =="
    claude_install_skills   "$plugin_dir" "$ns"
    claude_install_agents   "$plugin_dir" "$ns"
    claude_install_commands "$plugin_dir" "$ns"
    claude_install_styles   "$plugin_dir" "$ns"
    # Hook registration is NOT done per-plugin here anymore: lib/install.sh's
    # register_hooks() is the single authority for settings.json .hooks (it
    # collapses legacy untagged duplicates that this per-plugin tagged merge
    # could not, and excludes the test canary). claude_install_hooks is kept
    # defined for back-compat / standalone use but is no longer called.
  done < <(selected_plugins)
}

# ---------------------------------------------------------------------------
# Entry point: claude_uninstall
# ---------------------------------------------------------------------------

# Used by ../uninstall.sh. Removes symlinks under ~/.claude/{skills,agents,
# commands,output-styles} whose realpath is inside $ABS_MARKET_ROOT, prunes
# empty namespace dirs, strips settings.json hook entries tagged asha:* (and
# legacy marketplace:* for migration cleanup).
claude_uninstall() {
  [[ -f "$CLAUDE_SETTINGS_FILE" ]] || die "$CLAUDE_SETTINGS_FILE not found"
  say "[claude] target = $CLAUDE_HOME"

  local total=0 n
  for spec in "skills 1" "agents 2" "output-styles 1" "commands 2"; do
    set -- $spec
    local subdir="$1" depth="$2"
    n="$(remove_symlinks_under "$CLAUDE_HOME/$subdir" "$depth")"
    [[ "$n" -gt 0 ]] && say "[claude] removed $n symlink(s) from $CLAUDE_HOME/$subdir"
    total=$((total + n))
  done

  # Prune now-empty namespace dirs under commands/ and agents/.
  local parent
  for parent in "$CLAUDE_HOME/commands" "$CLAUDE_HOME/agents"; do
    [[ -d "$parent" ]] || continue
    local sub
    for sub in "$parent"/*/; do
      [[ -d "$sub" ]] || continue
      [[ -L "${sub%/}" ]] && continue
      if [[ -z "$(ls -A "$sub")" ]]; then
        if [[ $DRY_RUN -eq 1 ]]; then
          info "  RMDIR  $sub"
        else
          rmdir "$sub"
          log "rmdir: $sub"
        fi
      fi
    done
  done

  # Strip settings.json hook entries tagged asha:* (or legacy marketplace:*).
  local tag_regex='^(asha|marketplace):'
  local before after removed
  before="$(jq -r --arg re "$tag_regex" '[.hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | test($re))] | length' "$CLAUDE_SETTINGS_FILE")"
  if [[ "$before" -gt 0 ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[claude] would remove $before tagged hook entr$([[ $before -eq 1 ]] && echo y || echo ies) from settings.json"
    else
      claude_backup_settings_once
      claude_settings_update "
        if .hooks then
          .hooks |= with_entries(
            .value |= (
              map(
                .hooks |= map(select(((.source // \"\") | test(\"$tag_regex\")) | not))
              )
              | map(select(.hooks | length > 0))
            )
          )
          | .hooks |= with_entries(select(.value | length > 0))
        else . end
      "
      after="$(jq -r --arg re "$tag_regex" '[.hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | test($re))] | length' "$CLAUDE_SETTINGS_FILE")"
      removed=$((before - after))
      say "[claude] removed $removed tagged hook entr$([[ $removed -eq 1 ]] && echo y || echo ies) from settings.json"
    fi
  else
    log "[claude] no asha-tagged hooks in settings.json"
  fi

  CLAUDE_UNINSTALL_TOTAL=$total
}
