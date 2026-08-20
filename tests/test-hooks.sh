#!/usr/bin/env bash
# Memory v2 hook contract tests.
set -uo pipefail

REPO_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HANDLERS="$REPO_ROOT/plugins/session/hooks/handlers"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0 FAIL=0
ok() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else fail "$name"; fi; }

PROJECT="$WORK/project"
HOME_DIR="$WORK/home"
mkdir -p "$PROJECT/.asha" "$PROJECT/Memory" "$PROJECT/Work/markers" "$HOME_DIR/.asha"
printf '{"initialized":true,"memory_version":2,"project_id":"project-test"}\n' > "$PROJECT/.asha/config.json"
printf '# Objective\nO\n# State\nS\n# Next\n- N\n# Blockers\n- None\n' > "$PROJECT/Memory/activeContext.md"
printf '# Decisions\n- D\n' > "$PROJECT/Memory/decisions.md"
printf 'operation-v2-sentinel\n' > "$HOME_DIR/.asha/operation.md"

run_hook() {
  local hook="$1" payload="$2" harness="${3:-claude}"
  printf '%s' "$payload" | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" ASHA_HARNESS="$harness" \
    "$HANDLERS/$hook"
}

echo "--- Memory v2 hooks ---"

OUT="$(run_hook session-start.sh '{"session_id":"start-1","cwd":"'"$PROJECT"'"}')"
[[ "$OUT" == *operation-v2-sentinel* ]] && ok "SessionStart injects operational context" || fail "SessionStart injects operational context"
[[ "$OUT" == *"Published repository Memory v2"* && "$OUT" == *"-- activeContext.md --"* \
   && "$OUT" == *"-- decisions.md --"* && "$OUT" == *"- D"* ]] \
  && ok "SessionStart injects the coherent repository publication" \
  || fail "SessionStart injects the coherent repository publication"
CODEX_START="$(run_hook session-start.sh '{"session_id":"codex-start","cwd":"'"$PROJECT"'"}' codex)"
OPENCODE_START="$(run_hook session-start.sh '{"session_id":"opencode-start","cwd":"'"$PROJECT"'"}' opencode)"
COPILOT_START="$(run_hook session-start.sh '{"sessionId":"copilot-start","cwd":"'"$PROJECT"'"}' copilot)"
[[ "$CODEX_START" == *"Published repository Memory v2"* \
   && "$OPENCODE_START" == *"Published repository Memory v2"* ]] \
  && ok "Codex and OpenCode receive the shared project publication" \
  || fail "Codex and OpenCode receive the shared project publication"
printf '%s' "$COPILOT_START" | jq -e \
  '.additionalContext | contains("Published repository Memory v2") and contains("-- decisions.md --")' \
  >/dev/null 2>&1 \
  && ok "Copilot receives project publication through additionalContext" \
  || fail "Copilot receives project publication through additionalContext"
if [[ "$OUT" == *'\n'* ]]; then
  fail "SessionStart renders real newlines rather than literal backslash-n delimiters"
else
  ok "SessionStart renders real newlines rather than literal backslash-n delimiters"
fi
check "SessionStart writes a bounded project-local snapshot" test -f "$PROJECT/Work/session-state/claude-start-1.json"
[[ $(wc -c < "$PROJECT/Work/session-state/claude-start-1.json") -le 2048 ]] \
  && ok "SessionStart snapshot respects 2 KiB cap" || fail "SessionStart snapshot respects 2 KiB cap"

rm -f "$PROJECT/Work/session-state/claude-unknown.json"
run_hook user-prompt-submit.sh '{"cwd":"'"$PROJECT"'","prompt":"identity absent"}' >/dev/null
check "missing hook identity never collapses into unknown snapshot" test ! -e "$PROJECT/Work/session-state/claude-unknown.json"

