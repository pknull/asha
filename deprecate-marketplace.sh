#!/usr/bin/env bash
# deprecate-marketplace.sh — retire the Claude plugin-marketplace registration.
#
# Removes legacy registry state so that marketplace primitives are served
# ONLY by the symlinks install.sh creates under ~/.claude/*. Does NOT touch
# the source tree at ~/life/marketplace/plugins/ or any symlink the installer
# owns.
#
# Steps performed (all reversible via git on the edited JSON files):
#   1. Backup ~/.claude/settings.json and ~/.claude/plugins/installed_plugins.json
#   2. Remove the asha-marketplace entry from installed_plugins.json
#   3. Remove all enabledPlugins entries whose key ends with "@asha-marketplace"
#   4. Remove the ~/.claude/plugins/marketplaces/asha-marketplace symlink
#   5. Run install.sh --force to ensure every primitive is symlinked
#
# Usage:
#   ./deprecate-marketplace.sh [--dry-run]
#
# Exit codes:
#   0 success
#   1 usage / missing deps
#   2 safety refused (legacy state already gone, nothing to do)
#   4 json edit failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
MARKET_ROOT="$SCRIPT_DIR"

CLAUDE_HOME="$HOME/.claude"
SETTINGS_FILE="$CLAUDE_HOME/settings.json"
INSTALLED_FILE="$CLAUDE_HOME/plugins/installed_plugins.json"
MARKETPLACES_DIR="$CLAUDE_HOME/plugins/marketplaces"
LEGACY_LINK="$MARKETPLACES_DIR/asha-marketplace"

DRY_RUN=0

die() { echo "ERROR: $*" >&2; exit "${2:-1}"; }
say() { echo "$*"; }
info() { echo "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

command -v jq >/dev/null 2>&1 || die "jq not found"
[[ -f "$SETTINGS_FILE" ]]  || die "missing $SETTINGS_FILE"
[[ -f "$INSTALLED_FILE" ]] || die "missing $INSTALLED_FILE"

# ---------------------------------------------------------------------------
# Preflight: is there actually legacy state to remove?
# ---------------------------------------------------------------------------

enabled_count="$(jq -r '[.enabledPlugins // {} | to_entries[] | select(.key | endswith("@asha-marketplace"))] | length' "$SETTINGS_FILE")"
installed_has="$(jq -r 'if .plugins | keys | any(endswith("@asha-marketplace")) then "yes" else "no" end' "$INSTALLED_FILE")"
link_exists=0
[[ -L "$LEGACY_LINK" ]] && link_exists=1

if [[ "$enabled_count" -eq 0 && "$installed_has" == "no" && "$link_exists" -eq 0 ]]; then
  info "No legacy asha-marketplace state found. Nothing to deprecate."
  exit 2
fi

say "Legacy asha-marketplace state detected:"
say "  enabledPlugins entries: $enabled_count"
say "  installed_plugins.json: $installed_has"
say "  marketplaces symlink:   $([[ $link_exists -eq 1 ]] && echo yes || echo no)"
say ""

if [[ $DRY_RUN -eq 1 ]]; then
  say "--dry-run: would back up, strip enabledPlugins and installed_plugins, remove symlink, then install.sh --force"
  exit 0
fi

# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

stamp="$(date +%Y%m%d-%H%M%S)"
cp -p "$SETTINGS_FILE"  "$SETTINGS_FILE.bak-deprecate-$stamp"
cp -p "$INSTALLED_FILE" "$INSTALLED_FILE.bak-deprecate-$stamp"
say "backed up settings.json  -> $SETTINGS_FILE.bak-deprecate-$stamp"
say "backed up installed.json -> $INSTALLED_FILE.bak-deprecate-$stamp"

settings_update_single() {
  local expr="$1"
  local tmp="$2.tmp.$$"
  jq "$expr" "$2" > "$tmp" || { rm -f "$tmp"; die "jq filter failed on $2" 4; }
  jq empty "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "$2 invalid after filter" 4; }
  mv "$tmp" "$2"
}

# ---------------------------------------------------------------------------
# Strip enabledPlugins
# ---------------------------------------------------------------------------

if [[ "$enabled_count" -gt 0 ]]; then
  settings_update_single '
    .enabledPlugins = (
      .enabledPlugins
      | with_entries(select((.key | endswith("@asha-marketplace")) | not))
    )
  ' "$SETTINGS_FILE"
  say "removed $enabled_count enabledPlugins entries tied to asha-marketplace"
fi

# ---------------------------------------------------------------------------
# Strip installed_plugins.json entries
# ---------------------------------------------------------------------------

if [[ "$installed_has" == "yes" ]]; then
  before="$(jq -r '[.plugins | keys[] | select(endswith("@asha-marketplace"))] | length' "$INSTALLED_FILE")"
  settings_update_single '
    .plugins = (
      .plugins
      | with_entries(select((.key | endswith("@asha-marketplace")) | not))
    )
  ' "$INSTALLED_FILE"
  say "removed $before plugin entries from installed_plugins.json"
fi

# ---------------------------------------------------------------------------
# Remove legacy symlink
# ---------------------------------------------------------------------------

if [[ $link_exists -eq 1 ]]; then
  rm "$LEGACY_LINK"
  say "removed legacy symlink: $LEGACY_LINK"
fi

# ---------------------------------------------------------------------------
# Ensure all primitives are installed via the symlink-mount model
# ---------------------------------------------------------------------------

say ""
say "ensuring every primitive is symlinked via install.sh --force..."
"$MARKET_ROOT/install.sh" --force

say ""
say "deprecation complete. To reverse:"
say "  cp $SETTINGS_FILE.bak-deprecate-$stamp $SETTINGS_FILE"
say "  cp $INSTALLED_FILE.bak-deprecate-$stamp $INSTALLED_FILE"
say "  ln -s $MARKET_ROOT $LEGACY_LINK"
say "  restart Claude Code"
