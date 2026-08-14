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

# BSD wc pads counts with leading whitespace. The stat shim rejects GNU -c and
# implements BSD -f %m so this fixture also catches GNU-only mtime reads if they
# return to the doctor. Current command-skill checks compare rendered bytes and
# therefore do not need stat at all.
PORTABLE_BIN="$SANDBOX/portable-bin"
mkdir -p "$PORTABLE_BIN"
REAL_WC="$(command -v wc)"
cat > "$PORTABLE_BIN/wc" <<EOF
#!/usr/bin/env bash
out="\$("$REAL_WC" "\$@")"
printf '%8s\n' "\$out"
EOF
cat > "$PORTABLE_BIN/stat" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then
  exit 64
fi
if [[ "${1:-}" == "-f" && "${2:-}" == "%m" && $# -eq 3 ]]; then
  python3 -c 'import os, sys; print(int(os.stat(sys.argv[1]).st_mtime))' "$3"
  exit
fi
exit 64
EOF
chmod +x "$PORTABLE_BIN/wc" "$PORTABLE_BIN/stat"

run_portable() { # run with BSD-like wc/stat behavior on every host
  env -i HOME="$SANDBOX" PATH="$PORTABLE_BIN:$PATH" USER="${USER:-test}" \
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
grep -q 'compact identity merge valid' <<<"$out" \
  && ok "doctor validates the hot identity budget" \
  || fail "doctor validates the hot identity budget"

# Already-current recovery must not short-circuit cleanup of retired exact
# artifacts. Doctor --fix uses the same ownership-aware reconciliation.
jq -nc --arg e "$REPO_ROOT/plugins/session/hooks/handlers/nudge-engine.sh" '{
  version:1, hooks:{
    sessionStart:[{type:"command",bash:($e + " SessionStart"),timeoutSec:10}],
    userPromptSubmitted:[{type:"command",bash:($e + " UserPromptSubmit"),timeoutSec:10}],
    postToolUse:[{type:"command",bash:($e + " PostToolUse"),timeoutSec:10}]
  }
}' > "$SANDBOX/.copilot/hooks/asha-nudges.json"
out="$(run --target copilot --fix 2>&1 || true)"
[[ ! -e "$SANDBOX/.copilot/hooks/asha-nudges.json" ]] \
  && ok "doctor --fix reconciles retired hook beside current recovery artifact" \
  || fail "doctor --fix reconciles retired hook beside current recovery artifact"

IGNORE_PROJECT="$SANDBOX/ignore-project"
mkdir -p "$IGNORE_PROJECT/.asha"
printf '{"initialized":true,"memory_version":2,"project_id":"ignore-test"}\n' > "$IGNORE_PROJECT/.asha/config.json"
printf '/Work/session-state/\n!/Work/session-state/\n!/Work/session-state/*.json\n' > "$IGNORE_PROJECT/.gitignore"
git -C "$IGNORE_PROJECT" init -q
out="$(cd "$IGNORE_PROJECT" && run --target copilot 2>&1 || true)"
grep -q 'leaves Work/session-state JSON trackable' <<<"$out" \
  && ok "doctor verifies Git ignore semantics rather than a literal line" \
  || fail "doctor verifies Git ignore semantics rather than a literal line"

printf '{"initialized":true,"memory_version":2,"project_id":"   "}\n' > "$IGNORE_PROJECT/.asha/config.json"
out="$(cd "$IGNORE_PROJECT" && run --target copilot 2>&1 || true)"
grep -q 'config lacks memory_version=2 or project_id' <<<"$out" \
  && ok "doctor rejects a whitespace-only Memory v2 project_id" \
  || fail "doctor rejects a whitespace-only Memory v2 project_id"

# ---------------------------------------------------------------------------
echo "--- test 1b: Copilot version outside the live-verified range warns ---"
VERSION_BIN="$SANDBOX/version-bin"
mkdir -p "$VERSION_BIN"
cat > "$VERSION_BIN/copilot" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "--version" ]] && printf 'GitHub Copilot CLI %s\n' "${ASHA_TEST_COPILOT_VERSION:?}"
EOF
chmod +x "$VERSION_BIN/copilot"
run_with_copilot_version() {
  env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
    ASHA_COPILOT_CMD="$VERSION_BIN/copilot" ASHA_TEST_COPILOT_VERSION="$1" \
    bash "$REPO_ROOT/bin/asha-drift-check.sh" --target copilot
}
out="$(run_with_copilot_version 1.0.75 2>&1)"
if grep -q "outside the live-verified range" <<<"$out"; then
  fail "verified Copilot version does not warn"
