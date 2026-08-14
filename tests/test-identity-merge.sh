#!/usr/bin/env bash
# test-identity-merge.sh — sandboxed smoke tests for identity cache builders.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
IDENTITY_MERGE="$REPO_ROOT/identity/identity-merge.sh"
OPERATIONAL_MERGE="$REPO_ROOT/identity/operational-merge.sh"
DISPATCHER="$REPO_ROOT/bin/asha"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0

ok()   { echo -e "  ${GREEN}✓${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}✗${NC} $1" >&2; FAIL=$((FAIL + 1)); }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

reset_sandbox() {
  rm -rf "$SANDBOX"
  mkdir -p "$SANDBOX/.cache/asha"
}

run_identity_merge() {
  env -u XDG_CONFIG_HOME -u XDG_DATA_HOME HOME="$SANDBOX" \
    bash "$IDENTITY_MERGE" "$SANDBOX/.cache/asha/instructions.md"
}

run_operational_merge() {
  env -u XDG_CONFIG_HOME -u XDG_DATA_HOME HOME="$SANDBOX" \
    bash "$OPERATIONAL_MERGE" "$SANDBOX/.cache/asha/operational.md"
}

# ---------------------------------------------------------------------------
# Test 1: only the compact hot identity sources are merged
# ---------------------------------------------------------------------------
echo "--- test 1: identity sources are merged ---"
reset_sandbox
mkdir -p "$SANDBOX/.asha"
printf 'SOUL_SENTINEL\n' > "$SANDBOX/.asha/soul.md"
printf 'VOICE_SENTINEL\n' > "$SANDBOX/.asha/voice.md"
printf 'KEEPER_SENTINEL\n' > "$SANDBOX/.asha/keeper.md"
printf 'KEEPER_VOICE_COLD_SENTINEL\n' > "$SANDBOX/.asha/keeper-voice.md"
if run_identity_merge >/dev/null 2>&1; then
  ok "identity merge exits 0 with user sources"
else
  fail "identity merge exits 0 with user sources"
fi
for sentinel in SOUL_SENTINEL VOICE_SENTINEL KEEPER_SENTINEL; do
  grep -q "$sentinel" "$SANDBOX/.cache/asha/instructions.md" \
    && ok "merged identity contains $sentinel" \
    || fail "merged identity contains $sentinel"
done
if ! grep -q 'KEEPER_VOICE_COLD_SENTINEL' "$SANDBOX/.cache/asha/instructions.md"; then
  ok "cold keeper voice stays out of automatic identity"
else
  fail "cold keeper voice stays out of automatic identity"
fi

# ---------------------------------------------------------------------------
# Test 2: missing hot identity fails closed; operational fallback stays benign
# ---------------------------------------------------------------------------
echo "--- test 2: absent identity fails closed ---"
reset_sandbox
if run_identity_merge >/dev/null 2>&1; then
  fail "identity merge rejects absent hot files"
else
  ok "identity merge rejects absent hot files"
fi
[[ ! -e "$SANDBOX/.cache/asha/instructions.md" ]] \
  && ok "failed identity merge emits no partial cache" \
  || fail "failed identity merge emits no partial cache"
if run_operational_merge >/dev/null 2>&1; then
  ok "operational merge exits 0 without ~/.asha files"
else
  fail "operational merge exits 0 without ~/.asha files"
fi
[[ -s "$SANDBOX/.cache/asha/operational.md" ]] && \
  grep -q '^# Asha Operational Layer (merged)$' "$SANDBOX/.cache/asha/operational.md" \
  && ok "operational merge emits sane fallback output" \
  || fail "operational merge emits sane fallback output"

# ---------------------------------------------------------------------------
# Test 3: repeated runs preserve identical cache bytes
# ---------------------------------------------------------------------------
echo "--- test 3: repeated merges are byte-idempotent ---"
mkdir -p "$SANDBOX/.asha"
printf 'SOUL\n' > "$SANDBOX/.asha/soul.md"
printf 'VOICE\n' > "$SANDBOX/.asha/voice.md"
printf 'KEEPER\n' > "$SANDBOX/.asha/keeper.md"
run_identity_merge >/dev/null 2>&1
cp "$SANDBOX/.cache/asha/instructions.md" "$SANDBOX/identity.before"
cp "$SANDBOX/.cache/asha/operational.md" "$SANDBOX/operational.before"
if run_identity_merge >/dev/null 2>&1 && \
   cmp -s "$SANDBOX/identity.before" "$SANDBOX/.cache/asha/instructions.md"; then
  ok "second identity merge is byte-identical"
else
  fail "second identity merge is byte-identical"
fi
if run_operational_merge >/dev/null 2>&1 && \
   cmp -s "$SANDBOX/operational.before" "$SANDBOX/.cache/asha/operational.md"; then
  ok "second operational merge is byte-identical"
else
  fail "second operational merge is byte-identical"
fi

# ---------------------------------------------------------------------------
# Test 4: the hot identity budget fails closed without replacing prior output
# ---------------------------------------------------------------------------
echo "--- test 4: identity merge enforces its byte budget ---"
reset_sandbox
mkdir -p "$SANDBOX/.asha"
printf 'PRIOR_IDENTITY\n' > "$SANDBOX/.cache/asha/instructions.md"
awk 'BEGIN { for (i = 0; i < 12000; i++) printf "X"; print "IDENTITY_TAIL" }' \
  > "$SANDBOX/.asha/soul.md"
if ASHA_IDENTITY_MAX_BYTES=4096 run_identity_merge >/dev/null 2>&1; then
  fail "oversized hot identity is rejected"
