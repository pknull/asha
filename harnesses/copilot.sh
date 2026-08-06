#!/usr/bin/env bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
# Asha → GitHub Copilot harness adapter.
#
# Copilot uses native skill and agent directories, rendered command-skills,
# and three dedicated hook files: guardrails, advisory nudges, and lifecycle
# side effects. The active hook schema is emitted by the dedicated installers
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

# Advisory guidance nudges (session plugin nudge-engine). Verified live on
# 1.0.68 (2026-07-26): Copilot fires userPromptSubmitted/postToolUse hooks,
# shell-splits the command string (the engine takes the Claude event name as
# argv — Copilot payloads carry no hook_event_name), and injects ONLY via a
# top-level {"additionalContext": ...} JSON response (raw stdout discarded).
# The engine detects Copilot via the COPILOT_CLI=1 env it stamps on hook
# processes and emits that shape. preToolUse is not registered here: the only
# PreToolUse nudge row (memory-lexical) is Claude-only by design.
copilot_install_nudge_hooks() {
  local engine="$PLUGINS_DIR/session/hooks/handlers/nudge-engine.sh"
  if [[ ! -x "$engine" ]]; then
    log "[copilot] nudge engine missing/not executable ($engine); skipping nudge hooks"
    return 0
  fi
  local abs_engine content
  abs_engine="$(resolve_path "$engine")"

  content="$(jq -nc --arg e "$abs_engine" '{
    version: 1,
    hooks: {
      userPromptSubmitted: [{type:"command", bash:($e + " UserPromptSubmit"), timeoutSec:10}],
      postToolUse:         [{type:"command", bash:($e + " PostToolUse"),      timeoutSec:10}]
    }
  }')" || { log "[copilot] failed to build nudges json; skipping"; return 0; }

  if [[ $DRY_RUN -eq 1 ]]; then
    say "[copilot] would write $COPILOT_NUDGES_FILE (guidance nudges -> nudge-engine)"
    return 0
  fi

  ensure_dir "$(dirname "$COPILOT_NUDGES_FILE")"
  if [[ -f "$COPILOT_NUDGES_FILE" ]] \
     && [[ "$(jq -S . "$COPILOT_NUDGES_FILE" 2>/dev/null)" == "$(printf '%s' "$content" | jq -S .)" ]]; then
    log "[copilot] nudges unchanged"
    return 0
  fi
  local tmp="$COPILOT_NUDGES_FILE.tmp.$$"
  printf '%s\n' "$content" > "$tmp" && mv "$tmp" "$COPILOT_NUDGES_FILE"
  say "[copilot] installed guidance nudges -> $COPILOT_NUDGES_FILE"
}

# Lifecycle side-effect hooks (Claude parity; issue #13). Verified live on
# 1.0.75 (2026-07-27): sessionStart fires with {sessionId, timestamp, cwd,
# source, initialPrompt}; sessionEnd fires on clean exit with {sessionId,
# timestamp, cwd, reason} — reason "complete" (-p runs) / "user_exit"
# (interactive /exit). Handlers are the same event-specific scripts Claude
# registers:
#   sessionStart -> session-start.sh  (orphan recovery + marker cleanup; its
#                   raw-stdout context injection is DISCARDED by copilot —
#                   deliberate: the custom-instructions layer already injects
#                   the operational context at launch, so side effects only)
#   sessionEnd   -> session-end.sh    (detached automatic save; copilot's
#                   camelCase payload + clean-exit reasons handled there)
copilot_install_lifecycle_hooks() {
  local start_h="$PLUGINS_DIR/session/hooks/handlers/session-start.sh"
  local end_h="$PLUGINS_DIR/session/hooks/handlers/session-end.sh"
  if [[ ! -x "$start_h" || ! -x "$end_h" ]]; then
    log "[copilot] lifecycle handlers missing/not executable; skipping lifecycle hooks"
    return 0
  fi
  local abs_start abs_end content
  abs_start="$(resolve_path "$start_h")"
  abs_end="$(resolve_path "$end_h")"

  content="$(jq -nc --arg s "$abs_start" --arg e "$abs_end" '{
    version: 1,
    hooks: {
      sessionStart: [{type:"command", bash:$s, timeoutSec:60}],
      sessionEnd:   [{type:"command", bash:$e, timeoutSec:30}]
    }
  }')" || { log "[copilot] failed to build lifecycle json; skipping"; return 0; }

  if [[ $DRY_RUN -eq 1 ]]; then
    say "[copilot] would write $COPILOT_LIFECYCLE_FILE (lifecycle side effects -> session-start/end)"
    return 0
  fi

  ensure_dir "$(dirname "$COPILOT_LIFECYCLE_FILE")"
  if [[ -f "$COPILOT_LIFECYCLE_FILE" ]] \
     && [[ "$(jq -S . "$COPILOT_LIFECYCLE_FILE" 2>/dev/null)" == "$(printf '%s' "$content" | jq -S .)" ]]; then
    log "[copilot] lifecycle hooks unchanged"
    return 0
  fi
  local tmp="$COPILOT_LIFECYCLE_FILE.tmp.$$"
  printf '%s\n' "$content" > "$tmp" && mv "$tmp" "$COPILOT_LIFECYCLE_FILE"
  say "[copilot] installed lifecycle hooks -> $COPILOT_LIFECYCLE_FILE"
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
  copilot_install_nudge_hooks
  copilot_install_lifecycle_hooks
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

  # Asha's dedicated guardrails file (the current install path).
  if [[ -f "$COPILOT_GUARDRAILS_FILE" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[copilot] would remove $COPILOT_GUARDRAILS_FILE"
    else
      rm -f "$COPILOT_GUARDRAILS_FILE"
      say "[copilot] removed PreToolUse guardrails ($COPILOT_GUARDRAILS_FILE)"
    fi
  fi

  if [[ -f "$COPILOT_NUDGES_FILE" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[copilot] would remove $COPILOT_NUDGES_FILE"
    else
      rm -f "$COPILOT_NUDGES_FILE"
      say "[copilot] removed guidance nudges ($COPILOT_NUDGES_FILE)"
    fi
  fi

  if [[ -f "$COPILOT_LIFECYCLE_FILE" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "[copilot] would remove $COPILOT_LIFECYCLE_FILE"
    else
      rm -f "$COPILOT_LIFECYCLE_FILE"
      say "[copilot] removed lifecycle hooks ($COPILOT_LIFECYCLE_FILE)"
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

  # Read indirectly by lib/uninstall.sh after this sourced function returns.
  # shellcheck disable=SC2034
  COPILOT_UNINSTALL_TOTAL=$total
}
