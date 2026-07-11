#!/usr/bin/env bash
# OpenCode adapter and generated-artifact ownership regressions.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0 FAIL=0
ok() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }
assert() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }

install_into() {
  local home="$1"; shift
  HOME="$home" XDG_CONFIG_HOME="$home/config" ASHA_HOME="$home/.asha" \
    bash "$REPO_ROOT/install.sh" --target opencode "$@"
}

uninstall_from() {
  local home="$1"; shift
  HOME="$home" XDG_CONFIG_HOME="$home/config" ASHA_HOME="$home/.asha" \
    bash "$REPO_ROOT/uninstall.sh" --target opencode "$@"
}

echo "--- OpenCode native install ---"
H1="$WORK/h1"; mkdir -p "$H1"
if install_into "$H1" >/dev/null 2>&1; then ok "install exits 0"; else fail "install exits 0"; fi
OC1="$H1/config/opencode"
assert "uses native singular command directory" '[[ -f "$OC1/command/session-save.md" ]]'
assert "uses native singular agent directory" '[[ -f "$OC1/agent/code-reviewer.md" ]]'
assert "uses native singular plugin directory" '[[ -f "$OC1/plugin/asha-guardrails.js" ]]'
assert "does not emit rejected plural directories" '[[ ! -e "$OC1/commands" && ! -e "$OC1/agents" && ! -e "$OC1/plugins" ]]'
assert "skill destination follows declared frontmatter name" '[[ -L "$OC1/skills/test-ping" ]]'
assert "ownership manifest records generated files" '[[ $(jq -r ".artifacts | length" "$H1/.asha/install-manifests/opencode.json") -gt 20 ]]'

echo "--- full-install retirement reconciliation ---"
orphan="$OC1/command/retired-command.md"
printf 'managed old bytes\n' >"$orphan"
orphan_hash="$(sha256sum "$orphan" | awk '{print $1}')"
manifest="$H1/.asha/install-manifests/opencode.json"
jq --arg s "$REPO_ROOT/plugins/write/commands/retired-command.md" --arg d "$orphan" --arg h "$orphan_hash" \
  '.artifacts += [{source:$s,destination:$d,type:"opencode-command",sha256:$h,orphan:false}]' \
  "$manifest" >"$manifest.tmp" && mv "$manifest.tmp" "$manifest"
ln -s "$REPO_ROOT/plugins/write/skills/retired-skill" "$OC1/skills/retired-skill"
if install_into "$H1" >/dev/null 2>&1; then ok "full reinstall reconciles retired artifacts"; else fail "full reinstall reconciles retired artifacts"; fi
assert "unchanged retired generated file is removed" '[[ ! -e "$orphan" ]]'
assert "retired generated record is removed" '! jq -e --arg d "$orphan" ".artifacts[] | select(.destination == \$d)" "$manifest" >/dev/null'
assert "broken Asha-owned skill link is removed" '[[ ! -L "$OC1/skills/retired-skill" ]]'

if command -v opencode >/dev/null 2>&1; then
  if HOME="$H1" XDG_CONFIG_HOME="$H1/config" XDG_CACHE_HOME="$H1/cache" XDG_DATA_HOME="$H1/data" \
      OPENCODE_CONFIG_DIR="$OC1" opencode agent list >"$WORK/agents" 2>"$WORK/agent.err"; then
    ok "installed OpenCode accepts rendered config"
    grep -q '^code-reviewer (subagent)$' "$WORK/agents" \
      && ok "OpenCode discovers rendered reviewer agent" \
      || fail "OpenCode discovers rendered reviewer agent"
  else
    fail "installed OpenCode accepts rendered config ($(cat "$WORK/agent.err"))"
  fi
else
  echo "  - OpenCode CLI absent; live discovery skipped"
fi

echo "--- wrapper-scoped persona injection ---"
cat >"$WORK/fake-opencode" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${OPENCODE_CONFIG_CONTENT:-}" >"$ASHA_TEST_CAPTURE"
printf '%s\n' "$*" >"$ASHA_TEST_ARGS"
EOF
chmod +x "$WORK/fake-opencode"
ASHA_TEST_CAPTURE="$WORK/config-content" ASHA_TEST_ARGS="$WORK/args" \
HOME="$H1" XDG_CONFIG_HOME="$H1/config" ASHA_HOME="$H1/.asha" \
ASHA_OPENCODE_CMD="$WORK/fake-opencode" \
  bash "$REPO_ROOT/bin/asha" opencode probe >/dev/null 2>"$WORK/wrapper.err" || fail "wrapper launch exits 0"
assert "wrapper appends an OpenCode instructions file" 'jq -e ".instructions | length > 0" "$WORK/config-content" >/dev/null'
assert "wrapper forwards harness arguments" '[[ $(cat "$WORK/args") == probe ]]'
assert "plain config directory remains the install root" '[[ -d "$OC1/skills" ]]'

echo "--- foreign collision protection ---"
H2="$WORK/h2"; mkdir -p "$H2/config/opencode/command"
printf 'foreign\n' >"$H2/config/opencode/command/session-save.md"
if install_into "$H2" >/dev/null 2>&1; then
  fail "foreign generated-file collision is refused"
else
  ok "foreign generated-file collision is refused"
fi
assert "foreign file bytes remain intact" '[[ $(cat "$H2/config/opencode/command/session-save.md") == foreign ]]'

H4="$WORK/h4"; mkdir -p "$H4/config/opencode/command/session-save.md"
if install_into "$H4" --force >/dev/null 2>&1; then
  fail "--force refuses to replace a destination directory"
else
  ok "--force refuses to replace a destination directory"
fi
assert "directory collision receives no temporary payload" '[[ -z $(find "$H4/config/opencode/command/session-save.md" -mindepth 1 -print -quit) ]]'

H5="$WORK/h5"; custom="$H5/custom-opencode"; mkdir -p "$H5"
if OPENCODE_CONFIG_DIR="$custom" install_into "$H5" >/dev/null 2>&1; then
  ok "install honors OPENCODE_CONFIG_DIR"
else
  fail "install honors OPENCODE_CONFIG_DIR"
fi
assert "custom OpenCode directory receives artifacts" '[[ -f "$custom/command/session-save.md" ]]'
assert "default XDG OpenCode directory remains unused" '[[ ! -e "$H5/config/opencode/command/session-save.md" ]]'

echo "--- modified managed artifact preservation ---"
printf '\nuser modification\n' >>"$OC1/command/session-save.md"
if uninstall_from "$H1" >"$WORK/uninstall.out" 2>"$WORK/uninstall.err"; then
  ok "uninstall exits 0 with modified artifact"
else
  fail "uninstall exits 0 with modified artifact"
fi
assert "modified managed file is preserved" 'grep -q "user modification" "$OC1/command/session-save.md"'
assert "modified file remains in ownership manifest" 'jq -e --arg d "$OC1/command/session-save.md" ".artifacts[] | select(.destination == \$d)" "$H1/.asha/install-manifests/opencode.json" >/dev/null'
assert "unmodified guardrail plugin is removed" '[[ ! -e "$OC1/plugin/asha-guardrails.js" ]]'
assert "foreign and modified files are never reported as removed" 'grep -q "preserving modified managed artifact" "$WORK/uninstall.err"'

echo ""
echo "test-opencode: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
