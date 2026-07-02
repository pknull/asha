#!/usr/bin/env bash
# test-init-repo.sh — regression tests for `asha init-repo` (issue #3).
# Scaffolds into throwaway git repos under mktemp; nothing else is touched.
set -uo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PASS=0
FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq not available" >&2; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
T="$WORK/repo"
mkdir -p "$T" && git -C "$WORK" init -q "$T"

ir() { bash "$REPO_ROOT/bin/asha" init-repo --dir "$T" "$@"; }

INSTR="$T/.github/instructions/team-conventions.instructions.md"
SETTINGS="$T/.github/copilot/settings.json"

# ---------------------------------------------------------------------------
echo "--- test 1: dry-run writes nothing ---"
ir --dry-run >/dev/null 2>&1 && ok "dry-run exits 0" || fail "dry-run exits 0"
[[ ! -f "$T/AGENTS.md" ]] && ok "dry-run created nothing" || fail "dry-run created nothing"

# ---------------------------------------------------------------------------
echo "--- test 2: scaffold creates the three files; hint composes with copilot init ---"
out="$(ir 2>&1)" && ok "scaffold exits 0" || fail "scaffold exits 0"
for f in "$T/AGENTS.md" "$INSTR" "$SETTINGS"; do
  [[ -f "$f" ]] && ok "created ${f#"$T"/}" || fail "created ${f#"$T"/}"
done
grep -q "run 'copilot init'" <<<"$out" \
  && ok "hints at native copilot init (never writes copilot-instructions.md)" \
  || fail "hints at native copilot init"
[[ ! -f "$T/.github/copilot-instructions.md" ]] \
  && ok "did not write copilot-instructions.md" \
  || fail "did not write copilot-instructions.md"
jq -e '.enabledPlugins == {}' "$SETTINGS" >/dev/null \
  && ok "settings.json has empty enabledPlugins map" \
  || fail "settings.json has empty enabledPlugins map"

# ---------------------------------------------------------------------------
echo "--- test 3: idempotent — re-run skips, content unchanged ---"
echo "user edit" >> "$T/AGENTS.md"
sum_before="$(cat "$T/AGENTS.md" "$INSTR" "$SETTINGS" | cksum)"
ir >/dev/null 2>&1 && ok "re-run exits 0" || fail "re-run exits 0"
sum_after="$(cat "$T/AGENTS.md" "$INSTR" "$SETTINGS" | cksum)"
[[ "$sum_before" == "$sum_after" ]] \
  && ok "re-run changed nothing (user edit preserved)" \
  || fail "re-run changed nothing"

# ---------------------------------------------------------------------------
echo "--- test 4: --check semantics (OK / MISSING / DRIFT / LOCAL) ---"
ir --check >/dev/null 2>&1 && ok "--check conforming exits 0" || fail "--check conforming exits 0"

rm "$T/AGENTS.md"
out="$(ir --check 2>&1)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "MISSING  AGENTS.md" <<<"$out" \
  && ok "missing file -> MISSING, exit 1" || fail "missing file -> MISSING, exit 1 (rc=$rc)"
ir >/dev/null 2>&1  # restore

echo "extra line while marker present" >> "$INSTR"
out="$(ir --check 2>&1)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "DRIFT" <<<"$out" \
  && ok "marker-managed edit -> DRIFT, exit 1" || fail "marker-managed edit -> DRIFT, exit 1 (rc=$rc)"

# remove the marker line: team takes ownership
grep -vF '<!-- asha:init-repo' "$INSTR" > "$INSTR.new" && mv "$INSTR.new" "$INSTR"
out="$(ir --check 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && grep -q "LOCAL" <<<"$out" \
  && ok "marker removed -> LOCAL, exit 0" || fail "marker removed -> LOCAL, exit 0 (rc=$rc)"

echo '{ not json' > "$SETTINGS"
out="$(ir --check 2>&1)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "DRIFT.*not a JSON object" <<<"$out" \
  && ok "corrupt settings.json -> DRIFT, exit 1" || fail "corrupt settings.json -> DRIFT, exit 1 (rc=$rc)"
echo '{"enabledPlugins": {"asha-code@asha": true}}' > "$SETTINGS"
ir --check >/dev/null 2>&1 \
  && ok "team-populated enabledPlugins values pass --check" \
  || fail "team-populated enabledPlugins values pass --check"

# ---------------------------------------------------------------------------
echo "--- test 5: guards ---"
NT="$WORK/not-a-repo"; mkdir -p "$NT"
bash "$REPO_ROOT/bin/asha" init-repo --dir "$NT" >/dev/null 2>&1; rc=$?
[[ $rc -eq 2 ]] && ok "non-git target refused (exit 2)" || fail "non-git target refused (got $rc)"
bash "$REPO_ROOT/bin/asha" init-repo --dir "$NT" --force >/dev/null 2>&1 \
  && ok "non-git target allowed under --force" || fail "non-git target allowed under --force"
