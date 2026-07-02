#!/usr/bin/env bash
# lib/build.sh — asha build engine: package plugins for Copilot-native
# distribution (`asha build copilot`, issue #3).
#
# Emits a self-contained dist tree that an org can publish as a Copilot plugin
# marketplace (git init/tag/push to an internal mirror):
#
#   dist/copilot/
#   ├── marketplace.json          # index of built plugins            (UNVERIFIED schema)
#   ├── README.md                 # provenance: source SHA, versions, install matrix
#   ├── settings-snippet.json     # enabledPlugins block for consumer repos
#   └── plugins/asha-<ns>/
#       ├── plugin.json           # {name, version, description}      (UNVERIFIED fields)
#       ├── skills/<s>/SKILL.md   # real skills copied; commands/*.md converted
#       ├── agents/<a>.agent.md   # converted via _copilot_emit_agent_md
#       └── <other content dirs>  # modules/recipes/templates/tools/... verbatim
#
# NEVER packaged: hooks/ (Claude-schema mismatch + plugin hooks don't fire —
# github/copilot-cli#2540), .claude-plugin/ (Claude-era manifest).
#
# Copilot schema facts (marketplace owner+source, enabledPlugins object map,
# plugin.json minimal fields, ...) were established by live plant-and-probe.
# SINGLE SOURCE for the verification table: docs/distribution-copilot.md —
# update it there, not here (feedback_no_duplication).
#
# Does NOT `set -e` at source scope (callers own shell options; bin/asha wraps
# invocations in a `set -euo pipefail` subshell).
#
# Public entry point: asha_build_main "$@".

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

# shellcheck source=lib/portable.sh
source "$MARKET_ROOT/lib/portable.sh"
ABS_MARKET_ROOT="$(resolve_path "$MARKET_ROOT")"
PLUGINS_DIR="$MARKET_ROOT/plugins"

# ---------------------------------------------------------------------------
# Shared helpers (engine convention — mirrors lib/uninstall.sh)
# ---------------------------------------------------------------------------

die()  { echo "ERROR: ${1:-}" >&2; exit "${2:-1}"; }
log()  { [[ ${VERBOSE:-0} -eq 1 ]] && echo "  $*" >&2; return 0; }
info() { echo "$*" >&2; }
say()  { echo "$*"; }
ensure_dir() {
  [[ -d "$1" ]] && return 0
  if [[ ${DRY_RUN:-0} -eq 1 ]]; then log "would mkdir -p $1"; else mkdir -p "$1"; fi
}

# shellcheck source=harnesses/copilot-common.sh
source "$MARKET_ROOT/harnesses/copilot-common.sh"

# Namespaces built by default: the team basekit. Everything else is personal
# (soft-skip; explicit --only opts in). output-styles is hard-skipped
# (Claude-only) via _copilot_is_skip_plugin.
BUILD_DEFAULT_PLUGINS=(code devops security prompt)

usage() {
  cat <<'EOF'
asha build — package asha plugins for native distribution.

Usage:
  asha build copilot [--only ns1,ns2] [--out DIR] [--version X.Y.Z]
                     [--dry-run] [--force] [--verbose]

Options:
  --only ns1,ns2    Namespaces to build (default: code,devops,security,prompt).
                    Personal namespaces (admin asha image panel schedule
                    session test write) require explicit --only.
  --out DIR         Output directory (default: <repo>/dist/copilot).
  --version X.Y.Z   Override per-plugin versions (default: parsed from each
                    plugins/<ns>/README.md '**Version**:' line).
  --dry-run         Print the emit plan without writing.
  --force           Allow a non-empty --out (owned subtrees are replaced).
EOF
  exit 0
}

