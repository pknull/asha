#!/usr/bin/env bash
# uninstall.sh — reverse the symlink-mount install.
#
# Scans ~/.claude/{skills,agents,commands,output-styles} for symlinks whose
# realpath resolves inside this asha tree and removes them. Strips
# ~/.claude/settings.json hook entries tagged "source": "asha:*" (and
# legacy "source": "marketplace:*" for migration cleanup).
#
# Source tree is never touched.
#
# Usage:
#   ./uninstall.sh [--dry-run] [--verbose]
#
# Exit codes:
#   0  success (or dry-run)
#   3  dependency missing (jq)
#   4  settings.json edit failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
MARKET_ROOT="$SCRIPT_DIR"
ABS_MARKET_ROOT="$(readlink -f "$MARKET_ROOT")"

CLAUDE_HOME="$HOME/.claude"
SETTINGS_FILE="$CLAUDE_HOME/settings.json"

DRY_RUN=0
VERBOSE=0

die() { echo "ERROR: $*" >&2; exit "${2:-1}"; }
# log/info go to stderr so they don't contaminate $(...) captures of function output.
log()  { [[ $VERBOSE -eq 1 ]] && echo "  $*" >&2; return 0; }
info() { echo "$*" >&2; }
say()  { echo "$*"; }

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --verbose|-v) VERBOSE=1 ;;
      -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "unknown arg: $1" 1 ;;
    esac
    shift
  done
}

# Atomic settings.json update; skips on dry-run.
settings_update() {
  local jq_expr="$1"
  local tmp="$SETTINGS_FILE.tmp.$$"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "would apply jq filter to $SETTINGS_FILE"
    return 0
  fi
  jq "$jq_expr" "$SETTINGS_FILE" > "$tmp" || { rm -f "$tmp"; die "jq filter failed" 4; }
  jq empty "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "resulting settings.json invalid" 4; }
  mv "$tmp" "$SETTINGS_FILE"
}

# Back up settings.json once per run.
_backup_done=0
backup_settings_once() {
  [[ $DRY_RUN -eq 1 ]] && return 0
  [[ $_backup_done -eq 1 ]] && return 0
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  cp -p "$SETTINGS_FILE" "$SETTINGS_FILE.bak-$stamp"
  say "backed up settings.json -> $SETTINGS_FILE.bak-$stamp"
  _backup_done=1
}

# Remove symlinks under $1 whose realpath starts with $ABS_MARKET_ROOT.
# Max-depth limits kept to directories where the installer places links.
remove_symlinks_under() {
  local dir="$1" maxdepth="${2:-2}" count=0
  [[ -d "$dir" ]] || return 0

  while IFS= read -r -d '' link; do
    local target
    target="$(readlink -f "$link" 2>/dev/null || true)"
    [[ -z "$target" ]] && { log "dangling: $link (removing)"; rm -f "$link"; count=$((count+1)); continue; }
    case "$target" in
      "$ABS_MARKET_ROOT"|"$ABS_MARKET_ROOT"/*)
        if [[ $DRY_RUN -eq 1 ]]; then
          info "  RM  $link (-> $target)"
        else
          rm -f "$link"
          log "removed: $link"
        fi
        count=$((count+1))
        ;;
      *) log "skip (foreign target): $link -> $target" ;;
    esac
  done < <(find "$dir/" -mindepth 1 -maxdepth "$maxdepth" -type l -print0)

  # Only count on stdout — everything else went to stderr via info/log.
  echo "$count"
}

# Prune empty ~/.claude/{commands,agents}/<ns>/ subdirs after symlink removal.
prune_empty_namespace_dirs() {
  local parent
  for parent in "$CLAUDE_HOME/commands" "$CLAUDE_HOME/agents"; do
    [[ -d "$parent" ]] || continue
    local sub
    for sub in "$parent"/*/; do
      [[ -d "$sub" ]] || continue
      # Only prune dirs that are empty AND are real dirs (not symlinks); a symlinked
      # user dir (like dotfiles-backed) shouldn't be rmdir'd.
      [[ -L "${sub%/}" ]] && continue
      if [[ -z "$(ls -A "$sub")" ]]; then
        if [[ $DRY_RUN -eq 1 ]]; then
          info "  RMDIR  $sub"
        else
          rmdir "$sub"
          log "rmdir: $sub"
        fi
      fi
    done
  done
}

main() {
  parse_args "$@"
  command -v jq >/dev/null 2>&1 || die "jq not found" 3

  [[ -f "$SETTINGS_FILE" ]] || die "$SETTINGS_FILE not found"

  say "uninstall.sh: asha root = $ABS_MARKET_ROOT"
  [[ $DRY_RUN -eq 1 ]] && say "   (dry-run)"

  local total=0 n
  for spec in "skills 1" "agents 2" "output-styles 1" "commands 2"; do
    set -- $spec
    local subdir="$1" depth="$2"
    n="$(remove_symlinks_under "$CLAUDE_HOME/$subdir" "$depth")"
    [[ "$n" -gt 0 ]] && say "removed $n symlink(s) from $CLAUDE_HOME/$subdir"
    total=$((total + n))
  done

  # Prune now-empty namespace dirs under commands/ and agents/.
  prune_empty_namespace_dirs

  # Strip settings.json hook entries tagged asha:* (or legacy marketplace:*).
  # The dual-match keeps uninstall safe across the rename window.
  local tag_regex='^(asha|marketplace):'
  local before after removed
  before="$(jq -r --arg re "$tag_regex" '[.hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | test($re))] | length' "$SETTINGS_FILE")"
  if [[ "$before" -gt 0 ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      say "would remove $before tagged hook entr$([[ $before -eq 1 ]] && echo y || echo ies) from settings.json"
    else
      backup_settings_once
      settings_update "
        if .hooks then
          .hooks |= with_entries(
            .value |= (
              map(
                .hooks |= map(select(((.source // \"\") | test(\"$tag_regex\")) | not))
              )
              | map(select(.hooks | length > 0))
            )
          )
          | .hooks |= with_entries(select(.value | length > 0))
        else . end
      "
      after="$(jq -r --arg re "$tag_regex" '[.hooks // {} | .[] | .[]? | .hooks[]? | select((.source // "") | test($re))] | length' "$SETTINGS_FILE")"
      removed=$((before - after))
      say "removed $removed tagged hook entr$([[ $removed -eq 1 ]] && echo y || echo ies) from settings.json"
    fi
  else
    log "no asha-tagged hooks in settings.json"
  fi

  say ""
  say "done. total symlinks removed: $total"
}

main "$@"
