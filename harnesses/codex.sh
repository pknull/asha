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
#   skills/<cmd-name>/SKILL.md   → generated Codex-clean skill from
#                                  plugins/<ns>/commands/<cmd>.md
#   agents/<ns>-<agent>.toml     → generated Codex custom-agent TOML from
#                                  plugins/<ns>/agents/<agent>.md
#   config.toml                  → existing user config + appended fenced
#                                  region of [[hooks.X]] arrays tagged
#                                  "# asha:<ns>"
#   rules/asha.rules             → native Codex execution-policy prompts for
#                                  coarse shell approvals where hooks cannot
#                                  be relied upon as the enforcement boundary
#
# No persona overlay. asha-codex injects persona via `codex -c
# model_instructions_file=...` so plain codex and asha-codex share ~/.codex/.
#
# Plugins skipped entirely (Claude-only): none currently
# Hook events Codex doesn't support: SessionEnd, Setup (warned & dropped)

CODEX_HOME="$(asha_harness_home codex)"
CODEX_CONFIG_FILE="$CODEX_HOME/config.toml"
CODEX_SKILLS_DIR="$CODEX_HOME/skills"
CODEX_AGENTS_DIR="$CODEX_HOME/agents"
CODEX_RULES_DIR="$CODEX_HOME/rules"
CODEX_RULES_FILE="$CODEX_RULES_DIR/asha.rules"

# Legacy paths from pre-Step-7 installs that we clean up if found.
CODEX_LEGACY_PROMPTS_DIR="$CODEX_HOME/prompts"
CODEX_LEGACY_OVERLAY_HOME="$HOME/.codex-asha"

# Events Codex supports in current hook docs. Unsupported Claude events are
# warned and dropped during translation.
_CODEX_EVENTS=(SessionStart PreToolUse PermissionRequest PostToolUse PreCompact PostCompact UserPromptSubmit Stop SubagentStart SubagentStop)
_CODEX_SKIP_PLUGINS=()  # no Claude-only plugins currently shipped

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
  [[ ${#_CODEX_SKIP_PLUGINS[@]} -eq 0 ]] && return 1  # empty-array guard (bash 3.2 + set -u)
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
preamble = """## Codex harness adapter

This file was rendered from an Asha command source. Treat slash-command and Claude `Task` references below as workflow intent, not literal Codex tool names. When the workflow asks for agents, use Codex subagents/custom agents when available; otherwise execute the same phases inline and preserve the output contract.

"""
sys.stdout.write(f"---\n{new_fm}\n---\n{preamble}{body}")
PYEOF
)"

  local prepared
  prepared="$(mktemp)"
  printf '%s' "$content" > "$prepared"
  if declare -F asha_artifact_install_prepared >/dev/null 2>&1 \
     && [[ "${ASHA_ARTIFACT_HARNESS:-}" == codex ]]; then
    asha_artifact_install_prepared codex "$src" "$dest" codex-command-skill "$prepared"
  elif [[ $DRY_RUN -eq 1 ]]; then
    say "  EMIT [codex-command-skill]  $src -> $dest"
  else
    ensure_dir "$(dirname "$dest")"
    printf '%s' "$content" > "$dest"
    log "emitted [codex-command-skill]: $dest (from $src)"
  fi
  rm -f "$prepared"
}

# Generate Codex custom-agent TOML files from Asha agent Markdown. This is the
# native Codex surface: standalone TOML with name, description, and
# developer_instructions. The generated filename is namespaced to avoid file
# collisions, while the agent's declared name remains the source frontmatter
# name so existing workflow prose can still ask for `reviewer`, `thinker`, etc.
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
    local base declared_name dest legacy existing
    base="$(basename "$agent" .md)"
    declared_name="$(_codex_skill_name_from_md "$agent")"
    [[ -n "$declared_name" ]] || declared_name="$base"
    dest="$CODEX_AGENTS_DIR/${ns}-${declared_name}.toml"

    # Clean the legacy markdown-agent symlink for this source if present.
    legacy="$CODEX_AGENTS_DIR/${ns}-${base}.md"
    if [[ -L "$legacy" ]]; then
      existing="$(resolve_path "$legacy" 2>/dev/null || true)"
      if [[ "$existing" == "$(resolve_path "$agent")" ]]; then
        [[ $DRY_RUN -eq 1 ]] || rm -f "$legacy"
        log "[codex] removed legacy markdown agent symlink: $legacy"
      fi
    fi

    _codex_emit_agent_toml "$agent" "$dest"
  done
}

