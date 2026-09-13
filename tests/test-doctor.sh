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

command -v jq      >/dev/null 2>&1 || { echo "ERROR: jq not available" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not available" >&2; exit 1; }

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

echo "--- fixture: real codex install into sandbox HOME ---"
mkdir -p "$SANDBOX/.codex"
printf 'features.hooks=true\n' > "$SANDBOX/.codex/config.toml"
if env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
     bash "$REPO_ROOT/install.sh" --target codex >/dev/null 2>&1; then
  ok "sandbox codex install succeeds"
else
  fail "sandbox codex install succeeds (got $?)"
fi

# ---------------------------------------------------------------------------
echo "--- test 0: experience capability reporting names dormant/native limits ---"
out="$(env -i HOME="$SANDBOX" PATH="$PATH" bash -c \
  'source "$1/lib/doctor.sh"; _asha_doctor_session_profile_section all' _ "$REPO_ROOT" 2>&1)"
if [[ "$out" == *"session-experience capability"* && "$out" == *"session-guidance capability"* \
   && "$out" == *"experience-review capability"* && "$out" == *"native review gated"* ]]; then
  ok "doctor reports dormant capture/guidance and gated native review for all targets"
else
  fail "doctor experience capability report missing: $out"
fi

echo "--- test 0a: optional plugin drift follows --with-canary ---"
out="$(run --target copilot 2>&1)"; rc=$?
if [[ $rc -eq 0 && "$out" != *"/plugins/test/"* ]]; then
  ok "default drift excludes optional canary sources"
else
  fail "default drift excludes optional canary sources (rc=$rc)"
fi
out="$(run --target copilot --fix 2>&1)"; rc=$?
if [[ $rc -eq 0 && ! -e "$SANDBOX/.copilot/skills/test-ping" \
   && ! -e "$SANDBOX/.copilot/agents/test-test-echo.agent.md" ]]; then
  ok "default --fix does not recreate canary artifacts"
else
  fail "default --fix does not recreate canary artifacts (rc=$rc)"
fi
out="$(run --target copilot --with-canary 2>&1)"; rc=$?
if [[ $rc -ne 0 && "$out" == *"/plugins/test/"* ]]; then
  ok "--with-canary drift requires optional canary sources"
else
  fail "--with-canary drift requires optional canary sources (rc=$rc)"
fi
if env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
     bash "$REPO_ROOT/install.sh" --target copilot --with-canary >/dev/null 2>&1; then
  out="$(run --target copilot --with-canary 2>&1)"; rc=$?
  if [[ $rc -eq 0 && -L "$SANDBOX/.copilot/skills/test-ping" \
     && -f "$SANDBOX/.copilot/agents/test-test-echo.agent.md" ]]; then
    ok "--with-canary drift passes after an opt-in install"
  else
    fail "--with-canary drift passes after an opt-in install (rc=$rc)"
  fi
  out="$(env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
    bash "$REPO_ROOT/bin/asha" doctor copilot --with-canary 2>&1)"; rc=$?
  [[ $rc -eq 0 ]] \
    && ok "asha doctor forwards --with-canary" \
    || fail "asha doctor forwards --with-canary (rc=$rc)"
else
  fail "with-canary doctor fixture installs"
fi
if env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
     bash "$REPO_ROOT/install.sh" --target copilot >/dev/null 2>&1 \
   && [[ ! -e "$SANDBOX/.copilot/skills/test-ping" \
      && ! -e "$SANDBOX/.copilot/agents/test-test-echo.agent.md" ]]; then
  ok "default reinstall restores the default doctor fixture"
else
  fail "default reinstall restores the default doctor fixture"
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
grep -q 'verification-pass and style-audit source seams are complete' <<<"$out" \
  && ok "doctor validates completion/style source seams" \
  || fail "doctor validates completion/style source seams"
grep -q 'Memory v2 recovery hooks match installer-expected content' <<<"$out" \
  && jq -e '.hooks.userPromptSubmitted[1].bash
      | endswith("verify-pass-complete.sh")' \
    "$SANDBOX/.copilot/hooks/asha-recovery.json" >/dev/null 2>&1 \
  && ok "doctor validates Copilot next-prompt verification rendering" \
  || fail "doctor validates Copilot next-prompt verification rendering"