else
  ok "oversized hot identity is rejected"
fi
if grep -qx 'PRIOR_IDENTITY' "$SANDBOX/.cache/asha/instructions.md"; then
  ok "rejected merge preserves the prior cache"
else
  fail "rejected merge preserves the prior cache"
fi

# ---------------------------------------------------------------------------
# Test 5: operation content honors its 4000-byte budget and legacy learning
# stores do not regain authority.
# ---------------------------------------------------------------------------
echo "--- test 5: operational merge enforces v2 authority and byte caps ---"
reset_sandbox
mkdir -p "$SANDBOX/.asha"
awk 'BEGIN { for (i = 0; i < 5000; i++) printf "A"; print "OPERATION_TAIL" }' \
  > "$SANDBOX/.asha/operation.md"
awk 'BEGIN { for (i = 0; i < 4000; i++) printf "B"; print "LEARNINGS_TAIL" }' \
  > "$SANDBOX/.asha/learnings.md"
if run_operational_merge >/dev/null 2>&1; then
  ok "oversized operational inputs merge successfully"
else
  fail "oversized operational inputs merge successfully"
fi
if ! grep -q 'OPERATION_TAIL\|LEARNINGS_TAIL' "$SANDBOX/.cache/asha/operational.md" && \
   grep -q 'operation.md exceeded 4000 chars' "$SANDBOX/.cache/asha/operational.md"; then
  ok "operation cap holds and legacy flat learnings remain uninjected"
else
  fail "operation cap holds and legacy flat learnings remain uninjected"
fi
merged_bytes="$(wc -c < "$SANDBOX/.cache/asha/operational.md" | tr -d '[:space:]')"
[[ "$merged_bytes" -le 8000 ]] \
  && ok "merged operational cache stays within sane overhead ($merged_bytes bytes)" \
  || fail "merged operational cache stays within sane overhead (got $merged_bytes bytes)"

# ---------------------------------------------------------------------------
# Test 6: only active v2 learnings are rendered
# ---------------------------------------------------------------------------
echo "--- test 6: only active learnings are injected ---"
reset_sandbox
mkdir -p "$SANDBOX/.asha/learnings/active" "$SANDBOX/.asha/learnings/candidate"
cat > "$SANDBOX/.asha/learnings/active/active-rule.md" <<'EOF'
---
{"type":"learning","id":"active-rule","trigger":"ACTIVE_SENTINEL","action":"do active","state":"active","created":"2026-08-13","updated":"2026-08-13","retirement_reason":"","evidence":[]}
---
EOF
cat > "$SANDBOX/.asha/learnings/candidate/candidate-rule.md" <<'EOF'
---
{"type":"learning","id":"candidate-rule","trigger":"CANDIDATE_SENTINEL","action":"do candidate","state":"candidate","created":"2026-08-13","updated":"2026-08-13","retirement_reason":"","evidence":[]}
---
EOF
run_operational_merge >/dev/null 2>&1
if grep -q 'ACTIVE_SENTINEL' "$SANDBOX/.cache/asha/operational.md" \
    && ! grep -q 'CANDIDATE_SENTINEL' "$SANDBOX/.cache/asha/operational.md"; then
  ok "active learning renders and candidate remains non-authoritative"
else
  fail "active learning renders and candidate remains non-authoritative"
fi

# ---------------------------------------------------------------------------
# Test 7: Claude receives the same merged hot identity as other harnesses
# ---------------------------------------------------------------------------
echo "--- test 7: Claude launch uses the compact merge ---"
reset_sandbox
mkdir -p "$SANDBOX/.asha" "$SANDBOX/.claude/skills" "$SANDBOX/bin"
printf 'CLAUDE_SOUL_SENTINEL\n' > "$SANDBOX/.asha/soul.md"
printf 'CLAUDE_VOICE_SENTINEL\n' > "$SANDBOX/.asha/voice.md"
printf 'CLAUDE_KEEPER_SENTINEL\n' > "$SANDBOX/.asha/keeper.md"
printf 'CLAUDE_COLD_SENTINEL\n' > "$SANDBOX/.asha/keeper-voice.md"
printf '{}\n' > "$SANDBOX/.claude/settings.json"
ln -s "$REPO_ROOT/plugins/test" "$SANDBOX/.claude/skills/test-fixture"
cat > "$SANDBOX/bin/fake-claude" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@"
EOF
chmod +x "$SANDBOX/bin/fake-claude"
claude_cache="$SANDBOX/.cache/asha/claude-instructions.md"
if claude_args="$(HOME="$SANDBOX" ASHA_CLAUDE_CMD="$SANDBOX/bin/fake-claude" \
    ASHA_CLAUDE_INSTRUCTIONS_FILE="$claude_cache" bash "$DISPATCHER" claude PAYLOAD 2>/dev/null)" \
    && grep -q 'CLAUDE_SOUL_SENTINEL' "$claude_cache" \
    && grep -q 'CLAUDE_VOICE_SENTINEL' "$claude_cache" \
    && grep -q 'CLAUDE_KEEPER_SENTINEL' "$claude_cache" \
    && ! grep -q 'CLAUDE_COLD_SENTINEL' "$claude_cache" \
    && grep -q -- '--append-system-prompt-file' <<<"$claude_args" \
    && grep -q -- "$claude_cache" <<<"$claude_args" \
    && grep -q -- 'PAYLOAD' <<<"$claude_args"; then
  ok "Claude launch injects the same compact identity contract"
else
  fail "Claude launch injects the same compact identity contract"
fi

echo ""
echo "=== Identity Merge Test Summary ==="
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"

[[ $FAIL -eq 0 ]]