mkdir -p "$HOME_DIR/.asha/learnings/candidate"
cat > "$HOME_DIR/.asha/learnings/candidate/expired-hook.md" <<'EOF'
---
{"type":"learning","id":"expired-hook","trigger":"t","action":"a","state":"candidate","created":"2020-01-01","updated":"2020-01-01","retirement_reason":"","evidence":[]}
---

# expired-hook

**Trigger:** t

**Action:** a
EOF
run_hook session-start.sh '{"session_id":"expiry","cwd":"'"$PROJECT"'"}' >/dev/null
check "SessionStart wires 90-day candidate expiry into lifecycle" test -f "$HOME_DIR/.asha/learnings/retired/expired-hook.md"

run_hook user-prompt-submit.sh '{"session_id":"p1","cwd":"'"$PROJECT"'","prompt":"resume src/main.py"}' >/dev/null
python3 - "$PROJECT/Work/session-state/claude-p1.json" <<'PY' \
  && ok "UserPromptSubmit records only recovery fields" || fail "UserPromptSubmit records only recovery fields"
import json, pathlib, sys
data=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert data["prompt"] == "resume src/main.py"
assert set(data) <= {"version","snapshot_name","harness_sha256","session_sha256","harness_component","session_component","harness_redacted","session_redacted","harness_truncated","session_truncated","session_id","project_id","harness","created_at","updated_at","prompt","last_action","paths","blocker"}
PY

run_hook post-tool-use.sh '{"session_id":"p1","cwd":"'"$PROJECT"'","tool_name":"Edit","tool_input":{"file_path":"src/main.py"}}' >/dev/null
python3 - "$PROJECT/Work/session-state/claude-p1.json" <<'PY' \
  && ok "PostToolUse merges the touched path" || fail "PostToolUse merges the touched path"
import json, pathlib, sys
data=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert data["last_action"] == "Edit" and data["paths"] == ["src/main.py"]
PY

# Payload cwd is authoritative. Resolve initialized ancestors from nested cwd,
# never redirect to a stale ambient project, and reject HOME itself.
STALE="$WORK/stale"
mkdir -p "$PROJECT/src/nested" "$STALE/.asha" "$STALE/Work/markers"
printf '{"initialized":true,"memory_version":2,"project_id":"stale"}\n' > "$STALE/.asha/config.json"
printf '%s' '{"session_id":"nested","cwd":"'"$PROJECT/src/nested"'","prompt":"nested"}' \
  | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$STALE" CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" \
      ASHA_HARNESS=claude "$HANDLERS/user-prompt-submit.sh" >/dev/null
[[ -f "$PROJECT/Work/session-state/claude-nested.json" && ! -e "$STALE/Work/session-state/claude-nested.json" ]] \
  && ok "authoritative nested payload cwd resolves upward without ambient fallback" \
  || fail "authoritative nested payload cwd resolves upward without ambient fallback"
mkdir -p "$HOME_DIR/.asha" "$HOME_DIR/Work/markers"
printf '{"initialized":true,"memory_version":2,"project_id":"home-must-not-be-project"}\n' > "$HOME_DIR/.asha/config.json"
printf '%s' '{"session_id":"home","cwd":"'"$HOME_DIR"'","prompt":"no home writes"}' \
  | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$STALE" CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" \
      ASHA_HARNESS=claude "$HANDLERS/user-prompt-submit.sh" >/dev/null
[[ ! -e "$HOME_DIR/Work/session-state/claude-home.json" && ! -e "$STALE/Work/session-state/claude-home.json" ]] \
  && ok "authoritative HOME cwd is rejected without ambient fallback" \
  || fail "authoritative HOME cwd is rejected without ambient fallback"

touch "$PROJECT/Work/markers/rp-active"
RP="$(run_hook user-prompt-submit.sh '{"session_id":"p1","cwd":"'"$PROJECT"'","prompt":"continue"}')"
[[ "$RP" == *"RP session active"* ]] && ok "RP routing survives nudge removal" || fail "RP routing survives nudge removal"
COPILOT_RP="$(run_hook user-prompt-submit.sh '{"sessionId":"cp1","cwd":"'"$PROJECT"'","prompt":"continue"}' copilot)"
printf '%s' "$COPILOT_RP" | jq -e '.additionalContext | contains("RP session active")' >/dev/null 2>&1 \
  && ok "Copilot RP routing uses additionalContext" || fail "Copilot RP routing uses additionalContext"