out="$(run --target codex 2>&1)"; rc=$?
if [[ $rc -eq 0 ]] \
    && grep -Eq 'Codex [1-9][0-9]* expected commands registered, executable paths verified; verification Stop and style PostToolUse checked;' <<<"$out" \
    && jq -e --arg handlers "$REPO_ROOT/plugins/session/hooks/handlers/" '
      any(.hooks.Stop[]?.hooks[]?;
        .command == ("env ASHA_HARNESS=codex " + $handlers + "verify-pass-complete.sh"))
      and any(.hooks.PostToolUse[]?.hooks[]?;
        .command == ("env ASHA_HARNESS=codex " + $handlers + "post-tool-use.sh"))
    ' "$SANDBOX/.codex/hooks.json" >/dev/null; then
  ok "doctor validates Codex completion/style rendering"
else
  fail "doctor validates Codex completion/style rendering (rc=$rc)"
fi

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
grep -q 'working tree leaves .asha/control-task.json trackable' <<<"$out" \
  && ok "doctor reports missing Control marker working-tree readiness" \
  || fail "doctor reports missing Control marker working-tree readiness"

printf '{"initialized":true,"memory_version":2,"project_id":"   "}\n' > "$IGNORE_PROJECT/.asha/config.json"
out="$(cd "$IGNORE_PROJECT" && run --target copilot 2>&1 || true)"
grep -q 'config lacks memory_version=2 or project_id' <<<"$out" \
  && ok "doctor rejects a whitespace-only Memory v2 project_id" \
  || fail "doctor rejects a whitespace-only Memory v2 project_id"

MEMORY_LIMIT_PROJECT="$SANDBOX/memory-limit-project"
mkdir -p "$MEMORY_LIMIT_PROJECT/.asha" "$MEMORY_LIMIT_PROJECT/Memory"
printf '{"initialized":true,"memory_version":2,"project_id":"memory-limit-test"}\n' \
  > "$MEMORY_LIMIT_PROJECT/.asha/config.json"
printf '/Work/session-state/\n/Work/memory-migration/\n/.asha/control-task.json\n' \
  > "$MEMORY_LIMIT_PROJECT/.gitignore"
python3 - "$MEMORY_LIMIT_PROJECT/Memory/decisions.md" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
prefix = b"# Decisions\n\n"
path.write_bytes(prefix + b"x" * (64 * 1024 - len(prefix)))
PY
out="$(cd "$MEMORY_LIMIT_PROJECT" && run --target copilot 2>&1)"; rc=$?
if [[ $rc -eq 0 ]] \
    && grep -q 'Memory/decisions.md is within the 65,536-byte publication cap' <<<"$out" \
    && ! grep -q 'Memory/decisions.md exceeds' <<<"$out"; then
  ok "doctor accepts decisions.md at the 64 KiB publication cap"
else
  fail "doctor accepts decisions.md at the 64 KiB publication cap (rc=$rc)"
fi

printf 'x' >> "$MEMORY_LIMIT_PROJECT/Memory/decisions.md"
out="$(cd "$MEMORY_LIMIT_PROJECT" && run --target copilot 2>&1)"; rc=$?
if [[ $rc -eq 0 ]] \
    && grep -q 'WARN  current project Memory/decisions.md exceeds the 65,536-byte publication cap (65,537 bytes)' <<<"$out" \
    && grep -q '/session:consolidate' <<<"$out" \
    && grep -q '/session:save' <<<"$out"; then
  ok "doctor warns non-fatally on oversized decisions.md with migration guidance"
else
  fail "doctor warns non-fatally on oversized decisions.md with migration guidance (rc=$rc)"
fi

