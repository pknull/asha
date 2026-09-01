#!/usr/bin/env bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
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
# Symlinks skills / agents / commands into ~/.claude/*.
#
# Hook SCRIPTS are not symlinked — they stay in source. settings.json
# entries point at absolute source paths so each script's $(dirname "$0")
# resolves to its real directory.

CLAUDE_HOME="$(asha_harness_home claude)"
CLAUDE_SETTINGS_FILE="$(asha_harness_native_config claude)"

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
  local src_dir="$1" ns="$2" kind="${3:-plugin}"
  [[ -d "$src_dir" ]] || return 0
  validate_skill_source "$src_dir" "$kind"

  local skill
  while IFS= read -r skill; do
    [[ -n "$skill" && -d "$skill" ]] || continue
    local skill_name
    skill_name="$(basename "$skill")"
    [[ -f "$skill/SKILL.md" ]] || { log "skip skill (no SKILL.md): $skill"; continue; }
    local source="${skill%/}" dest_name="${ns}-${skill_name}"
    if [[ "$kind" == imported ]]; then
      prepare_imported_skill_adapter "$source" "$dest_name"
      mklink_imported_skill "$source" "$ASHA_IMPORTED_SKILL_ADAPTER" \
        "$CLAUDE_HOME/skills/$dest_name" "skill-dir"
      continue
    fi
    mklink "$source" "$CLAUDE_HOME/skills/$dest_name" "skill-dir"
  done < <(skill_dirs_from_source "$src_dir" "$kind")
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

# ---------------------------------------------------------------------------
# Entry point: claude_install
# ---------------------------------------------------------------------------

claude_install() {
  [[ -f "$CLAUDE_SETTINGS_FILE" ]] || die "claude settings.json not found: $CLAUDE_SETTINGS_FILE"

  ensure_dir "$CLAUDE_HOME/skills"
  ensure_dir "$CLAUDE_HOME/agents"
  ensure_dir "$CLAUDE_HOME/commands"

  say "[claude] target = $CLAUDE_HOME"

  local plugin_dir ns src_dir kind label
  while IFS=$'\t' read -r src_dir ns kind label; do
    [[ -n "$src_dir" ]] || continue
    say ""
    say "== [claude] $label skills  (ns=$ns) =="
    claude_install_skills "$src_dir" "$ns" "$kind"
  done < <(selected_imported_skill_sources)

  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || { echo "WARN: not a plugin dir: $plugin_dir" >&2; continue; }
    ns="$(ns_for "$plugin_dir")"
    say ""
    say "== [claude] $plugin_dir  (ns=$ns) =="
    claude_install_skills   "$PLUGINS_DIR/$plugin_dir/skills" "$ns" plugin
    claude_install_agents   "$plugin_dir" "$ns"
    claude_install_commands "$plugin_dir" "$ns"
  done < <(selected_plugins)
}

# ---------------------------------------------------------------------------
# Entry point: claude_uninstall
# ---------------------------------------------------------------------------

# Used by ../uninstall.sh. Removes symlinks under ~/.claude/{skills,agents,
# commands} whose realpath is inside $ABS_MARKET_ROOT, prunes
# empty namespace dirs, strips settings.json hook entries tagged asha:* (and
# legacy marketplace:* for migration cleanup).
claude_uninstall() {
  # Missing settings.json is a benign state (claude harness never installed,
  # or already cleaned): still sweep the symlink mounts below, skip only the
  # hook strip. die()-ing here stranded codex/copilot under --target all
  # (issue #4 review). A PRESENT-but-corrupt settings.json is a real failure:
  # fail this harness loudly rather than silently skipping the hook strip.
  local have_settings=0
  if [[ -f "$CLAUDE_SETTINGS_FILE" ]]; then
    jq empty "$CLAUDE_SETTINGS_FILE" >/dev/null 2>&1 \
      || die "$CLAUDE_SETTINGS_FILE is not valid JSON — fix it, then re-run" 4
    have_settings=1
  else
    say "[claude] $CLAUDE_SETTINGS_FILE not found; sweeping symlinks only"
  fi
  say "[claude] target = $CLAUDE_HOME"

  local total=0 n
  # output-styles is cleanup-only: current installs never create it, but the
  # previous release did. Keep sweeping that legacy root so uninstall and
  # upgrades retire Asha-owned links even when the root itself is symlinked.
  for spec in "skills 1" "agents 2" "commands 2" "output-styles 1"; do
    local subdir depth
    read -r subdir depth <<< "$spec"
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
          # Best-effort: a failed rmdir must not abort the uninstall (set -e)
          # before the hook strip below runs (issue #4 class).
          rmdir "$sub" 2>/dev/null && log "rmdir: $sub" || true
        fi
      fi
    done
  done

  # Strip settings.json hook entries that belong to asha. Claude Code drops
  # the non-standard "source" key when it re-serializes settings.json, so the
  # tag alone is NOT durable — live hooks are usually untagged. Match by
  # command path-prefix FIRST (mirrors register_hooks() in lib/install.sh),
  # keeping the asha:*/marketplace:* tag as fallback (issue #4). strip_group
  # guards a missing .hooks key with (.hooks // []) exactly as register_hooks'
  # strip_asha_hooks does — a matcher-only group must not error the filter.
  if [[ $have_settings -eq 1 ]]; then
    : "${ABS_MARKET_ROOT:=$(resolve_path "$MARKET_ROOT")}"
    local prefix="$ABS_MARKET_ROOT/plugins/"
    local tag_regex='^(asha|marketplace):'
    # jq variables must remain protected from shell expansion.
    # shellcheck disable=SC2016
    local count_expr='[.hooks // {} | .[] | .[]? | .hooks[]?
      | select(((.command // "") | startswith($prefix)) or ((.source // "") | test($re)))] | length'
    local before after removed
    before="$(jq -r --arg prefix "$prefix" --arg re "$tag_regex" "$count_expr" "$CLAUDE_SETTINGS_FILE")"
    if [[ "$before" -gt 0 ]]; then
      if [[ $DRY_RUN -eq 1 ]]; then
        say "[claude] would remove $before asha hook entr$([[ $before -eq 1 ]] && echo y || echo ies) from settings.json"
      else
        claude_backup_settings_once
        # jq variables must remain protected from shell expansion.
        # shellcheck disable=SC2016
        claude_settings_update '
          def is_asha_hook:
            ((.command // "") | startswith($prefix))
            or ((.source // "") | test($re));
          def strip_group:
            ((.hooks // []) | map(select(is_asha_hook | not))) as $kept
            | if ($kept | length) > 0 then [ (.hooks = $kept) ] else [] end;
          if .hooks then
            .hooks |= with_entries(.value |= (map(strip_group) | add // []))
            | .hooks |= with_entries(select(.value | length > 0))
          else . end
        ' --arg prefix "$prefix" --arg re "$tag_regex"
        after="$(jq -r --arg prefix "$prefix" --arg re "$tag_regex" "$count_expr" "$CLAUDE_SETTINGS_FILE")"
        removed=$((before - after))
        say "[claude] removed $removed asha hook entr$([[ $removed -eq 1 ]] && echo y || echo ies) from settings.json"
      fi
    else
      log "[claude] no asha hooks in settings.json"
    fi
  fi

  # Read indirectly by lib/uninstall.sh after this sourced function returns.
  # shellcheck disable=SC2034
  CLAUDE_UNINSTALL_TOTAL=$total
}