bash "$REPO_ROOT/bin/asha" init-repo --dir "$T" --template nope >/dev/null 2>&1; rc=$?
[[ $rc -eq 2 ]] && ok "unknown template refused (exit 2)" || fail "unknown template refused (got $rc)"
bash "$REPO_ROOT/bin/asha" init-repo --bogus >/dev/null 2>&1; rc=$?
[[ $rc -eq 2 ]] && ok "unknown flag refused (exit 2)" || fail "unknown flag refused (got $rc)"

# ---------------------------------------------------------------------------
echo "--- test 6: --force restores managed files, never settings values ---"
echo "wrecked" > "$T/AGENTS.md"
ir --force >/dev/null 2>&1 || fail "--force exits 0"
grep -q "Agent Guidance" "$T/AGENTS.md" \
  && ok "--force restored AGENTS.md from template" \
  || fail "--force restored AGENTS.md from template"
jq -e '.enabledPlugins["asha-code@asha"] == true' "$SETTINGS" >/dev/null \
  && ok "--force preserved team enabledPlugins values" \
  || fail "--force preserved team enabledPlugins values"

# ---------------------------------------------------------------------------
echo "--- test 7: review-finding regressions ---"
# non-git --force must NOT overwrite an existing file (no git = no undo)
NG="$WORK/non-git"; mkdir -p "$NG"
echo "hand-written" > "$NG/AGENTS.md"
bash "$REPO_ROOT/bin/asha" init-repo --dir "$NG" --force >/dev/null 2>&1
grep -q "hand-written" "$NG/AGENTS.md" \
  && ok "non-git --force preserves existing files (creates missing only)" \
  || fail "non-git --force preserves existing files"
[[ -f "$NG/.github/copilot/settings.json" ]] \
  && ok "non-git --force still creates missing files" \
  || fail "non-git --force still creates missing files"
# invalid settings.json: dry-run and live agree (SKIP, never a planned WRITE)
echo '{ not json' > "$SETTINGS"
out_dry="$(ir --dry-run 2>&1)"
out_live="$(ir 2>&1)"
if ! grep -q "WRITE.*settings.json" <<<"$out_dry" && grep -q "not a JSON object" <<<"$out_live"; then
  ok "invalid settings.json: dry-run plan matches live behavior (SKIP)"
else
  fail "invalid settings.json: dry-run plan matches live behavior"
fi
echo '{"enabledPlugins": {}}' > "$SETTINGS"
# null-valued enabledPlugins: --check OK and scaffold must not rewrite it
printf '{\n    "enabledPlugins": null,\n    "teamKey": "keep"\n}\n' > "$SETTINGS"
ir --check >/dev/null 2>&1 && ir >/dev/null 2>&1
grep -q '"teamKey": "keep"' "$SETTINGS" && grep -q '    "enabledPlugins": null' "$SETTINGS" \
  && ok "scaffold never rewrites a file --check calls conforming" \
  || fail "scaffold never rewrites a file --check calls conforming"
# write failures must exit non-zero, not report WROTE (set -e wrapper)
RO="$WORK/readonly"; mkdir -p "$RO" && git -C "$WORK" init -q "$RO" && chmod 555 "$RO"
if bash "$REPO_ROOT/bin/asha" init-repo --dir "$RO" >/dev/null 2>&1; then
  fail "failed writes exit non-zero"
else
  ok "failed writes exit non-zero"
fi
chmod 755 "$RO"

# ---------------------------------------------------------------------------
echo "--- test 8: review-pass-2 regressions ---"
# trailing value-flag dies loudly with exit 2, not silently under set -e
rc=0; out="$(bash "$REPO_ROOT/bin/asha" init-repo --dir 2>&1)" || rc=$?
if [[ $rc -eq 2 && "$out" == *"--dir requires a value"* ]]; then
  ok "trailing --dir dies loudly (exit 2)"
else
  fail "trailing --dir dies loudly (rc=$rc, out=$out)"
fi
rc=0; out="$(bash "$REPO_ROOT/bin/asha" doctor --target 2>&1)" || rc=$?
if [[ $rc -eq 2 && "$out" == *"--target requires a value"* ]]; then
  ok "trailing doctor --target dies loudly (exit 2)"
else
  fail "trailing doctor --target dies loudly (rc=$rc, out=$out)"
fi
# non-object settings.json: dry-run matches live (SKIP), scaffold completes
echo '[]' > "$SETTINGS"
rm -f "$T/AGENTS.md"
out_dry="$(ir --dry-run 2>&1)"
rc=0; out_live="$(ir 2>&1)" || rc=$?
if ! grep -q "WRITE.*settings.json" <<<"$out_dry" \
   && grep -q "not a JSON object" <<<"$out_live" \
   && [[ $rc -eq 0 && -f "$T/AGENTS.md" ]]; then
  ok "non-object settings.json: SKIP in both modes, scaffold completes"
else
  fail "non-object settings.json: SKIP in both modes, scaffold completes (rc=$rc)"
fi
rc=0; ir --check >/dev/null 2>&1 || rc=$?
[[ $rc -eq 1 ]] && ok "--check flags non-object settings.json" || fail "--check flags non-object settings.json (rc=$rc)"
echo '{"enabledPlugins": {}}' > "$SETTINGS"

echo ""
echo "test-init-repo: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