_codex_emit_agent_toml() {
  local src="$1" dest="$2"
  local content
  content="$(python3 - "$src" <<'PYEOF'
import json, re, sys

src = sys.argv[1]
text = open(src, encoding="utf-8").read()
name = ""
description = ""
body = text

if text.startswith("---\n"):
    end = text.find("\n---\n", 4)
    if end != -1:
        fm = text[4:end]
        body = text[end+5:]

        def field(key):
            m = re.search(rf"^{re.escape(key)}\s*:\s*(.+)$", fm, re.MULTILINE)
            if not m:
                return ""
            value = m.group(1).strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            return value

        name = field("name")
        description = field("description")

if not name:
    name = re.sub(r"\.md$", "", src.rsplit("/", 1)[-1])
if not description:
    description = f"Asha agent rendered from {src}"

instructions = (
    "You are an Asha custom agent rendered for OpenAI Codex. "
    "Follow the source agent instructions below. If they mention Claude-only "
    "tool names, map them to the closest available Codex tool or report the "
    "missing capability explicitly.\n\n"
    + body.strip()
    + "\n"
)

print(f"name = {json.dumps(name)}")
print(f"description = {json.dumps(description)}")
print("developer_instructions = " + json.dumps(instructions))
PYEOF
)"

  local prepared
  prepared="$(mktemp)"
  printf '%s\n' "$content" > "$prepared"
  if declare -F asha_artifact_install_prepared >/dev/null 2>&1 \
     && [[ "${ASHA_ARTIFACT_HARNESS:-}" == codex ]]; then
    asha_artifact_install_prepared codex "$src" "$dest" codex-agent-toml "$prepared"
  elif [[ $DRY_RUN -eq 1 ]]; then
    say "  EMIT [codex-agent-toml]  $src -> $dest"
  else
    ensure_dir "$(dirname "$dest")"
    printf '%s\n' "$content" > "$dest"
    log "emitted [codex-agent-toml]: $dest (from $src)"
  fi
  rm -f "$prepared"
}

# ---------------------------------------------------------------------------
# Native Codex execution-policy rules
# ---------------------------------------------------------------------------

codex_install_rules() {
  local content
  content="$(cat <<'EOF'
# Managed by asha installer; do not edit.
#
# These native Codex rules are a coarse fallback for command approvals. Asha's
# richer policy engine remains hook-based, but current Codex shell execution can
# bypass PreToolUse. Rules operate at approval/sandbox boundaries and use prefix
# matching only, so they are deliberately narrower than policy-guard.sh.

prefix_rule(
    pattern = ["find", "/home"],
    decision = "prompt",
    justification = "Broad scan over /home can cause severe disk pressure; scope to a subdirectory first.",
    match = ["find /home -name x"],
)

prefix_rule(
    pattern = ["find", "/home/pknull"],
    decision = "prompt",
    justification = "Broad scan over /home/pknull can cause severe disk pressure; scope to a subdirectory first.",
    match = ["find /home/pknull -name x"],
)

prefix_rule(
    pattern = ["bfs", "/home"],
    decision = "prompt",
    justification = "Broad scan over /home can cause severe disk pressure; scope to a subdirectory first.",
    match = ["bfs /home -name x"],
)

prefix_rule(
    pattern = ["bfs", "/home/pknull"],
    decision = "prompt",
    justification = "Broad scan over /home/pknull can cause severe disk pressure; scope to a subdirectory first.",
    match = ["bfs /home/pknull -name x"],
)

prefix_rule(
    pattern = ["git", "reset", "--hard"],
    decision = "prompt",
    justification = "Destructive git reset; confirm before discarding local work.",
    match = ["git reset --hard", "git reset --hard HEAD~1"],
)

prefix_rule(
    pattern = ["git", "push", "--force"],
    decision = "prompt",
    justification = "Force-push affects shared state; confirm before proceeding.",
    match = ["git push --force", "git push --force origin main"],
)

prefix_rule(
    pattern = ["git", "push", "-f"],
    decision = "prompt",
    justification = "Force-push affects shared state; confirm before proceeding.",
    match = ["git push -f", "git push -f origin main"],
)

prefix_rule(
    pattern = ["git", "branch", "-D", "main"],
    decision = "prompt",
    justification = "Protected-branch delete; confirm before proceeding.",
    match = ["git branch -D main"],
)

prefix_rule(
    pattern = ["git", "branch", "-D", "master"],
    decision = "prompt",
    justification = "Protected-branch delete; confirm before proceeding.",
    match = ["git branch -D master"],
)

prefix_rule(
    pattern = ["git", "branch", "-d", "main"],
    decision = "prompt",
    justification = "Protected-branch delete; confirm before proceeding.",
    match = ["git branch -d main"],
)

prefix_rule(
    pattern = ["git", "branch", "-d", "master"],
    decision = "prompt",
    justification = "Protected-branch delete; confirm before proceeding.",
    match = ["git branch -d master"],
)
EOF
)"

  if [[ $DRY_RUN -eq 1 ]]; then
    say "  WRITE [codex-rules]  $CODEX_RULES_FILE"
    return 0
  fi

  ensure_dir "$CODEX_RULES_DIR"
  if [[ -f "$CODEX_RULES_FILE" ]] && [[ "$(cat "$CODEX_RULES_FILE")" == "$content" ]]; then
    log "[codex] native rules unchanged: $CODEX_RULES_FILE"
    return 0
  fi
  printf '%s\n' "$content" > "$CODEX_RULES_FILE"
  log "[codex] installed native execution-policy rules: $CODEX_RULES_FILE"
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
CODEX_EVENTS = {
    "SessionStart", "PreToolUse", "PermissionRequest", "PostToolUse",
    "PreCompact", "PostCompact", "UserPromptSubmit", "Stop",
    "SubagentStart", "SubagentStop",
}

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
        harnesses = grp.get("_asha_harnesses")
        if harnesses is not None and "codex" not in harnesses:
            continue
        matcher = grp.get("matcher")
        if matcher == "*": matcher = None
        for h in grp.get("hooks", []):
            if h.get("type") != "command": continue
            cmd = resolve_command(h.get("command",""))
            if not cmd: continue
            # Current Codex TOML schema uses a matcher group containing one or
            # more nested hook handlers.
            out.append(f"[[hooks.{event}]]")
            if matcher: out.append(f"matcher = {toml_str(matcher)}")
            out.append(f"[[hooks.{event}.hooks]]")
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
  done < <(all_plugin_dirs)

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
      # rmdir if now empty (and a real dir, not a symlink). `|| true`: a failed
      # rmdir at the tail of an && list aborts the run under `set -e` (issue #4).
      [[ $DRY_RUN -eq 0 && ! -L "$CODEX_LEGACY_PROMPTS_DIR" && -z "$(ls -A "$CODEX_LEGACY_PROMPTS_DIR")" ]] && rmdir "$CODEX_LEGACY_PROMPTS_DIR" || true
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
  asha_artifact_begin codex

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
  say "== [codex] native rules =="
  codex_install_rules

  say ""
  say "== [codex] hooks =="
  codex_install_hooks
  asha_artifact_finalize codex "$([[ -z "${ONLY:-}" ]] && echo 1 || echo 0)"
}

