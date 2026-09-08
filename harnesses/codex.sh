#!/usr/bin/env bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
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
#   hooks.json                   → strictly owned native hook definitions
#   config.toml                  → native user config, inspected READ-ONLY
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
CODEX_HOOKS_FILE="$CODEX_HOME/hooks.json"
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
  local src_dir="$1" ns="$2" kind="${3:-plugin}" label="${4:-}"
  if [[ "$kind" == plugin ]] && _codex_is_skip_plugin "$label"; then
    return 0
  fi
  [[ -d "$src_dir" ]] || return 0
  validate_skill_source "$src_dir" "$kind" || return $?

  local skill
  while IFS= read -r skill; do
    [[ -n "$skill" && -d "$skill" ]] || continue
    local skill_name; skill_name="$(basename "$skill")"
    [[ -f "$skill/SKILL.md" ]] || { log "skip skill (no SKILL.md): $skill"; continue; }

    local dest_name
    if [[ "$kind" == imported ]]; then
      dest_name="${ns}-${skill_name}"
      prepare_imported_skill_adapter "${skill%/}" "$dest_name" || return $?
      mklink_imported_skill "${skill%/}" "$ASHA_IMPORTED_SKILL_ADAPTER" \
        "$CODEX_SKILLS_DIR/${dest_name}" "codex-skill" || return $?
      continue
    fi
    if ! dest_name="$(plugin_skill_destination_name "${skill%/}" "$ns")"; then
      continue
    fi

    mklink "${skill%/}" "$CODEX_SKILLS_DIR/${dest_name}" "codex-skill" || return $?
  done < <(skill_dirs_from_source "$src_dir" "$kind")
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

    ensure_dir "$skill_dir" || return $?
    _codex_emit_command_skill "$cmd" "$skill_dir/SKILL.md" || return $?
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
)" || return $?

  local prepared
  prepared="$(mktemp)" || return $?
  printf '%s' "$content" > "$prepared"
  if declare -F asha_artifact_install_prepared >/dev/null 2>&1 \
     && [[ "${ASHA_ARTIFACT_HARNESS:-}" == codex ]]; then
    asha_artifact_install_prepared codex "$src" "$dest" codex-command-skill "$prepared" || { local rc=$?; rm -f "$prepared"; return "$rc"; }
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

  ensure_dir "$CODEX_AGENTS_DIR" || return $?
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] || continue
    local base declared_name dest legacy existing
    base="$(basename "$agent" .md)"
    declared_name="$(_codex_skill_name_from_md "$agent")"
    [[ -n "$declared_name" ]] || declared_name="$base"
    declared_name="${declared_name//:/-}"
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

    _codex_emit_agent_toml "$agent" "$dest" || return $?
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
)" || return $?

  local prepared
  prepared="$(mktemp)" || return $?
  printf '%s\n' "$content" > "$prepared"
  if declare -F asha_artifact_install_prepared >/dev/null 2>&1 \
     && [[ "${ASHA_ARTIFACT_HARNESS:-}" == codex ]]; then
    asha_artifact_install_prepared codex "$src" "$dest" codex-agent-toml "$prepared" || { local rc=$?; rm -f "$prepared"; return "$rc"; }
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
  local content user_home rules_template
  rules_template="$(mktemp)" || return $?
  cat > "$rules_template" <<'EOF'
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
    pattern = ["find", "__ASHA_USER_HOME__"],
    decision = "prompt",
    justification = "Broad scan over __ASHA_USER_HOME__ can cause severe disk pressure; scope to a subdirectory first.",
    match = ["find __ASHA_USER_HOME__ -name x"],
)

prefix_rule(
    pattern = ["bfs", "/home"],
    decision = "prompt",
    justification = "Broad scan over /home can cause severe disk pressure; scope to a subdirectory first.",
    match = ["bfs /home -name x"],
)

