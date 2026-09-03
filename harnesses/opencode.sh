#!/usr/bin/env bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
# Asha -> OpenCode stable-v1 install/uninstall adapter.
#
# Native surfaces (OpenCode >=1.15.11):
#   skills/<declared-name>/       native agent skills
#   commands/<name>.md            native slash commands
#   agents/<namespace>-<name>.md native Markdown subagents
#   plugins/asha.js               hooks, policy bridge, context, recovery lifecycle
#
# Identity is launch-scoped in bin/asha. The installed plugin is persona-free.

OPENCODE_HOME="$(asha_harness_home opencode)"
OPENCODE_SKILLS_DIR="$OPENCODE_HOME/skills"
OPENCODE_COMMANDS_DIR="$OPENCODE_HOME/commands"
OPENCODE_AGENTS_DIR="$OPENCODE_HOME/agents"
OPENCODE_PLUGINS_DIR="$OPENCODE_HOME/plugins"
OPENCODE_PLUGIN_FILE="$OPENCODE_PLUGINS_DIR/asha.js"

_opencode_field() {
  local md="$1" key="$2"
  python3 - "$md" "$key" <<'PYEOF'
import re, sys
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

_opencode_version_supported() {
  local actual="$1" minimum="$2"
  python3 - "$actual" "$minimum" <<'PYEOF'
import re, sys
def version(value):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise SystemExit(2)
    return tuple(map(int, match.groups()))
raise SystemExit(0 if version(sys.argv[1]) >= version(sys.argv[2]) else 1)
PYEOF
}

opencode_check_version() {
  local cmd minimum actual rc=0
  cmd="$(asha_harness_executable opencode)"
  minimum="$(asha_opencode_min_version)"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "WARN: OpenCode executable '$cmd' not found; installing artifacts for later use" >&2
    return 0
  fi
  actual="$("$cmd" --version 2>/dev/null | head -1 || true)"
  _opencode_version_supported "$actual" "$minimum" || rc=$?
  if [[ $rc -eq 2 ]]; then
    die "cannot parse OpenCode version from: ${actual:-<empty>}" 3
  fi
  [[ $rc -eq 0 ]] || die "OpenCode $actual is unsupported; Asha requires >=$minimum" 3
}

opencode_install_skills() {
  local src_dir="$1" ns="$2" kind="${3:-plugin}" skill declared dest_name
  [[ -d "$src_dir" ]] || return 0
  validate_skill_source "$src_dir" "$kind"
  while IFS= read -r skill; do
    [[ -n "$skill" ]] || continue
    [[ -f "$skill/SKILL.md" ]] || continue
    declared="$(_opencode_field "$skill/SKILL.md" name)"
    if [[ "$kind" == imported ]]; then
      dest_name="${ns}-$(basename "$skill")"
      prepare_imported_skill_adapter "${skill%/}" "$dest_name"
      mklink_imported_skill "${skill%/}" "$ASHA_IMPORTED_SKILL_ADAPTER" \
        "$OPENCODE_SKILLS_DIR/$dest_name" "opencode-skill"
      continue
    else
      dest_name="${declared:-${ns}-$(basename "$skill")}"
    fi
    if ! _opencode_valid_name "$dest_name"; then
      echo "WARN: invalid OpenCode skill name '$dest_name' in $skill/SKILL.md; skipping" >&2
      continue
    fi
    mklink "${skill%/}" "$OPENCODE_SKILLS_DIR/$dest_name" "opencode-skill"
  done < <(skill_dirs_from_source "$src_dir" "$kind")
}

_opencode_emit_command() {
  local src="$1" dest="$2" mode="${3:-install}" content prepared
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
print("This command is rendered from Asha's shared source. Map Claude-specific "
      "tool names to OpenCode tools and use native subagents where named.")
print()
print(body.lstrip(), end="")
PYEOF
)"
  prepared="$(mktemp)"
  printf '%s\n' "$content" >"$prepared"
  if [[ "$mode" == render ]]; then
    cat "$prepared" >"$dest"
  else
    asha_artifact_install_prepared opencode "$src" "$dest" opencode-command "$prepared"
  fi
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
  local src="$1" dest="$2" src_dir="$3" ns="$4" mode="${5:-install}" content prepared
  content="$(python3 - "$src" "$src_dir" "$ns" <<'PYEOF'
import json, pathlib, re, sys
src, src_dir, namespace = sys.argv[1:]
text = open(src, encoding="utf-8").read()
description = "Asha subagent"
body = text
if text.startswith("---\n"):
    end = text.find("\n---\n", 4)
    if end >= 0:
        fm, body = text[4:end], text[end + 5:]
        m = re.search(r"^description\s*:\s*(.+)$", fm, re.M)
        if m:
            description = m.group(1).strip().strip("\"'")

# OpenCode agent identifiers are hyphen-only, whilst some Claude-native source
# agents use a colon family (for example character:template). Render exact
# references to sibling agents through the same namespace/name mapping so the
# generated orchestrator invokes an agent that actually exists.
for sibling in pathlib.Path(src_dir).glob("*.md"):
    sibling_text = sibling.read_text(encoding="utf-8")
    match = re.search(r"^name\s*:\s*(.+)$", sibling_text, re.M)
    declared = match.group(1).strip().strip("\"'") if match else sibling.stem
    rendered = f"{namespace}-{declared.replace(':', '-')}"
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", rendered):
        body = body.replace(f'"{declared}"', f'"{rendered}"')
        body = body.replace(f'`{declared}`', f'`{rendered}`')
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
  if [[ "$mode" == render ]]; then
    cat "$prepared" >"$dest"
  else
    asha_artifact_install_prepared opencode "$src" "$dest" opencode-agent "$prepared"
  fi
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
    declared="${declared//:/-}"
    dest_name="${ns}-${declared:-$(basename "$agent" .md)}"
    if ! _opencode_valid_name "$dest_name"; then
      echo "WARN: invalid OpenCode agent name '$dest_name' in $agent; skipping" >&2
      continue
    fi
    _opencode_emit_agent "$agent" "$OPENCODE_AGENTS_DIR/$dest_name.md" "$src_dir" "$ns"
  done
}

