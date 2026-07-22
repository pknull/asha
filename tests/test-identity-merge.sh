#!/usr/bin/env bash
# test-identity-merge.sh — sandboxed smoke tests for identity cache builders.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
IDENTITY_MERGE="$REPO_ROOT/identity/identity-merge.sh"
OPERATIONAL_MERGE="$REPO_ROOT/identity/operational-merge.sh"

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
# Test 1: every user identity source is merged
# ---------------------------------------------------------------------------
echo "--- test 1: identity sources are merged ---"
reset_sandbox
mkdir -p "$SANDBOX/.asha"
printf 'SOUL_SENTINEL\n' > "$SANDBOX/.asha/soul.md"
printf 'VOICE_SENTINEL\n' > "$SANDBOX/.asha/voice.md"
printf 'KEEPER_SENTINEL\n' > "$SANDBOX/.asha/keeper.md"
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

# ---------------------------------------------------------------------------
# Test 2: missing ~/.asha inputs are benign for both builders
# ---------------------------------------------------------------------------
echo "--- test 2: absent user inputs are benign ---"
reset_sandbox
if run_identity_merge >/dev/null 2>&1; then
  ok "identity merge exits 0 without ~/.asha files"
else
  fail "identity merge exits 0 without ~/.asha files"
fi
[[ -s "$SANDBOX/.cache/asha/instructions.md" ]] && \
  grep -q '^# Asha Identity Layer (merged)$' "$SANDBOX/.cache/asha/instructions.md" \
  && ok "identity merge emits sane fallback output" \
  || fail "identity merge emits sane fallback output"
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
# Test 4: operational content honors its 4000 + 3000 byte budgets
# ---------------------------------------------------------------------------
echo "--- test 4: operational merge enforces byte caps ---"
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
   grep -q 'operation.md exceeded 4000 chars' "$SANDBOX/.cache/asha/operational.md" && \
   grep -q 'learnings.md exceeded 3000 chars' "$SANDBOX/.cache/asha/operational.md"; then
  ok "content beyond both documented caps is excluded"
else
  fail "content beyond both documented caps is excluded"
fi
merged_bytes="$(wc -c < "$SANDBOX/.cache/asha/operational.md" | tr -d '[:space:]')"
[[ "$merged_bytes" -le 8000 ]] \
  && ok "merged operational cache stays within sane overhead ($merged_bytes bytes)" \
  || fail "merged operational cache stays within sane overhead (got $merged_bytes bytes)"

echo ""
echo "=== Identity Merge Test Summary ==="
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"

[[ $FAIL -eq 0 ]]