# ---------------------------------------------------------------------------
echo "--- test 1a: current source defeats matching stale ownership ledgers ---"
assert_command_skill_source_freshness() { # target skill_md
  local target="$1" skill_md="$2" manifest stale_hash out rc
  manifest="$SANDBOX/.asha/install-manifests/$target.json"

  printf '\nstale rendered bytes\n' >> "$skill_md"
  stale_hash="$(python3 -c \
    'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$skill_md")"
  if jq --arg d "$skill_md" --arg h "$stale_hash" \
      '(.artifacts[] | select(.destination == $d) | .sha256) = $h' \
      "$manifest" > "$manifest.tmp" && mv "$manifest.tmp" "$manifest" \
      && jq -e --arg d "$skill_md" --arg h "$stale_hash" \
        '.artifacts[] | select(.destination == $d and .sha256 == $h)' \
        "$manifest" >/dev/null; then
    ok "$target fixture gives stale command-skill bytes a matching ownership hash"
  else
    fail "$target fixture gives stale command-skill bytes a matching ownership hash"
    return
  fi

  out="$(run --target "$target" 2>&1)"; rc=$?
  if [[ $rc -ne 0 ]] && grep -q "command-skill content drifted" <<<"$out"; then
    ok "$target doctor re-renders command source rather than trusting the ownership ledger"
  else
    fail "$target doctor re-renders command source rather than trusting the ownership ledger (rc=$rc)"
  fi
  grep -q "generated-artifact ownership manifest clean ($target)" <<<"$out" \
    && ok "$target stale-source fixture leaves ownership validation clean" \
    || fail "$target stale-source fixture leaves ownership validation clean"

  out="$(run --target "$target" --fix 2>&1)"; rc=$?
  if [[ $rc -eq 0 ]] && grep -q "FIXED  regenerated drifted command-skill" <<<"$out"; then
    ok "$target --fix regenerates source-drifted command-skill"
  else
    fail "$target --fix regenerates source-drifted command-skill (rc=$rc)"
  fi
  run --target "$target" >/dev/null 2>&1 \
    && ok "$target source-freshness post-fix re-run is clean" \
    || fail "$target source-freshness post-fix re-run is clean"
}

assert_command_skill_source_freshness \
  copilot "$SANDBOX/.copilot/skills/session-save/SKILL.md"
assert_command_skill_source_freshness \
  codex "$SANDBOX/.codex/skills/session-save/SKILL.md"

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
echo "--- test 2e: a source skill missing from the install is named; install restores it ---"
skill_link="$SANDBOX/.codex/skills/session-orchestrate-initiative"
if [[ -L "$skill_link" ]]; then
  unlink "$skill_link"
  out="$(run --target codex 2>&1 || true)"
  if grep -q "installed skill symlinks missing (codex); run: asha install codex" <<<"$out" && grep -q "skills/orchestrate-initiative" <<<"$out"; then
    ok "missing source skill FAILS --target codex and is named"
  else
    fail "missing source skill not reported: $(grep -i skill <<<"$out" | head -3)"
  fi
  out="$(run --target codex --fix 2>&1 || true)"
  if [[ ! -e "$skill_link" ]] && grep -q "run: asha install codex" <<<"$out"; then
    ok "--fix defers codex skill naming to the installer"
  else
    fail "--fix should not guess codex skill names"
  fi
  env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
    bash "$REPO_ROOT/install.sh" --target codex >/dev/null 2>&1 || true
  if [[ -L "$skill_link" ]]; then
    ok "reinstall restores the skill link"
  else
    fail "reinstall did not restore $skill_link"
  fi
  out="$(run --target codex 2>&1 || true)"
  if grep -q "every source skill is installed (codex)" <<<"$out"; then
    ok "codex probe passes after reinstall"
  else
    fail "codex probe after reinstall: $(grep -iE "skill|FAIL" <<<"$out" | head -4 | tr '\n' '|')"
  fi
else
  fail "fixture: expected codex skill symlink at $skill_link"
fi

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
# Now point it at a real file: path extraction passes and is counted, while the
# newly required completion/style seam correctly remains missing.
jq -n --arg repo "$REPO_ROOT" '{
  hooks: {
    PostToolUse: [
      { matcher: "*",
        hooks: [ { type: "command", command: ($repo + "/plugins/session/hooks/hooks.json SessionStart") } ] }
    ]
  }
}' > "$SANDBOX/.claude/settings.json"
out="$(run --target claude 2>&1)"; rc=$?
if [[ $rc -ne 0 ]] && grep -q "1 asha hook entry registered" <<<"$out" \
    && grep -q "Claude verification Stop or style PostToolUse seam is missing" <<<"$out" \
    && ! grep -q "asha hook paths missing" <<<"$out"; then
  ok "untagged existing hook path passes extraction while required seams stay enforced"