# ---------------------------------------------------------------------------
# Entry point: codex_uninstall
# ---------------------------------------------------------------------------

codex_uninstall() {
  command -v python3 >/dev/null 2>&1 || die "python3 required for Codex uninstall (TOML validation)" 3
  [[ -d "$CODEX_HOME" ]] || { say "[codex] $CODEX_HOME does not exist; nothing to remove"; CODEX_UNINSTALL_TOTAL=0; return 0; }

  local ownership_manifest
  ownership_manifest="$(asha_artifact_manifest_path codex)"
  if [[ ! -f "$ownership_manifest" ]] && {
       grep -rlq '## Codex harness adapter' "$CODEX_SKILLS_DIR" 2>/dev/null \
       || grep -rlq 'Asha custom agent rendered for OpenAI Codex' "$CODEX_AGENTS_DIR" 2>/dev/null;
     }; then
    die "pre-manifest Codex artifacts detected; run 'asha install codex --force' once, then retry uninstall" 2
  fi

  say "[codex] target = $CODEX_HOME"

  local total=0 n
  n="$(asha_artifact_uninstall codex)"
  [[ "$n" -gt 0 ]] && say "[codex] removed $n owned generated artifact(s)"
  total=$((total + n))

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
    # `|| true`: a failed rmdir at the tail of an && list aborts under set -e.
    [[ $DRY_RUN -eq 0 && ! -L "$CODEX_LEGACY_PROMPTS_DIR" && -z "$(ls -A "$CODEX_LEGACY_PROMPTS_DIR")" ]] && rmdir "$CODEX_LEGACY_PROMPTS_DIR" || true
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

  # Dedicated native Codex execution-policy rules file.
  if [[ -f "$CODEX_RULES_FILE" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[codex] would remove native rules file $CODEX_RULES_FILE"
    else
      rm -f "$CODEX_RULES_FILE"
      rmdir "$CODEX_RULES_DIR" 2>/dev/null || true
      say "[codex] removed native rules file $CODEX_RULES_FILE"
    fi
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
      # `|| true` is load-bearing: the cache dir usually still holds OTHER
      # harnesses' files (codex runs before copilot in `--target all`), so this
      # rmdir fails — and with stderr silenced, an unguarded failure under the
      # shim's `set -e` killed the whole uninstall here, stranding every
      # harness after codex (issue #4, 2026-07-01 relocation).
      rmdir "$HOME/.cache/asha" 2>/dev/null || true
      log "[codex] removed cached identity"
    fi
  fi

  CODEX_UNINSTALL_TOTAL=$total
}
