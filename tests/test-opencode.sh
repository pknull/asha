#!/usr/bin/env bash
# OpenCode stable-v1 adapter and generated-artifact ownership regressions.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0 FAIL=0
ok() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }
assert() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }

OPENCODE_OK="$WORK/opencode-ok"
cat >"$OPENCODE_OK" <<'EOF'
#!/usr/bin/env bash
echo 1.17.18
EOF
chmod +x "$OPENCODE_OK"

install_into() {
  local home="$1"; shift
  HOME="$home" XDG_CONFIG_HOME="$home/config" ASHA_HOME="$home/.asha" \
    ASHA_OPENCODE_CMD="${ASHA_OPENCODE_CMD:-$OPENCODE_OK}" \
    bash "$REPO_ROOT/install.sh" --target opencode "$@"
}

uninstall_from() {
  local home="$1"; shift
  HOME="$home" XDG_CONFIG_HOME="$home/config" ASHA_HOME="$home/.asha" \
    bash "$REPO_ROOT/uninstall.sh" --target opencode "$@"
}

echo "--- OpenCode stable-v1 native install ---"
H1="$WORK/h1"; mkdir -p "$H1"
if install_into "$H1" >"$WORK/install.out" 2>"$WORK/install.err"; then ok "install exits 0"; else fail "install exits 0"; fi
OC1="$H1/config/opencode"
assert "uses native plural commands directory" '[[ -f "$OC1/commands/session-save.md" ]]'
assert "uses native plural agents directory" '[[ -f "$OC1/agents/code-reviewer.md" ]]'
assert "uses native plural plugins directory" '[[ -f "$OC1/plugins/asha.js" ]]'
assert "does not emit obsolete singular directories" '[[ ! -e "$OC1/command" && ! -e "$OC1/agent" && ! -e "$OC1/plugin" ]]'
assert "skill destination follows declared frontmatter name" '[[ -L "$OC1/skills/test-ping" ]]'
assert "rendered command has OpenCode frontmatter" 'head -3 "$OC1/commands/session-save.md" | grep -q "description:"'
assert "rendered agent declares subagent mode" 'grep -q "^mode: subagent$" "$OC1/agents/code-reviewer.md"'
assert "colon-family source agent receives a valid OpenCode name" '[[ -f "$OC1/agents/rp-character-template.md" ]]'
assert "rendered RP orchestrator uses the installed OpenCode agent name" 'grep -q '\''subagent_type: "rp-character-template"'\'' "$OC1/agents/rp-roleplay-gm.md"'
assert "valid colon-family rendering emits no skipped-agent warning" '! grep -q "invalid OpenCode agent name.*character" "$WORK/install.err"'
assert "ownership manifest records generated files" '[[ $(jq -r ".artifacts | length" "$H1/.asha/install-manifests/opencode.json") -gt 20 ]]'
assert "plugin bridges tool policy" 'grep -q "tool.execute.before" "$OC1/plugins/asha.js"'
assert "plugin surfaces status-0 policy warnings at the native seam" 'grep -q "result.status === 0.*process.stderr.write" "$OC1/plugins/asha.js"'
assert "plugin injects shell identity" 'grep -q "shell.env" "$OC1/plugins/asha.js"'
assert "plugin routes prompt recovery directly" 'grep -q "user-prompt-submit.sh" "$OC1/plugins/asha.js"'
assert "plugin avoids root lifecycle side effects for known child sessions" 'grep -q "childSessions" "$OC1/plugins/asha.js"'
assert "plugin seals recovery on dispose without semantic save" 'grep -q "session-end.sh" "$OC1/plugins/asha.js" && ! grep -Eq "detached-save|save-session|setsid" "$OC1/plugins/asha.js"'
if command -v node >/dev/null 2>&1 && node --check "$OC1/plugins/asha.js" >/dev/null 2>&1; then
  ok "generated plugin parses as JavaScript"
elif command -v node >/dev/null 2>&1; then
  fail "generated plugin parses as JavaScript"
else
  echo "  - node absent; JavaScript parse check skipped"
fi
if HOME="$H1" XDG_CONFIG_HOME="$H1/config" ASHA_HOME="$H1/.asha" \
    ASHA_OPENCODE_CMD="$OPENCODE_OK" \
    bash "$REPO_ROOT/bin/asha-drift-check.sh" --target opencode \
    >"$WORK/doctor.out" 2>"$WORK/doctor.err"; then
  ok "OpenCode doctor passes a healthy install"
else
  fail "OpenCode doctor passes a healthy install ($(tail -3 "$WORK/doctor.out"))"
fi

