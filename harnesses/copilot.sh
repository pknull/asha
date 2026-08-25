#!/usr/bin/env bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
# Asha → GitHub Copilot harness adapter.
#
# Copilot uses native skill and agent directories, rendered command-skills,
# and two dedicated hook files: guardrails and Memory v2 recovery callbacks.
# The active hook schema is emitted by the dedicated installers
# below; user-owned hooks.json is never modified.
#
# Sourced by ../install.sh and ../uninstall.sh. Expects globals from the
# dispatcher: MARKET_ROOT, PLUGINS_DIR, NAMESPACES_FILE, DRY_RUN, FORCE,
# VERBOSE, ONLY, ABS_MARKET_ROOT (uninstall only).
#
# And these helpers (defined in the dispatcher):
#   die, log, say, ensure_dir, mklink, ns_for, selected_plugins, info

COPILOT_HOME="$(asha_harness_home copilot)"
COPILOT_SKILLS_DIR="$COPILOT_HOME/skills"
COPILOT_AGENTS_DIR="$COPILOT_HOME/agents"
# Kept only to remove tagged artifacts emitted by pre-dedicated-hook releases.
COPILOT_HOOKS_FILE="$COPILOT_HOME/hooks/hooks.json"
# Asha's own guardrail hooks live in a dedicated file so user hooks.json is
# untouched (Copilot loads every ~/.copilot/hooks/*.json).
COPILOT_GUARDRAILS_FILE="$COPILOT_HOME/hooks/asha-guardrails.json"
COPILOT_RECOVERY_FILE="$COPILOT_HOME/hooks/asha-recovery.json"
# Removed v1 artifacts, pruned on install/uninstall.
COPILOT_NUDGES_FILE="$COPILOT_HOME/hooks/asha-nudges.json"
COPILOT_LIFECYCLE_FILE="$COPILOT_HOME/hooks/asha-lifecycle.json"

# Shared converters (skip-plugin policy, frontmatter parsing, command-skill and
# agent emitters) — also sourced by lib/build.sh for plugin packaging.
# shellcheck source=harnesses/copilot-common.sh
source "$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)/copilot-common.sh"

# Atomic write to legacy hooks.json, validated by jq re-parse.
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

# Strip Asha-tagged entries from legacy hooks.json releases. Echoes the
# cleaned JSON; current installations never write this file.
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

# PreToolUse guardrails (policy-guard + block-secrets) use a dedicated adapter.
#
# Copilot's hook contract differs from Claude's — flat schema with a `bash`
# field + top-level `{version:1}`, decision via stdout `permissionDecision` JSON,
# tool names like bash/create/edit. So we install a DEDICATED guardrails file
# pointing at copilot-policy-adapter.sh, which bridges Copilot ⇄ the Claude-shaped
# handlers (see that script's header). Soft deterrent only: Copilot bypasses
# preToolUse under parallel tool calls (github/copilot-cli#2893).
#
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

  local prepared
  prepared="$(mktemp)"
  printf '%s\n' "$content" > "$prepared"
  asha_artifact_install_prepared copilot "$adapter" "$COPILOT_GUARDRAILS_FILE" copilot-guardrails "$prepared"
  rm -f "$prepared"
  say "[copilot] installed PreToolUse guardrails -> $COPILOT_GUARDRAILS_FILE"
}

# Memory v2 recovery callbacks plus direct SessionStart/RP context delivery.
copilot_install_recovery_hooks() {
  local start_h="$PLUGINS_DIR/session/hooks/handlers/session-start.sh"
  local prompt_h="$PLUGINS_DIR/session/hooks/handlers/user-prompt-submit.sh"
  local post_h="$PLUGINS_DIR/session/hooks/handlers/post-tool-use.sh"
  local end_h="$PLUGINS_DIR/session/hooks/handlers/session-end.sh"
  if [[ ! -x "$start_h" || ! -x "$prompt_h" || ! -x "$post_h" || ! -x "$end_h" ]]; then
    log "[copilot] recovery handlers missing/not executable; skipping recovery hooks"
    return 0
  fi
  local abs_start abs_prompt abs_post abs_end content
  abs_start="$(resolve_path "$start_h")"
  abs_prompt="$(resolve_path "$prompt_h")"
  abs_post="$(resolve_path "$post_h")"
  abs_end="$(resolve_path "$end_h")"

  content="$(jq -nc --arg s "$abs_start" --arg p "$abs_prompt" --arg t "$abs_post" --arg e "$abs_end" '{
    version: 1,
    hooks: {
      sessionStart:        [{type:"command", bash:$s, timeoutSec:15}],
      userPromptSubmitted: [{type:"command", bash:$p, timeoutSec:10}],
      postToolUse:         [{type:"command", bash:$t, timeoutSec:10}],
      sessionEnd:          [{type:"command", bash:$e, timeoutSec:10}]
    }
  }')" || { log "[copilot] failed to build recovery json; skipping"; return 0; }

  local prepared
  prepared="$(mktemp)"
  printf '%s\n' "$content" > "$prepared"
  asha_artifact_install_prepared copilot "$start_h" "$COPILOT_RECOVERY_FILE" copilot-recovery "$prepared"
  rm -f "$prepared"
  say "[copilot] installed Memory v2 recovery hooks -> $COPILOT_RECOVERY_FILE"
}