else
  fail "untagged existing hook path extraction or required-seam enforcement failed (rc=$rc)"
fi

# ---------------------------------------------------------------------------
echo "--- test 3b: codex hook audit resolves env-wrapped executables ---"
mkdir -p "$SANDBOX/.codex"
printf 'features.hooks=true\n' > "$SANDBOX/.codex/config.toml"
out="$(run --target codex 2>&1 || true)"
if grep -q "expected commands registered, executable paths verified" <<<"$out" \
    && grep -q "verification Stop and style PostToolUse checked" <<<"$out" \
    && grep -q 'env ASHA_HARNESS=codex' "$SANDBOX/.codex/hooks.json"; then
  ok "owned JSON env wrappers resolve real expected hook executables and required seams"
else
  fail "owned JSON command/seam inspection (output: $(grep -E 'Codex|hook' <<<"$out"))"
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

echo "--- test 6: claude home with one asha skill is audited; --fix links the rest by <ns>-<dir> ---"
mkdir -p "$SANDBOX/.claude/skills"
ln -sfn "$REPO_ROOT/plugins/session/skills/skill-creator" "$SANDBOX/.claude/skills/session-skill-creator"
out="$(run --target claude 2>&1 || true)"
if grep -q "installed skill symlinks missing (claude)" <<<"$out" && grep -q "skills/orchestrate-initiative" <<<"$out"; then
  ok "partially installed claude home names the missing skills"
else
  fail "claude missing-skill probe: $(grep -i skill <<<"$out" | head -3)"
fi
out="$(run --target claude --fix 2>&1 || true)"
if grep -q "FIXED  linked missing skill: $SANDBOX/.claude/skills/session-orchestrate-initiative" <<<"$out" \
   && [[ "$(readlink -f "$SANDBOX/.claude/skills/session-orchestrate-initiative")" == "$(readlink -f "$REPO_ROOT/plugins/session/skills/orchestrate-initiative")" ]]; then
  ok "--fix links missing claude skills to their sources"
else
  fail "--fix did not link claude skills: $(grep FIXED <<<"$out" | head -2)"
fi
out="$(run --target claude 2>&1 || true)"
if grep -q "every source skill is installed (claude)" <<<"$out"; then
  ok "claude post-fix probe passes"
else
  fail "claude post-fix probe still failing: $(grep -iE "skill|FAIL" <<<"$out" | head -4 | tr '\n' '|')"
fi

echo ""
# The doctor remains read-only: it diagnoses preserved explicit hooks=false
# rather than granting trust or flipping the user's feature flag on their behalf.
if python3 - "$REPO_ROOT" "$SANDBOX" <<'PY_U8_DOCTOR'
import hashlib, json, os, pathlib, shlex, site, subprocess, sys, tempfile
tomllib = __import__("tomllib" if sys.version_info >= (3, 11) else "tomli")
root, work = map(pathlib.Path, sys.argv[1:])
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
from lib.control.doctor import codex_hooks_probe
def snapshot(home):
    return {str(p.relative_to(home)): (p.lstat().st_mode, p.lstat().st_uid,
            p.lstat().st_gid, os.readlink(p) if p.is_symlink() else
            None if p.is_dir() else p.read_bytes()) for p in home.rglob('*')}
