#!/usr/bin/env bash
# asha-drift-check.sh — audit the asha symlink-mount install for drift.
# Exits 0 if everything is clean, 1 if any check fails, 2 on usage error.
# Intended for manual runs or scheduled via systemd-user-timer / crontab.
# `asha doctor` is the front door for this script (lib/doctor.sh).
#
# Usage:
#   asha-drift-check.sh [--target {claude,codex,copilot,all}] [--fix]
#
# Default target is 'all'. Per-target flags scope the checks.
# --fix self-heals stale codex/copilot command-skills (regenerates SKILL.md
#   from its source command MD); without --fix the script only audits.

set -uo pipefail

CLAUDE="$HOME/.claude"
CODEX="$HOME/.codex"
COPILOT="$HOME/.copilot"
CODEX_OVERLAY="$HOME/.codex-asha"
# Resolve the repo root from this script's own location (repo bin/), following
# symlinks (may be invoked via ~/.local/bin). Portable — no GNU `readlink -f`.
__src="${BASH_SOURCE[0]}"
while [ -h "$__src" ]; do
  __dir="$(cd -P "$(dirname "$__src")" >/dev/null 2>&1 && pwd)"
  __src="$(readlink "$__src")"
  case "$__src" in /*) ;; *) __src="$__dir/$__src" ;; esac
done
ASHA="$(dirname "$(cd -P "$(dirname "$__src")" >/dev/null 2>&1 && pwd)")"
unset __src __dir

TARGET="all"
FIX=0          # --fix: self-heal stale codex command-skills (audit-only otherwise)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) shift; TARGET="${1:-}" ;;
    --target=*) TARGET="${1#--target=}" ;;
    --fix) FIX=1 ;;
    -h|--help)
      sed -n '2,/^[^#]/{/^#/!d; s/^# \{0,1\}//; p}' "$0"
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$TARGET" in
  claude|codex|copilot|all) ;;
  *) echo "invalid --target '$TARGET'" >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS  $1"; }
nope() { echo "FAIL  $1"; fail=$((fail+1)); }
warn() { echo "WARN  $1"; }          # non-failing observation
info_line() { echo "INFO  $1"; }     # context, never a problem
section() { echo ""; echo "── $1 ──"; }

# --fix self-heal: regenerate a stale codex command-skill SKILL.md from its
# source command MD. Reuses the exact generator the installer uses
# (harnesses/codex.sh:_codex_emit_command_skill), which strips Claude-only
# frontmatter keys and writes idempotently — so a regenerated file is identical
# to what `./install.sh --target codex` would produce. The install engine and
# codex harness are sourced lazily on first use (they define the helper plus the
# say/log/resolve_path/ns_for/path globals it depends on). DRY_RUN/VERBOSE are
# pinned to 0 so the generator actually writes and stays quiet.
_FIX_CODEX_SOURCED=0
fix_regen_command_skill() {
  local cmd="$1" skill_md="$2"
  if [[ $_FIX_CODEX_SOURCED -eq 0 ]]; then
    DRY_RUN=0 VERBOSE=0
    # shellcheck source=../lib/install.sh
    source "$ASHA/lib/install.sh"
    # shellcheck source=../harnesses/codex.sh
    source "$ASHA/harnesses/codex.sh"
    _FIX_CODEX_SOURCED=1
  fi
  _codex_emit_command_skill "$cmd" "$skill_md"
}

# Copilot twin: same lazy-source pattern, but only the shared converter module
# is needed (harnesses/copilot-common.sh defines _copilot_emit_command_skill).
_FIX_COPILOT_SOURCED=0
fix_regen_copilot_command_skill() {
  local cmd="$1" skill_md="$2"
  if [[ $_FIX_COPILOT_SOURCED -eq 0 ]]; then
    DRY_RUN=0 VERBOSE=0
    # shellcheck source=../lib/install.sh
    source "$ASHA/lib/install.sh"
    # shellcheck source=../harnesses/copilot-common.sh
    source "$ASHA/harnesses/copilot-common.sh"
    _FIX_COPILOT_SOURCED=1
  fi
  _copilot_emit_command_skill "$cmd" "$skill_md"
}

# ── Shared command-skill coverage check (codex + copilot) ──
# Every plugin command MD (except output-styles) should have a SKILL.md under
# <skills_dir>/<name>/. Generated files are checked for freshness via mtime
# (--fix regenerates from source); legacy symlinked SKILL.md must resolve to
# the source; a whole-dir symlink collision (plugin skill claims the name) is
# an accepted skip.
check_command_skills() { # skills_dir label fix_fn
  local skills_dir="$1" label="$2" fix_fn="$3"
  local missing_cmd_skills=0 cmd name skill_md cmd_mtime skill_mtime target
  for cmd in "$ASHA"/plugins/*/commands/*.md; do
    [[ -f "$cmd" ]] || continue
    case "$cmd" in *output-styles*) continue ;; esac

    name=$(awk '/^---$/{if (++c==2) exit} c==1 && /^name:/ {print $2; exit}' "$cmd")
    [[ -z "$name" ]] && {
      [[ $missing_cmd_skills -eq 0 ]] && nope "command MDs without name: frontmatter:"
      echo "  $cmd"
      missing_cmd_skills=$((missing_cmd_skills+1))
      continue
    }

    # Collision skip FIRST: a whole-dir symlink means a plugin skill claimed
    # this name and the installer deliberately never generates a command-skill.
    # This must gate ALL arms — through the symlink a SKILL.md exists, and the
    # mtime arm would compare unrelated files, with --fix then clobbering the
    # repo's plugin-skill source THROUGH the symlink (review finding).
    if [[ -L "$skills_dir/$name" ]]; then
      continue
    fi

    skill_md="$skills_dir/$name/SKILL.md"
    if [[ ! -e "$skill_md" ]]; then
      [[ $missing_cmd_skills -eq 0 ]] && nope "command-skill SKILL.md missing for command ($label):"
      echo "  $cmd → expected at $skill_md"
      missing_cmd_skills=$((missing_cmd_skills+1))
      continue
    fi

    # Generated command-skill (real file): check freshness via mtime
    if [[ -f "$skill_md" && ! -L "$skill_md" ]]; then
      cmd_mtime="$(stat -c %Y "$cmd" 2>/dev/null || echo 0)"
      skill_mtime="$(stat -c %Y "$skill_md" 2>/dev/null || echo 0)"
      if [[ "$cmd_mtime" -gt "$skill_mtime" ]]; then
        if [[ $FIX -eq 1 ]]; then
          "$fix_fn" "$cmd" "$skill_md"
          echo "FIXED  regenerated stale command-skill: $skill_md  (source: $cmd)"
        else
          [[ $missing_cmd_skills -eq 0 ]] && nope "command-skill stale (source newer); rerun ./install.sh --target $label (or pass --fix):"
          echo "  $skill_md  (source: $cmd)"
          missing_cmd_skills=$((missing_cmd_skills+1))
        fi
      fi
      continue
    fi

    # Symlinked SKILL.md (legacy pre-frontmatter-strip): verify resolves to source
    if [[ -L "$skill_md" ]]; then
      target="$(readlink -f "$skill_md")"
      if [[ "$target" != "$(readlink -f "$cmd")" ]]; then
        [[ $missing_cmd_skills -eq 0 ]] && nope "command-skill symlink points elsewhere ($label):"
        echo "  $skill_md -> $target (expected $(readlink -f "$cmd"))"
        missing_cmd_skills=$((missing_cmd_skills+1))
      fi
    fi
  done
  [[ $missing_cmd_skills -eq 0 ]] && pass "command-skills present and fresh ($label)"
  return 0
}

# ── Shared dangling-symlink check ──
check_dangling() { # home_dir label dir:depth...
  local home_dir="$1" label="$2"; shift 2
  local dangling=0 spec d depth f t
  for spec in "$@"; do
    d="${spec%%:*}"; depth="${spec##*:}"
    [[ -d "$home_dir/$d" ]] || continue
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      t="$(readlink -f "$f" 2>/dev/null || true)"
      case "$t" in
        "$ASHA"|"$ASHA"/*)
          if [[ ! -e "$t" ]]; then
            [[ $dangling -eq 0 ]] && nope "dangling asha symlinks under $home_dir:"
            echo "  $f -> $t"
            dangling=$((dangling+1))
          fi
          ;;
      esac
    done < <(find "$home_dir/$d/" -maxdepth "$depth" -type l 2>/dev/null)
  done
  [[ $dangling -eq 0 ]] && pass "no dangling asha symlinks under $home_dir"
  return 0
}

# ===========================================================================
# Repo-wide checks (always run)
# ===========================================================================

section "repo state"

# Installer scripts present
gone=0
for f in install.sh uninstall.sh namespaces.json INSTALLER.md harnesses/claude.sh; do
  if [[ ! -f "$ASHA/$f" ]]; then
    [[ $gone -eq 0 ]] && nope "installer scripts missing:"
    echo "  $f"
    gone=$((gone+1))
  fi
done
[[ $gone -eq 0 ]] && pass "installer scripts present"

# No residual ${CLAUDE_PLUGIN_ROOT} in plugin command/skill/agent markdown
# (symlinked verbatim, so a placeholder there would reach the model unported).
# Excludes docs/ — design docs legitimately show the hooks.json placeholder.
n="$(grep -rn 'CLAUDE_PLUGIN_ROOT' "$ASHA/plugins" --include='*.md' --exclude-dir=docs 2>/dev/null | wc -l)"
if [[ "$n" == "0" ]]; then
  pass "no CLAUDE_PLUGIN_ROOT in plugin markdown"
else
  nope "$n CLAUDE_PLUGIN_ROOT refs remain in plugin markdown:"
  grep -rn 'CLAUDE_PLUGIN_ROOT' "$ASHA/plugins" --include='*.md' --exclude-dir=docs | head -5
fi

# ===========================================================================
# Claude harness checks
# ===========================================================================

if [[ "$TARGET" == "claude" || "$TARGET" == "all" ]]; then
  section "claude harness"

  # Legacy enabledPlugins / installed_plugins.json / marketplaces symlink
  if [[ -f "$CLAUDE/settings.json" ]]; then
    n="$(jq -r '[.enabledPlugins // {} | to_entries[] | select(.key | endswith("@asha-marketplace"))] | length' "$CLAUDE/settings.json")"
    if [[ "$n" == "0" ]]; then pass "enabledPlugins clean"; else nope "$n legacy enabledPlugins entries"; fi

    if [[ -f "$CLAUDE/plugins/installed_plugins.json" ]]; then
      n="$(jq -r '[.plugins | keys[] | select(endswith("@asha-marketplace"))] | length' "$CLAUDE/plugins/installed_plugins.json")"
      if [[ "$n" == "0" ]]; then pass "installed_plugins.json clean"; else nope "$n legacy plugin keys"; fi
    fi

    if [[ -L "$CLAUDE/plugins/marketplaces/asha-marketplace" ]]; then
      nope "legacy marketplaces symlink present"
    else
      pass "no legacy marketplaces symlink"
    fi
  else
    nope "$CLAUDE/settings.json missing"
  fi

  # No dangling asha symlinks under Claude scan dirs
  check_dangling "$CLAUDE" claude skills:2 agents:2 commands:2 output-styles:2

  # Every asha hook command path exists on disk. Match by command path-prefix
  # OR source tag (mirrors register_hooks in lib/install.sh): Claude Code
  # strips the non-standard "source" key on re-serialize, so live hooks are
  # usually untagged and a tag-only selector is blind to them (issue #4).
  if [[ -f "$CLAUDE/settings.json" ]]; then
    n="$(jq -r --arg prefix "$ASHA/plugins/" '[.hooks // {} | .[] | .[]? | .hooks[]?
          | select(((.command // "") | startswith($prefix)) or ((.source // "") | test("^(asha|marketplace):")))] | length' "$CLAUDE/settings.json")"
    info_line "$n asha hook entr$([[ "$n" == "1" ]] && echo y || echo ies) registered (path-prefix or tag)"
    missing=0
    while IFS= read -r c; do
      [[ -z "$c" ]] && continue
      if [[ ! -e "$c" ]]; then
        [[ $missing -eq 0 ]] && nope "asha hook paths missing in settings.json:"
        echo "  $c"
        missing=$((missing+1))
      fi
    done < <(jq -r --arg prefix "$ASHA/plugins/" '.hooks // {} | .[] | .[]? | .hooks[]?
          | select(((.command // "") | startswith($prefix)) or ((.source // "") | test("^(asha|marketplace):"))) | .command // empty' "$CLAUDE/settings.json")
    [[ $missing -eq 0 ]] && pass "all asha hook paths exist (claude)"
  fi
fi

# ===========================================================================
# Codex harness checks
# ===========================================================================

if [[ "$TARGET" == "codex" || "$TARGET" == "all" ]]; then
  section "codex harness"

  if [[ ! -d "$CODEX" ]]; then
    pass "codex not installed (skipping codex checks)"
  else
    # No dangling asha symlinks under Codex scan dirs
    check_dangling "$CODEX" codex skills:1 agents:1 prompts:1

    # config.toml parses as TOML
    if [[ -f "$CODEX/config.toml" ]]; then
      if python3 -c "import tomllib; tomllib.load(open('$CODEX/config.toml','rb'))" 2>/dev/null; then
        pass "~/.codex/config.toml parses as valid TOML"
      else
        nope "~/.codex/config.toml is invalid TOML"
      fi

      # Every tagged hook command path exists
      missing=0
      while IFS= read -r c; do
        [[ -z "$c" ]] && continue
        if [[ ! -e "$c" ]]; then
          [[ $missing -eq 0 ]] && nope "tagged hook paths missing in config.toml:"
          echo "  $c"
          missing=$((missing+1))
        fi
      done < <(python3 -c "
import tomllib
c = tomllib.load(open('$CODEX/config.toml','rb'))
for ev, blocks in (c.get('hooks') or {}).items():
    for b in blocks:
        cmd = b.get('command')
        if cmd: print(cmd)
" 2>/dev/null)
      [[ $missing -eq 0 ]] && pass "all hook command paths exist (codex)"
    fi

    # ───── Command-skill coverage check (shared with copilot) ─────
    check_command_skills "$CODEX/skills" codex fix_regen_command_skill

    # ───── Cached identity check (regenerated on each `asha codex` launch) ─────
    if [[ -f "$HOME/.cache/asha/instructions.md" ]]; then
      pass "cached identity exists at ~/.cache/asha/instructions.md"
    else
      # Not actually a failure — wrapper regenerates on launch — but worth logging
      log_msg="cached identity not yet generated (run \`asha codex --version\` to seed it)"
      pass "$log_msg"
    fi

    # ───── Stale overlay warning ─────
    if [[ -d "$HOME/.codex-asha" ]]; then
      nope "legacy overlay still present at ~/.codex-asha (Step 7-revised removed it; run ./uninstall.sh --target codex && ./install.sh --target codex)"
    fi
  fi
fi

# ===========================================================================
# Copilot harness checks
# ===========================================================================

if [[ "$TARGET" == "copilot" || "$TARGET" == "all" ]]; then
  section "copilot harness"

  if [[ ! -d "$COPILOT" ]]; then
    pass "copilot not installed (skipping copilot checks)"
  else
    # No dangling asha symlinks under Copilot scan dirs
    check_dangling "$COPILOT" copilot skills:2 agents:1

    # Command-skill coverage + freshness (generated SKILL.md files)
    check_command_skills "$COPILOT/skills" copilot fix_regen_copilot_command_skill

    # ───── PreToolUse guardrails file matches what the installer emits ─────
    guardrails="$COPILOT/hooks/asha-guardrails.json"
    adapter="$ASHA/plugins/session/hooks/handlers/copilot-policy-adapter.sh"
    if [[ ! -x "$adapter" ]]; then
      nope "guardrail adapter missing or not executable: $adapter"
    elif [[ ! -f "$guardrails" ]]; then
      nope "guardrails file missing: $guardrails (run ./install.sh --target copilot)"
    elif ! jq empty "$guardrails" 2>/dev/null; then
      nope "guardrails file is invalid JSON: $guardrails"
    else
      # Recompute expected content exactly as copilot_install_hooks does.
      expected="$(jq -nc --arg cmd "$adapter" \
        '{version:1, hooks:{preToolUse:[{type:"command", bash:$cmd, timeoutSec:15}]}}')"
      if [[ "$(jq -S . "$guardrails")" == "$(jq -S . <<<"$expected")" ]]; then
        pass "guardrails file matches installer-expected content"
        info_line "guardrails fail open under parallel tool calls (copilot-cli#2893) — soft deterrent, not containment"
      else
        if [[ $FIX -eq 1 ]]; then
          printf '%s\n' "$expected" > "$guardrails"
          echo "FIXED  rewrote guardrails file: $guardrails"
        else
          nope "guardrails file content drifted from installer-expected (pass --fix or rerun ./install.sh --target copilot)"
        fi
      fi
    fi

    # ───── Context (never failures) ─────
    info_line "persona loads via 'asha copilot' wrapper only (by design); plain 'copilot' is persona-free"
    if command -v copilot >/dev/null 2>&1; then
      info_line "copilot CLI: $(copilot --version 2>/dev/null | head -1 || echo 'version unknown')"
    else
      warn "copilot CLI not on PATH (install state can still be audited)"
    fi
    [[ -f "$COPILOT/copilot-instructions.md" ]] \
      && info_line "user-managed $COPILOT/copilot-instructions.md present (not asha-owned; auto-loads globally)"
  fi
fi

# ===========================================================================
# Bin + identity checks (always run)
# ===========================================================================

section "bin + identity"

# ~/.local/bin/asha should resolve into THIS checkout (a different checkout is
# the stale-foreign state that strands installs — see bin/asha:harness_configured).
user_bin="$HOME/.local/bin/asha"
if [[ -L "$user_bin" ]]; then
  t="$(readlink -f "$user_bin" 2>/dev/null || true)"
  case "$t" in
    "$ASHA"/*) pass "~/.local/bin/asha resolves into this checkout" ;;
    *) nope "~/.local/bin/asha resolves elsewhere: $t (foreign checkout? rerun ./install.sh --bin all)" ;;
  esac
  for shim in asha-claude asha-codex asha-copilot; do
    [[ -e "$HOME/.local/bin/$shim" ]] || warn "shim missing: ~/.local/bin/$shim (optional; ./install.sh --bin all)"
  done
elif [[ -e "$user_bin" ]]; then
  warn "~/.local/bin/asha exists but is not a symlink (legacy standalone wrapper?)"
else
  warn "asha dispatcher not installed at ~/.local/bin/asha (optional; ./install.sh --bin all)"
fi
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "~/.local/bin not in PATH" ;;
esac

# Repo identity file is a hard requirement of identity-merge.sh; the ~/.asha
# layer is optional-with-warn (the installer never creates soul/voice).
if [[ -f "$ASHA/identity/asha-identity-system-prompt.md" ]]; then
  pass "repo identity file present"
else
  nope "repo identity file missing: identity/asha-identity-system-prompt.md"
fi
for f in soul.md voice.md keeper.md config.json; do
  [[ -f "$HOME/.asha/$f" ]] || warn "~/.asha/$f absent (optional; session:init or /save can seed it)"
done

# ===========================================================================
# Summary
# ===========================================================================

echo ""
if [[ $fail -eq 0 ]]; then
  echo "All checks pass. ($TARGET)"
  exit 0
else
  echo "$fail check(s) failed. See above. ($TARGET)"
  exit 1
fi