opencode_install_plugin() {
  local mode="$1" destination="$2"
  local handlers="$PLUGINS_DIR/session/hooks/handlers"
  local adapter="$handlers/opencode-policy-adapter.sh"
  local start="$handlers/session-start.sh"
  local prompt="$handlers/user-prompt-submit.sh"
  local post="$handlers/post-tool-use.sh"
  local end="$handlers/session-end.sh"
  local verify="$handlers/verify-pass-complete.sh"
  [[ -x "$adapter" ]] || { echo "WARN: OpenCode policy adapter missing: $adapter" >&2; return 0; }
  [[ -x "$verify" ]] || { echo "WARN: OpenCode verification handler missing: $verify" >&2; return 0; }

  local content prepared
  content="$(python3 - "$adapter" "$start" "$prompt" "$post" "$end" "$verify" <<'PYEOF'
import json, sys
adapter, start, prompt, post, end, verify = map(json.dumps, sys.argv[1:])
print('import { spawnSync } from "node:child_process"')
print('')
print('export const AshaPlugin = async ({ directory }) => {')
print('  let latestSessionID = ""')
print('  const started = new Set()')
print('  const childSessions = new Set()')
print('  const pending = new Map()')
print('  const envFor = (sid) => ({ ...process.env, OPENCODE: "1", OPENCODE_SESSION_ID: sid || "", ASHA_HARNESS: "opencode", ASHA_SESSION_ID: sid || "", CLAUDE_PROJECT_DIR: directory })')
print('  const remember = (sid) => { if (sid) latestSessionID = sid }')
print('  const append = (sid, value) => {')
print('    value = (value || "").trim()')
print('    if (!sid || !value || value === "{}") return')
print('    value = value.replace(/\\n?\\{\\}\\s*$/, "").trim()')
print('    if (!value) return')
print('    pending.set(sid, [pending.get(sid), value].filter(Boolean).join("\\n\\n"))')
print('  }')
print('  const run = (command, event, payload, sid) => {')
print('    try {')
print('      const result = spawnSync(command, event ? [event] : [], { input: JSON.stringify(payload || {}), encoding: "utf8", timeout: 15000, env: envFor(sid) })')
print('      return { status: result.status, stdout: result.stdout || "", stderr: result.stderr || "" }')
print('    } catch (_) { return { status: 0, stdout: "", stderr: "" } }')
print('  }')
print('  const ensureStarted = (sid) => {')
print('    if (!sid || childSessions.has(sid) || started.has(sid)) return')
print('    started.add(sid)')
print(f'    append(sid, run({start}, "", {{ session_id: sid, cwd: directory, reason: "opencode-session-created" }}, sid).stdout)')
print('  }')
print('  return {')
print('    "chat.message": async (input, output) => {')
print('      const sid = input.sessionID || ""; remember(sid); ensureStarted(sid)')
print('      const prompt = (output.parts || []).filter((p) => p && p.type === "text").map((p) => p.text || "").join("\\n")')
print(f'      append(sid, run({prompt}, "", {{ prompt, session_id: sid, cwd: directory }}, sid).stdout)')
print('    },')
print('    "shell.env": async (input, output) => {')
print('      const sid = input.sessionID || latestSessionID || ""; remember(sid); ensureStarted(sid)')
print('      output.env.ASHA_HARNESS = "opencode"')
print('      output.env.OPENCODE = "1"')
print('      if (sid) { output.env.ASHA_SESSION_ID = sid; output.env.OPENCODE_SESSION_ID = sid }')
print('      output.env.CLAUDE_PROJECT_DIR = input.cwd || directory')
print('    },')
print('    "tool.execute.before": async (input, output) => {')
print('      const sid = input.sessionID || ""; remember(sid); ensureStarted(sid)')
print('      const payload = { session_id: sid, cwd: directory, tool_name: input.tool || "", tool_input: output.args || {} }')
print(f'      const result = run({adapter}, "", payload, sid)')
print('      if (result.status === 0 && result.stderr.trim()) { append(sid, result.stderr.trim()); process.stderr.write(result.stderr) }')
print('      if (result.status === 2) throw new Error((result.stderr || "Blocked by Asha policy").trim())')
print('    },')
print('    "tool.execute.after": async (input) => {')
print('      const sid = input.sessionID || ""; remember(sid)')
print('      const payload = { hook_event_name: "PostToolUse", session_id: sid, cwd: directory, tool_name: input.tool || "", tool_input: input.args || {} }')
print(f'      append(sid, run({post}, "", payload, sid).stdout)')
print('    },')
print('    "experimental.chat.system.transform": async (input, output) => {')
print('      const sid = input.sessionID || latestSessionID || ""; remember(sid); ensureStarted(sid)')
print('      const text = pending.get(sid) || ""')
print('      if (text) { output.system.push(text); pending.delete(sid) }')
print('    },')
print('    event: async ({ event }) => {')
print('      if (event && event.type === "session.created") {')
print('        const info = event.properties?.info || event.properties || {}')
print('        const sid = info.id || info.sessionID || ""; remember(sid)')
print('        if (info.parentID || info.parentId) childSessions.add(sid)')
print('        ensureStarted(sid)')
print('      } else if (event && event.type === "session.idle") {')
print('        const info = event.properties?.info || event.properties || {}')
print('        const sid = info.id || info.sessionID || latestSessionID || ""; remember(sid)')
print(f'        append(sid, run({verify}, "", {{ session_id: sid, cwd: directory }}, sid).stdout)')
print('      }')
print('    },')
print('    dispose: async () => {')
print('      if (!latestSessionID) return')
print(f'      run({end}, "", {{ session_id: latestSessionID, cwd: directory, reason: "dispose" }}, latestSessionID)')
print('    },')
print('  }')
print('}')
PYEOF
)"
  prepared="$(mktemp)"
  printf '%s\n' "$content" >"$prepared"
  if [[ "$mode" == render ]]; then
    cat "$prepared" >"$destination"
  else
    asha_artifact_install_prepared opencode "$adapter" "$destination" opencode-plugin "$prepared"
  fi
  rm -f "$prepared"
}