PRICED="$(run_hook user-prompt-submit.sh '{"session_id":"priced","cwd":"'"$PROJECT"'","prompt":"What price must we pay to escape forever?"}')"
[[ "$PRICED" == *"PRICED STAKE"* && "$PRICED" == *"SOURCE_LOG.priced_stake_touched"* ]] \
  && ok "RP priced-stakes safeguard is re-homed at the prompt seam" \
  || fail "RP priced-stakes safeguard is re-homed at the prompt seam"
PRICED_AGAIN="$(run_hook user-prompt-submit.sh '{"session_id":"priced","cwd":"'"$PROJECT"'","prompt":"What price must we pay to escape forever?"}')"
[[ "$PRICED_AGAIN" != *"PRICED STAKE"* ]] \
  && ok "RP priced-stakes direct safeguard preserves its cooldown" \
  || fail "RP priced-stakes direct safeguard preserves its cooldown"
rm -f "$PROJECT/Work/markers/rp-active"

# A workspace child receives its repository publication plus the workspace
# publication. A launch at the workspace root receives the pair once and keeps
# only the workspace metadata wrapper beside it.
cat > "$PROJECT/.asha/workspace.json" <<'JSON'
{
  "version": 1,
  "workspace_name": "hook-workspace",
  "repositories": [{"path": "child", "docs": "knowledge/repos/child"}],
  "memory": {
    "operational_root": "Memory",
    "personal_root": "memory-local",
    "shared_root": "knowledge",
    "shared_git_root": ".",
    "promotion_mode": "pull-request"
  }
}
JSON
CHILD="$PROJECT/child"
mkdir -p "$CHILD/.asha" "$CHILD/Memory" "$CHILD/Work/markers"
printf '{"initialized":true,"memory_version":2,"project_id":"child-test"}\n' > "$CHILD/.asha/config.json"
printf '# Objective\nchild-publication-sentinel\n# State\nReady\n# Next\n- N\n# Blockers\n- None\n' > "$CHILD/Memory/activeContext.md"
printf '# Decisions\n- child-decision-sentinel\n' > "$CHILD/Memory/decisions.md"
printf '# Objective\nworkspace-publication-sentinel\n# State\nReady\n# Next\n- N\n# Blockers\n- None\n' > "$PROJECT/Memory/activeContext.md"
CHILD_OUT="$(run_hook session-start.sh '{"session_id":"ws-child","cwd":"'"$CHILD"'"}')"
[[ "$CHILD_OUT" == *"Published repository Memory v2"* \
   && "$CHILD_OUT" == *child-publication-sentinel* \
   && "$CHILD_OUT" == *workspace-publication-sentinel* \
   && "$CHILD_OUT" == *"active repo: child"* ]] \
  && ok "workspace child receives distinct project and workspace context" \
  || fail "workspace child receives distinct project and workspace context"
ROOT_OUT="$(run_hook session-start.sh '{"session_id":"ws-root","cwd":"'"$PROJECT"'"}')"
ROOT_SENTINELS="$(printf '%s' "$ROOT_OUT" | grep -o 'workspace-publication-sentinel' | wc -l)"
[[ "$ROOT_OUT" == *"Published workspace Memory v2"* \
   && "$ROOT_OUT" == *"Workspace: hook-workspace"* \
   && "$ROOT_SENTINELS" -eq 1 ]] \
  && ok "workspace root publication is injected once with workspace metadata" \
  || fail "workspace root publication is injected once with workspace metadata"