prefix_rule(
    pattern = ["bfs", "__ASHA_USER_HOME__"],
    decision = "prompt",
    justification = "Broad scan over __ASHA_USER_HOME__ can cause severe disk pressure; scope to a subdirectory first.",
    match = ["bfs __ASHA_USER_HOME__ -name x"],
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
  content="$(cat "$rules_template")"
  rm -f "$rules_template"

  user_home="${HOME:-}"
  if [[ -z "$user_home" ]]; then
    user_home="$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f6 || true)"
  fi
  user_home="${user_home%/}"

  if [[ -z "$user_home" || "$user_home" == "/home" || "$user_home" == "/" ]]; then
    local command rule_start block_remainder
    for command in find bfs; do
      rule_start=$'\nprefix_rule(\n    pattern = ["'"$command"'", "__ASHA_USER_HOME__"],'
      if [[ "$content" == *"$rule_start"* ]]; then
        block_remainder="${content#*"$rule_start"}"
        content="${content%%"$rule_start"*}${block_remainder#*$'\n)\n'}"
      fi
    done
  else
    content="${content//__ASHA_USER_HOME__/$user_home}"
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    say "  WRITE [codex-rules]  $CODEX_RULES_FILE"
    return 0
  fi

  ensure_dir "$CODEX_RULES_DIR" || return $?
  if [[ -f "$CODEX_RULES_FILE" ]] && [[ "$(cat "$CODEX_RULES_FILE")" == "$content" ]]; then
    log "[codex] native rules unchanged: $CODEX_RULES_FILE"
    return 0
  fi
  printf '%s\n' "$content" > "$CODEX_RULES_FILE" || return $?
  log "[codex] installed native execution-policy rules: $CODEX_RULES_FILE"
}

# ---------------------------------------------------------------------------
# Hooks (native owned JSON; legacy TOML is read-only)
# ---------------------------------------------------------------------------

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
            # Bare `codex` launches do not inherit the `asha codex` wrapper's
            # ASHA_HARNESS export. Stamp identity at the native translation
            # seam so response-shape and harness-allowlist decisions remain
            # correct for every generated hook.
            cmd = "env ASHA_HARNESS=codex " + cmd
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
  local plugin_dir ns plugin_root abs_root hooks_json count=0 plugin_list
  local emitted="$CODEX_HOOK_FENCE_START"$'\n'

  plugin_list="$(
    if [[ "${1:-}" != all ]] && declare -F all_plugin_dirs >/dev/null; then
      all_plugin_dirs
    else
      # Uninstall has no install-only enumeration helpers. Ownership includes
      # previously enabled optional plugins as well as currently selected ones.
      for plugin_root in "$PLUGINS_DIR"/*/; do
        [[ -d "$plugin_root" ]] || continue
        plugin_root="${plugin_root%/}"
        printf '%s\n' "${plugin_root##*/}"
      done
    fi
  )" || return 4
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || continue
    _codex_is_skip_plugin "$plugin_dir" && continue

    plugin_root="$PLUGINS_DIR/$plugin_dir"
    abs_root="$(resolve_path "$plugin_root")" || return 4
    if   [[ -f "$plugin_root/hooks/hooks.json" ]]; then hooks_json="$plugin_root/hooks/hooks.json"
    elif [[ -f "$plugin_root/hooks.json"      ]]; then hooks_json="$plugin_root/hooks.json"
    else continue
    fi

    local lifecycles_count
    lifecycles_count="$(jq -r '.hooks // {} | length' "$hooks_json")" || return 4
    [[ "$lifecycles_count" -gt 0 ]] || continue

    ns="$(ns_for "$plugin_dir")" || return 4
    local plugin_emit
    plugin_emit="$(_codex_emit_hooks_for_plugin "$abs_root" "$hooks_json" "$ns")" || return 4
    [[ -z "$plugin_emit" ]] && continue
    emitted+="$plugin_emit"$'\n'
    count=$((count+1))
  done <<< "$plugin_list"

  emitted+="$CODEX_HOOK_FENCE_END"$'\n'
  [[ $count -eq 0 ]] && return 1
  printf '%s' "$emitted"
}

