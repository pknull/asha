#!/usr/bin/env bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
# Pi 1.6.1: native skills, prompt templates, and a single-agent extension.
PI_HOME="$(asha_harness_home pi)"

pi_check_version() {
  local cmd actual
  cmd="$(asha_harness_executable pi)"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "WARN: Pi executable '$cmd' not found; installing artifacts for later use" >&2
    return 0
  fi
  actual="$("$cmd" --version 2>/dev/null)" || return 1
  python3 - "$actual" <<'PY'
import re, sys
m = re.fullmatch(r"(?:v)?(\d+)\.(\d+)\.(\d+)", sys.argv[1].strip())
if not m or tuple(map(int, m.groups())) < (1, 6, 1):
    sys.exit("Pi >=1.6.1 required")
if m.group(0).lstrip('v') != '1.6.1':
    print("WARN: Pi version is outside the verified 1.6.1 release", file=sys.stderr)
PY
}

pi_render_command() {
  python3 - "$1" <<'PY'
import json, re, sys
text = open(sys.argv[1], encoding='utf-8').read()
fm, body = '', text
if text.startswith('---\n') and '\n---\n' in text[4:]:
    fm, body = text[4:].split('\n---\n', 1)
m = re.search(r'^description:\s*(.+)$', fm, re.M)
description = m.group(1).strip().strip('\"\'') if m else 'Asha command'
print('---\ndescription: ' + json.dumps(description) + '\n---\n')
print('## Pi harness adapter\n')
print('Use Pi read, write, edit and bash tools for the corresponding workflow intent. '
      'This integration is single-agent: delegated agents and independent review are unavailable. '
      'Do not fabricate delegation or claim an independent gate passed. '
      'If that gate is required, report the unsupported step. '
      'Room save instructions take precedence over generic completion or Git steps: '
      'a checkpoint keeps the Room open and does not authorize commit or push.\n')
print(body.lstrip(), end='')
PY
}

pi_render_extension() {
  python3 - "${MARKET_ROOT:-$ASHA_ROOT}" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
print((root / 'harnesses/pi-extension.ts').read_text().replace('__ASHA_ROOT__', json.dumps(str(root))), end='')
PY
}

pi_install_skills() {
  local src_dir="$1" ns="$2" kind="$3" skill name
  [[ -d "$src_dir" ]] || return 0
  validate_skill_source "$src_dir" "$kind"
  while IFS= read -r skill; do
    [[ -f "$skill/SKILL.md" ]] || continue
    if [[ "$kind" == imported ]]; then
      name="${ns}-$(basename "$skill")"
      prepare_imported_skill_adapter "$skill" "$name"
      mklink_imported_skill "$skill" "$ASHA_IMPORTED_SKILL_ADAPTER" "$PI_HOME/skills/$name" pi-skill
    else
      name="$(plugin_skill_destination_name "$skill" "$ns")" || continue
      mklink "$skill" "$PI_HOME/skills/$name" pi-skill
    fi
  done < <(skill_dirs_from_source "$src_dir" "$kind")
}

pi_install() {
  pi_check_version || return $?
  local plugin ns src kind label cmd name prepared
  ensure_dir "$PI_HOME/skills"
  ensure_dir "$PI_HOME/prompts"
  asha_artifact_begin pi
  while IFS=$'\t' read -r src ns kind label; do
    [[ -n "$src" ]] || continue
    say "== [pi] $label skills (ns=$ns) =="
    pi_install_skills "$src" "$ns" "$kind"
  done < <(selected_imported_skill_sources)
  while read -r plugin; do
    [[ -n "$plugin" ]] || continue
    ns="$(ns_for "$plugin")"
    pi_install_skills "$PLUGINS_DIR/$plugin/skills" "$ns" plugin
    for cmd in "$PLUGINS_DIR/$plugin/commands/"*.md; do
      [[ -f "$cmd" ]] || continue
      name="${ns}-$(basename "$cmd" .md)"
      prepared="$(mktemp)"
      pi_render_command "$cmd" > "$prepared" || return $?
      asha_artifact_install_prepared pi "$cmd" "$PI_HOME/prompts/$name.md" pi-command "$prepared" || return $?
      rm -f "$prepared"
    done
  done < <(selected_plugins)
  prepared="$(mktemp)"
  pi_render_extension > "$prepared" || return $?
  asha_artifact_install_prepared pi "$MARKET_ROOT/harnesses/pi-extension.ts" "$PI_HOME/extensions/asha.ts" pi-extension "$prepared" || return $?
  rm -f "$prepared"
  asha_artifact_finalize pi "$([[ -z "${ONLY:-}" ]] && echo 1 || echo 0)"
}

pi_uninstall() {
  local links=0 artifacts=0
  [[ ! -d "$PI_HOME/skills" ]] || links="$(remove_symlinks_under "$PI_HOME/skills" 2)"
  artifacts="$(asha_artifact_uninstall pi)"
  PI_UNINSTALL_TOTAL=$((links + artifacts))
  say "[pi] removed $PI_UNINSTALL_TOTAL managed artifact(s)"
}

pi_drift_check() {
  local plugin ns cmd name result=0
  pi_check_version || result=1
  asha_artifact_doctor pi || result=1
  if ! cmp -s "$PI_HOME/extensions/asha.ts" <(pi_render_extension); then
    echo 'Pi extension differs from current source' >&2; result=1
  fi
  while read -r plugin; do
    ns="$(ns_for "$plugin")"
    for cmd in "$PLUGINS_DIR/$plugin/commands/"*.md; do
      [[ -f "$cmd" ]] || continue
      name="${ns}-$(basename "$cmd" .md)"
      if ! cmp -s "$PI_HOME/prompts/$name.md" <(pi_render_command "$cmd"); then
        echo "Pi prompt missing or stale: $name" >&2; result=1
      fi
    done
  done < <(selected_plugins)
  return "$result"
}

pi_launch() {
  local cmd id op brief
  local -a context=()
  cmd="$(asha_harness_executable pi)"
  command -v "$cmd" >/dev/null 2>&1 || die "Pi executable not found: $cmd"
  pi_check_version || die "unsupported Pi version"
  # Native management commands must keep their own grammar.
  case "${1:-}" in
    install|remove|uninstall|update|list|config|auth|mcp|--version|--help) exec "$cmd" "$@" ;;
  esac
  if asha_persona_enabled; then
    id="$ASHA_HOME/cache/instructions-pi.md"
    "$MERGE_SCRIPT" "$id" >&2 || die "Pi identity render failed"
    export ASHA_PERSONA=1
    context+=(--append-system-prompt "$id")
    op="$ASHA_HOME/cache/instructions-pi-operational.md"
    if asha_render_operational_layer "$op"; then context+=(--append-system-prompt "$op"); fi
    if asha_orchestrator_stance_enabled; then
      brief="$(asha_orchestrator_brief_file)"
      [[ ! -s "$brief" ]] || context+=(--append-system-prompt "$brief")
    fi
  fi
  exec "$cmd" "${context[@]}" "$@"
}