rm -rf "$CHILD"
rm -f "$PROJECT/.asha/workspace.json"
printf '# Objective\nO\n# State\nS\n# Next\n- N\n# Blockers\n- None\n' > "$PROJECT/Memory/activeContext.md"

# Compatibility alias: an existing opt-out must not silently resume workspace
# injection after upgrading from the nudge engine.
printf '{"roots":[{"path":"."}]}\n' > "$PROJECT/.asha/workspace.json"
touch "$PROJECT/Work/markers/nudge-ws-context-off"
WS_OFF="$(run_hook session-start.sh '{"session_id":"ws-off","cwd":"'"$PROJECT"'"}')"
[[ "$WS_OFF" != *"Workspace knowledge"* ]] \
  && ok "legacy nudge-ws-context-off marker remains honored" \
  || fail "legacy nudge-ws-context-off marker remains honored"
rm -f "$PROJECT/Work/markers/nudge-ws-context-off" "$PROJECT/.asha/workspace.json"

BEFORE="$(sha256sum "$PROJECT/Memory/activeContext.md" "$PROJECT/Memory/decisions.md")"
LAST_ACTION_BEFORE="$(jq -r '.last_action' "$PROJECT/Work/session-state/claude-p1.json")"
run_hook session-end.sh '{"session_id":"p1","cwd":"'"$PROJECT"'","reason":"logout"}' >/dev/null
AFTER="$(sha256sum "$PROJECT/Memory/activeContext.md" "$PROJECT/Memory/decisions.md")"
[[ "$BEFORE" == "$AFTER" ]] && ok "SessionEnd never publishes semantic Memory" || fail "SessionEnd never publishes semantic Memory"
jq -e --arg before "$LAST_ACTION_BEFORE" \
  '.last_action == $before and (.sealed_at | type == "string" and length > 0)' \
  "$PROJECT/Work/session-state/claude-p1.json" >/dev/null \
  && ok "SessionEnd seals without erasing the last mechanical action" \
  || fail "SessionEnd seals without erasing the last mechanical action"

touch "$PROJECT/Work/markers/silence"
rm -f "$PROJECT/Work/session-state/claude-silent.json"
run_hook user-prompt-submit.sh '{"session_id":"silent","cwd":"'"$PROJECT"'","prompt":"do not persist"}' >/dev/null
check "silence marker disables recovery persistence" test ! -e "$PROJECT/Work/session-state/claude-silent.json"
rm -f "$PROJECT/Work/markers/silence"

MALFORMED="$(run_hook post-tool-use.sh 'not-json')"; RC=$?
[[ $RC -eq 0 && "$MALFORMED" == '{}' ]] && ok "malformed hook payload fails open" || fail "malformed hook payload fails open"

HOOKS="$REPO_ROOT/plugins/session/hooks/hooks.json"
jq -e '.hooks | has("SessionStart") and has("UserPromptSubmit") and has("PostToolUse") and has("PermissionRequest") and has("SessionEnd") and has("Stop")' "$HOOKS" >/dev/null \
  && ok "hook registry carries every claimed Control observation" || fail "hook registry carries every claimed Control observation"
if ! rg -n 'pattern_analyzer|jsonl_reader|event_store|detached-save|save-session|git (commit|push)|Memory/' \
      "$HANDLERS/session-start.sh" "$HANDLERS/user-prompt-submit.sh" \
      "$HANDLERS/post-tool-use.sh" "$HANDLERS/session-end.sh" >/dev/null; then
  ok "recovery hooks contain no transcript, semantic save, Memory-write, or Git path"
else
  fail "recovery hooks contain no transcript, semantic save, Memory-write, or Git path"
fi

