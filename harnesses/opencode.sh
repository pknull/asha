#!/usr/bin/env bash
# harnesses/opencode.sh — OpenCode install/uninstall adapter.
#
# Native surfaces:
#   skills/<declared-name>/       plugin skills
#   command/<name>.md             native slash commands
#   agent/<namespace>-<name>.md   native subagents
#   plugin/asha-guardrails.js     tool.execute.before policy bridge
#
# Identity is intentionally absent here. bin/asha injects the identity and
# operational layers through a launch-scoped OPENCODE_CONFIG_DIR overlay, so a
# plain `opencode` launch remains persona-free.

OPENCODE_HOME="$(asha_harness_home opencode)"
OPENCODE_SKILLS_DIR="$OPENCODE_HOME/skills"
OPENCODE_COMMANDS_DIR="$OPENCODE_HOME/command"
OPENCODE_AGENTS_DIR="$OPENCODE_HOME/agent"
OPENCODE_PLUGINS_DIR="$OPENCODE_HOME/plugin"
OPENCODE_GUARDRAILS_FILE="$OPENCODE_PLUGINS_DIR/asha-guardrails.js"

_opencode_field() {
  local md="$1" key="$2"
  python3 - "$md" "$key" <<'PYEOF'
import json, re, sys
text = open(sys.argv[1], encoding="utf-8").read()
if not text.startswith("---\n"):
    raise SystemExit(0)
end = text.find("\n---\n", 4)
if end < 0:
    raise SystemExit(0)
m = re.search(rf"^{re.escape(sys.argv[2])}\s*:\s*(.+)$", text[4:end], re.M)
if m:
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    print(value)
PYEOF
}

