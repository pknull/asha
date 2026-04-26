#!/usr/bin/env bash
# harnesses/codex.sh — OpenAI Codex CLI install/uninstall logic.
#
# Sourced by ../install.sh and ../uninstall.sh. Expects globals from the
# dispatcher: MARKET_ROOT, PLUGINS_DIR, NAMESPACES_FILE, DRY_RUN, FORCE,
# VERBOSE, ONLY, ABS_MARKET_ROOT (uninstall only).
#
# Install layout under ~/.codex/:
#   skills/<ns>-<skill>/      → symlink to plugins/<ns>/skills/<skill>/
#   prompts/<ns>-<cmd>.md     → symlink to plugins/<ns>/commands/<cmd>.md
#                               (Codex prompts dir is FLAT — no subdirs)
#   config.toml               → existing user config + appended fenced block
#                               of [[hooks.X]] arrays, tagged "# asha:<ns>"
#
# Hook scripts are not symlinked; absolute paths in config.toml.
#
# Plugins skipped entirely (Claude-only): output-styles
# Hook events Codex doesn't support: SessionEnd, Setup (warned & dropped)

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_CONFIG_FILE="$CODEX_HOME/config.toml"
CODEX_SKILLS_DIR="$CODEX_HOME/skills"
CODEX_PROMPTS_DIR="$CODEX_HOME/prompts"

# Overlay (persona) layout — separate CODEX_HOME, used by asha-codex wrapper.
# Skills/prompts/agents are symlinked back to the main CODEX_HOME so the
# overlay inherits them without duplication. config.toml and instructions.md
# are generated copies (drift-checked).
CODEX_OVERLAY_HOME="$HOME/.codex-asha"
CODEX_OVERLAY_CONFIG="$CODEX_OVERLAY_HOME/config.toml"
CODEX_OVERLAY_INSTRUCTIONS="$CODEX_OVERLAY_HOME/instructions.md"
CODEX_OVERLAY_INHERIT_DIRS=(skills prompts agents)

# Events Codex 0.125+ supports.
_CODEX_EVENTS=(SessionStart PreToolUse PostToolUse Stop UserPromptSubmit PermissionRequest)
_CODEX_SKIP_PLUGINS=(output-styles)

CODEX_HOOK_FENCE_START="# ===== asha:start (managed by asha installer; do not edit) ====="
CODEX_HOOK_FENCE_END="# ===== asha:end ====="

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_codex_is_event() {
  local e="$1" ev
  for ev in "${_CODEX_EVENTS[@]}"; do [[ "$e" == "$ev" ]] && return 0; done
  return 1
}

_codex_is_skip_plugin() {
  local p="$1" sp
  for sp in "${_CODEX_SKIP_PLUGINS[@]}"; do [[ "$p" == "$sp" ]] && return 0; done
  return 1
}

# Atomic write to config.toml, validated by tomllib re-parse.
_codex_atomic_write_config() {
  local content="$1"
  local tmp="$CODEX_CONFIG_FILE.tmp.$$"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "would write $CODEX_CONFIG_FILE ($(printf '%s' "$content" | wc -c) bytes)"
    return 0
  fi
  printf '%s' "$content" > "$tmp"
  python3 -c "import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))" "$tmp" \
    || { rm -f "$tmp"; die "config.toml would be invalid TOML after write" 4; }
  mv "$tmp" "$CODEX_CONFIG_FILE"
}

# Back up config.toml once per run if we're about to mutate it.
_codex_backup_done=0
_codex_backup_config_once() {
  [[ $DRY_RUN -eq 1 ]] && return 0
  [[ $_codex_backup_done -eq 1 ]] && return 0
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local bkp="$CODEX_CONFIG_FILE.bak-$stamp"
  cp -p "$CODEX_CONFIG_FILE" "$bkp"
  say "backed up config.toml -> $bkp"
  _codex_backup_done=1
}

# ---------------------------------------------------------------------------
# Per-primitive installers
# ---------------------------------------------------------------------------

