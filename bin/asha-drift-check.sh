#!/usr/bin/env bash
# asha-drift-check.sh — audit the asha symlink-mount install for drift.
# Exits 0 if everything is clean, 1 if any check fails.
# Intended for manual runs or scheduled via systemd-user-timer / crontab.
#
# Usage:
#   asha-drift-check.sh [--target {claude,codex,all}]
#
# Default target is 'all'. Per-target flags scope the checks.

set -uo pipefail

CLAUDE="$HOME/.claude"
CODEX="$HOME/.codex"
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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) shift; TARGET="${1:-}" ;;
    --target=*) TARGET="${1#--target=}" ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done
case "$TARGET" in
  claude|codex|all) ;;
  *) echo "invalid --target '$TARGET'" >&2; exit 1 ;;
esac

fail=0
pass() { echo "PASS  $1"; }
nope() { echo "FAIL  $1"; fail=$((fail+1)); }
section() { echo ""; echo "── $1 ──"; }

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
  dangling=0
  for d in skills agents commands output-styles; do
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      t="$(readlink -f "$f" 2>/dev/null || true)"
      case "$t" in
        "$ASHA"|"$ASHA"/*)
          if [[ ! -e "$t" ]]; then
            [[ $dangling -eq 0 ]] && nope "dangling asha symlinks under ~/.claude:"
            echo "  $f -> $t"
            dangling=$((dangling+1))
          fi
          ;;
      esac
    done < <(find "$CLAUDE/$d/" -maxdepth 2 -type l 2>/dev/null)
  done
  [[ $dangling -eq 0 ]] && pass "no dangling asha symlinks under ~/.claude"

  # Every tagged hook command path exists on disk
  if [[ -f "$CLAUDE/settings.json" ]]; then
    missing=0
    while IFS= read -r c; do
      [[ -z "$c" ]] && continue
      if [[ ! -e "$c" ]]; then
        [[ $missing -eq 0 ]] && nope "tagged hook paths missing in settings.json:"
        echo "  $c"
        missing=$((missing+1))
      fi
    done < <(jq -r '.hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | test("^(asha|marketplace):")) | .command' "$CLAUDE/settings.json")
    [[ $missing -eq 0 ]] && pass "all tagged hook paths exist (claude)"
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
    dangling=0
    for d in skills agents prompts; do
      [[ -d "$CODEX/$d" ]] || continue
      while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        t="$(readlink -f "$f" 2>/dev/null || true)"
        case "$t" in
          "$ASHA"|"$ASHA"/*)
            if [[ ! -e "$t" ]]; then
              [[ $dangling -eq 0 ]] && nope "dangling asha symlinks under ~/.codex:"
              echo "  $f -> $t"
              dangling=$((dangling+1))
            fi
            ;;
        esac
      done < <(find "$CODEX/$d/" -maxdepth 1 -type l 2>/dev/null)
    done
    [[ $dangling -eq 0 ]] && pass "no dangling asha symlinks under ~/.codex"

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

    # ───── Command-skill coverage check ─────
    # Every plugin command MD (except output-styles) should have a matching
    # SKILL.md symlink under ~/.codex/skills/<name>/.
    missing_cmd_skills=0
    for cmd in "$ASHA"/plugins/*/commands/*.md; do
      [[ -f "$cmd" ]] || continue
      case "$cmd" in *output-styles*) continue ;; esac

      # Read name field
      name=$(awk '/^---$/{if (++c==2) exit} c==1 && /^name:/ {print $2; exit}' "$cmd")
      [[ -z "$name" ]] && {
        [[ $missing_cmd_skills -eq 0 ]] && nope "command MDs without name: frontmatter:"
        echo "  $cmd"
        missing_cmd_skills=$((missing_cmd_skills+1))
        continue
      }

      skill_md="$CODEX/skills/$name/SKILL.md"
      if [[ ! -e "$skill_md" ]]; then
        # OK if blocked by plugin-skill collision (whole-dir symlink claims the name)
        if [[ -L "$CODEX/skills/$name" ]]; then
          continue
        fi
        [[ $missing_cmd_skills -eq 0 ]] && nope "command-skill SKILL.md missing for command:"
        echo "  $cmd → expected at $skill_md"
        missing_cmd_skills=$((missing_cmd_skills+1))
        continue
      fi

      # Generated command-skill (real file): check freshness via mtime
      if [[ -f "$skill_md" && ! -L "$skill_md" ]]; then
        cmd_mtime="$(stat -c %Y "$cmd" 2>/dev/null || echo 0)"
        skill_mtime="$(stat -c %Y "$skill_md" 2>/dev/null || echo 0)"
        if [[ "$cmd_mtime" -gt "$skill_mtime" ]]; then
          [[ $missing_cmd_skills -eq 0 ]] && nope "command-skill stale (source newer); rerun ./install.sh --target codex:"
          echo "  $skill_md  (source: $cmd)"
          missing_cmd_skills=$((missing_cmd_skills+1))
        fi
        continue
      fi

      # Symlinked SKILL.md (legacy pre-frontmatter-strip): verify resolves to source
      if [[ -L "$skill_md" ]]; then
        target="$(readlink -f "$skill_md")"
        if [[ "$target" != "$(readlink -f "$cmd")" ]]; then
          [[ $missing_cmd_skills -eq 0 ]] && nope "command-skill symlink points elsewhere:"
          echo "  $skill_md -> $target (expected $(readlink -f "$cmd"))"
          missing_cmd_skills=$((missing_cmd_skills+1))
        fi
      fi
    done
    [[ $missing_cmd_skills -eq 0 ]] && pass "command-skills present and fresh (or collision-skipped)"

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