# Existing policy behavior remains independent of Memory persistence.
policy_decision() {
  local rc=0
  printf '%s' "$1" | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" ASHA_HARNESS=claude \
    "$HANDLERS/policy-guard.sh" >/dev/null 2>&1 || rc=$?
  [[ $rc -eq 2 ]] && printf deny || printf allow
}
POLICY_OK=1
policy_case() {
  local label="$1" payload="$2" want="$3" got
  got="$(policy_decision "$payload")"
  [[ "$got" == "$want" ]] || { POLICY_OK=0; fail "policy $label ($got != $want)"; }
}
policy_case force-push '{"tool_name":"Bash","tool_input":{"command":"git push --force"}}' deny
policy_case force-with-lease '{"tool_name":"Bash","tool_input":{"command":"git push --force-with-lease"}}' allow
policy_case broad-home '{"tool_name":"Bash","tool_input":{"command":"find /home -name x"}}' deny
policy_case scoped-home '{"tool_name":"Bash","tool_input":{"command":"find /home/alice/code -name x"}}' allow
policy_case archive-delete '{"tool_name":"Bash","tool_input":{"command":"rm backup.7z"}}' deny
policy_case marker-delete '{"tool_name":"Bash","tool_input":{"command":"rm -f Work/markers/silence"}}' allow
policy_case active-edit '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/activeContext.md"}}' deny
policy_case decisions-edit '{"tool_name":"Edit","tool_input":{"file_path":"/p/Memory/decisions.md"}}' deny
policy_case relative-active-edit '{"tool_name":"Write","tool_input":{"path":"Memory/activeContext.md"}}' deny
policy_case patch-decisions-edit '{"tool_name":"apply_patch","tool_input":{"patch":"*** Update File: Memory/decisions.md"}}' deny
policy_case legacy-edit '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/projectbrief.md"}}' allow
policy_case scratch-edit '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/scratchpad.md"}}' allow
policy_case rp-invariants-edit '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/invariants.md"}}' allow
policy_case rp-canon-layout-edit '{"tool_name":"Edit","tool_input":{"file_path":"/p/Memory/canon-layout.md"}}' allow
[[ $POLICY_OK -eq 1 ]] && ok "policy matrix preserves destructive guards and v2 Memory boundary"

WARN_OUT="$WORK/policy-warn.out"
WARN_ERR="$WORK/policy-warn.err"
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" ASHA_HARNESS=claude "$HANDLERS/policy-guard.sh" >"$WARN_OUT" 2>"$WARN_ERR"
WARN_RC=$?
[[ $WARN_RC -eq 0 && -z "$(cat "$WARN_OUT")" && "$(cat "$WARN_ERR")" == *"WARNING by Asha policy [vault-structure]"* ]] \
  && ok "warn policy remains awareness-producing without blocking" \
  || fail "warn policy remains awareness-producing without blocking"

cat > "$HOME_DIR/.asha/policies.json" <<'JSON'
{"rules":[{"id":"deny-random-vault","tool":"Write","file_path_regex":"/Vault/Random/","action":"deny","reason":"later deny"}]}
JSON
OVERLAP_ERR="$WORK/policy-overlap.err"
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" ASHA_HARNESS=claude "$HANDLERS/policy-guard.sh" >/dev/null 2>"$OVERLAP_ERR"
OVERLAP_RC=$?
[[ $OVERLAP_RC -eq 2 && "$(cat "$OVERLAP_ERR")" == *"vault-structure"* && "$(cat "$OVERLAP_ERR")" == *"deny-random-vault"* ]] \
  && ok "warn policy continues evaluation to a later deny" \
  || fail "warn policy continues evaluation to a later deny"
rm -f "$HOME_DIR/.asha/policies.json"

