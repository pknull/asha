#!/usr/bin/env bash
# lib/init-repo.sh — `asha init-repo` verb: scaffold Copilot/agent standards
# files into a target repository (team onboarding, issue #3).
#
# File classes (drive both scaffold and --check semantics):
#   stub    AGENTS.md — created if absent, NEVER overwritten, content never
#           compared (meant to be edited).
#   marker  .github/instructions/team-conventions.instructions.md — carries a
#           managed-marker comment. --check: marker present + differs from
#           template = DRIFT (fail); marker removed = LOCAL (team took
#           ownership, ok).
#   strict  .github/copilot/settings.json — must exist, parse, and carry an
#           enabledPlugins key; values are team-owned and never compared.
#
# asha never writes .github/copilot-instructions.md — that is native
# `copilot init` territory (codebase analysis); we only hint.
#
# Exit: 0 ok, 1 --check drift/missing, 2 usage error.
# Does NOT `set -e` at source scope (bin/asha wraps in set -euo pipefail).
#
# Public entry point: asha_init_repo_main "$@".

# Resolve repo root from THIS file's location (portable; no GNU readlink -f).
# asha-bootstrap-symlink-walk: resolve our own real path, portable (readlink -f is GNU-only).
# Duplicated across 7 scripts — find all: `grep -rn asha-bootstrap-symlink-walk`. Cannot DRY into
# lib/portable.sh:resolve_path() — this runs *before* portable.sh is locatable. Keep copies in sync.
__eng_src="${BASH_SOURCE[0]}"
while [ -h "$__eng_src" ]; do
  __eng_dir="$(cd -P "$(dirname "$__eng_src")" >/dev/null 2>&1 && pwd)"
  __eng_src="$(readlink "$__eng_src")"
  case "$__eng_src" in /*) ;; *) __eng_src="$__eng_dir/$__eng_src" ;; esac
done
__ASHA_LIB_DIR="$(cd -P "$(dirname "$__eng_src")" >/dev/null 2>&1 && pwd)"
unset __eng_src __eng_dir
MARKET_ROOT="${MARKET_ROOT:-$(dirname "$__ASHA_LIB_DIR")}"

_IR_MARKER='<!-- asha:init-repo'

_ir_usage() {
  cat <<'EOF'
asha init-repo — scaffold agent/Copilot standards files into a repository.

Usage:
  asha init-repo [--dir D] [--dry-run | --check] [--force] [--template NAME]

Writes (idempotent; existing files are never clobbered without --force):
  AGENTS.md                                          agent guidance stub
  .github/instructions/team-conventions.instructions.md   team norms (managed marker)
  .github/copilot/settings.json                      enabledPlugins pin file

Modes:
  --dry-run   print the WRITE/SKIP plan, change nothing
  --check     CI mode: verify conformance (OK/MISSING/DRIFT/LOCAL), exit 1 on
              MISSING or DRIFT; a marker-removed file is LOCAL (team-owned, ok)
  --force     overwrite stub/marker files from templates (git is the backup);
              settings.json values are never touched

Out of scope by design: persona/identity, secrets, hooks (user-scope), and
.github/copilot-instructions.md (run native `copilot init` for that).
EOF
}

# Classify a template-relative path. Echoes stub|marker|strict.
_ir_class() {
  case "$1" in
    AGENTS.md) echo stub ;;
    *.instructions.md) echo marker ;;
    .github/copilot/settings.json) echo strict ;;
    *) echo stub ;;
  esac
}

_ir_scaffold() { # rel src dest class
  local rel="$1" src="$2" dest="$3" class="$4"
  if [[ -f "$dest" ]]; then
    if [[ $IR_FORCE -eq 1 && "$class" != "strict" ]]; then
      if [[ $IR_DRY -eq 1 ]]; then echo "  WRITE (force)  $rel"; else
        cp "$src" "$dest"; echo "  WROTE (force)  $rel"
      fi
    elif [[ "$class" == "strict" ]]; then
      # never clobber; re-add a missing enabledPlugins key only
      if jq -e '.enabledPlugins' "$dest" >/dev/null 2>&1; then
        echo "  SKIP   $rel (exists)"
      elif [[ $IR_DRY -eq 1 ]]; then
        echo "  WRITE  $rel (re-add enabledPlugins key)"
      else
        local tmp="$dest.tmp.$$"
        if jq '.enabledPlugins = (.enabledPlugins // {})' "$dest" > "$tmp" 2>/dev/null; then
          mv "$tmp" "$dest"; echo "  WROTE  $rel (re-added enabledPlugins key)"
        else
          rm -f "$tmp"; echo "  SKIP   $rel (exists but invalid JSON — fix by hand)" >&2
        fi
      fi
    else
      echo "  SKIP   $rel (exists)"
    fi
    return 0
  fi
  if [[ $IR_DRY -eq 1 ]]; then
    echo "  WRITE  $rel"
  else
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
    echo "  WROTE  $rel"
  fi
}

_ir_check() { # rel src dest class ; increments IR_FAILS
  local rel="$1" src="$2" dest="$3" class="$4"
  if [[ ! -f "$dest" ]]; then
    echo "  MISSING  $rel"
    IR_FAILS=$((IR_FAILS+1))
    return 0
  fi
  case "$class" in
    stub)
      echo "  OK       $rel" ;;
    marker)
      if grep -qF "$_IR_MARKER" "$dest"; then
        if cmp -s "$src" "$dest"; then
          echo "  OK       $rel"
        else
          echo "  DRIFT    $rel (differs from template while still marker-managed)"
          IR_FAILS=$((IR_FAILS+1))
        fi
      else
        echo "  LOCAL    $rel (marker removed — team-owned)"
      fi ;;
    strict)
      if ! jq empty "$dest" 2>/dev/null; then
        echo "  DRIFT    $rel (invalid JSON)"
        IR_FAILS=$((IR_FAILS+1))
      elif ! jq -e 'has("enabledPlugins")' "$dest" >/dev/null 2>&1; then
        echo "  DRIFT    $rel (missing enabledPlugins key)"
        IR_FAILS=$((IR_FAILS+1))
      else
        echo "  OK       $rel"
      fi ;;
  esac
}

asha_init_repo_main() {
  local dir="$PWD" template="default" mode="scaffold"
  IR_DRY=0; IR_FORCE=0; IR_FAILS=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) shift; dir="${1:-}" ;;
      --dir=*) dir="${1#--dir=}" ;;
      --template) shift; template="${1:-}" ;;
      --template=*) template="${1#--template=}" ;;
      --dry-run) IR_DRY=1 ;;
      --check) mode="check" ;;
      --force) IR_FORCE=1 ;;
      -h|--help) _ir_usage; return 0 ;;
      *) echo "ERROR: unknown arg: $1 (see: asha init-repo --help)" >&2; return 2 ;;
    esac
    shift
  done

  command -v jq >/dev/null 2>&1 || { echo "ERROR: jq required" >&2; return 3; }
  local tdir="$MARKET_ROOT/templates/init-repo/$template"
  [[ -d "$tdir" ]] || { echo "ERROR: unknown template '$template' (no $tdir)" >&2; return 2; }
  [[ -d "$dir" ]] || { echo "ERROR: target dir not found: $dir" >&2; return 2; }

  # This is a repo-scaffolding tool: refuse non-git targets unless --force
  # (git is the undo mechanism for --force overwrites).
  if ! git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [[ $IR_FORCE -eq 1 ]]; then
      echo "WARN: $dir is not a git worktree (continuing under --force)" >&2
    else
      echo "ERROR: $dir is not a git worktree (use --force to scaffold anyway)" >&2
      return 2
    fi
  fi

  echo "init-repo: target = $dir  (template: $template, mode: $mode$( [[ $IR_DRY -eq 1 ]] && echo ', dry-run'))"

  local src rel class dest
  while IFS= read -r src; do
    rel="${src#"$tdir"/}"
    class="$(_ir_class "$rel")"
    dest="$dir/$rel"
    if [[ "$mode" == "check" ]]; then
      _ir_check "$rel" "$src" "$dest" "$class"
    else
      _ir_scaffold "$rel" "$src" "$dest" "$class"
    fi
  done < <(find "$tdir" -type f | sort)

  # Compose with, never duplicate, native codebase analysis.
  if [[ ! -f "$dir/.github/copilot-instructions.md" ]]; then
    echo "hint: run 'copilot init' in $dir to generate .github/copilot-instructions.md (native codebase analysis — asha does not write it)"
  fi

  if [[ "$mode" == "check" ]]; then
    if [[ $IR_FAILS -gt 0 ]]; then
      echo "init-repo --check: $IR_FAILS problem(s)"
      return 1
    fi
    echo "init-repo --check: conforming"
  fi
  return 0
}