# Reconcile the two retired, pre-ledger dedicated hook files independently of
# whether the replacement recovery file changed. Exact installer output is
# removable; modified bytes become reviewable managed drift.
copilot_reconcile_retired_hooks() {
  local nudge_engine="$PLUGINS_DIR/session/hooks/handlers/nudge-engine.sh"
  local start_h="$PLUGINS_DIR/session/hooks/handlers/session-start.sh"
  local end_h="$PLUGINS_DIR/session/hooks/handlers/session-end.sh"
  local content prepared

  content="$(jq -nc --arg e "$(resolve_path "$nudge_engine")" '{
    version: 1,
    hooks: {
      sessionStart:        [{type:"command", bash:($e + " SessionStart"), timeoutSec:10}],
      userPromptSubmitted: [{type:"command", bash:($e + " UserPromptSubmit"), timeoutSec:10}],
      postToolUse:         [{type:"command", bash:($e + " PostToolUse"), timeoutSec:10}]
    }
  }')" || content=""
  if [[ -n "$content" ]]; then
    prepared="$(mktemp)"; printf '%s\n' "$content" > "$prepared"
    asha_artifact_retire_prepared copilot "$nudge_engine" "$COPILOT_NUDGES_FILE" copilot-retired-nudges "$prepared"
    rm -f "$prepared"
  fi

  content="$(jq -nc --arg s "$(resolve_path "$start_h")" --arg e "$(resolve_path "$end_h")" '{
    version: 1,
    hooks: {
      sessionStart: [{type:"command", bash:$s, timeoutSec:60}],
      sessionEnd:   [{type:"command", bash:$e, timeoutSec:30}]
    }
  }')" || content=""
  if [[ -n "$content" ]]; then
    prepared="$(mktemp)"; printf '%s\n' "$content" > "$prepared"
    asha_artifact_retire_prepared copilot "$start_h" "$COPILOT_LIFECYCLE_FILE" copilot-retired-lifecycle "$prepared"
    rm -f "$prepared"
  fi
}

# ---------------------------------------------------------------------------
# Entry point: copilot_install
# ---------------------------------------------------------------------------

copilot_install() {
  command -v jq      >/dev/null 2>&1 || die "jq required for Copilot install (JSON manipulation)" 3
  command -v python3 >/dev/null 2>&1 || die "python3 required for Copilot install (frontmatter + hook translation)" 3

  : "${ABS_MARKET_ROOT:=$(resolve_path "$MARKET_ROOT")}"

  ensure_dir "$COPILOT_SKILLS_DIR"

  # Legacy aggregate hooks.json remains user-owned and is never bootstrapped.

  say "[copilot] target = $COPILOT_HOME"
  asha_artifact_begin copilot

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
  copilot_install_recovery_hooks
  copilot_reconcile_retired_hooks
  asha_artifact_finalize copilot "$([[ -z "${ONLY:-}" ]] && echo 1 || echo 0)"
}

# ---------------------------------------------------------------------------
# Entry point: copilot_uninstall
# ---------------------------------------------------------------------------

copilot_uninstall() {
  command -v jq      >/dev/null 2>&1 || die "jq required for Copilot uninstall (JSON manipulation)" 3
  command -v python3 >/dev/null 2>&1 || die "python3 required for Copilot uninstall (frontmatter parsing)" 3
  [[ -d "$COPILOT_HOME" ]] || { say "[copilot] $COPILOT_HOME does not exist; nothing to remove"; COPILOT_UNINSTALL_TOTAL=0; return 0; }

  local ownership_manifest
  ownership_manifest="$(asha_artifact_manifest_path copilot)"
  if [[ ! -f "$ownership_manifest" ]] && {
       grep -rlq '## Copilot harness adapter' "$COPILOT_SKILLS_DIR" 2>/dev/null \
       || find "$COPILOT_AGENTS_DIR" -maxdepth 1 -type f -name '*.agent.md' -print -quit 2>/dev/null | grep -q .;
     }; then
    die "pre-manifest Copilot artifacts detected; run 'asha install copilot --force' once, then retry uninstall" 2
  fi

  say "[copilot] target = $COPILOT_HOME"

  local total=0 n
  n="$(asha_artifact_uninstall copilot)"
  [[ "$n" -gt 0 ]] && say "[copilot] removed $n owned generated artifact(s)"
  total=$((total + n))

  # Skills cleanup — same three categories as codex:
  #   1. Whole-dir symlinks (plugin skills)
  #   2. SKILL.md symlinks inside dirs we created
  #   3. Generated SKILL.md files (current command-skills with stripped frontmatter)
  if [[ -d "$COPILOT_SKILLS_DIR" ]]; then
    n="$(remove_symlinks_under "$COPILOT_SKILLS_DIR" 2)"
    [[ "$n" -gt 0 ]] && say "[copilot] removed $n skill symlink(s) from $COPILOT_SKILLS_DIR"
    total=$((total + n))

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
  if [[ -f "${ASHA_HOME:-$HOME/.asha}/cache/instructions-copilot.md" || -d "${ASHA_HOME:-$HOME/.asha}/cache/copilot-instr" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[copilot] would remove ~/.asha/cache/instructions-copilot.md + copilot-instr/"
    else
      rm -f "${ASHA_HOME:-$HOME/.asha}/cache/instructions-copilot.md"
      rm -rf "${ASHA_HOME:-$HOME/.asha}/cache/copilot-instr"
      # `|| true` is load-bearing: unguarded rmdir of a non-empty dir dies
      # under `set -e` with stderr silenced — see issue #4 (codex twin).
      rmdir "${ASHA_HOME:-$HOME/.asha}/cache" 2>/dev/null || true
      log "[copilot] removed cached identity"
    fi
  fi

  # Read indirectly by lib/uninstall.sh after this sourced function returns.
  # shellcheck disable=SC2034
  COPILOT_UNINSTALL_TOTAL=$total
}