build_parse_args() {
  TARGET=""; ONLY=""; OUT="$MARKET_ROOT/dist/copilot"
  VERSION_OVERRIDE=""; DRY_RUN=0; FORCE=0; VERBOSE=0
  # Value-taking flags validate BEFORE consuming: `--out` as the last arg
  # would otherwise make the loop-bottom shift fail with $#=0 and die
  # silently under the dispatcher's set -e (review finding).
  while [[ $# -gt 0 ]]; do
    case "$1" in
      copilot) TARGET="copilot" ;;
      --target) [[ -n "${2:-}" ]] || die "--target requires a value" 2; TARGET="$2"; shift ;;
      --target=*) TARGET="${1#--target=}" ;;
      --only) [[ -n "${2:-}" ]] || die "--only requires a value" 2; ONLY="$2"; shift ;;
      --only=*) ONLY="${1#--only=}" ;;
      --out) [[ -n "${2:-}" ]] || die "--out requires a value" 2; OUT="$2"; shift ;;
      --out=*) OUT="${1#--out=}" ;;
      --version) [[ -n "${2:-}" ]] || die "--version requires a value" 2; VERSION_OVERRIDE="$2"; shift ;;
      --version=*) VERSION_OVERRIDE="${1#--version=}" ;;
      --dry-run) DRY_RUN=1 ;;
      --force) FORCE=1 ;;
      --verbose|-v) VERBOSE=1 ;;
      -h|--help) usage ;;
      *) die "unknown arg: $1 (see: asha build --help)" 2 ;;
    esac
    shift
  done
  [[ "$TARGET" == "copilot" ]] \
    || die "asha build supports only 'copilot' (codex/claude consume the source tree directly)" 2
}