codex_install_skills() {
  local plugin_dir="$1" ns="$2"
  _codex_is_skip_plugin "$plugin_dir" && return 0
  local src_dir="$PLUGINS_DIR/$plugin_dir/skills"
  [[ -d "$src_dir" ]] || return 0

  local skill
  for skill in "$src_dir"/*/; do
    [[ -d "$skill" ]] || continue
    local skill_name; skill_name="$(basename "$skill")"
    [[ -f "$skill/SKILL.md" ]] || { log "skip skill (no SKILL.md): $skill"; continue; }
    mklink "${skill%/}" "$CODEX_SKILLS_DIR/${ns}-${skill_name}" "codex-skill"
  done
}

# Install agent markdown into ~/.codex/agents/. Codex 0.125 multi-agent
# uses a different YAML schema, but the .md files are harmless — if Codex
# discovers them, great; if it ignores them, the symlinks cost nothing.
# This is a best-effort port pending verified Codex multi-agent docs.
codex_install_agents() {
  local plugin_dir="$1" ns="$2"
  _codex_is_skip_plugin "$plugin_dir" && return 0
  local src_dir="$PLUGINS_DIR/$plugin_dir/agents"
  [[ -d "$src_dir" ]] || return 0

  local agent has=0
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] && { has=1; break; }
  done
  [[ $has -eq 1 ]] || return 0

  ensure_dir "$CODEX_HOME/agents"
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] || continue
    local agent_name; agent_name="$(basename "$agent")"
    mklink "$agent" "$CODEX_HOME/agents/${ns}-${agent_name}" "codex-agent"
  done
}

codex_install_prompts() {
  local plugin_dir="$1" ns="$2"
  _codex_is_skip_plugin "$plugin_dir" && return 0
  local src_dir="$PLUGINS_DIR/$plugin_dir/commands"
  [[ -d "$src_dir" ]] || return 0

  # Codex prompts dir is FLAT — no subdirs allowed. Flatten to <ns>-<cmd>.md.
  local cmd
  for cmd in "$src_dir"/*.md; do
    [[ -f "$cmd" ]] || continue
    local cmd_name; cmd_name="$(basename "$cmd" .md)"
    mklink "$cmd" "$CODEX_PROMPTS_DIR/${ns}-${cmd_name}.md" "codex-prompt"
  done
}

# Excise any existing fenced asha block. Whether or not we replace it depends
# on the caller; on dry-run we only report.
_codex_excise_fence() {
  [[ -f "$CODEX_CONFIG_FILE" ]] || return 0
  awk -v s="$CODEX_HOOK_FENCE_START" -v e="$CODEX_HOOK_FENCE_END" '
    BEGIN { skip = 0 }
    $0 == s { skip = 1; next }
    $0 == e { skip = 0; next }
    skip == 0 { print }
  ' "$CODEX_CONFIG_FILE"
}

# Convert a plugin's hooks.json into TOML [[hooks.X]] blocks.
# Args: plugin_root_abs, hooks_json_path, ns, plugins_to_skip_csv
# Output: TOML text on stdout. Drops events Codex doesn't support, with a
# stderr warning. Drops the entire plugin if it's in the skip list.
_codex_emit_hooks_for_plugin() {
  local abs_root="$1" hooks_json="$2" ns="$3"

  # Use python for the JSON→TOML transform — bash + jq is fragile here
  # (we need to escape strings into TOML-quoted form correctly).
  PYTHONIOENCODING=utf-8 python3 - "$abs_root" "$hooks_json" "$ns" <<'PYEOF'
import json, sys, re

abs_root, hooks_json, ns = sys.argv[1], sys.argv[2], sys.argv[3]

CODEX_EVENTS = {"SessionStart","PreToolUse","PostToolUse","Stop","UserPromptSubmit","PermissionRequest"}

def toml_str(s: str) -> str:
    # Use double-quoted basic TOML strings; escape backslashes, quotes, control chars.
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\b","\\b").replace("\t","\\t").replace("\n","\\n").replace("\f","\\f").replace("\r","\\r")
    return '"' + s + '"'

def resolve_command(cmd: str) -> str:
    return cmd.replace("${CLAUDE_PLUGIN_ROOT}", abs_root)

with open(hooks_json) as f:
    data = json.load(f)

events = (data or {}).get("hooks") or {}
out = []
dropped_events = []
for event, groups in events.items():
    if event not in CODEX_EVENTS:
        dropped_events.append(event)
        continue
    if not isinstance(groups, list):
        continue
    for grp in groups:
        matcher = grp.get("matcher")
        # Claude's "*" means match-all; Codex idiom is to omit matcher
        # (or use ".*"). Omit for cleanliness.
        if matcher == "*":
            matcher = None
        for h in grp.get("hooks", []):
            if h.get("type") != "command":
                continue
            cmd = resolve_command(h.get("command",""))
            if not cmd:
                continue
            out.append(f"[[hooks.{event}]]")
            if matcher:
                out.append(f"matcher = {toml_str(matcher)}")
            out.append(f'type = "command"')
            out.append(f"command = {toml_str(cmd)}")
            timeout = h.get("timeout")
            if isinstance(timeout, int):
                out.append(f"timeout = {timeout}")
            out.append(f"# asha:{ns}")
            out.append("")

for e in dropped_events:
    sys.stderr.write(f"  WARN: dropped {ns}/{e} (Codex does not support this event)\n")

sys.stdout.write("\n".join(out))
PYEOF
}

# Build the entire fenced block from all selected plugins' hooks.
# Echoes TOML to stdout. Empty output (no plugins have portable hooks) → caller
# excises the fence and writes nothing new.
_codex_build_hook_block() {
  local plugin_dir ns plugin_root abs_root hooks_json count=0
  local emitted="$CODEX_HOOK_FENCE_START"$'\n'

  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || continue
    _codex_is_skip_plugin "$plugin_dir" && { log "[codex] skip hooks: $plugin_dir (Claude-only)"; continue; }

    plugin_root="$PLUGINS_DIR/$plugin_dir"
    abs_root="$(readlink -f "$plugin_root")"
    if   [[ -f "$plugin_root/hooks/hooks.json" ]]; then hooks_json="$plugin_root/hooks/hooks.json"
    elif [[ -f "$plugin_root/hooks.json"      ]]; then hooks_json="$plugin_root/hooks.json"
    else continue
    fi

    local lifecycles_count
    lifecycles_count="$(jq -r '.hooks // {} | length' "$hooks_json")"
    [[ "$lifecycles_count" -gt 0 ]] || continue

    ns="$(ns_for "$plugin_dir")"
    local plugin_emit
    plugin_emit="$(_codex_emit_hooks_for_plugin "$abs_root" "$hooks_json" "$ns")"
    [[ -z "$plugin_emit" ]] && continue
    emitted+="$plugin_emit"$'\n'
    count=$((count+1))
  done < <(selected_plugins)

  emitted+="$CODEX_HOOK_FENCE_END"$'\n'

  if [[ $count -eq 0 ]]; then
    return 1   # nothing to emit
  fi
  printf '%s' "$emitted"
}

codex_install_hooks() {
  [[ -f "$CODEX_CONFIG_FILE" ]] || die "Codex config.toml not found: $CODEX_CONFIG_FILE"

  local existing_no_fence
  existing_no_fence="$(_codex_excise_fence)"

  local block status
  block="$(_codex_build_hook_block)" && status=0 || status=$?

  # Compose new file: existing-without-fence + (block | nothing) + trailing newline
  local new_content="$existing_no_fence"
  # Ensure trailing newline before append.
  if [[ -n "$new_content" && "${new_content: -1}" != $'\n' ]]; then
    new_content+=$'\n'
  fi
  if [[ $status -eq 0 && -n "$block" ]]; then
    new_content+=$'\n'"$block"
  fi

  # Skip write if content is byte-identical to current file (idempotent no-op).
  if [[ -f "$CODEX_CONFIG_FILE" ]]; then
    local current
    current="$(cat "$CODEX_CONFIG_FILE")"
    if [[ "$current" == "${new_content%$'\n'}" || "$current" == "$new_content" ]]; then
      log "[codex] config.toml hook block unchanged"
      return 0
    fi
  fi

  _codex_backup_config_once
  _codex_atomic_write_config "$new_content"

  # Tally registered hooks (count blocks with our tag comment).
  local n
  n="$(grep -c '^# asha:' "$CODEX_CONFIG_FILE" 2>/dev/null || true)"
  n="${n:-0}"
  log "[codex] registered $n hook entr$([[ $n -eq 1 ]] && echo y || echo ies)"
}

# ---------------------------------------------------------------------------
# Persona overlay (~/.codex-asha/) — toggled by bin/asha-codex wrapper
# ---------------------------------------------------------------------------

# Symlink the inherit dirs so the overlay sees the main CODEX_HOME's
# skills/prompts/agents without duplication.
_codex_overlay_inherit_links() {
  local sub
  for sub in "${CODEX_OVERLAY_INHERIT_DIRS[@]}"; do
    local target="$CODEX_HOME/$sub"
    local link="$CODEX_OVERLAY_HOME/$sub"
    # Ensure target dir exists in main home (codex_install may not have
    # created agents/ yet since v1 doesn't write there).
    ensure_dir "$target"
    mklink "$target" "$link" "overlay-inherit"
  done
  ensure_dir "$CODEX_OVERLAY_HOME/sessions"   # separate session history
}

# Regenerate ~/.codex-asha/config.toml = user's ~/.codex/config.toml +
# model_instructions_file pointing at the merged identity.
# Idempotent: only writes when content differs.
_codex_overlay_write_config() {
  [[ -f "$CODEX_CONFIG_FILE" ]] || die "main Codex config.toml not found: $CODEX_CONFIG_FILE"

  # Strip any existing top-level model_instructions_file from the source so we
  # control its value. Only matches lines BEFORE the first [section] header
  # to avoid touching keys inside tables.
  local merged
  merged="$(awk '
    BEGIN { in_section = 0 }
    /^[ \t]*\[/ { in_section = 1 }
    in_section == 0 && /^[ \t]*model_instructions_file[ \t]*=/ { next }
    { print }
  ' "$CODEX_CONFIG_FILE")"

  # Prepend our line. (Top-level keys must come before any [section].)
  local new_content
  new_content="$(printf '%s\n%s\n' \
    "model_instructions_file = \"$CODEX_OVERLAY_INSTRUCTIONS\"" \
    "$merged")"

  # Idempotent: skip write if existing matches.
  if [[ -f "$CODEX_OVERLAY_CONFIG" ]]; then
    local current; current="$(cat "$CODEX_OVERLAY_CONFIG")"
    if [[ "$current" == "$new_content" ]]; then
      log "[codex] overlay config.toml unchanged"
      return 0
    fi
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    log "would write $CODEX_OVERLAY_CONFIG"
    return 0
  fi

  local tmp="$CODEX_OVERLAY_CONFIG.tmp.$$"
  printf '%s' "$new_content" > "$tmp"
  python3 -c "import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))" "$tmp" \
    || { rm -f "$tmp"; die "overlay config.toml would be invalid TOML" 4; }
  mv "$tmp" "$CODEX_OVERLAY_CONFIG"
  say "[codex] wrote overlay config.toml -> $CODEX_OVERLAY_CONFIG"
}

# Generate the merged identity file via identity-merge.sh.
_codex_overlay_write_instructions() {
  local merge_script="$MARKET_ROOT/identity/identity-merge.sh"
  [[ -x "$merge_script" ]] || die "identity-merge.sh missing or not executable: $merge_script" 1

  if [[ $DRY_RUN -eq 1 ]]; then
    log "would run identity-merge.sh -> $CODEX_OVERLAY_INSTRUCTIONS"
    return 0
  fi
  "$merge_script" "$CODEX_OVERLAY_INSTRUCTIONS"
}

codex_install_overlay() {
  ensure_dir "$CODEX_OVERLAY_HOME"
  say ""
  say "== [codex] overlay ($CODEX_OVERLAY_HOME) =="
  _codex_overlay_inherit_links
  _codex_overlay_write_instructions
  _codex_overlay_write_config
}

# ---------------------------------------------------------------------------
# Entry point: codex_install
# ---------------------------------------------------------------------------

codex_install() {
  command -v python3 >/dev/null 2>&1 || die "python3 required for Codex install (TOML emission)" 3

  ensure_dir "$CODEX_SKILLS_DIR"
  ensure_dir "$CODEX_PROMPTS_DIR"

  [[ -f "$CODEX_CONFIG_FILE" ]] || die "Codex config.toml not found: $CODEX_CONFIG_FILE (run codex once to bootstrap)"

  say "[codex] target = $CODEX_HOME"

  local plugin_dir ns
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || { echo "WARN: not a plugin dir: $plugin_dir" >&2; continue; }
    if _codex_is_skip_plugin "$plugin_dir"; then
      say ""
      say "== [codex] $plugin_dir  (skipped: Claude-only) =="
      continue
    fi
    ns="$(ns_for "$plugin_dir")"
    say ""
    say "== [codex] $plugin_dir  (ns=$ns) =="
    codex_install_skills  "$plugin_dir" "$ns"
    codex_install_agents  "$plugin_dir" "$ns"
    codex_install_prompts "$plugin_dir" "$ns"
  done < <(selected_plugins)

  say ""
  say "== [codex] hooks =="
  codex_install_hooks

  codex_install_overlay
}

# ---------------------------------------------------------------------------
# Entry point: codex_uninstall
# ---------------------------------------------------------------------------

# Used by ../uninstall.sh. Removes asha-rooted symlinks under
# ~/.codex/{skills,prompts}, excises fenced hook block from config.toml.
codex_uninstall() {
  command -v python3 >/dev/null 2>&1 || die "python3 required for Codex uninstall (TOML validation)" 3
  [[ -d "$CODEX_HOME" ]] || { say "[codex] $CODEX_HOME does not exist; nothing to remove"; CODEX_UNINSTALL_TOTAL=0; return 0; }

  say "[codex] target = $CODEX_HOME"

  local total=0 n
  for spec in "skills 1" "agents 1" "prompts 1"; do
    set -- $spec
    local subdir="$1" depth="$2"
    [[ -d "$CODEX_HOME/$subdir" ]] || continue
    n="$(remove_symlinks_under "$CODEX_HOME/$subdir" "$depth")"
    [[ "$n" -gt 0 ]] && say "[codex] removed $n symlink(s) from $CODEX_HOME/$subdir"
    total=$((total + n))
  done

  # Excise fenced hook block from config.toml, if present.
  if [[ -f "$CODEX_CONFIG_FILE" ]] && grep -q "^${CODEX_HOOK_FENCE_START}\$" "$CODEX_CONFIG_FILE" 2>/dev/null; then
    if [[ $DRY_RUN -eq 1 ]]; then
      local count
      count="$(grep -c '^# asha:' "$CODEX_CONFIG_FILE" 2>/dev/null || true)"
      count="${count:-0}"
      say "[codex] would remove $count tagged hook entr$([[ $count -eq 1 ]] && echo y || echo ies) from config.toml"
    else
      _codex_backup_config_once
      local content; content="$(_codex_excise_fence)"
      # Trim trailing whitespace lines to keep config tidy.
      _codex_atomic_write_config "$content"
      say "[codex] excised asha hook block from config.toml"
    fi
  else
    log "[codex] no asha hook fence in config.toml"
  fi

  # Remove the persona overlay if present. Only deletes if it looks like ours
  # (has our generated config/instructions or our inherit symlinks).
  if [[ -d "$CODEX_OVERLAY_HOME" ]]; then
    local looks_ours=0
    [[ -L "$CODEX_OVERLAY_HOME/skills" ]] && looks_ours=1
    [[ -f "$CODEX_OVERLAY_INSTRUCTIONS" ]] && looks_ours=1
    if [[ $looks_ours -eq 1 ]]; then
      if [[ $DRY_RUN -eq 1 ]]; then
        say "[codex] would remove overlay $CODEX_OVERLAY_HOME (preserves sessions/)"
      else
        # Remove generated files + inherit symlinks; keep sessions/ (user history).
        local sub
        for sub in "${CODEX_OVERLAY_INHERIT_DIRS[@]}"; do
          local link="$CODEX_OVERLAY_HOME/$sub"
          [[ -L "$link" ]] && rm "$link"
        done
        rm -f "$CODEX_OVERLAY_CONFIG" "$CODEX_OVERLAY_INSTRUCTIONS"
        # rmdir overlay home if empty (sessions/ may keep it alive intentionally).
        rmdir "$CODEX_OVERLAY_HOME" 2>/dev/null || log "[codex] overlay dir not empty (sessions/ preserved)"
        say "[codex] removed overlay artifacts from $CODEX_OVERLAY_HOME"
      fi
    else
      log "[codex] overlay dir exists but doesn't look ours; leaving alone"
    fi
  fi

  CODEX_UNINSTALL_TOTAL=$total
}