# Codex-local preflight and publication. The lexical TOML reader is retained
# solely to classify legacy inline definitions, never to rewrite shared config.
# Plans bind the owned JSON and consumed manifest, NOT a native config snapshot.
_codex_hook_plan() {
  local manifest
  manifest="$(asha_artifact_manifest_path codex)" || return $?
  python3 - "$CODEX_CONFIG_FILE" "$CODEX_HOOK_FENCE_START" "$CODEX_HOOK_FENCE_END" \
    "$CODEX_HOOKS_FILE" "$manifest" "$MARKET_ROOT/harnesses/codex.sh" "$$" "$@" <<'PYEOF'
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile

tomllib = __import__('tomllib' if sys.version_info >= (3, 11) else 'tomli')
path, start, end, destination, manifest, source, shell_pid, action = sys.argv[1:9]
LIMIT = 4 * 1024 * 1024

def refuse(message):
    raise ValueError(message)

def identity(s):
    return [s.st_dev, s.st_ino, s.st_uid, s.st_gid, s.st_mode,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink]

def canonical(value):
    return (isinstance(value, str) and value.startswith('/') and
            os.path.normpath(value) == value and not value.startswith('//') and
            not any(ord(c) < 32 or ord(c) == 127 for c in value))

def parents(filename):
    if not canonical(filename):
        refuse('noncanonical path: ' + str(filename))
    result = []
    root_uid = os.lstat('/').st_uid
    system_paths = ('/', '/tmp', '/home', '/Users', '/private', '/private/tmp')
    parent = os.path.dirname(filename)
    while True:
        try:
            s = os.lstat(parent)
        except FileNotFoundError:
            parent = os.path.dirname(parent)
            continue
        if not stat.S_ISDIR(s.st_mode):
            refuse('symlink or non-directory ancestor: ' + parent)
        if s.st_uid not in (0, os.getuid()) and not (parent in system_paths and s.st_uid == root_uid):
            refuse('unsafe ancestor: ' + parent)
        result.append([parent, s.st_dev, s.st_ino, s.st_uid, s.st_gid, s.st_mode])
        if parent == '/':
            break
        parent = os.path.dirname(parent)
    private = False
    for parent, _, _, uid, _, mode in reversed(result):
        system = uid == 0 or (parent in system_paths and uid == root_uid)
        if mode & 0o022 and not private and not (system and mode & stat.S_ISVTX):
            refuse('unsafe writable ancestor: ' + parent)
        if uid == os.getuid() and not mode & 0o077:
            private = True
    return result

def capture(filename):
    ancestry = parents(filename)
    try:
        s = os.lstat(filename)
    except FileNotFoundError:
        return None, b''
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1:
        refuse('not a regular file owned by this user with one link (no symlinks): ' + filename)
    if s.st_size > LIMIT:
        refuse('bounded read limit exceeded: ' + filename)
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        if identity(os.fstat(handle.fileno())) != identity(s):
            refuse('identity changed while opening: ' + filename)
        data = handle.read(LIMIT + 1)
        if len(data) > LIMIT or identity(os.fstat(handle.fileno())) != identity(s):
            refuse('identity changed or read limit exceeded: ' + filename)
    if identity(os.lstat(filename)) != identity(s) or parents(filename) != ancestry:
        refuse('identity changed while capturing: ' + filename)
    return identity(s), data

def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            refuse('duplicate JSON key: ' + key)
        result[key] = value
    return result

def json_value(raw):
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: refuse('invalid JSON constant: ' + value))

def statements(text):
    # Split ONLY at lexical statement boundaries. Header/fence-looking lines
    # in multiline strings or nested arrays are ordinary value bytes. Parsing
    # validity remains tomllib's job; this scanner never repairs invalid TOML.
    offset = i = depth = 0
    quote = None
    while i < len(text):
        c = text[i]
        if quote:
            if quote[0] == '"' and c == '\\':
                i += 2
                continue
            if text.startswith(quote, i):
                width = len(quote)
                if width == 3:
                    # TOML permits one or two quote characters immediately
                    # before a multiline closing delimiter (runs of 4 or 5).
                    while i + width < len(text) and text[i + width] == quote[0]:
                        width += 1
                i += width
                quote = None
                continue
        elif c in ('"', "'"):
            quote = c * 3 if text.startswith(c * 3, i) else c
            i += len(quote)
            continue
        elif c == '#':
            newline = text.find('\n', i)
            i = len(text) if newline < 0 else newline
            if i == len(text):
                break
            c = '\n'
        elif c in '[{':
            depth += 1
        elif c in ']}':
            depth -= 1
        if c == '\n' and not quote and depth == 0:
            yield offset, i + 1, text[offset:i + 1]
            offset = i + 1
        i += 1
    if offset < len(text):
        yield offset, len(text), text[offset:]