_opencode_valid_name() {
  [[ "$1" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]
}

opencode_install_skills() {
  local plugin_dir="$1" ns="$2" src_dir skill declared dest_name
  src_dir="$PLUGINS_DIR/$plugin_dir/skills"
  [[ -d "$src_dir" ]] || return 0
  for skill in "$src_dir"/*/; do
    [[ -f "$skill/SKILL.md" ]] || continue
    declared="$(_opencode_field "$skill/SKILL.md" name)"
    dest_name="${declared:-${ns}-$(basename "$skill")}" 
    if ! _opencode_valid_name "$dest_name"; then
      echo "WARN: invalid OpenCode skill name '$dest_name' in $skill/SKILL.md; skipping" >&2
      continue
    fi
    mklink "${skill%/}" "$OPENCODE_SKILLS_DIR/$dest_name" "opencode-skill"
  done
}

_opencode_emit_command() {
  local src="$1" dest="$2" content prepared
  content="$(python3 - "$src" <<'PYEOF'
import json, re, sys
text = open(sys.argv[1], encoding="utf-8").read()
description = "Asha command"
body = text
if text.startswith("---\n"):
    end = text.find("\n---\n", 4)
    if end >= 0:
        fm, body = text[4:end], text[end + 5:]
        m = re.search(r"^description\s*:\s*(.+)$", fm, re.M)
        if m:
            description = m.group(1).strip().strip("\"'")
print("---")
print("description: " + json.dumps(description))
print("---")
print("## OpenCode harness adapter")
print()
print("This command was rendered from Asha's shared source. Map Claude-specific "
      "tool names to OpenCode tools and use native subagents where named.")
print()
print(body.lstrip(), end="")
PYEOF
)"
  prepared="$(mktemp)"
  printf '%s\n' "$content" >"$prepared"
  asha_artifact_install_prepared opencode "$src" "$dest" opencode-command "$prepared"
  rm -f "$prepared"
}

opencode_install_commands() {
  local plugin_dir="$1" ns="$2" src_dir cmd declared dest_name
  src_dir="$PLUGINS_DIR/$plugin_dir/commands"
  [[ -d "$src_dir" ]] || return 0
  ensure_dir "$OPENCODE_COMMANDS_DIR"
  for cmd in "$src_dir"/*.md; do
    [[ -f "$cmd" ]] || continue
    declared="$(_opencode_field "$cmd" name)"
    dest_name="${declared:-${ns}-$(basename "$cmd" .md)}"
    if ! _opencode_valid_name "$dest_name"; then
      echo "WARN: invalid OpenCode command name '$dest_name' in $cmd; skipping" >&2
      continue
    fi
    _opencode_emit_command "$cmd" "$OPENCODE_COMMANDS_DIR/$dest_name.md"
  done
}

_opencode_emit_agent() {
  local src="$1" dest="$2" content prepared
  content="$(python3 - "$src" <<'PYEOF'
import json, re, sys
text = open(sys.argv[1], encoding="utf-8").read()
description = "Asha subagent"
body = text
if text.startswith("---\n"):
    end = text.find("\n---\n", 4)
    if end >= 0:
        fm, body = text[4:end], text[end + 5:]
        m = re.search(r"^description\s*:\s*(.+)$", fm, re.M)
        if m:
            description = m.group(1).strip().strip("\"'")
print("---")
print("description: " + json.dumps(description))
print("mode: subagent")
print("---")
print("You are an Asha agent rendered for OpenCode. Follow the source role below. "
      "Map harness-specific tools to the closest OpenCode capability and state "
      "any missing capability rather than simulating it.")
print()
print(body.lstrip(), end="")
PYEOF
)"
  prepared="$(mktemp)"
  printf '%s\n' "$content" >"$prepared"
  asha_artifact_install_prepared opencode "$src" "$dest" opencode-agent "$prepared"
  rm -f "$prepared"
}

opencode_install_agents() {
  local plugin_dir="$1" ns="$2" src_dir agent declared dest_name
  src_dir="$PLUGINS_DIR/$plugin_dir/agents"
  [[ -d "$src_dir" ]] || return 0
  ensure_dir "$OPENCODE_AGENTS_DIR"
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] || continue
    declared="$(_opencode_field "$agent" name)"
    dest_name="${ns}-${declared:-$(basename "$agent" .md)}"
    if ! _opencode_valid_name "$dest_name"; then
      echo "WARN: invalid OpenCode agent name '$dest_name' in $agent; skipping" >&2
      continue
    fi
    _opencode_emit_agent "$agent" "$OPENCODE_AGENTS_DIR/$dest_name.md"
  done
}

opencode_install_guardrails() {
  local adapter="$PLUGINS_DIR/session/hooks/handlers/opencode-policy-adapter.sh"
  [[ -x "$adapter" ]] || {
    echo "WARN: OpenCode policy adapter missing or not executable: $adapter" >&2
    return 0
  }
  local abs_adapter content prepared
  abs_adapter="$(resolve_path "$adapter")"
  content="$(python3 - "$abs_adapter" <<'PYEOF'
import json, sys
adapter = json.dumps(sys.argv[1])
print('import { spawnSync } from "node:child_process"')
print('')
print('export const AshaGuardrails = async () => ({')
print('  "tool.execute.before": async (input, output) => {')
print('    const payload = JSON.stringify({')
print('      session_id: input.sessionID || input.sessionId || "",')
print('      tool_name: input.tool || input.toolName || "",')
print('      tool_input: output.args || output.input || {},')
print('    })')
print(f'    const result = spawnSync({adapter}, [], {{ input: payload, encoding: "utf8", timeout: 15000, env: {{ ...process.env, ASHA_HARNESS: "opencode" }} }})')
print('    if (result.status === 2) {')
print('      throw new Error((result.stderr || "Blocked by Asha policy").trim())')
print('    }')
print('  },')
print('})')
PYEOF
)"
  prepared="$(mktemp)"
  printf '%s\n' "$content" >"$prepared"
  asha_artifact_install_prepared opencode "$adapter" "$OPENCODE_GUARDRAILS_FILE" opencode-guardrails "$prepared"
  rm -f "$prepared"
}

opencode_install() {
  command -v python3 >/dev/null 2>&1 || die "python3 required for OpenCode install" 3
  ensure_dir "$OPENCODE_SKILLS_DIR"
  asha_artifact_begin opencode
  say "[opencode] target = $OPENCODE_HOME"
  local plugin_dir ns
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || { echo "WARN: not a plugin dir: $plugin_dir" >&2; continue; }
    ns="$(ns_for "$plugin_dir")"
    say ""
    say "== [opencode] $plugin_dir  (ns=$ns) =="
    opencode_install_skills "$plugin_dir" "$ns"
    opencode_install_commands "$plugin_dir" "$ns"
    opencode_install_agents "$plugin_dir" "$ns"
  done < <(selected_plugins)
  say ""
  say "== [opencode] guardrails =="
  opencode_install_guardrails
  asha_artifact_finalize opencode "$([[ -z "${ONLY:-}" ]] && echo 1 || echo 0)"
}

opencode_uninstall() {
  [[ -d "$OPENCODE_HOME" ]] || { say "[opencode] $OPENCODE_HOME does not exist; nothing to remove"; OPENCODE_UNINSTALL_TOTAL=0; return 0; }
  local total=0 n=0

  if [[ -d "$OPENCODE_SKILLS_DIR" ]]; then
    n="$(remove_symlinks_under "$OPENCODE_SKILLS_DIR" 2)"
    total=$((total + n))
  fi

  n="$(asha_artifact_uninstall opencode)"
  total=$((total + n))
  OPENCODE_UNINSTALL_TOTAL=$total
  say "[opencode] removed $total managed artifact(s)"
}