# Retain an adversarial slice of the pre-v2 policy suite. These rails are not
# memory machinery and do not disappear merely because the suite got shorter.
policy_case checkout-discard '{"tool_name":"Bash","tool_input":{"command":"git checkout -- src/a.py"}}' deny
policy_case restore-staged '{"tool_name":"Bash","tool_input":{"command":"git restore --staged src/a.py"}}' allow
policy_case clean-force '{"tool_name":"Bash","tool_input":{"command":"git clean -fd"}}' deny
policy_case quoted-archive '{"tool_name":"Bash","tool_input":{"command":"rm \"backup.7z\""}}' deny
policy_case mixed-archive '{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/extract ~/Downloads/pdfs.7z"}}' deny
policy_case docker-rm '{"tool_name":"Bash","tool_input":{"command":"docker rm -f container"}}' allow
policy_case home-tilde '{"tool_name":"Bash","tool_input":{"command":"grep -r pattern ~"}}' deny
policy_case home-var-scoped '{"tool_name":"Bash","tool_input":{"command":"find \"$HOME/Code\" -name x"}}' allow
policy_case cross-repo-force '{"tool_name":"Bash","tool_input":{"command":"git -C /workspace/shared push --force origin main"}}' deny
policy_case cross-repo-lease '{"tool_name":"Bash","tool_input":{"command":"git -C /workspace/shared push --force-with-lease origin main"}}' allow
policy_case continuation-force '{"tool_name":"Bash","tool_input":{"command":"git -C /ws \\\n push --force"}}' deny
[[ $POLICY_OK -eq 1 ]] && ok "adversarial policy regression matrix remains green"

copilot_decision() {
  printf '%s' "$1" | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    "$HANDLERS/copilot-policy-adapter.sh" 2>/dev/null \
    | jq -r '.permissionDecision // "allow"'
}
[[ "$(copilot_decision '{"toolName":"bash","toolArgs":{"command":"git push --force"}}')" == deny \
   && "$(copilot_decision '{"toolName":"create","toolArgs":{"path":"/p/.ssh/id_rsa"}}')" == deny \
   && "$(copilot_decision '{"toolName":"apply_patch","toolArgs":{"patch":"*** Update File: Memory/activeContext.md"}}')" == deny \
   && "$(copilot_decision '{"toolName":"edit","toolArgs":{"filePath":"Memory/decisions.md"}}')" == deny \
   && "$(copilot_decision '{"toolName":"view","toolArgs":{"path":"/tmp/readme"}}')" == allow ]] \
  && ok "Copilot adapter translates policy and secret decisions" \
  || fail "Copilot adapter translates policy and secret decisions"
CP_WARN_ERR="$WORK/copilot-warn.err"
CP_WARN_OUT="$(printf '%s' '{"toolName":"create","toolArgs":{"path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" "$HANDLERS/copilot-policy-adapter.sh" 2>"$CP_WARN_ERR")"
[[ "$(printf '%s' "$CP_WARN_OUT" | jq -r '.permissionDecision')" == allow \
   && "$(cat "$CP_WARN_ERR")" == *"WARNING by Asha policy [vault-structure]"* ]] \
  && ok "Copilot adapter preserves warn awareness whilst allowing" \
  || fail "Copilot adapter preserves warn awareness whilst allowing"
cat > "$HOME_DIR/.asha/policies.json" <<'JSON'
{"rules":[{"id":"deny-random-vault","tool":"Write","file_path_regex":"/Vault/Random/","action":"deny","reason":"later deny"}]}
JSON
CP_OVERLAP="$(copilot_decision '{"toolName":"create","toolArgs":{"path":"/p/Vault/Random/x.md"}}')"
[[ "$CP_OVERLAP" == deny ]] \
  && ok "Copilot warn continues to later deny" \
  || fail "Copilot warn continues to later deny"
rm -f "$HOME_DIR/.asha/policies.json"
OC_WARN_ERR="$WORK/opencode-warn.err"
printf '%s' '{"tool_name":"write","args":{"path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" "$HANDLERS/opencode-policy-adapter.sh" >/dev/null 2>"$OC_WARN_ERR"
OC_WARN_RC=$?
[[ $OC_WARN_RC -eq 0 && "$(cat "$OC_WARN_ERR")" == *"WARNING by Asha policy [vault-structure]"* ]] \
  && ok "OpenCode adapter preserves warn awareness whilst allowing" \
  || fail "OpenCode adapter preserves warn awareness whilst allowing"