def header(raw):
    raw = raw.lstrip()
    if not raw.startswith('['):
        return None
    array = raw.startswith('[[')
    # Let TOML decode quoted, escaped and dotted key components for us.
    value = tomllib.loads(raw)
    keys = []
    while isinstance(value, dict) and len(value) == 1:
        key, value = next(iter(value.items()))
        keys.append(key)
        if isinstance(value, list):
            value = value[0]
    return tuple(keys), array

def same(left, right):
    # TOML booleans, integers and floats are distinct, unlike Python's ==.
    # NaN is a valid TOML value and must compare equal to its reparse here.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and math.isnan(left):
        return math.isnan(right)
    return left == right

def classify(raw, wanted, catalog, installing):
    text = raw.decode('utf-8')
    original = tomllib.loads(text)
    hooks = original.get('hooks', {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get('state', {}), dict):
        refuse('hooks and hooks.state must be tables')
    for slot, trust in hooks.get('state', {}).items():
        if not isinstance(trust, dict):
            refuse('malformed hook trust slot: ' + slot)
        if 'trusted_hash' in trust and not isinstance(trust['trusted_hash'], str):
            refuse('malformed trusted_hash in hook trust slot: ' + slot)
        if 'enabled' in trust and not isinstance(trust['enabled'], bool):
            refuse('malformed enabled value in hook trust slot: ' + slot)
    for event, groups in hooks.items():
        if event != 'state':
            validate_groups(event, groups)
    tokens = list(statements(text))
    fences = [(i, r.strip()) for i, (_, _, r) in enumerate(tokens)
              if r.strip() in (start, end)]
    if fences and ([v for _, v in fences] != [start, end]):
        refuse('unmatched, nested or multiple managed fences')
    lo, hi = (fences[0][0], fences[1][0]) if fences else (-1, -1)
    known = tomllib.loads(catalog).get('hooks', {})
    catalog_tags = {}
    catalog_indexes = {}
    current = None
    for _, _, r in statements(catalog):
        h = header(r)
        if h and h[1] and len(h[0]) == 2 and h[0][0] == 'hooks':
            event = h[0][1]
            index = catalog_indexes.get(event, 0)
            catalog_indexes[event] = index + 1
            current = (event, index)
        if current and re.fullmatch(r'# asha:[A-Za-z0-9_-]+', r.strip()):
            catalog_tags[current] = r.strip()
    desired = tomllib.loads(wanted).get('hooks', {})
    headers = [(i, header(r)) for i, (_, _, r) in enumerate(tokens)
               if r.lstrip().startswith('[')]
    counters = {}
    owned = {}
    accounted_tags = set()
    for position, (i, (keys, array)) in enumerate(headers):
        if not (array and len(keys) == 2 and keys[0] == 'hooks'):
            continue
        event = keys[1]
        index = counters.get(event, 0)
        counters[event] = index + 1
        j = len(tokens)
        for next_i, (next_keys, _) in headers[position + 1:]:
            if next_keys[:2] != keys or len(next_keys) <= 2:
                j = next_i
                break
        group = original['hooks'][event][index]
        if not lo < i < hi:
            # Unfenced generated-looking handlers are ambiguous, not foreign.
            if any('/plugins/' in str(h.get('command', '')) or
                   'ASHA_HARNESS=codex' in str(h.get('command', ''))
                   for h in group.get('hooks', []) if isinstance(h, dict)):
                refuse('unfenced Asha inline hooks require manual inspection')
            continue
        tags = [r.strip() for _, _, r in tokens[i:min(j, hi)]
                if r.strip().startswith('# asha:')]
        accounted_tags.update(k for k in range(i, min(j, hi))
                              if tokens[k][2].strip().startswith('# asha:'))
        candidates = known.get(event, [])
        proven = any(same(group, candidate) and
                     tags == [catalog_tags.get((event, ci))]
                     for ci, candidate in enumerate(candidates))
        if not proven:
            if tags:
                refuse('tagged hook ownership cannot be proven for ' + event)
            if any('/plugins/' in str(h.get('command', '')) or
                   'ASHA_HARNESS=codex' in str(h.get('command', ''))
                   for h in group.get('hooks', []) if isinstance(h, dict)):
                refuse('untagged Asha inline ownership is ambiguous')
            continue  # genuinely foreign group: keep every byte and value
        # Assignments beyond the fence make its ownership region ambiguous.
        for k in range(i, j):
            r = tokens[k][2].strip()
            if k >= hi and r and not r.startswith('#'):
                refuse('generated hook extends beyond its managed fence')
        owned.setdefault(event, []).append(group)
    for i, (_, _, raw_token) in enumerate(tokens):
        if raw_token.strip().startswith('# asha:') and i not in accounted_tags:
            refuse('unassociated Asha inline ownership tag')
    for event, groups in hooks.items():
        if event == 'state':
            continue
        proven_groups = list(owned.get(event, []))
        for group in groups:
            if group in proven_groups:
                proven_groups.remove(group)
            elif any('/plugins/' in str(h.get('command', '')) or
                     'ASHA_HARNESS=codex' in str(h.get('command', ''))
                     for h in group['hooks']):
                refuse('unproven dotted/inline or duplicate Asha hook definition')
    features = original.get('features', {})
    if not isinstance(features, dict):
        refuse('features is not a table')
    if 'hooks' in features and not isinstance(features['hooks'], bool):
        refuse('features.hooks must be a boolean')
    for event, groups in hooks.items():
        if event == 'state':
            continue
        validate_groups(event, groups)
    if owned:
        if not installing or not same(owned, desired):
            refuse('legacy inline hooks need update/removal; config.toml is read-only; inspect and migrate manually')
        return 'legacy'
    if fences:
        refuse('managed fence without proven selected hooks requires manual inspection')
    return 'json'

def validate_groups(event, groups):
    if not isinstance(groups, list):
        refuse('hook event is not an array: ' + event)
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
            refuse('malformed hook group: ' + event)
        if 'matcher' in group and not isinstance(group['matcher'], str):
            refuse('malformed matcher: ' + event)
        for hook in group['hooks']:
            if not isinstance(hook, dict) or hook.get('type', 'command') != 'command' or not isinstance(hook.get('command'), str):
                refuse('malformed hook command: ' + event)

def inspect(wanted, catalog, installing, diagnostic=False):
    if destination != os.path.dirname(path) + '/hooks.json' or os.path.basename(path) != 'config.toml':
        refuse('native config and owned hooks paths are incoherent')
    _, raw = capture(path)
    mode = classify(raw, wanted, catalog, installing)
    hook_id, hook_raw = capture(destination)
    manifest_id, manifest_raw = capture(manifest)
    if os.path.lexists(manifest + '.tmp.' + shell_pid):
        refuse('existing generic manifest staging path; inspect interrupted publication')
    record = None
    if manifest_id is not None:
        ledger = json_value(manifest_raw)
        if (not isinstance(ledger, dict) or set(ledger) != {'schema_version', 'harness', 'artifacts'} or
                type(ledger['schema_version']) is not int or ledger['schema_version'] != 1 or
                ledger['harness'] != 'codex' or not isinstance(ledger['artifacts'], list)):
            refuse('invalid Codex generated-artifact manifest')
        seen = set()
        home = os.path.dirname(destination)
        for row in ledger['artifacts']:
            if (not isinstance(row, dict) or set(row) != {'source', 'destination', 'type', 'sha256', 'orphan'} or
                    not canonical(row['source']) or not canonical(row['destination']) or
                    not isinstance(row['type'], str) or type(row['orphan']) is not bool or
                    not isinstance(row['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', row['sha256'])):
                refuse('malformed consumed manifest row')
            dest = row['destination']
            if dest in seen:
                refuse('duplicate/conflicting manifest destination: ' + dest)
            seen.add(dest)
            allowed = ((row['type'] == 'codex-hooks-json' and dest == destination) or
                       (row['type'] == 'codex-command-skill' and dest.startswith(home + '/skills/') and dest.endswith('/SKILL.md')) or
                       (row['type'] == 'codex-agent-toml' and dest.startswith(home + '/agents/') and dest.endswith('.toml')))
            if not allowed:
                refuse('unsafe manifest type/destination: ' + dest)
            parents(dest)  # generic lifecycle must never follow an unsafe parent
            if dest == destination:
                if row['source'] != source or row['orphan']:
                    refuse('hook manifest source/type ownership does not match current adapter')
                record = row
    if hook_id is not None:
        value = json_value(hook_raw)
        if not isinstance(value, dict) or set(value) != {'hooks'} or not isinstance(value['hooks'], dict) or 'state' in value['hooks']:
            refuse('malformed native hooks.json (hooks.state is native-only)')
        for event, groups in value['hooks'].items():
            validate_groups(event, groups)
        if record is None or record['sha256'] != hashlib.sha256(hook_raw).hexdigest():
            refuse('hooks.json is unrecorded or modified; FORCE cannot authorize adoption; inspect ownership manually')
    elif record is not None:
        refuse('recorded hooks.json is missing; inspect interrupted ownership manually')
    if mode == 'legacy' and hook_id is not None:
        refuse('ambiguous inline/JSON Asha hook duplication')
    if mode == 'json' and tomllib.loads(raw.decode('utf-8')).get('hooks', {}).keys() - {'state'}:
        print('WARN: foreign inline hooks coexist with owned JSON; native trust remains user-controlled', file=sys.stderr)
    desired = tomllib.loads(wanted).get('hooks', {})
    result = {'mode': mode, 'hook_identity': hook_id,
            'hook_hash': hashlib.sha256(hook_raw).hexdigest(),
            'manifest_identity': manifest_id,
            'manifest_hash': hashlib.sha256(manifest_raw).hexdigest(),
            'wanted': wanted, 'catalog': catalog,
            'content': json.dumps({'hooks': desired}, indent=2, sort_keys=True) + '\n'}
    if diagnostic:
        # Only read-only doctor evidence includes native bytes. Publication
        # never binds, rewrites or restores a shared-config snapshot.
        result.update(config_text=raw.decode('utf-8'), json_text=hook_raw.decode('utf-8'))
    return result

try:
    if action in ('install', 'uninstall', 'inspect'):
        print(json.dumps(inspect(sys.argv[9], sys.argv[10], action != 'uninstall', action == 'inspect')))
    elif action in ('publish', 'recheck'):
        plan = json_value(sys.argv[9])
        current = inspect(plan['wanted'], plan['catalog'], action == 'publish')
        if current != plan:
            refuse('owned hooks/manifest drift since preflight; inspect before retrying')
        if action == 'publish' and plan['mode'] != 'legacy' and sys.argv[10] != '1':
            content = plan['content'].encode('utf-8')
            if plan['hook_identity'] is None or hashlib.sha256(content).hexdigest() != plan['hook_hash']:
                directory = os.path.dirname(destination)
                os.makedirs(directory, exist_ok=True)
                parents(destination)
                fd, temporary = tempfile.mkstemp(prefix='.asha-hooks-', dir=directory)
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if inspect(plan['wanted'], plan['catalog'], True) != plan:
                        refuse('owned hooks/manifest drift before publication')
                    if plan['hook_identity'] is None:
                        # Atomic no-clobber for absent destinations, including
                        # newly appeared foreign paths at the last syscall.
                        os.link(temporary, destination)
                    else:
                        # Detectable drift refuses. This is NOT arbitrary-writer
                        # compare-and-swap at the final replace syscall.
                        os.replace(temporary, destination)
                finally:
                    if os.path.lexists(temporary):
                        os.unlink(temporary)
        if action == 'publish':
            print('legacy' if plan['mode'] == 'legacy' else hashlib.sha256(plan['content'].encode()).hexdigest())
    else:
        refuse('unknown owned hook action')
except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
    print('ERROR: Codex preservation refused: ' + str(exc), file=sys.stderr)
    sys.exit(4)
PYEOF
}

_codex_prepare_hooks() {
  local action="$1" block="" catalog="" rc
  if [[ "$action" == install || "$action" == inspect ]]; then
    block="$(_codex_build_hook_block)" || { rc=$?; [[ $rc -eq 1 ]] || return "$rc"; }
  fi
  catalog="$(_codex_build_hook_block all)" || { rc=$?; [[ $rc -eq 1 ]] || return "$rc"; }
  _codex_hook_plan "$action" "$block" "$catalog"
}

# Publish through the strict Codex seam; reuse only generic ledger recording.
# No --force adoption and no shared config writer exists in this path.
_codex_publish_hooks() {
  local plan="$1" digest
  digest="$(_codex_hook_plan publish "$plan" "${DRY_RUN:-0}")" || return $?
  [[ "$digest" != legacy ]] || { log "[codex] equivalent legacy inline hooks: no-op"; return 0; }
  [[ ${DRY_RUN:-0} -ne 1 ]] || { say "  EMIT [codex-hooks-json] $CODEX_HOOKS_FILE"; return 0; }
  asha_artifact_record "$MARKET_ROOT/harnesses/codex.sh" "$CODEX_HOOKS_FILE" codex-hooks-json "$digest" || return $?
}

# A standalone sourced call owns a partial manifest cycle. A subshell isolates
# all stage/result/option state from a caller's active, unrelated artifact cycle.
codex_install_hooks() (
  # New generated directories must pass the next install's ownership check,
  # even when the caller uses a group-writable umask. Keep stricter masks.
  umask go-w
  local plan TMPDIR ASHA_ARTIFACT_HARNESS ASHA_ARTIFACT_STAGE
  plan="$(_codex_prepare_hooks install)" || return $?
  [[ "$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["mode"])')" != legacy ]] || return 0
  TMPDIR="$(mktemp -d)" || return $?
  trap 'rc=$?
    if [[ $rc -eq 0 ]]; then
      { rm -f "${ASHA_ARTIFACT_STAGE:-}" && rmdir "$TMPDIR"; } || rc=$?
    else
      printf "WARN: Codex install failed; staging evidence retained at %s\n" "$TMPDIR" >&2
    fi
    exit "$rc"' EXIT
  asha_artifact_begin codex || return $?
  _codex_publish_hooks "$plan" || return $?
  asha_artifact_finalize codex 0 || return $?
)

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

codex_install() (
  umask go-w
  command -v python3 >/dev/null 2>&1 || die "python3 required for Codex install (TOML + frontmatter parsing)" 3

  : "${ABS_MARKET_ROOT:=$(resolve_path "$MARKET_ROOT")}"

  local plan TMPDIR ASHA_ARTIFACT_HARNESS ASHA_ARTIFACT_STAGE
  plan="$(_codex_prepare_hooks install)" || return $?
  TMPDIR="$(mktemp -d)" || return $?
  trap 'rc=$?
    if [[ $rc -eq 0 ]]; then
      { rm -f "${ASHA_ARTIFACT_STAGE:-}" && rmdir "$TMPDIR"; } || rc=$?
    else
      printf "WARN: Codex install failed; staging evidence retained at %s\n" "$TMPDIR" >&2
    fi
    exit "$rc"' EXIT
  asha_artifact_begin codex || return $?
  _codex_publish_hooks "$plan" || return $?
  ensure_dir "$CODEX_SKILLS_DIR" || return $?
  say "[codex] target = $CODEX_HOME"

  _codex_migrate_legacy || return $?

  local plugin_dir ns src_dir kind label
  while IFS=$'\t' read -r src_dir ns kind label; do
    [[ -n "$src_dir" ]] || continue
    say ""
    say "== [codex] $label skills  (ns=$ns) =="
    codex_install_skills "$src_dir" "$ns" "$kind" "$label" || return $?
  done < <(selected_imported_skill_sources)

  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || { echo "WARN: not a plugin dir: $plugin_dir" >&2; continue; }
    if _codex_is_skip_plugin "$plugin_dir"; then
      say ""
      say "== [codex] $plugin_dir  (skipped: Claude-only) =="
      continue
    fi
    ns="$(ns_for "$plugin_dir")" || return $?
    say ""
    say "== [codex] $plugin_dir  (ns=$ns) =="
    codex_install_skills         "$PLUGINS_DIR/$plugin_dir/skills" "$ns" plugin "$plugin_dir" || return $?
    codex_install_agents         "$plugin_dir" "$ns" || return $?
    codex_install_command_skills "$plugin_dir" "$ns" || return $?
  done < <(selected_plugins)

  say ""
  say "== [codex] native rules =="
  codex_install_rules || return $?

  asha_artifact_finalize codex "$([[ -z "${ONLY:-}" ]] && echo 1 || echo 0)" || return $?
)

# ---------------------------------------------------------------------------
# Entry point: codex_uninstall
# ---------------------------------------------------------------------------

codex_uninstall() {
  command -v python3 >/dev/null 2>&1 || die "python3 required for Codex uninstall (TOML validation)" 3
  local hook_plan
  hook_plan="$(_codex_prepare_hooks uninstall)" || return $?
  [[ -d "$CODEX_HOME" ]] || { say "[codex] $CODEX_HOME does not exist; nothing to remove"; CODEX_UNINSTALL_TOTAL=0; return 0; }
  # Legacy generated artifacts retain their separate adoption path. Strict
  # hook proof never authorizes adoption of native JSON or TOML.
  local ownership_manifest
  ownership_manifest="$(asha_artifact_manifest_path codex)"
  if [[ ! -f "$ownership_manifest" ]] && {
       grep -rlq '## Codex harness adapter' "$CODEX_SKILLS_DIR" 2>/dev/null \
       || grep -rlq 'Asha custom agent rendered for OpenAI Codex' "$CODEX_AGENTS_DIR" 2>/dev/null;
     }; then
    die "pre-manifest Codex artifacts detected; run 'asha install codex --force' once, then retry uninstall" 2
  fi

  _codex_hook_plan recheck "$hook_plan" >/dev/null || return $?
  say "[codex] target = $CODEX_HOME"

  local total=0 n
  n="$(asha_artifact_uninstall codex)" || return $?
  if [[ ${DRY_RUN:-0} -ne 1 && ( -e "$CODEX_HOOKS_FILE" || -L "$CODEX_HOOKS_FILE" ) ]]; then
    info "ERROR: Codex owned hooks remained after removal; inspect concurrent drift"
    return 4
  fi
  [[ "$n" -gt 0 ]] && say "[codex] removed $n owned generated artifact(s)"
  total=$((total + n))

  # Skills cleanup: three kinds of asha-installed entries to remove —
  #   1. Whole-dir symlinks (plugin skills) — remove via remove_symlinks_under
  #   2. SKILL.md symlinks inside our created dirs (legacy command-skills,
  #      pre-frontmatter-strip era) — same scan handles them
  #   3. Generated SKILL.md files inside our created dirs (current command-
  #      skills with stripped frontmatter) — match by source name lookup
  if [[ -d "$CODEX_SKILLS_DIR" ]]; then
    n="$(remove_symlinks_under "$CODEX_SKILLS_DIR" 2)" || return $?
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
    n="$(remove_symlinks_under "$CODEX_AGENTS_DIR" 1)" || return $?
    [[ "$n" -gt 0 ]] && say "[codex] removed $n agent symlink(s) from $CODEX_AGENTS_DIR"
    total=$((total + n))

  fi

  # Legacy: any remaining prompts dir entries from pre-Step-7 installs
  if [[ -d "$CODEX_LEGACY_PROMPTS_DIR" ]]; then
    n="$(remove_symlinks_under "$CODEX_LEGACY_PROMPTS_DIR" 1)" || return $?
    [[ "$n" -gt 0 ]] && say "[codex] removed $n legacy prompt symlink(s) from $CODEX_LEGACY_PROMPTS_DIR"
    total=$((total + n))
    # `|| true`: a failed rmdir at the tail of an && list aborts under set -e.
    [[ $DRY_RUN -eq 0 && ! -L "$CODEX_LEGACY_PROMPTS_DIR" && -z "$(ls -A "$CODEX_LEGACY_PROMPTS_DIR")" ]] && rmdir "$CODEX_LEGACY_PROMPTS_DIR" || true
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
  if [[ -f "${ASHA_HOME:-$HOME/.asha}/cache/instructions.md" || -f "${ASHA_HOME:-$HOME/.asha}/cache/instructions-codex.md" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[codex] would remove ~/.asha/cache/instructions.md + instructions-codex.md"
    else
      rm -f "${ASHA_HOME:-$HOME/.asha}/cache/instructions.md" "${ASHA_HOME:-$HOME/.asha}/cache/instructions-codex.md"
      # `|| true` is load-bearing: the cache dir usually still holds OTHER
      # harnesses' files (codex runs before copilot in `--target all`), so this
      # rmdir fails — and with stderr silenced, an unguarded failure under the
      # shim's `set -e` killed the whole uninstall here, stranding every
      # harness after codex (issue #4, 2026-07-01 relocation).
      rmdir "${ASHA_HOME:-$HOME/.asha}/cache" 2>/dev/null || true
      log "[codex] removed cached identity"
    fi
  fi

  # Read indirectly by lib/uninstall.sh after this sourced function returns.
  # shellcheck disable=SC2034
  CODEX_UNINSTALL_TOTAL=$total
}
