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
#   6. Agent files: generated .agent.md files with Copilot-clean frontmatter
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
# Asha's own guardrail hooks live in a DEDICATED file so we never touch a user's
# hooks.json (Copilot loads every ~/.copilot/hooks/*.json).
COPILOT_GUARDRAILS_FILE="$COPILOT_HOME/hooks/asha-guardrails.json"
# Referenced for forward-compat; this implementation does NOT manage MCP.
COPILOT_MCP_FILE="$COPILOT_HOME/mcp-config.json"

# Events Copilot is assumed to support (camelCase). UNVERIFIED — see header.
_COPILOT_EVENTS=(sessionStart sessionEnd userPromptSubmitted preToolUse postToolUse errorOccurred)

# Shared converters (skip-plugin policy, frontmatter parsing, command-skill and
# agent emitters) — also sourced by lib/build.sh for plugin packaging.
# shellcheck source=harnesses/copilot-common.sh
source "$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)/copilot-common.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_copilot_is_event() {
  local e="$1" ev
  for ev in "${_COPILOT_EVENTS[@]}"; do [[ "$e" == "$ev" ]] && return 0; done
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

# _copilot_skill_name_from_md moved to copilot-common.sh (shared with build).

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

# _copilot_emit_command_skill moved to copilot-common.sh (shared with build).

# Generate Copilot-native `.agent.md` files from Asha agent Markdown. Keep the
# conversion path aligned with `asha build copilot`, so local installs and
# packaged plugins expose the same agent shape.
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
    local base declared_name dest legacy existing
    base="$(basename "$agent" .md)"
    declared_name="$(_copilot_skill_name_from_md "$agent")"
    [[ -n "$declared_name" ]] || declared_name="$base"
    dest="$COPILOT_AGENTS_DIR/${ns}-${declared_name}.agent.md"

    # Clean legacy bare-markdown symlink for this source if present.
    legacy="$COPILOT_AGENTS_DIR/${ns}-${base}.md"
    if [[ -L "$legacy" ]]; then
      existing="$(resolve_path "$legacy" 2>/dev/null || true)"
      if [[ "$existing" == "$(resolve_path "$agent")" ]]; then
        [[ $DRY_RUN -eq 1 ]] || rm -f "$legacy"
        log "[copilot] removed legacy markdown agent symlink: $legacy"
      fi
    fi

    _copilot_emit_agent_md "$agent" "$dest"
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
# RETIRED 2026-05-10: Asha capture (events.jsonl) now derived on-demand at
# /save time from the host's native session log
# (~/.copilot/session-state/<sid>/events.jsonl), via jsonl_reader. Hooks are
# no longer the data source for synthesis. The previous Copilot-specific
# blocker (v1.0.44 hooks fire but don't pipe payload data) is moot — we
# don't need their payloads when the data is already on disk in events.jsonl.
#
# Capture no longer needs hooks (events.jsonl is read at /save). But the
# PreToolUse GUARDRAILS (policy-guard + block-secrets) DO work on Copilot 1.0.63
# (verified 2026-06-24: a preToolUse hook fires and can deny a tool call).
#
# Copilot's hook contract differs from Claude's — flat schema with a `bash`
# field + top-level `{version:1}`, decision via stdout `permissionDecision` JSON,
# tool names like bash/create/edit. So we install a DEDICATED guardrails file
# pointing at copilot-policy-adapter.sh, which bridges Copilot ⇄ the Claude-shaped
# handlers (see that script's header). Soft deterrent only: Copilot bypasses
# preToolUse under parallel tool calls (github/copilot-cli#2893).
#
# The legacy _copilot_emit_hooks_for_plugin / _copilot_strip_asha_entries helpers
# above are now unused (they emitted the wrong, Claude-style schema) and may be
# pruned in a later pass.
copilot_install_hooks() {
  local adapter abs_adapter content
  adapter="$PLUGINS_DIR/session/hooks/handlers/copilot-policy-adapter.sh"
  if [[ ! -x "$adapter" ]]; then
    log "[copilot] guardrail adapter missing/not executable ($adapter); skipping guardrail hooks"
    return 0
  fi
  abs_adapter="$(resolve_path "$adapter")"

  content="$(jq -nc --arg cmd "$abs_adapter" \
    '{version:1, hooks:{preToolUse:[{type:"command", bash:$cmd, timeoutSec:15}]}}')" \
    || { log "[copilot] failed to build guardrails json; skipping"; return 0; }

  if [[ $DRY_RUN -eq 1 ]]; then
    say "[copilot] would write $COPILOT_GUARDRAILS_FILE (PreToolUse guardrails -> adapter)"
    return 0
  fi

  ensure_dir "$(dirname "$COPILOT_GUARDRAILS_FILE")"
  if [[ -f "$COPILOT_GUARDRAILS_FILE" ]] \
     && [[ "$(jq -S . "$COPILOT_GUARDRAILS_FILE" 2>/dev/null)" == "$(printf '%s' "$content" | jq -S .)" ]]; then
    log "[copilot] guardrails unchanged"
    return 0
  fi
  local tmp="$COPILOT_GUARDRAILS_FILE.tmp.$$"
  printf '%s\n' "$content" > "$tmp" && mv "$tmp" "$COPILOT_GUARDRAILS_FILE"
  say "[copilot] installed PreToolUse guardrails -> $COPILOT_GUARDRAILS_FILE"
}

