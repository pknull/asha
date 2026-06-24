#!/usr/bin/env bash
# harnesses/codex.sh — OpenAI Codex CLI install/uninstall logic (Step 7-revised).
#
# Sourced by ../install.sh and ../uninstall.sh. Expects globals from the
# dispatcher: MARKET_ROOT, PLUGINS_DIR, NAMESPACES_FILE, DRY_RUN, FORCE,
# VERBOSE, ONLY, ABS_MARKET_ROOT (uninstall only).
#
# Install layout under ~/.codex/:
#   skills/<skill-name>/         → symlink to plugins/<ns>/skills/<skill>/
#                                  (skill-name = SKILL.md's `name:` field)
#   skills/<cmd-name>/SKILL.md   → symlink to plugins/<ns>/commands/<cmd>.md
#                                  (cmd-name = command MD's `name:` field)
#   agents/<ns>-<agent>.md       → symlink to plugins/<ns>/agents/<agent>.md
#   config.toml                  → existing user config + appended fenced
#                                  region of [[hooks.X]] arrays tagged
#                                  "# asha:<ns>"
#
# No persona overlay. asha-codex injects persona via `codex -c
# model_instructions_file=...` so plain codex and asha-codex share ~/.codex/.
#
# Plugins skipped entirely (Claude-only): output-styles
# Hook events Codex doesn't support: SessionEnd, Setup (warned & dropped)

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_CONFIG_FILE="$CODEX_HOME/config.toml"
CODEX_SKILLS_DIR="$CODEX_HOME/skills"
CODEX_AGENTS_DIR="$CODEX_HOME/agents"

# Legacy paths from pre-Step-7 installs that we clean up if found.
CODEX_LEGACY_PROMPTS_DIR="$CODEX_HOME/prompts"
CODEX_LEGACY_OVERLAY_HOME="$HOME/.codex-asha"

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

# Extract the `name:` value from a YAML frontmatter file. Echoes the name
# (or empty string if not present). Looks at the first frontmatter block only.
_codex_skill_name_from_md() {
  local md="$1"
  python3 - "$md" <<'PYEOF'
import re, sys
text = open(sys.argv[1]).read()
if not text.startswith("---\n"):
    sys.exit(0)
end = text.find("\n---\n", 4)
if end == -1:
    sys.exit(0)
fm = text[4:end]
m = re.search(r"^name\s*:\s*(\S+)", fm, re.MULTILINE)
if m:
    print(m.group(1).strip())
PYEOF
}

# ---------------------------------------------------------------------------
# Per-primitive installers
# ---------------------------------------------------------------------------

# Install plugin skills (real skill dirs containing SKILL.md). The destination
# directory name comes from the SKILL.md's `name:` frontmatter so dir name
# matches the invocation key.
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

    # Prefer the SKILL.md's name field; fall back to <ns>-<dir-name>.
    local declared_name
    declared_name="$(_codex_skill_name_from_md "$skill/SKILL.md")"
    local dest_name="${declared_name:-${ns}-${skill_name}}"

    mklink "${skill%/}" "$CODEX_SKILLS_DIR/${dest_name}" "codex-skill"
  done
}