cat > "$HOME_DIR/.asha/policies.json" <<'JSON'
{"rules":[{"id":"deny-random-vault","tool":"Write","file_path_regex":"/Vault/Random/","action":"deny","reason":"later deny"}]}
JSON
printf '%s' '{"tool_name":"write","args":{"path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" "$HANDLERS/opencode-policy-adapter.sh" >/dev/null 2>"$WORK/oc-overlap.err"
OC_OVERLAP_RC=$?
[[ $OC_OVERLAP_RC -eq 2 ]] \
  && ok "OpenCode warn continues to later deny" \
  || fail "OpenCode warn continues to later deny"
rm -f "$HOME_DIR/.asha/policies.json"

COVERED="$(jq -r '[.hooks.PreToolUse[] | select(any(.hooks[]?; ((.command // "") | test("policy-guard\\.sh$")))) | (.matcher // "*")] | join("|")' "$HOOKS")"
REACHABLE=1
while IFS= read -r tool; do
  IFS='|' read -ra tokens <<< "$tool"
  for token in "${tokens[@]}"; do
    [[ "|$COVERED|" == *"|$token|"* || "|$COVERED|" == *"|*|"* ]] || REACHABLE=0
  done
done < <(jq -r '.rules[].tool' "$REPO_ROOT/plugins/session/hooks/policies/rules.json")
[[ $REACHABLE -eq 1 ]] \
  && ok "policy guard remains reachable for every declared tool" \
  || fail "policy guard remains reachable for every declared tool"