echo "--- current-source drift defeats a stale matching ledger ---"
printf '\nstale rendered bytes\n' >>"$OC1/commands/session-save.md"
stale_hash="$(sha256sum "$OC1/commands/session-save.md" | awk '{print $1}')"
manifest="$H1/.asha/install-manifests/opencode.json"
jq --arg d "$OC1/commands/session-save.md" --arg h "$stale_hash" \
  '(.artifacts[] | select(.destination == $d) | .sha256) = $h' \
  "$manifest" >"$manifest.tmp" && mv "$manifest.tmp" "$manifest"
if HOME="$H1" XDG_CONFIG_HOME="$H1/config" ASHA_HOME="$H1/.asha" \
    ASHA_OPENCODE_CMD="$OPENCODE_OK" \
    bash "$REPO_ROOT/bin/asha-drift-check.sh" --target opencode \
    >"$WORK/stale-source.out" 2>"$WORK/stale-source.err"; then
  fail "OpenCode doctor re-renders source rather than trusting a stale ledger"
else
  ok "OpenCode doctor re-renders source rather than trusting a stale ledger"
fi
assert "OpenCode source-drift diagnostic names generated source freshness" \
  'grep -q "source" "$WORK/stale-source.out"'
if install_into "$H1" --force >/dev/null 2>&1; then
  ok "OpenCode force reinstall repairs stale rendered source"
else
  fail "OpenCode force reinstall repairs stale rendered source"
fi

echo "--- complete current-source artifact set ---"
SET_REPO="$WORK/source-set-repo"
cp -a "$REPO_ROOT" "$SET_REPO"
HSET="$WORK/hset"; mkdir -p "$HSET"
install_set() {
  HOME="$HSET" XDG_CONFIG_HOME="$HSET/config" ASHA_HOME="$HSET/.asha" \
    ASHA_OPENCODE_CMD="$OPENCODE_OK" \
    bash "$SET_REPO/install.sh" --target opencode "$@"
}
drift_set() {
  HOME="$HSET" XDG_CONFIG_HOME="$HSET/config" ASHA_HOME="$HSET/.asha" \
    ASHA_OPENCODE_CMD="$OPENCODE_OK" \
    bash "$SET_REPO/bin/asha-drift-check.sh" --target opencode
}
if install_set >/dev/null 2>&1; then ok "artifact-set fixture install succeeds"; else fail "artifact-set fixture install succeeds"; fi
unowned="$HSET/config/opencode/commands/session-save.md"
unowned_hash="$(sha256sum "$unowned" | awk '{print $1}')"
set_manifest="$HSET/.asha/install-manifests/opencode.json"
jq --arg d "$unowned" '.artifacts |= map(select(.destination != $d))' \
  "$set_manifest" >"$set_manifest.tmp" && mv "$set_manifest.tmp" "$set_manifest"
if drift_set >"$WORK/unowned-set.out" 2>"$WORK/unowned-set.err"; then
  fail "current OpenCode artifact without a managed-set record fails drift"
else
  ok "current OpenCode artifact without a managed-set record fails drift"
fi
assert "unowned current artifact is reported without changing foreign bytes" \
  'grep -q "not recorded as managed" "$WORK/unowned-set.out" && [[ $(sha256sum "$unowned" | awk '\''{print $1}'\'') == "$unowned_hash" ]]'
if install_set --force >/dev/null 2>&1; then ok "reinstall restores complete managed artifact set"; else fail "reinstall restores complete managed artifact set"; fi
cat >"$SET_REPO/plugins/test/commands/new-source-set.md" <<'EOF'
---
name: test-new-source-set
description: "Disposable artifact-set command"
---

# Disposable command
EOF
cat >"$SET_REPO/plugins/test/agents/new-source-set.md" <<'EOF'
---
name: new-source-set
description: "Disposable artifact-set agent"
---

# Disposable agent
EOF
if drift_set >"$WORK/new-set.out" 2>"$WORK/new-set.err"; then
  fail "new OpenCode sources missing at destination fail drift"
else
  ok "new OpenCode sources missing at destination fail drift"
fi
assert "new-source drift names both missing generated destinations" \
  'grep -q "/commands/test-new-source-set.md" "$WORK/new-set.out" && grep -q "/agents/test-new-source-set.md" "$WORK/new-set.out"'
if install_set --force >/dev/null 2>&1 && drift_set >"$WORK/unchanged-set.out" 2>"$WORK/unchanged-set.err"; then
  ok "unchanged complete OpenCode artifact set passes drift"
else
  fail "unchanged complete OpenCode artifact set passes drift"
fi
rm "$SET_REPO/plugins/test/commands/new-source-set.md" \
   "$SET_REPO/plugins/test/agents/new-source-set.md"
if drift_set >"$WORK/removed-set.out" 2>"$WORK/removed-set.err"; then
  fail "retired OpenCode sources with installed artifacts fail drift"
else
  ok "retired OpenCode sources with installed artifacts fail drift"