# Install command MDs as Codex skills. Codex 0.125's skill loader rejects
# YAML frontmatter with non-schema keys (argument-hint, allowed-tools — both
# Claude-specific). We can't symlink directly: must generate a SKILL.md with
# the Claude keys stripped. The generated file is a content-mode duplicate of
# the source body; drift-check verifies freshness via mtime.
#
# Source command MD frontmatter retained: name, description.
# Stripped: argument-hint, allowed-tools (anything else Claude-specific can be
# added to _CODEX_DROP_FRONTMATTER_KEYS as it's discovered).
codex_install_command_skills() {
  local plugin_dir="$1" ns="$2"
  _codex_is_skip_plugin "$plugin_dir" && return 0
  local src_dir="$PLUGINS_DIR/$plugin_dir/commands"
  [[ -d "$src_dir" ]] || return 0

  local cmd
  for cmd in "$src_dir"/*.md; do
    [[ -f "$cmd" ]] || continue

    local declared_name
    declared_name="$(_codex_skill_name_from_md "$cmd")"
    if [[ -z "$declared_name" ]]; then
      echo "WARN: command MD missing name: frontmatter; skipping for codex: $cmd" >&2
      continue
    fi

    local skill_dir="$CODEX_SKILLS_DIR/$declared_name"

    # Collision guard: if the skill dir is already a symlink, a plugin skill
    # claimed this name first. Skip.
    if [[ -L "$skill_dir" ]]; then
      log "[codex] skip command-skill '$declared_name' (plugin skill already claims this name)"
      continue
    fi

    ensure_dir "$skill_dir"
    _codex_emit_command_skill "$cmd" "$skill_dir/SKILL.md"
  done
}

# Generate a Codex-clean SKILL.md from a Claude command MD. Strips the keys
# Codex's parser rejects (argument-hint, allowed-tools) and any other keys
# we've identified as non-portable. Idempotent (only writes when content differs).
_codex_emit_command_skill() {
  local src="$1" dest="$2"

  # Use python so we can do correct YAML-style frontmatter manipulation.
  local content
  content="$(python3 - "$src" <<'PYEOF'
import re, sys

KEYS_TO_DROP = {"argument-hint", "allowed-tools"}

src = sys.argv[1]
text = open(src).read()
if not text.startswith("---\n"):
    sys.stderr.write(f"WARN: no frontmatter, emitting body only: {src}\n")
    sys.stdout.write(text)
    sys.exit(0)

end = text.find("\n---\n", 4)
if end == -1:
    sys.stderr.write(f"WARN: no closing ---, emitting body only: {src}\n")
    sys.stdout.write(text)
    sys.exit(0)

fm = text[4:end]
body = text[end+5:]

# Drop blocks. We treat any line starting with `<key>:` as a top-level key,
# and skip until the next top-level key or end. Indented continuation lines
# belong to the previous key.
out_lines = []
skip_until_next_key = False
for line in fm.split("\n"):
    # Top-level key match: starts at column 0, has a colon
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
    if m:
        key = m.group(1)
        skip_until_next_key = key in KEYS_TO_DROP
        if not skip_until_next_key:
            out_lines.append(line)
    else:
        # Continuation (indented) — keep iff the most recent top-level key was kept
        if not skip_until_next_key:
            out_lines.append(line)

new_fm = "\n".join(out_lines)
sys.stdout.write(f"---\n{new_fm}\n---\n{body}")
PYEOF
)"

  # Idempotent write. On the unchanged path, still bump dest mtime — the
  # drift check compares source vs dest mtimes, so a content-identical dest
  # with an old mtime would be flagged stale forever.
  if [[ -f "$dest" ]]; then
    local current; current="$(cat "$dest")"
    if [[ "$current" == "$content" ]]; then
      [[ $DRY_RUN -eq 1 ]] || touch "$dest"
      log "[codex] command-skill unchanged: $dest"
      return 0
    fi
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    say "  EMIT [codex-command-skill]  $src -> $dest"
  else
    printf '%s' "$content" > "$dest"
    log "emitted [codex-command-skill]: $dest (from $src)"
  fi
}

# Install agent MD files. Codex 0.125 multi-agent YAML schema is unverified;
# this is best-effort symlinking — Codex either picks them up or ignores them.
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

  ensure_dir "$CODEX_AGENTS_DIR"
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] || continue
    local agent_name; agent_name="$(basename "$agent")"
    mklink "$agent" "$CODEX_AGENTS_DIR/${ns}-${agent_name}" "codex-agent"
  done
}

# ---------------------------------------------------------------------------
# Hooks (TOML emission, fenced, atomic)
# ---------------------------------------------------------------------------

_codex_excise_fence() {
  [[ -f "$CODEX_CONFIG_FILE" ]] || return 0
  awk -v s="$CODEX_HOOK_FENCE_START" -v e="$CODEX_HOOK_FENCE_END" '
    BEGIN { skip = 0 }
    $0 == s { skip = 1; next }
    $0 == e { skip = 0; next }
    skip == 0 { print }
  ' "$CODEX_CONFIG_FILE"
}

_codex_emit_hooks_for_plugin() {
  local abs_root="$1" hooks_json="$2" ns="$3"
  PYTHONIOENCODING=utf-8 python3 - "$abs_root" "$hooks_json" "$ns" <<'PYEOF'
import json, sys, re
abs_root, hooks_json, ns = sys.argv[1], sys.argv[2], sys.argv[3]
CODEX_EVENTS = {"SessionStart","PreToolUse","PostToolUse","Stop","UserPromptSubmit","PermissionRequest"}

def toml_str(s):
    s = s.replace("\\","\\\\").replace('"','\\"')
    s = s.replace("\b","\\b").replace("\t","\\t").replace("\n","\\n").replace("\f","\\f").replace("\r","\\r")
    return '"' + s + '"'

def resolve_command(cmd):
    return cmd.replace("${CLAUDE_PLUGIN_ROOT}", abs_root)

with open(hooks_json) as f:
    data = json.load(f)
events = (data or {}).get("hooks") or {}
out = []
dropped = []
for event, groups in events.items():
    if event not in CODEX_EVENTS:
        dropped.append(event); continue
    if not isinstance(groups, list): continue
    for grp in groups:
        matcher = grp.get("matcher")
        if matcher == "*": matcher = None
        for h in grp.get("hooks", []):
            if h.get("type") != "command": continue
            cmd = resolve_command(h.get("command",""))
            if not cmd: continue
            out.append(f"[[hooks.{event}]]")
            if matcher: out.append(f"matcher = {toml_str(matcher)}")
            out.append('type = "command"')
            out.append(f"command = {toml_str(cmd)}")
            timeout = h.get("timeout")
            if isinstance(timeout, int):
                out.append(f"timeout = {timeout}")
            out.append(f"# asha:{ns}")
            out.append("")
for e in dropped:
    sys.stderr.write(f"  WARN: dropped {ns}/{e} (Codex does not support this event)\n")
sys.stdout.write("\n".join(out))
PYEOF
}

_codex_build_hook_block() {
  local plugin_dir ns plugin_root abs_root hooks_json count=0
  local emitted="$CODEX_HOOK_FENCE_START"$'\n'

  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || continue
    _codex_is_skip_plugin "$plugin_dir" && continue

    plugin_root="$PLUGINS_DIR/$plugin_dir"
    abs_root="$(resolve_path "$plugin_root")"
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
  [[ $count -eq 0 ]] && return 1
  printf '%s' "$emitted"
}

codex_install_hooks() {
  [[ -f "$CODEX_CONFIG_FILE" ]] || die "Codex config.toml not found: $CODEX_CONFIG_FILE"

  local existing_no_fence
  existing_no_fence="$(_codex_excise_fence)"

  local block status
  block="$(_codex_build_hook_block)" && status=0 || status=$?

  local new_content="$existing_no_fence"
  if [[ -n "$new_content" && "${new_content: -1}" != $'\n' ]]; then
    new_content+=$'\n'
  fi
  if [[ $status -eq 0 && -n "$block" ]]; then
    new_content+=$'\n'"$block"
  fi

  if [[ -f "$CODEX_CONFIG_FILE" ]]; then
    local current; current="$(cat "$CODEX_CONFIG_FILE")"
    if [[ "$current" == "${new_content%$'\n'}" || "$current" == "$new_content" ]]; then
      log "[codex] config.toml hook block unchanged"
      return 0
    fi
  fi

  _codex_backup_config_once
  _codex_atomic_write_config "$new_content"

  local n
  n="$(grep -c '^# asha:' "$CODEX_CONFIG_FILE" 2>/dev/null || true)"
  n="${n:-0}"
  log "[codex] registered $n hook entr$([[ $n -eq 1 ]] && echo y || echo ies)"
}

# ---------------------------------------------------------------------------
# Migration: clean up pre-Step-7 install state if present
# ---------------------------------------------------------------------------

_codex_migrate_legacy() {
  # If a previous overlay exists, blow it away. It's a generated artifact.
  if [[ -d "$CODEX_LEGACY_OVERLAY_HOME" ]]; then
    say "[codex] migrating: removing legacy overlay at $CODEX_LEGACY_OVERLAY_HOME"
    if [[ $DRY_RUN -eq 0 ]]; then
      # Preserve sessions/ if it has user content
      local sessions="$CODEX_LEGACY_OVERLAY_HOME/sessions"
      if [[ -d "$sessions" && -n "$(ls -A "$sessions" 2>/dev/null)" ]]; then
        say "[codex]   note: legacy overlay sessions/ preserved at $sessions (user history)"
        # Remove everything except sessions/
        find "$CODEX_LEGACY_OVERLAY_HOME" -mindepth 1 -maxdepth 1 ! -name 'sessions' -exec rm -rf {} +
      else
        rm -rf "$CODEX_LEGACY_OVERLAY_HOME"
      fi
    fi
  fi

  # If pre-Step-7 prompts/ symlinks exist, they're invisible to Codex 0.125 — clean them.
  if [[ -d "$CODEX_LEGACY_PROMPTS_DIR" ]]; then
    local n=0
    while IFS= read -r -d '' link; do
      local target; target="$(resolve_path "$link" 2>/dev/null || true)"
      case "$target" in
        "$ABS_MARKET_ROOT"|"$ABS_MARKET_ROOT"/*|"$MARKET_ROOT"|"$MARKET_ROOT"/*)
          [[ $DRY_RUN -eq 0 ]] && rm -f "$link"
          n=$((n+1)) ;;
      esac
    done < <(find "$CODEX_LEGACY_PROMPTS_DIR" -mindepth 1 -maxdepth 1 -type l -print0 2>/dev/null)
    if [[ $n -gt 0 ]]; then
      say "[codex] migrated: removed $n legacy prompt symlink(s) from $CODEX_LEGACY_PROMPTS_DIR"
      # rmdir if now empty (and a real dir, not a symlink)
      [[ $DRY_RUN -eq 0 && ! -L "$CODEX_LEGACY_PROMPTS_DIR" && -z "$(ls -A "$CODEX_LEGACY_PROMPTS_DIR")" ]] && rmdir "$CODEX_LEGACY_PROMPTS_DIR"
    fi
  fi
}

# ---------------------------------------------------------------------------
# Entry point: codex_install
# ---------------------------------------------------------------------------

codex_install() {
  command -v python3 >/dev/null 2>&1 || die "python3 required for Codex install (TOML + frontmatter parsing)" 3

  : "${ABS_MARKET_ROOT:=$(resolve_path "$MARKET_ROOT")}"

  ensure_dir "$CODEX_SKILLS_DIR"

  [[ -f "$CODEX_CONFIG_FILE" ]] || die "Codex config.toml not found: $CODEX_CONFIG_FILE (run codex once to bootstrap)"

  say "[codex] target = $CODEX_HOME"

  _codex_migrate_legacy

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
    codex_install_skills         "$plugin_dir" "$ns"
    codex_install_agents         "$plugin_dir" "$ns"
    codex_install_command_skills "$plugin_dir" "$ns"
  done < <(selected_plugins)

  say ""
  say "== [codex] hooks =="
  codex_install_hooks
}

# ---------------------------------------------------------------------------
# Entry point: codex_uninstall
# ---------------------------------------------------------------------------

codex_uninstall() {
  command -v python3 >/dev/null 2>&1 || die "python3 required for Codex uninstall (TOML validation)" 3
  [[ -d "$CODEX_HOME" ]] || { say "[codex] $CODEX_HOME does not exist; nothing to remove"; CODEX_UNINSTALL_TOTAL=0; return 0; }

  say "[codex] target = $CODEX_HOME"

  local total=0 n

  # Skills cleanup: three kinds of asha-installed entries to remove —
  #   1. Whole-dir symlinks (plugin skills) — remove via remove_symlinks_under
  #   2. SKILL.md symlinks inside our created dirs (legacy command-skills,
  #      pre-frontmatter-strip era) — same scan handles them
  #   3. Generated SKILL.md files inside our created dirs (current command-
  #      skills with stripped frontmatter) — match by source name lookup
  if [[ -d "$CODEX_SKILLS_DIR" ]]; then
    n="$(remove_symlinks_under "$CODEX_SKILLS_DIR" 2)"
    [[ "$n" -gt 0 ]] && say "[codex] removed $n skill symlink(s) from $CODEX_SKILLS_DIR"
    total=$((total + n))

    # Generated command-skills (real files): identify by walking plugin command MDs,
    # reading their declared name, and checking if a non-symlink SKILL.md exists.
    local removed_generated=0
    while IFS= read -r cmd; do
      [[ -f "$cmd" ]] || continue
      case "$cmd" in *output-styles*) continue ;; esac
      local declared_name
      declared_name="$(_codex_skill_name_from_md "$cmd")"
      [[ -z "$declared_name" ]] && continue
      local skill_md="$CODEX_SKILLS_DIR/$declared_name/SKILL.md"
      # Only remove if it's a real file (not symlink — symlinks were handled above)
      if [[ -f "$skill_md" && ! -L "$skill_md" ]]; then
        if [[ $DRY_RUN -eq 1 ]]; then
          info "  RM (generated)  $skill_md"
        else
          rm -f "$skill_md"
          log "removed generated command-skill: $skill_md"
        fi
        removed_generated=$((removed_generated+1))
      fi
    done < <(find "$PLUGINS_DIR" -mindepth 3 -maxdepth 3 -path '*/commands/*.md' -type f 2>/dev/null)
    [[ $removed_generated -gt 0 ]] && say "[codex] removed $removed_generated generated command-skill(s)"
    total=$((total + removed_generated))

    # Prune now-empty skill dirs that we created (only real dirs, not .system)
    while IFS= read -r d; do
      [[ -z "$d" ]] && continue
      [[ -L "$d" ]] && continue
      [[ "$(basename "$d")" == ".system" ]] && continue
      [[ -z "$(ls -A "$d" 2>/dev/null)" ]] || continue
      if [[ $DRY_RUN -eq 1 ]]; then
        info "  RMDIR  $d"
      else
        rmdir "$d" 2>/dev/null && log "rmdir: $d"
      fi
    done < <(find "$CODEX_SKILLS_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
  fi

  # Agents: depth 1
  if [[ -d "$CODEX_AGENTS_DIR" ]]; then
    n="$(remove_symlinks_under "$CODEX_AGENTS_DIR" 1)"
    [[ "$n" -gt 0 ]] && say "[codex] removed $n agent symlink(s) from $CODEX_AGENTS_DIR"
    total=$((total + n))
  fi

  # Legacy: any remaining prompts dir entries from pre-Step-7 installs
  if [[ -d "$CODEX_LEGACY_PROMPTS_DIR" ]]; then
    n="$(remove_symlinks_under "$CODEX_LEGACY_PROMPTS_DIR" 1)"
    [[ "$n" -gt 0 ]] && say "[codex] removed $n legacy prompt symlink(s) from $CODEX_LEGACY_PROMPTS_DIR"
    total=$((total + n))
    [[ $DRY_RUN -eq 0 && ! -L "$CODEX_LEGACY_PROMPTS_DIR" && -z "$(ls -A "$CODEX_LEGACY_PROMPTS_DIR")" ]] && rmdir "$CODEX_LEGACY_PROMPTS_DIR"
  fi

  # Excise hook fence from config.toml
  if [[ -f "$CODEX_CONFIG_FILE" ]] && grep -q "^${CODEX_HOOK_FENCE_START}\$" "$CODEX_CONFIG_FILE" 2>/dev/null; then
    if [[ $DRY_RUN -eq 1 ]]; then
      local count
      count="$(grep -c '^# asha:' "$CODEX_CONFIG_FILE" 2>/dev/null || true)"
      count="${count:-0}"
      say "[codex] would remove $count tagged hook entr$([[ $count -eq 1 ]] && echo y || echo ies) from config.toml"
    else
      _codex_backup_config_once
      local content; content="$(_codex_excise_fence)"
      _codex_atomic_write_config "$content"
      say "[codex] excised asha hook block from config.toml"
    fi
  else
    log "[codex] no asha hook fence in config.toml"
  fi

  # Legacy overlay cleanup (if user is uninstalling after upgrading)
  if [[ -d "$CODEX_LEGACY_OVERLAY_HOME" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[codex] would remove legacy overlay $CODEX_LEGACY_OVERLAY_HOME (preserves sessions/)"
    else
      local sessions="$CODEX_LEGACY_OVERLAY_HOME/sessions"
      if [[ -d "$sessions" && -n "$(ls -A "$sessions" 2>/dev/null)" ]]; then
        find "$CODEX_LEGACY_OVERLAY_HOME" -mindepth 1 -maxdepth 1 ! -name 'sessions' -exec rm -rf {} +
        say "[codex] removed legacy overlay artifacts (sessions/ preserved)"
      else
        rm -rf "$CODEX_LEGACY_OVERLAY_HOME"
        say "[codex] removed legacy overlay $CODEX_LEGACY_OVERLAY_HOME"
      fi
    fi
  fi

  # Cached identity + combined identity-plus-operational file (both regenerated
  # on the next asha-codex launch; safe to remove)
  if [[ -f "$HOME/.cache/asha/instructions.md" || -f "$HOME/.cache/asha/instructions-codex.md" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[codex] would remove ~/.cache/asha/instructions.md + instructions-codex.md"
    else
      rm -f "$HOME/.cache/asha/instructions.md" "$HOME/.cache/asha/instructions-codex.md"
      rmdir "$HOME/.cache/asha" 2>/dev/null
      log "[codex] removed cached identity"
    fi
  fi

  CODEX_UNINSTALL_TOTAL=$total
}