jq -e '.hooks.PreToolUse[] | select((._asha_harnesses // []) | index("codex"))
  | select((.matcher // "") | test("Edit|Write|apply_patch"))
  | any(.hooks[]?; (.command // "") | test("policy-guard\\.sh$"))' "$HOOKS" >/dev/null 2>&1 \
  && ok "Codex apply_patch alias reaches published-Memory policy" \
  || fail "Codex apply_patch alias reaches published-Memory policy"

CONTROL_REACHABLE=1
while read -r native control_event; do
  jq -e --arg native "$native" '.hooks[$native][]
    | any(.hooks[]?; (.command // "")
      | endswith("control-event.sh " + $native))' "$HOOKS" >/dev/null 2>&1 \
    || CONTROL_REACHABLE=0
done <<'EOF'
SessionStart session-start
UserPromptSubmit prompt-submitted
PostToolUse tool-completed
PermissionRequest permission-requested
Stop turn-stopped
SessionEnd session-ended
EOF
[[ $CONTROL_REACHABLE -eq 1 ]] \
  && ok "Control event handler is reachable from every registered native event" \
  || fail "Control event handler is reachable from every registered native event"

CONTROL_HANDLER="$HANDLERS/control-event.sh"
if grep -Fq '[[ -t 0 ]] || IFS= read -r -N 4096 INPUT || true' "$CONTROL_HANDLER" \
  && grep -Fq 'timeout --signal=TERM 15 "$ASHA_CMD" "${ARGS[@]}"' "$CONTROL_HANDLER"; then
  ok "Control event bridge guards tty input and bounds controller time"
else
  fail "Control event bridge guards tty input and bounds controller time"
fi
CONTROL_OUTPUT="$(timeout 2 env ASHA_CONTROL_MANAGED=1 ASHA_ROOT="$REPO_ROOT" \
  "$CONTROL_HANDLER" PostToolUse </dev/null)"
CONTROL_STATUS=$?
[[ $CONTROL_STATUS -eq 0 && "$CONTROL_OUTPUT" == '{}' ]] \
  && ok "Control event bridge remains fail-open with empty stdin" \
  || fail "Control event bridge remains fail-open with empty stdin"

FAKE_CONTROL_ROOT="$WORK/fake-control-root"
CONTROL_CAPTURE="$WORK/permission-requested.args"
mkdir -p "$FAKE_CONTROL_ROOT/bin"
cat > "$FAKE_CONTROL_ROOT/bin/asha" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$CONTROL_CAPTURE"
EOF
chmod +x "$FAKE_CONTROL_ROOT/bin/asha"
printf '%s' '{"session_id":"permission-live-gate"}' \
  | timeout 2 env ASHA_CONTROL_MANAGED=1 ASHA_ROOT="$FAKE_CONTROL_ROOT" \
      ASHA_HARNESS=codex CONTROL_CAPTURE="$CONTROL_CAPTURE" \
      "$CONTROL_HANDLER" PermissionRequest >/dev/null
[[ -f "$CONTROL_CAPTURE" \
   && "$(cat "$CONTROL_CAPTURE")" == *"control event --event permission-requested"* \
   && "$(cat "$CONTROL_CAPTURE")" == *"--harness codex"* \
   && "$(cat "$CONTROL_CAPTURE")" == *"--session-id permission-live-gate"* ]] \
  && ok "Codex PermissionRequest maps to the bounded Control event" \
  || fail "Codex PermissionRequest maps to the bounded Control event"

if jq -e '[.hooks.SessionStart[], .hooks.UserPromptSubmit[], .hooks.PostToolUse[]]
    | all(.[] | select(any(.hooks[]?; (.command // "") | contains("control-event.sh")));
          has("_asha_harnesses") | not)' "$HOOKS" >/dev/null 2>&1 \
  && jq -e '.hooks.Stop[]
    | select(any(.hooks[]?; (.command // "") | contains("control-event.sh")))
    | ._asha_harnesses == ["claude", "codex"]' "$HOOKS" >/dev/null 2>&1 \
  && jq -e '.hooks.PermissionRequest[]
    | select(any(.hooks[]?; (.command // "") | contains("control-event.sh")))
    | ._asha_harnesses == ["codex"]' "$HOOKS" >/dev/null 2>&1 \
  && jq -e '.hooks.SessionEnd[]
    | select(any(.hooks[]?; (.command // "") | contains("control-event.sh")))
    | ._asha_harnesses == ["claude"]' "$HOOKS" >/dev/null 2>&1; then
  ok "cross-harness and Claude-only Control groups carry the exact tags"
else
  fail "cross-harness and Claude-only Control groups carry the exact tags"
fi

asha_harness_home() { printf '%s\n' "$WORK/codex-render-home"; }
# shellcheck source=../harnesses/codex.sh
source "$REPO_ROOT/harnesses/codex.sh"
CODEX_RENDER="$(_codex_emit_hooks_for_plugin \
  "$REPO_ROOT/plugins/session" "$HOOKS" session 2>/dev/null)"
[[ "$CODEX_RENDER" == *"control-event.sh SessionStart"* \
   && "$CODEX_RENDER" == *"control-event.sh UserPromptSubmit"* \
   && "$CODEX_RENDER" == *"control-event.sh PostToolUse"* \
   && "$CODEX_RENDER" == *"control-event.sh PermissionRequest"* \
   && "$CODEX_RENDER" == *"control-event.sh Stop"* \
   && "$CODEX_RENDER" != *"control-event.sh SessionEnd"* \
   && "$CODEX_RENDER" == *"[[hooks.PermissionRequest]]"* \
   && "$CODEX_RENDER" == *"[[hooks.Stop]]"* \
   && "$CODEX_RENDER" != *"[[hooks.SessionEnd]]"* ]] \
  && ok "Codex renderer includes the five live-proven Control events" \
  || fail "Codex renderer includes the five live-proven Control events"

TEST_HOOKS="$REPO_ROOT/plugins/test/hooks/hooks.json"
if jq -e '.hooks | has("PermissionRequest") | not' "$TEST_HOOKS" >/dev/null 2>&1 \
    && [[ ! -e "$REPO_ROOT/plugins/test/hooks/permission-request-probe.sh" ]]; then
  ok "temporary Codex PermissionRequest probe is retired"
else
  fail "temporary Codex PermissionRequest probe is retired"
fi

echo "test-hooks: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
