#!/usr/bin/env bash
# test-doctor.sh — regression tests for `asha doctor` / bin/asha-drift-check.sh
# (issue #3: copilot target, shared checks, claude untagged-hook selector).
#
# Sandbox-HOME pattern: fixtures are built by running the REAL installer with
# HOME=<sandbox>; the user's HOME is never touched.
set -uo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PASS=0
FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }

command -v jq      >/dev/null 2>&1 || { echo "SKIP: jq not available" >&2; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not available" >&2; exit 0; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

run() { # forwards to drift-check with sandbox HOME
  env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
    bash "$REPO_ROOT/bin/asha-drift-check.sh" "$@"
}

# ---------------------------------------------------------------------------
echo "--- fixture: real copilot install into sandbox HOME ---"
mkdir -p "$SANDBOX/.copilot"
if env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
     bash "$REPO_ROOT/install.sh" --target copilot >/dev/null 2>&1; then
  ok "sandbox copilot install succeeds"
else
  fail "sandbox copilot install succeeds (got $?)"
fi

# ---------------------------------------------------------------------------
echo "--- test 1: healthy install passes --target copilot ---"
if out="$(run --target copilot 2>&1)"; then
  ok "doctor exits 0 on healthy copilot install"
else
  fail "doctor exits 0 on healthy copilot install (output: $(grep FAIL <<<"$out" | head -3))"
fi
grep -q "guardrails file matches installer-expected content" <<<"$out" \
  && ok "guardrails content check ran and passed" \
  || fail "guardrails content check ran and passed"
grep -q "persona loads via 'asha copilot' wrapper only" <<<"$out" \
  && ok "wrapper-scoped persona reported as INFO (by design, not failure)" \
  || fail "wrapper-scoped persona reported as INFO (by design, not failure)"

# ---------------------------------------------------------------------------
echo "--- test 2: broken copilot install fails, --fix heals what it owns ---"
# 2a. dangling asha-rooted symlink
ln -s "$REPO_ROOT/plugins/does-not-exist" "$SANDBOX/.copilot/skills/dangler"
# 2b. content-drifted generated command-skill. Keep a current timestamp to
# prove doctor compares deterministic bytes rather than mtimes.
stale_md="$SANDBOX/.copilot/skills/session-save/SKILL.md"
if [[ -f "$stale_md" ]]; then
  echo "corrupted" > "$stale_md"
  touch "$stale_md"
else
  fail "fixture: expected generated command-skill at $stale_md"
fi
# 2c. drifted guardrails
echo '{"version":1,"hooks":{}}' > "$SANDBOX/.copilot/hooks/asha-guardrails.json"

if run --target copilot >/dev/null 2>&1; then
  fail "doctor exits non-zero on broken install"
else
  ok "doctor exits non-zero on broken install"
fi
out="$(run --target copilot 2>&1 || true)"
grep -q "dangling asha symlinks" <<<"$out" && ok "dangling symlink detected" || fail "dangling symlink detected"
grep -q "command-skill content drifted" <<<"$out" && ok "content-drifted command-skill detected" || fail "content-drifted command-skill detected"
grep -q "guardrails file content drifted" <<<"$out" && ok "guardrails drift detected" || fail "guardrails drift detected"

out="$(run --target copilot --fix 2>&1 || true)"
grep -q "FIXED  regenerated drifted command-skill" <<<"$out" \
  && ok "--fix regenerates the content-drifted command-skill" \
  || fail "--fix regenerates the content-drifted command-skill"
grep -q "FIXED  rewrote guardrails file" <<<"$out" \
  && ok "--fix rewrites drifted guardrails" \
  || fail "--fix rewrites drifted guardrails"
# remove the dangler (not --fix territory: deleting user files is uninstall's job)
rm "$SANDBOX/.copilot/skills/dangler"
if run --target copilot >/dev/null 2>&1; then
  ok "post-fix re-run is clean"
else
  fail "post-fix re-run is clean"
fi

# ---------------------------------------------------------------------------
echo "--- test 3: claude untagged (tag-stripped) hooks are audited by path-prefix ---"
mkdir -p "$SANDBOX/.claude/skills" "$SANDBOX/.claude/agents" \
         "$SANDBOX/.claude/commands" "$SANDBOX/.claude/output-styles"
jq -n --arg repo "$REPO_ROOT" '{
  hooks: {
    PostToolUse: [
      { matcher: "*",
        hooks: [ { type: "command", command: ($repo + "/plugins/session/hooks/no-such-hook.sh") } ] }
    ]
  }
}' > "$SANDBOX/.claude/settings.json"
out="$(run --target claude 2>&1)"; rc=$?
if [[ $rc -ne 0 ]] && grep -q "asha hook paths missing" <<<"$out"; then
  ok "untagged asha hook with missing path FAILS --target claude (Gap-2 selector fix)"
else
  fail "untagged asha hook with missing path FAILS --target claude (rc=$rc)"
fi
# now point it at a real file: should pass and be counted
jq -n --arg repo "$REPO_ROOT" '{
  hooks: {
    PostToolUse: [
      { matcher: "*",
        hooks: [ { type: "command", command: ($repo + "/plugins/session/hooks/hooks.json") } ] }
    ]
  }
}' > "$SANDBOX/.claude/settings.json"
out="$(run --target claude 2>&1)"; rc=$?
if [[ $rc -eq 0 ]] && grep -q "1 asha hook entry registered" <<<"$out"; then
  ok "untagged asha hook with existing path passes and is counted"
else
  fail "untagged asha hook with existing path passes and is counted (rc=$rc)"
fi

# ---------------------------------------------------------------------------
echo "--- test 4: usage contract ---"
run --target bogus >/dev/null 2>&1; rc=$?
[[ $rc -eq 2 ]] && ok "invalid target exits 2" || fail "invalid target exits 2 (got $rc)"
bash "$REPO_ROOT/bin/asha" doctor --help >/dev/null 2>&1 \
  && ok "asha doctor --help exits 0" \
  || fail "asha doctor --help exits 0"
bash "$REPO_ROOT/bin/asha" doctor bogus >/dev/null 2>&1; rc=$?
[[ $rc -eq 2 ]] && ok "asha doctor bogus exits 2" || fail "asha doctor bogus exits 2 (got $rc)"

echo ""
echo "test-doctor: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