# Echo the namespaces to build, one per line. Validates --only entries:
# unknown dirs, Claude-only plugins, duplicates (which would nest cp -R
# copies and double marketplace entries), and empty selections all die.
build_selected_plugins() {
  local ns s
  if [[ -z "$ONLY" ]]; then
    printf '%s\n' "${BUILD_DEFAULT_PLUGINS[@]}"
    return 0
  fi
  local -a seen=()
  for ns in ${ONLY//,/ }; do
    [[ -d "$PLUGINS_DIR/$ns" ]] || die "--only: not a plugin dir: $ns" 2
    if _copilot_is_skip_plugin "$ns"; then
      die "--only: '$ns' is Claude-only and cannot be packaged for Copilot" 2
    fi
    for s in "${seen[@]-}"; do
      [[ "$s" == "$ns" ]] && die "--only: duplicate namespace '$ns'" 2
    done
    seen+=("$ns")
    printf '%s\n' "$ns"
  done
  [[ ${#seen[@]} -gt 0 ]] || die "--only: no namespaces selected" 2
}

# --- per-plugin metadata ----------------------------------------------------

_build_plugin_version() { # ns -> echoes version
  local ns="$1"
  if [[ -n "$VERSION_OVERRIDE" ]]; then echo "$VERSION_OVERRIDE"; return 0; fi
  local v
  v="$(grep -m1 -oE '^\*\*Version\*\*: *[0-9]+\.[0-9]+(\.[0-9]+)?' "$PLUGINS_DIR/$ns/README.md" 2>/dev/null \
       | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' || true)"
  [[ -n "$v" ]] \
    || die "no '**Version**: X.Y.Z' line in plugins/$ns/README.md — fix it or pass --version" 2
  echo "$v"
}

_build_plugin_description() { # ns -> echoes one-line description
  local ns="$1"
  # First non-empty, non-heading, non-metadata prose line of the README.
  local d
  d="$(grep -m1 -vE '^(#|\*\*|\s*$|---|>)' "$PLUGINS_DIR/$ns/README.md" 2>/dev/null || true)"
  echo "${d:-asha $ns plugin}"
}

# --- emit steps ---------------------------------------------------------------

_build_write() { # dest content-via-stdin ; honors DRY_RUN
  local dest="$1"
  if [[ $DRY_RUN -eq 1 ]]; then
    say "  EMIT  $dest"
    cat >/dev/null
  else
    ensure_dir "$(dirname "$dest")"
    cat > "$dest"
    log "wrote $dest"
  fi
}

_build_emit_plugin_json() { # ns dest_root version
  local ns="$1" dest="$2" version="$3"
  local desc; desc="$(_build_plugin_description "$ns")"
  jq -n --arg name "asha-$ns" --arg version "$version" --arg desc "$desc" \
    '{name: $name, version: $version, description: $desc}' \
    | _build_write "$dest/plugin.json"
}

_build_copy_skills() { # ns dest_root
  local ns="$1" dest="$2"
  local src_dir="$PLUGINS_DIR/$ns/skills"
  [[ -d "$src_dir" ]] || return 0
  local skill
  for skill in "$src_dir"/*/; do
    [[ -d "$skill" ]] || continue
    local skill_name; skill_name="$(basename "$skill")"
    [[ -f "$skill/SKILL.md" ]] || { info "WARN: [$ns] skill without SKILL.md skipped: $skill_name"; continue; }
    local declared; declared="$(_copilot_skill_name_from_md "$skill/SKILL.md")"
    local dest_name="${declared:-${ns}-${skill_name}}"
    if [[ $DRY_RUN -eq 1 ]]; then
      say "  COPY  skills/$dest_name/ (from plugins/$ns/skills/$skill_name)"
    else
      ensure_dir "$dest/skills"
      cp -R "${skill%/}" "$dest/skills/$dest_name"
    fi
  done
}

# Rewrite paths in a GENERATED file so they resolve inside the dist plugin.
#   depth: number of "../" hops from the file to the plugin root.
#   own-ns $ASHA_ROOT refs -> relative; relative md links gain one level
#   (generated skills sit one dir deeper than source commands/).
#   Cross-namespace $ASHA_ROOT refs are left intact but WARNed (auditable).
_build_rewrite_paths() { # file ns rel_prefix
  local file="$1" ns="$2" rel="$3"
  [[ -f "$file" ]] || return 0
  # Relative md links gain one level ONLY for command-skills (rel ../../):
  # generated skills sit one dir deeper than source commands/, but agents
  # keep their depth. Run this FIRST so the $ASHA_ROOT rewrite below cannot
  # be re-bumped by it (review findings: agent links broken; compounding).
  if [[ "$rel" == "../../" ]] && grep -q '](\.\./' "$file"; then
    sed -i.bak 's#](\.\./#](../../#g' "$file" && rm -f "$file.bak"
    info "  REWROTE relative md links (+1 level) in ${file##*/skills/}"
  fi
  local before_own='\$ASHA_ROOT/plugins/'"$ns"'/'
  if grep -qE "$before_own" "$file"; then
    # Replace the path only — surrounding quotes must stay balanced.
    sed -i.bak "s#\\\$ASHA_ROOT/plugins/$ns/#$rel#g" "$file" && rm -f "$file.bak"
    info "  REWROTE own-ns \$ASHA_ROOT refs -> $rel in ${file##*/skills/}"
  fi
  local residue
  residue="$(grep -n '\$ASHA_ROOT' "$file" || true)"
  [[ -n "$residue" ]] && info "WARN: [$ns] unresolvable \$ASHA_ROOT reference(s) survive in $file:"$'\n'"$residue"
  return 0
}

_build_emit_command_skills() { # ns dest_root
  local ns="$1" dest="$2"
  local src_dir="$PLUGINS_DIR/$ns/commands"
  [[ -d "$src_dir" ]] || return 0
  local cmd
  for cmd in "$src_dir"/*.md; do
    [[ -f "$cmd" ]] || continue
    local declared; declared="$(_copilot_skill_name_from_md "$cmd")"
    if [[ -z "$declared" ]]; then
      info "WARN: [$ns] command MD missing name: frontmatter; skipped: $cmd"
      continue
    fi
    # Collision guard: a real plugin skill copied under this name wins.
    if [[ -d "$dest/skills/$declared" && $DRY_RUN -eq 0 ]]; then
      log "[$ns] skip command-skill '$declared' (plugin skill claims this name)"
      continue
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
      say "  EMIT  skills/$declared/SKILL.md (from plugins/$ns/commands/$(basename "$cmd"))"
    else
      ensure_dir "$dest/skills/$declared"
      _copilot_emit_command_skill "$cmd" "$dest/skills/$declared/SKILL.md"
      _build_rewrite_paths "$dest/skills/$declared/SKILL.md" "$ns" "../../"
    fi
  done
}

_build_emit_agents() { # ns dest_root
  local ns="$1" dest="$2"
  local src_dir="$PLUGINS_DIR/$ns/agents"
  [[ -d "$src_dir" ]] || return 0
  local agent
  for agent in "$src_dir"/*.md; do
    [[ -f "$agent" ]] || continue
    local base; base="$(basename "$agent" .md)"
    local declared; declared="$(_copilot_skill_name_from_md "$agent")"
    local dest_name="${declared:-$base}"
    if [[ $DRY_RUN -eq 1 ]]; then
      say "  EMIT  agents/$dest_name.agent.md (from plugins/$ns/agents/$base.md)"
    else
      ensure_dir "$dest/agents"
      _copilot_emit_agent_md "$agent" "$dest/agents/$dest_name.agent.md"
      _build_rewrite_paths "$dest/agents/$dest_name.agent.md" "$ns" "../"
    fi
  done
}

# Copy remaining plugin content verbatim, EXCEPT converted/excluded entries.
_build_copy_content() { # ns dest_root
  local ns="$1" dest="$2"
  local entry base
  for entry in "$PLUGINS_DIR/$ns"/* "$PLUGINS_DIR/$ns"/.claude-plugin; do
    [[ -e "$entry" ]] || continue
    base="$(basename "$entry")"
    case "$base" in
      skills|commands|agents) continue ;;         # converted above
      hooks|hooks.json) continue ;;               # EXCLUDED: Claude schema + copilot-cli#2540
      .claude-plugin) continue ;;                 # Claude-era manifest
    esac
    if [[ $DRY_RUN -eq 1 ]]; then
      say "  COPY  $base"
    else
      cp -R "$entry" "$dest/$base"
    fi
  done
}

# --- dist-level emissions -----------------------------------------------------

_build_emit_marketplace_json() { # built list "ns version desc" lines on stdin
  # Schema verified empirically against Copilot CLI 1.0.65's validator
  # (2026-07-01): top-level `owner` is required and plugin entries use
  # `source` (relative path), matching the Claude marketplace.json format.
  jq -Rn '
    {name: "asha",
     owner: {name: "asha"},
     plugins: [inputs | select(length > 0) | split("\t")
               | {name: ("asha-" + .[0]),
                  source: ("./plugins/asha-" + .[0]),
                  version: .[1],
                  description: .[2]}]}' \
    | _build_write "$OUT/marketplace.json"
}

_build_emit_settings_snippet() {
  # enabledPlugins is an OBJECT map keyed "plugin@marketplace" (verified
  # empirically 2026-07-01: installing writes {"asha-test@asha": true} into
  # ~/.copilot/settings.json), not an array.
  jq -Rn '
    {enabledPlugins: ([inputs | select(length > 0) | split("\t")
                       | {key: ("asha-" + .[0] + "@asha"), value: true}]
                      | from_entries)}' \
    | _build_write "$OUT/settings-snippet.json"
}

_build_emit_dist_readme() { # built-lines file
  local built_file="$1"
  local sha date
  sha="$(git -C "$MARKET_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  # Deterministic Built line (source commit date, not wall clock): rebuilds
  # of the same tree must be byte-identical — a minute-resolution wall clock
  # made that a coin flip across minute boundaries (review finding).
  date="$(TZ=UTC git -C "$MARKET_ROOT" log -1 --date=format-local:'%Y-%m-%d %H:%M UTC' --format=%cd 2>/dev/null || date -u +'%Y-%m-%d %H:%M UTC')"
  git -C "$MARKET_ROOT" diff-index --quiet HEAD -- 2>/dev/null || date="$date (+ uncommitted changes)"
  {
    echo "# asha — Copilot plugin distribution"
    echo
    echo "Generated by \`asha build copilot\`. Do not edit — regenerate from source."
    echo
    echo "- **Source commit**: $sha"
    echo "- **Built**: $date"
    echo
    echo "| Plugin | Version | Description |"
    echo "|--------|---------|-------------|"
    awk -F'\t' '{printf "| asha-%s | %s | %s |\n", $1, $2, $3}' "$built_file"
    echo
    echo "## Install"
    echo
    echo '```bash'
    echo "# Direct from this repo (per plugin):"
    echo "copilot plugin install <owner>/<this-repo>:plugins/asha-code"
    echo
    echo "# Via marketplace:"
    echo "copilot plugin marketplace add <owner>/<this-repo>"
    echo "copilot plugin install asha-code@asha"
    echo '```'
    echo
    echo "Repo-scope pinning: merge \`settings-snippet.json\` into your repo's"
    echo '`.github/copilot/settings.json` (`enabledPlugins`).'
  } | _build_write "$OUT/README.md"
}

# --- orchestrator -------------------------------------------------------------

build_copilot() {
  command -v jq      >/dev/null 2>&1 || die "jq required for build" 3
  command -v python3 >/dev/null 2>&1 || die "python3 required for build (frontmatter conversion)" 3

  say "build: asha root = $ABS_MARKET_ROOT"
  say "   out = $OUT"
  [[ $DRY_RUN -eq 1 ]] && say "   (dry-run)"

  # Materialize the selection AND all per-plugin metadata BEFORE anything
  # destructive: die()s inside process substitutions would kill only the
  # feeder subshell (issue-#4 class), and validation — including per-plugin
  # version parsing, which can fail — must fully precede the --force cleanup,
  # or a refused build would leave the previous dist wiped/partial (review
  # finding: destructive-before-validate, second occurrence).
  local selected
  selected="$(build_selected_plugins)" || return $?

  local manifest="" ns version desc
  while read -r ns; do
    [[ -n "$ns" ]] || continue
    version="$(_build_plugin_version "$ns")" || return $?
    desc="$(_build_plugin_description "$ns")"
    manifest+="$ns"$'\t'"$version"$'\t'"$desc"$'\n'
  done <<< "$selected"

  # Out-dir hygiene: refuse non-empty without --force. --force removes ONLY
  # what this build owns: plugins/asha-* and the three generated metadata
  # files — a foreign plugins/<other>/ in a shared dist repo survives.
  if [[ -d "$OUT" && -n "$(ls -A "$OUT" 2>/dev/null)" ]]; then
    if [[ $FORCE -eq 1 ]]; then
      if [[ $DRY_RUN -eq 0 ]]; then
        rm -rf "$OUT"/plugins/asha-* "$OUT/marketplace.json" "$OUT/settings-snippet.json" "$OUT/README.md"
      fi
    else
      die "output dir not empty: $OUT (use --force to replace the build-owned subtrees)" 2
    fi
  fi

  local built_file="${TMPDIR:-/tmp}/.asha-build-manifest.$$"
  printf '%s' "$manifest" > "$built_file"

  while IFS=$'\t' read -r ns version desc; do
    [[ -n "$ns" ]] || continue
    say ""
    say "== [build] plugins/asha-$ns  (v$version) =="
    local dest="$OUT/plugins/asha-$ns"
    ensure_dir "$dest"
    _build_emit_plugin_json     "$ns" "$dest" "$version"
    _build_copy_skills          "$ns" "$dest"
    _build_emit_command_skills  "$ns" "$dest"
    _build_emit_agents          "$ns" "$dest"
    _build_copy_content         "$ns" "$dest"
  done <<< "$manifest"

  say ""
  say "== [build] dist metadata =="
  _build_emit_marketplace_json  < "$built_file"
  _build_emit_settings_snippet  < "$built_file"
  _build_emit_dist_readme "$built_file"
  rm -f "$built_file"

  say ""
  if [[ $DRY_RUN -eq 1 ]]; then
    say "dry-run complete (nothing written)."
  else
    say "build complete: $OUT"
    say "publish: git -C '$OUT' init && git add -A && commit/tag/push to your distribution remote"
    say "         (see docs/distribution-copilot.md)"
  fi
}

asha_build_main() {
  build_parse_args "$@"
  build_copilot
}