fi
assert "retired-source drift names managed artifact-set residue" \
  'grep -q "retired" "$WORK/removed-set.out" && grep -q "test-new-source-set.md" "$WORK/removed-set.out"'

echo "--- full-install retirement reconciliation ---"
orphan="$OC1/commands/retired-command.md"
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

echo "--- wrapper-scoped persona injection ---"
cat >"$WORK/fake-opencode" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo 1.17.18; exit 0; fi
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

echo "--- native tool policy bridge ---"
POLICY_ADAPTER="$REPO_ROOT/plugins/session/hooks/handlers/opencode-policy-adapter.sh"
if printf '%s' '{"session_id":"sid","cwd":"/tmp","tool_name":"read","tool_input":{"filePath":"README.md"}}' \
    | "$POLICY_ADAPTER" >"$WORK/policy-read.out" 2>"$WORK/policy-read.err"; then
  ok "read tool passes the policy bridge"
else
  fail "read tool passes the policy bridge"
fi
if printf '%s' '{"session_id":"sid","cwd":"/tmp","tool_name":"bash","tool_input":{"command":"rm -rf /important"}}' \
    | "$POLICY_ADAPTER" >"$WORK/policy-rm.out" 2>"$WORK/policy-rm.err"; then
  fail "destructive bash is denied by the policy bridge"
else
  policy_rc=$?
  [[ $policy_rc -eq 2 ]] \
    && ok "destructive bash is denied by the policy bridge" \
    || fail "destructive bash denial returns rc 2 (got $policy_rc)"
fi
assert "policy denial preserves the guard reason" 'grep -q "destructive-delete" "$WORK/policy-rm.err"'

echo "--- version floor ---"
cat >"$WORK/opencode-old" <<'EOF'
#!/usr/bin/env bash
echo 1.15.10
EOF
chmod +x "$WORK/opencode-old"
HOLD="$WORK/hold"; mkdir -p "$HOLD"
if ASHA_OPENCODE_CMD="$WORK/opencode-old" install_into "$HOLD" >"$WORK/old.out" 2>&1; then
  fail "unsupported OpenCode version is refused"
else
  ok "unsupported OpenCode version is refused"
fi
assert "version error names minimum" 'grep -q "requires >=1.15.11" "$WORK/old.out"'

echo "--- foreign collision protection ---"
H2="$WORK/h2"; mkdir -p "$H2/config/opencode/commands"
printf 'foreign\n' >"$H2/config/opencode/commands/session-save.md"
if install_into "$H2" >/dev/null 2>&1; then
  fail "foreign generated-file collision is refused"
else
  ok "foreign generated-file collision is refused"
fi
assert "foreign file bytes remain intact" '[[ $(cat "$H2/config/opencode/commands/session-save.md") == foreign ]]'

H4="$WORK/h4"; mkdir -p "$H4/config/opencode/commands/session-save.md"
if install_into "$H4" --force >/dev/null 2>&1; then
  fail "--force refuses to replace a destination directory"
else
  ok "--force refuses to replace a destination directory"
fi
assert "directory collision receives no temporary payload" '[[ -z $(find "$H4/config/opencode/commands/session-save.md" -mindepth 1 -print -quit) ]]'

H5="$WORK/h5"; custom="$H5/custom-opencode"; mkdir -p "$H5"
if OPENCODE_CONFIG_DIR="$custom" install_into "$H5" >/dev/null 2>&1; then
  ok "install honors OPENCODE_CONFIG_DIR"
else
  fail "install honors OPENCODE_CONFIG_DIR"
fi
assert "custom OpenCode directory receives artifacts" '[[ -f "$custom/commands/session-save.md" ]]'
assert "default XDG OpenCode directory remains unused" '[[ ! -e "$H5/config/opencode/commands/session-save.md" ]]'

echo "--- modified managed artifact preservation ---"
printf '\nuser modification\n' >>"$OC1/commands/session-save.md"
if uninstall_from "$H1" >"$WORK/uninstall.out" 2>"$WORK/uninstall.err"; then
  ok "uninstall exits 0 with modified artifact"
else
  fail "uninstall exits 0 with modified artifact"
fi
assert "modified managed file is preserved" 'grep -q "user modification" "$OC1/commands/session-save.md"'
assert "modified file remains in ownership manifest" 'jq -e --arg d "$OC1/commands/session-save.md" ".artifacts[] | select(.destination == \$d)" "$H1/.asha/install-manifests/opencode.json" >/dev/null'
assert "unmodified integration plugin is removed" '[[ ! -e "$OC1/plugins/asha.js" ]]'
assert "modified files are reported as preserved" 'grep -q "preserving modified managed artifact" "$WORK/uninstall.err"'

echo ""
echo "test-opencode: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