else
  ok "verified Copilot version does not warn"
fi
out="$(run_with_copilot_version 1.0.78 2>&1)"
if grep -q "outside the live-verified range" <<<"$out"; then
  fail "workspace-v2 verified Copilot version does not warn"
else
  ok "workspace-v2 verified Copilot version does not warn"
fi
out="$(run_with_copilot_version 1.0.79 2>&1)"
grep -q "outside the live-verified range 1.0.63-1.0.78" <<<"$out" \
  && ok "newer Copilot version warns to run the live canary" \
  || fail "newer Copilot version warns to run the live canary"

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
  || { jq -e '.hooks.preToolUse[0].bash | endswith("copilot-policy-adapter.sh")' \
        "$SANDBOX/.copilot/hooks/asha-guardrails.json" >/dev/null 2>&1 \
       && ok "--fix rewrites drifted guardrails through artifact ownership" \
       || fail "--fix rewrites drifted guardrails"; }
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
         "$SANDBOX/.claude/commands"
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
echo "--- test 3b: codex hook audit resolves env-wrapped executables ---"
mkdir -p "$SANDBOX/.codex"
cat > "$SANDBOX/.codex/config.toml" <<EOF
[features]
hooks = true

[[hooks.SessionStart]]
[[hooks.SessionStart.hooks]]
command = "env ASHA_HARNESS=codex $REPO_ROOT/plugins/session/hooks/handlers/session-start.sh"
EOF
out="$(run --target codex 2>&1 || true)"
if grep -q "all hook command paths exist (codex: 1 command(s) enumerated)" <<<"$out" \
    && ! grep -q "tagged hook paths missing" <<<"$out"; then
  ok "env wrapper resolves to the actual hook executable"
else
  fail "env wrapper resolves to the actual hook executable (output: $(grep -E 'hook command|hook paths' <<<"$out"))"
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

# ---------------------------------------------------------------------------
echo "--- test 5: BSD userland compatibility ---"
out="$(run_portable --target copilot 2>&1)"; rc=$?
if [[ $rc -eq 0 ]] && grep -q "no CLAUDE_PLUGIN_ROOT in plugin markdown" <<<"$out"; then
  ok "BSD-padded wc count does not cause a false repo-state failure"
else
  fail "BSD-padded wc count does not cause a false repo-state failure (rc=$rc)"
fi
if grep -Eq '^FAIL[[:space:]]+0 CLAUDE_PLUGIN_ROOT refs remain' <<<"$out"; then
  fail "healthy BSD-like run does not emit FAIL 0"
else
  ok "healthy BSD-like run does not emit FAIL 0"
fi

echo "corrupted on BSD fixture" > "$stale_md"
touch "$stale_md"
out="$(run_portable --target copilot 2>&1 || true)"
grep -q "command-skill content drifted" <<<"$out" \
  && ok "drifted command-skill is detected without GNU stat" \
  || fail "drifted command-skill is detected without GNU stat"
out="$(run_portable --target copilot --fix 2>&1 || true)"
grep -q "FIXED  regenerated drifted command-skill" <<<"$out" \
  && ok "--fix repairs a drifted command-skill without GNU stat" \
  || fail "--fix repairs a drifted command-skill without GNU stat"
run_portable --target copilot >/dev/null 2>&1 \
  && ok "BSD-like post-fix re-run is clean" \
  || fail "BSD-like post-fix re-run is clean"

echo ""
echo "test-doctor: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