opencode_install() {
  command -v python3 >/dev/null 2>&1 || die "python3 required for OpenCode install" 3
  opencode_check_version
  ensure_dir "$OPENCODE_SKILLS_DIR"
  asha_artifact_begin opencode
  say "[opencode] target = $OPENCODE_HOME"
  local plugin_dir ns src_dir kind label
  while IFS=$'\t' read -r src_dir ns kind label; do
    [[ -n "$src_dir" ]] || continue
    say ""
    say "== [opencode] $label skills  (ns=$ns) =="
    opencode_install_skills "$src_dir" "$ns" "$kind"
  done < <(selected_imported_skill_sources)
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || { echo "WARN: not a plugin dir: $plugin_dir" >&2; continue; }
    ns="$(ns_for "$plugin_dir")"
    say ""
    say "== [opencode] $plugin_dir  (ns=$ns) =="
    opencode_install_skills "$PLUGINS_DIR/$plugin_dir/skills" "$ns" plugin
    opencode_install_commands "$plugin_dir" "$ns"
    opencode_install_agents "$plugin_dir" "$ns"
  done < <(selected_plugins)
  say ""
  say "== [opencode] integration plugin =="
  opencode_install_plugin install "$OPENCODE_PLUGIN_FILE"
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
  # Read indirectly by lib/uninstall.sh after this sourced function returns.
  # shellcheck disable=SC2034
  OPENCODE_UNINSTALL_TOTAL=$total
  say "[opencode] removed $total managed artifact(s)"
}
