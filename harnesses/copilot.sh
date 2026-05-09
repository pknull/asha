#!/usr/bin/env bash
# Asha → GitHub Copilot harness adapter.
#
# Mirrors harnesses/codex.sh. Key divergences:
#   - $COPILOT_HOME defaults to ~/.copilot (matches Codex/Claude pattern)
#   - Hook config is JSON (~/.copilot/hooks/hooks.json), not TOML
#   - Six lifecycle events use camelCase (sessionStart, preToolUse, ...)
#     vs Claude's PascalCase (SessionStart, PreToolUse, ...)
#   - Slash commands fold into skills (codex pattern)
#
# UNVERIFIED ASSUMPTIONS (test against live Copilot CLI):
#   1. Hook config location: ~/.copilot/hooks/hooks.json (Q1, defaulted user-scope)
#   2. Stop → sessionEnd event mapping (Claude has both Stop and SessionEnd)
#   3. PermissionRequest events have no Copilot analog (currently dropped + warned)
#   4. Hook stdin/stdout JSON contract field names (e.g. tool_name vs toolName)
#   5. Veto semantics — exit-code-2 vs {decision:"block"} return payload
#   6. Agent files: using bare .md extension; conventional Copilot is .agent.md
#   7. ${CLAUDE_PLUGIN_ROOT} substitution semantics (assumed identical to Claude)
#
# When verification happens, the seam to update is _copilot_translate_event,
# the JSON-shape jq filter in copilot_install_hooks, and (for veto) any future
# stdin/stdout shim layer between Copilot and existing hook scripts.
#
# Sourced by ../install.sh and ../uninstall.sh. Expects globals from the
# dispatcher: MARKET_ROOT, PLUGINS_DIR, NAMESPACES_FILE, DRY_RUN, FORCE,
# VERBOSE, ONLY, ABS_MARKET_ROOT (uninstall only).
#
# And these helpers (defined in the dispatcher):
#   die, log, say, ensure_dir, mklink, ns_for, selected_plugins, info

COPILOT_HOME="${COPILOT_HOME:-$HOME/.copilot}"
COPILOT_SKILLS_DIR="$COPILOT_HOME/skills"
COPILOT_AGENTS_DIR="$COPILOT_HOME/agents"
COPILOT_HOOKS_FILE="$COPILOT_HOME/hooks/hooks.json"
# Referenced for forward-compat; this implementation does NOT manage MCP.
COPILOT_MCP_FILE="$COPILOT_HOME/mcp-config.json"

# Events Copilot is assumed to support (camelCase). UNVERIFIED — see header.
_COPILOT_EVENTS=(sessionStart sessionEnd userPromptSubmitted preToolUse postToolUse errorOccurred)
_COPILOT_SKIP_PLUGINS=(output-styles)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_copilot_is_event() {
  local e="$1" ev
  for ev in "${_COPILOT_EVENTS[@]}"; do [[ "$e" == "$ev" ]] && return 0; done
  return 1
}

_copilot_is_skip_plugin() {
  local p="$1" sp
  for sp in "${_COPILOT_SKIP_PLUGINS[@]}"; do [[ "$p" == "$sp" ]] && return 0; done
  return 1
}

# Translate a Claude PascalCase event name to its Copilot camelCase equivalent.
# Echoes the translated name on stdout, or empty string if no mapping exists
# (caller should warn + drop the entry).
#
# UNVERIFIED MAPPINGS:
#   - Stop → sessionEnd (Claude has both Stop and SessionEnd; both currently
#     fold into the single Copilot sessionEnd event)
#   - PermissionRequest → (dropped — no known Copilot analog)
_copilot_translate_event() {
  case "$1" in
    SessionStart)      echo "sessionStart" ;;
    SessionEnd)        echo "sessionEnd" ;;
    Stop)              echo "sessionEnd" ;;   # UNVERIFIED — see header
    UserPromptSubmit)  echo "userPromptSubmitted" ;;
    PreToolUse)        echo "preToolUse" ;;
    PostToolUse)       echo "postToolUse" ;;
    ErrorOccurred)     echo "errorOccurred" ;;
    PermissionRequest) echo "" ;;             # UNVERIFIED — no Copilot analog
    *)                 echo "" ;;
  esac
}