with tempfile.TemporaryDirectory(dir=work) as directory:
    home = pathlib.Path(directory)
    (home/'.codex').mkdir()
    config = home/'.codex/config.toml'
    raw = (b'# exact foreign bytes\r\n["features"]\r\nhooks = false\r\n'
           b'[mcp_servers.playwright]\r\ncommand = "/bin/true"\r\n'
           b'[hooks.state."one.slot"]\r\ntrusted_hash = "one"\r\n'
           b'[hooks.state."two.slot"]\r\ntrusted_hash = "two"\r\n# tail\r\n\r\n')
    config.write_bytes(raw)
    config.chmod(0o640)
    env = {'HOME':directory, 'PATH':os.environ['PATH'], 'USER':os.environ.get('USER','test'),
           'PYTHONPATH':site.getusersitepackages()}
    installed = subprocess.run([str(root/'install.sh'),'--target','codex'], cwd=root,
        env=env, capture_output=True, timeout=180)
    assert installed.returncode == 0, installed.stderr.decode()
    assert raw in config.read_bytes()
    parsed = tomllib.loads(config.read_text())
    original = tomllib.loads(raw.decode())
    assert parsed['features'] == original['features']
    assert parsed['mcp_servers'] == original['mcp_servers']
    assert parsed['hooks']['state'] == original['hooks']['state']
    assert config.stat().st_mode & 0o777 == 0o640

    hooks = home/'.codex/hooks.json'
    ledger = home/'.asha/install-manifests/codex.json'
    hook_bytes, ledger_bytes = hooks.read_bytes(), ledger.read_bytes()
    groups = json.loads(hook_bytes)['hooks']
    expected_count = sum(len(group['hooks']) for event in groups.values() for group in event)
    assert expected_count > 0
    seams = (('Stop', 'verify-pass-complete.sh'), ('PostToolUse', 'post-tool-use.sh'))
    for event, name in seams:
        expected = ['env', 'ASHA_HARNESS=codex', str(root/'plugins/session/hooks/handlers'/name)]
        assert sum(shlex.split(h['command']) == expected
                   for group in groups[event] for h in group['hooks']) == 1

    for case in ('healthy', 'Stop', 'PostToolUse', 'disabled', 'malformed'):
        config.write_bytes(raw.replace(b'hooks = false', b'hooks = true'))
        hooks.write_bytes(hook_bytes)
        ledger.write_bytes(ledger_bytes)
        if case in ('Stop', 'PostToolUse'):
            # Keep ownership valid so this tests missing semantic coverage,
            # not just the separate modified-artifact refusal.
            value = json.loads(hook_bytes)
            name = dict(seams)[case]
            value['hooks'][case] = [group for group in value['hooks'][case]
                if not any(shlex.split(h['command'])[-1].endswith('/'+name) for h in group['hooks'])]
            hooks.write_text(json.dumps(value))
            rows = json.loads(ledger_bytes)
            for row in rows['artifacts']:
                if row['destination'] == str(hooks):
                    row['sha256'] = hashlib.sha256(hooks.read_bytes()).hexdigest()
            ledger.write_text(json.dumps(rows))
        elif case == 'disabled': config.write_bytes(raw)
        elif case == 'malformed': config.write_bytes(b'bad = [')
        before = snapshot(home)
        probe = codex_hooks_probe(home/'.codex', home/'.asha', user_home=home, root=root)
        checked = subprocess.run([str(root/'bin/asha-drift-check.sh'),'--target','codex'],
            cwd=root, env=env, capture_output=True, timeout=180)
        assert checked.returncode == (0 if case == 'healthy' else 1), checked.stdout.decode()+checked.stderr.decode()
        assert snapshot(home) == before
        assert probe.detail.encode() in checked.stdout, (probe, checked.stdout)
        if case == 'healthy':
            assert probe.outcome == 'match', probe
            assert f'Codex {expected_count} expected commands registered, executable paths verified;' in probe.detail
            assert 'verification Stop and style PostToolUse checked;' in probe.detail
            assert 'native trust and execution NOT verified' in probe.detail
        elif case in ('Stop', 'PostToolUse'):
            assert probe.outcome == 'missing', probe
            assert 'missing/duplicate expected hook groups: '+case in probe.detail
        elif case == 'malformed':
            assert probe.outcome == 'unavailable', probe
            assert b'Codex hook inspection refused' in checked.stdout
            assert b'Invalid value' in checked.stdout
        else:
            assert probe.outcome == 'mismatch', probe
            assert b'Codex hooks registered but disabled' in checked.stdout
            assert b'explicit features.hooks=false' in checked.stdout
        print(f'U8 doctor {case}: rc={checked.returncode}, outcome={probe.outcome}, {probe.detail}')
PY_U8_DOCTOR
then ok "U8 read-only doctor preserves foreign bytes, parsed trust, modes and artifacts"
else fail "U8 read-only doctor preserves foreign bytes, parsed trust, modes and artifacts"
fi

echo "test-doctor: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