# ---------------------------------------------------------------------------
# Entry point: copilot_install
# ---------------------------------------------------------------------------

copilot_install() {
  command -v jq      >/dev/null 2>&1 || die "jq required for Copilot install (JSON manipulation)" 3
  command -v python3 >/dev/null 2>&1 || die "python3 required for Copilot install (frontmatter + hook translation)" 3

  : "${ABS_MARKET_ROOT:=$(resolve_path "$MARKET_ROOT")}"

  ensure_dir "$COPILOT_SKILLS_DIR"

  # Hook install retired (capture now derived on-demand at /save time).
  # No longer bootstrap COPILOT_HOOKS_FILE — would orphan the file.

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

    local removed_generated_agents=0
    while IFS= read -r agent; do
      [[ -f "$agent" ]] || continue
      local plugin_dir ns declared_name base dest
      plugin_dir="$(basename "$(dirname "$(dirname "$agent")")")"
      ns="$(ns_for "$plugin_dir")"
      base="$(basename "$agent" .md)"
      declared_name="$(_copilot_skill_name_from_md "$agent")"
      [[ -n "$declared_name" ]] || declared_name="$base"
      dest="$COPILOT_AGENTS_DIR/${ns}-${declared_name}.agent.md"
      if [[ -f "$dest" && ! -L "$dest" ]]; then
        if [[ $DRY_RUN -eq 1 ]]; then
          info "  RM (generated agent)  $dest"
        else
          rm -f "$dest"
          log "removed generated Copilot agent: $dest"
        fi
        removed_generated_agents=$((removed_generated_agents+1))
      fi
    done < <(find "$PLUGINS_DIR" -mindepth 3 -maxdepth 3 -path '*/agents/*.md' -type f 2>/dev/null)
    [[ $removed_generated_agents -gt 0 ]] && say "[copilot] removed $removed_generated_agents generated agent file(s)"
    total=$((total + removed_generated_agents))
  fi

  # Asha's dedicated guardrails file (the current install path).
  if [[ -f "$COPILOT_GUARDRAILS_FILE" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[copilot] would remove $COPILOT_GUARDRAILS_FILE"
    else
      rm -f "$COPILOT_GUARDRAILS_FILE"
      say "[copilot] removed PreToolUse guardrails ($COPILOT_GUARDRAILS_FILE)"
    fi
  fi

  # Strip Asha-tagged hooks from hooks.json (legacy path; harmless if absent).
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

  # Cached identity + per-launch instructions dir (both regenerated on next
  # asha-copilot launch; safe to remove)
  if [[ -f "$HOME/.cache/asha/instructions-copilot.md" || -d "$HOME/.cache/asha/copilot-instr" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[copilot] would remove ~/.cache/asha/instructions-copilot.md + copilot-instr/"
    else
      rm -f "$HOME/.cache/asha/instructions-copilot.md"
      rm -rf "$HOME/.cache/asha/copilot-instr"
      # `|| true` is load-bearing: unguarded rmdir of a non-empty dir dies
      # under `set -e` with stderr silenced — see issue #4 (codex twin).
      rmdir "$HOME/.cache/asha" 2>/dev/null || true
      log "[copilot] removed cached identity"
    fi
  fi

  COPILOT_UNINSTALL_TOTAL=$total
}