# Atomic write to hooks.json, validated by jq re-parse.
_copilot_atomic_write_hooks() {
  local content="$1"
  local tmp="$COPILOT_HOOKS_FILE.tmp.$$"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "would write $COPILOT_HOOKS_FILE ($(printf '%s' "$content" | wc -c) bytes)"
    return 0
  fi
  ensure_dir "$(dirname "$COPILOT_HOOKS_FILE")"
  printf '%s' "$content" > "$tmp"
  jq empty < "$tmp" >/dev/null 2>&1 \
    || { rm -f "$tmp"; die "hooks.json would be invalid JSON after write" 4; }
  mv "$tmp" "$COPILOT_HOOKS_FILE"
}

_copilot_backup_done=0
_copilot_backup_hooks_once() {
  [[ $DRY_RUN -eq 1 ]] && return 0
  [[ $_copilot_backup_done -eq 1 ]] && return 0
  [[ -f "$COPILOT_HOOKS_FILE" ]] || { _copilot_backup_done=1; return 0; }
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local bkp="$COPILOT_HOOKS_FILE.bak-$stamp"
  cp -p "$COPILOT_HOOKS_FILE" "$bkp"
  say "backed up hooks.json -> $bkp"
  _copilot_backup_done=1
}

# Extract the `name:` value from a YAML frontmatter file. Echoes the name
# (or empty string if not present). Looks at the first frontmatter block only.
_copilot_skill_name_from_md() {
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
copilot_install_skills() {
  local plugin_dir="$1" ns="$2"
  _copilot_is_skip_plugin "$plugin_dir" && return 0
  local src_dir="$PLUGINS_DIR/$plugin_dir/skills"
  [[ -d "$src_dir" ]] || return 0

  local skill
  for skill in "$src_dir"/*/; do
    [[ -d "$skill" ]] || continue
    local skill_name; skill_name="$(basename "$skill")"
    [[ -f "$skill/SKILL.md" ]] || { log "skip skill (no SKILL.md): $skill"; continue; }

    # Prefer the SKILL.md's name field; fall back to <ns>-<dir-name>.
    local declared_name
    declared_name="$(_copilot_skill_name_from_md "$skill/SKILL.md")"
    local dest_name="${declared_name:-${ns}-${skill_name}}"

    mklink "${skill%/}" "$COPILOT_SKILLS_DIR/${dest_name}" "copilot-skill"
  done
}

# Install command MDs as Copilot skills. Mirrors codex's command-skill emission:
# we generate a SKILL.md with Claude-specific frontmatter keys stripped so
# Copilot's loader does not reject them. The generated file is a content-mode
# duplicate of the source body; drift-check verifies freshness via mtime.
#
# Source command MD frontmatter retained: name, description.
# Stripped: argument-hint, allowed-tools (anything else specifically Claude
# can be added to KEYS_TO_DROP in _copilot_emit_command_skill).
copilot_install_command_skills() {
  local plugin_dir="$1" ns="$2"
  _copilot_is_skip_plugin "$plugin_dir" && return 0
  local src_dir="$PLUGINS_DIR/$plugin_dir/commands"
  [[ -d "$src_dir" ]] || return 0

  local cmd
  for cmd in "$src_dir"/*.md; do
    [[ -f "$cmd" ]] || continue

    local declared_name
    declared_name="$(_copilot_skill_name_from_md "$cmd")"
    if [[ -z "$declared_name" ]]; then
      echo "WARN: command MD missing name: frontmatter; skipping for copilot: $cmd" >&2
      continue
    fi

    local skill_dir="$COPILOT_SKILLS_DIR/$declared_name"

    # Collision guard: if the skill dir is already a symlink, a plugin skill
    # claimed this name first. Skip.
    if [[ -L "$skill_dir" ]]; then
      log "[copilot] skip command-skill '$declared_name' (plugin skill already claims this name)"
      continue
    fi

    ensure_dir "$skill_dir"
    _copilot_emit_command_skill "$cmd" "$skill_dir/SKILL.md"
  done
}

# Generate a Copilot-clean SKILL.md from a Claude command MD. Strips the keys
# Copilot's parser is assumed to reject (argument-hint, allowed-tools) plus any
# other keys identified as non-portable. Idempotent (only writes on diff).
_copilot_emit_command_skill() {
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

  # Idempotent write
  if [[ -f "$dest" ]]; then
    local current; current="$(cat "$dest")"
    if [[ "$current" == "$content" ]]; then
      log "[copilot] command-skill unchanged: $dest"
      return 0
    fi
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    say "  EMIT [copilot-command-skill]  $src -> $dest"
  else
    printf '%s' "$content" > "$dest"
    log "emitted [copilot-command-skill]: $dest (from $src)"
  fi
}

# Install agent MD files. Best-effort symlinking — Copilot either picks them up
# or ignores them. Multi-agent YAML schema is unverified.
#
# TODO: Copilot agent files conventionally use `.agent.md` extension; deferred
# until verified — currently using bare `.md`.
copilot_install_agents() {
  local plugin_dir="$1" ns="$2"
  _copilot_is_skip_plugin "$plugin_dir" && return 0
  local src_dir="$PLUGINS_DIR/$plugin_dir/agents"
  [[ -d "$src_dir" ]] || return 0

  local agent has=0
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] && { has=1; break; }
  done
  [[ $has -eq 1 ]] || return 0

  ensure_dir "$COPILOT_AGENTS_DIR"
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] || continue
    local agent_name; agent_name="$(basename "$agent")"
    mklink "$agent" "$COPILOT_AGENTS_DIR/${ns}-${agent_name}" "copilot-agent"
  done
}

# ---------------------------------------------------------------------------
# Hooks (JSON emission, atomic, source-tagged)
# ---------------------------------------------------------------------------

# Walk one plugin's hooks.json and emit a JSON object {<event>: [groups...]}
# with:
#   - Claude PascalCase events translated to Copilot camelCase
#   - ${CLAUDE_PLUGIN_ROOT} placeholders resolved to the plugin's absolute path
#   - each hook entry tagged with "source": "asha:<ns>"
#   - dropped events (no Copilot analog) warned to stderr
#
# Echoes the JSON object on stdout (empty object if nothing emitted).
_copilot_emit_hooks_for_plugin() {
  local abs_root="$1" hooks_json="$2" ns="$3"
  PYTHONIOENCODING=utf-8 python3 - "$abs_root" "$hooks_json" "$ns" <<'PYEOF'
import json, sys

abs_root, hooks_json, ns = sys.argv[1], sys.argv[2], sys.argv[3]

# UNVERIFIED — Stop folds into sessionEnd; PermissionRequest is dropped.
EVENT_MAP = {
    "SessionStart":      "sessionStart",
    "SessionEnd":        "sessionEnd",
    "Stop":              "sessionEnd",
    "UserPromptSubmit":  "userPromptSubmitted",
    "PreToolUse":        "preToolUse",
    "PostToolUse":       "postToolUse",
    "ErrorOccurred":     "errorOccurred",
}
DROP = {"PermissionRequest"}

source_tag = f"asha:{ns}"

def resolve_command(cmd):
    return cmd.replace("${CLAUDE_PLUGIN_ROOT}", abs_root)

with open(hooks_json) as f:
    data = json.load(f)

events = (data or {}).get("hooks") or {}
out = {}
dropped = []
for event, groups in events.items():
    if event in DROP:
        dropped.append(event)
        continue
    target = EVENT_MAP.get(event)
    if not target:
        dropped.append(event)
        continue
    if not isinstance(groups, list):
        continue
    new_groups = []
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        new_grp = {}
        if "matcher" in grp:
            new_grp["matcher"] = grp["matcher"]
        new_hooks = []
        for h in grp.get("hooks", []) or []:
            if not isinstance(h, dict) or h.get("type") != "command":
                continue
            cmd = resolve_command(h.get("command", ""))
            if not cmd:
                continue
            entry = dict(h)
            entry["command"] = cmd
            entry["source"] = source_tag
            new_hooks.append(entry)
        if new_hooks:
            new_grp["hooks"] = new_hooks
            new_groups.append(new_grp)
    if new_groups:
        out.setdefault(target, []).extend(new_groups)

for e in dropped:
    sys.stderr.write(f"  WARN: dropped {ns}/{e} (no Copilot event mapping)\n")

sys.stdout.write(json.dumps(out))
PYEOF
}

# Strip every Asha-tagged hook entry from the current hooks.json (or
# bootstrap from {"hooks":{}} if missing). Echoes the cleaned JSON.
_copilot_strip_asha_entries() {
  local current
  if [[ -f "$COPILOT_HOOKS_FILE" ]]; then
    current="$(cat "$COPILOT_HOOKS_FILE")"
  else
    current='{"hooks":{}}'
  fi
  printf '%s' "$current" | jq '
    if .hooks then
      .hooks |= with_entries(
        .value |= (
          map(
            .hooks |= map(select(((.source // "") | startswith("asha:")) | not))
          )
          | map(select(.hooks | length > 0))
        )
      )
      | .hooks |= with_entries(select(.value | length > 0))
    else . + {hooks: {}} end
  '
}

# Walk all selected plugins, build their tagged hook JSON, merge into the
# cleaned base, and atomically write the result.
#
# DEFERRED in v1: Copilot CLI 1.0.x reads hooks from project-scope only
# (CWD-local or .github/hooks/ — verified empirically 2026-05-09). Writing
# to ~/.copilot/hooks/hooks.json is a no-op for the CLI; cloud-agent uses
# repo-rooted .github/hooks/. Until a per-project install path is designed,
# this function logs a notice and exits without writing. The full install
# logic below remains intact (gated by ASHA_COPILOT_HOOKS_FORCE=1) so it
# can be re-enabled when scope question is answered.
copilot_install_hooks() {
  if [[ "${ASHA_COPILOT_HOOKS_FORCE:-0}" != "1" ]]; then
    echo "[copilot] hooks: deferred — Copilot CLI hooks are project-scope (.github/hooks/) only." >&2
    echo "[copilot] hooks: to manually install, copy plugin hooks/hooks.json files into" >&2
    echo '[copilot] hooks:   <project>/.github/hooks/  with ${CLAUDE_PLUGIN_ROOT} pre-expanded.' >&2
    echo "[copilot] hooks: set ASHA_COPILOT_HOOKS_FORCE=1 to write to ~/.copilot/hooks/hooks.json anyway." >&2
    return 0
  fi

  local cleaned
  cleaned="$(_copilot_strip_asha_entries)" || die "failed to strip existing asha hook entries" 4

  local merged="$cleaned"
  local plugin_dir ns plugin_root abs_root hooks_json count=0

  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || continue
    _copilot_is_skip_plugin "$plugin_dir" && continue

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
    plugin_emit="$(_copilot_emit_hooks_for_plugin "$abs_root" "$hooks_json" "$ns")"
    [[ -z "$plugin_emit" || "$plugin_emit" == "{}" ]] && continue

    # Merge: for each event in plugin_emit, append its groups to the merged base.
    merged="$(printf '%s' "$merged" | jq --argjson add "$plugin_emit" '
      .hooks = (.hooks // {})
      | reduce ($add | to_entries[]) as $e (
          .;
          .hooks[$e.key] = ((.hooks[$e.key] // []) + $e.value)
        )
    ')"
    count=$((count+1))
  done < <(selected_plugins)

  # Compare against current on-disk content; skip write if identical.
  if [[ -f "$COPILOT_HOOKS_FILE" ]]; then
    local current; current="$(cat "$COPILOT_HOOKS_FILE")"
    # Normalize both via jq for stable diff.
    local cur_norm new_norm
    cur_norm="$(printf '%s' "$current" | jq -S . 2>/dev/null || echo '')"
    new_norm="$(printf '%s' "$merged" | jq -S .)"
    if [[ "$cur_norm" == "$new_norm" ]]; then
      log "[copilot] hooks.json unchanged"
      return 0
    fi
  fi

  _copilot_backup_hooks_once
  _copilot_atomic_write_hooks "$merged"

  local n
  n="$(printf '%s' "$merged" | jq '[.hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | startswith("asha:"))] | length')"
  log "[copilot] registered $n hook entr$([[ $n -eq 1 ]] && echo y || echo ies)"
}

# ---------------------------------------------------------------------------
# Entry point: copilot_install
# ---------------------------------------------------------------------------

copilot_install() {
  command -v jq      >/dev/null 2>&1 || die "jq required for Copilot install (JSON manipulation)" 3
  command -v python3 >/dev/null 2>&1 || die "python3 required for Copilot install (frontmatter + hook translation)" 3

  : "${ABS_MARKET_ROOT:=$(readlink -f "$MARKET_ROOT")}"

  ensure_dir "$COPILOT_SKILLS_DIR"

  # Bootstrap an empty hooks.json only if hook install is force-enabled.
  # Default mode defers hook install entirely (Copilot CLI hooks are
  # project-scope only — verified empirically 2026-05-09); bootstrapping
  # would orphan the file with no install behind it.
  if [[ "${ASHA_COPILOT_HOOKS_FORCE:-0}" == "1" && ! -f "$COPILOT_HOOKS_FILE" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "would bootstrap $COPILOT_HOOKS_FILE with {\"hooks\":{}}"
    else
      ensure_dir "$(dirname "$COPILOT_HOOKS_FILE")"
      printf '%s\n' '{"hooks":{}}' > "$COPILOT_HOOKS_FILE"
      log "bootstrapped $COPILOT_HOOKS_FILE"
    fi
  fi

  say "[copilot] target = $COPILOT_HOME"

  local plugin_dir ns
  while read -r plugin_dir; do
    [[ -n "$plugin_dir" ]] || continue
    [[ -d "$PLUGINS_DIR/$plugin_dir" ]] || { echo "WARN: not a plugin dir: $plugin_dir" >&2; continue; }
    if _copilot_is_skip_plugin "$plugin_dir"; then
      say ""
      say "== [copilot] $plugin_dir  (skipped: Claude-only) =="
      continue
    fi
    ns="$(ns_for "$plugin_dir")"
    say ""
    say "== [copilot] $plugin_dir  (ns=$ns) =="
    copilot_install_skills         "$plugin_dir" "$ns"
    copilot_install_agents         "$plugin_dir" "$ns"
    copilot_install_command_skills "$plugin_dir" "$ns"
  done < <(selected_plugins)

  say ""
  say "== [copilot] hooks =="
  copilot_install_hooks
}

# ---------------------------------------------------------------------------
# Entry point: copilot_uninstall
# ---------------------------------------------------------------------------

copilot_uninstall() {
  command -v jq      >/dev/null 2>&1 || die "jq required for Copilot uninstall (JSON manipulation)" 3
  command -v python3 >/dev/null 2>&1 || die "python3 required for Copilot uninstall (frontmatter parsing)" 3
  [[ -d "$COPILOT_HOME" ]] || { say "[copilot] $COPILOT_HOME does not exist; nothing to remove"; COPILOT_UNINSTALL_TOTAL=0; return 0; }

  say "[copilot] target = $COPILOT_HOME"

  local total=0 n

  # Skills cleanup — same three categories as codex:
  #   1. Whole-dir symlinks (plugin skills)
  #   2. SKILL.md symlinks inside dirs we created
  #   3. Generated SKILL.md files (current command-skills with stripped frontmatter)
  if [[ -d "$COPILOT_SKILLS_DIR" ]]; then
    n="$(remove_symlinks_under "$COPILOT_SKILLS_DIR" 2)"
    [[ "$n" -gt 0 ]] && say "[copilot] removed $n skill symlink(s) from $COPILOT_SKILLS_DIR"
    total=$((total + n))

    # Generated command-skills (real files): identify by walking plugin command MDs,
    # reading their declared name, and checking if a non-symlink SKILL.md exists.
    local removed_generated=0
    while IFS= read -r cmd; do
      [[ -f "$cmd" ]] || continue
      case "$cmd" in *output-styles*) continue ;; esac
      local declared_name
      declared_name="$(_copilot_skill_name_from_md "$cmd")"
      [[ -z "$declared_name" ]] && continue
      local skill_md="$COPILOT_SKILLS_DIR/$declared_name/SKILL.md"
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
    [[ $removed_generated -gt 0 ]] && say "[copilot] removed $removed_generated generated command-skill(s)"
    total=$((total + removed_generated))

    # Prune now-empty skill dirs that we created (only real dirs, not .system).
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
    done < <(find "$COPILOT_SKILLS_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
  fi

  # Agents: depth 1
  if [[ -d "$COPILOT_AGENTS_DIR" ]]; then
    n="$(remove_symlinks_under "$COPILOT_AGENTS_DIR" 1)"
    [[ "$n" -gt 0 ]] && say "[copilot] removed $n agent symlink(s) from $COPILOT_AGENTS_DIR"
    total=$((total + n))
  fi

  # Strip Asha-tagged hooks from hooks.json.
  if [[ -f "$COPILOT_HOOKS_FILE" ]]; then
    local before after removed
    before="$(jq -r '[.hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | startswith("asha:"))] | length' "$COPILOT_HOOKS_FILE" 2>/dev/null || echo 0)"
    if [[ "$before" -gt 0 ]]; then
      if [[ $DRY_RUN -eq 1 ]]; then
        say "[copilot] would remove $before tagged hook entr$([[ $before -eq 1 ]] && echo y || echo ies) from hooks.json"
      else
        _copilot_backup_hooks_once
        local cleaned
        cleaned="$(_copilot_strip_asha_entries)"
        _copilot_atomic_write_hooks "$cleaned"
        after="$(jq -r '[.hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | startswith("asha:"))] | length' "$COPILOT_HOOKS_FILE" 2>/dev/null || echo 0)"
        removed=$((before - after))
        say "[copilot] removed $removed tagged hook entr$([[ $removed -eq 1 ]] && echo y || echo ies) from hooks.json"
      fi
    else
      log "[copilot] no asha-tagged hooks in hooks.json"
    fi
  else
    log "[copilot] no hooks.json at $COPILOT_HOOKS_FILE"
  fi

  # Cached identity (regenerated on next asha-copilot launch; safe to remove)
  if [[ -f "$HOME/.cache/asha/instructions-copilot.md" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[copilot] would remove ~/.cache/asha/instructions-copilot.md"
    else
      rm -f "$HOME/.cache/asha/instructions-copilot.md"
      rmdir "$HOME/.cache/asha" 2>/dev/null
      log "[copilot] removed cached identity"
    fi
  fi

  COPILOT_UNINSTALL_TOTAL=$total
}
