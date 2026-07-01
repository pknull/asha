#!/usr/bin/env bash
# test-uninstall.sh — regression tests for issue #4 (uninstall gaps).
#
# Gap 1: a failing `rmdir` of the shared ~/.cache/asha dir inside
#         codex_uninstall died silently under `set -e`, so copilot_uninstall
#         never ran and every ~/.copilot symlink was stranded.
# Gap 2: claude_uninstall stripped settings.json hooks by "source" tag only,
#         but Claude Code drops that non-standard key on re-serialize, so
#         live (untagged) hooks were never removed.
#
# Strategy: build a sandbox HOME with all three harness mounts symlinked into
# THIS repo, a tag-stripped hooks fixture, and a non-empty ~/.cache/asha, then
# run the real uninstall engine with HOME=<sandbox>. The real user HOME is
# never touched.
set -euo pipefail

# Physical paths (cd -P): the engine canonicalizes MARKET_ROOT via readlink,
# so fixture paths built from a logical (symlinked) pwd would never match.
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PASS=0
FAIL=0

ok()   { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }

assert_eq() { # desc expected actual
  if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 (expected: $2, got: $3)"; fi
}

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq not available" >&2; exit 0; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

# ---------------------------------------------------------------------------
# Fixture: fake HOME wired to this repo
# ---------------------------------------------------------------------------
build_sandbox() {
  rm -rf "$SANDBOX"/.claude "$SANDBOX"/.codex "$SANDBOX"/.copilot \
         "$SANDBOX"/.local "$SANDBOX"/.cache
  mkdir -p "$SANDBOX/.claude/skills" "$SANDBOX/.claude/commands/session" \
           "$SANDBOX/.claude/agents/session" "$SANDBOX/.claude/output-styles" \
           "$SANDBOX/.claude/hooks" \
           "$SANDBOX/.codex/skills" "$SANDBOX/.codex/agents" \
           "$SANDBOX/.copilot/skills" "$SANDBOX/.copilot/agents" \
           "$SANDBOX/.local/bin" \
           "$SANDBOX/.cache/asha/leftover-dir"

  # Symlink mounts into the real repo (targets must resolve inside the market
  # root for remove_symlinks_under to claim them).
  ln -s "$REPO_ROOT/plugins/session/skills/memory-maintenance" "$SANDBOX/.claude/skills/session-memory-maintenance"
  ln -s "$REPO_ROOT/plugins/session/commands/save.md"          "$SANDBOX/.claude/commands/session/save.md"
  ln -s "$REPO_ROOT/plugins/session/skills/memory-maintenance" "$SANDBOX/.codex/skills/memory-maintenance"
  ln -s "$REPO_ROOT/plugins/session/skills/memory-maintenance" "$SANDBOX/.copilot/skills/memory-maintenance"
  ln -s "$REPO_ROOT/plugins/write/skills/book-maker"           "$SANDBOX/.copilot/skills/book-maker"
  ln -s "$REPO_ROOT/bin/asha"                                  "$SANDBOX/.local/bin/asha"

  # A foreign symlink that must survive every sweep.
  ln -s /usr/bin/env "$SANDBOX/.copilot/skills/foreign-tool"

  # Gap 1 trigger: shared cache dir that stays non-empty after codex removes
  # its own files — the unguarded rmdir here is what killed the old code.
  touch "$SANDBOX/.cache/asha/instructions.md" \
        "$SANDBOX/.cache/asha/instructions-codex.md" \
        "$SANDBOX/.cache/asha/leftover-dir/keep.txt"

  # Minimal codex config (no asha fence — excise path idles).
  printf 'model = "gpt-5"\n' > "$SANDBOX/.codex/config.toml"

  # Gap 2 fixture: settings.json as Claude Code re-serializes it — asha hooks
  # UNTAGGED (source key stripped), identified only by command path-prefix.
  # Includes a tagged legacy entry, a foreign hook, and a mixed group.
  jq -n --arg repo "$REPO_ROOT" '{
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    hooks: {
      PostToolUse: [
        { matcher: "*",
          hooks: [ { type: "command", command: ($repo + "/plugins/session/hooks/session-watch.sh") } ] },
        { matcher: "*",
          hooks: [ { type: "command", command: "/home/user/.claude/hooks/console-log-check.sh" } ] }
      ],
      SessionEnd: [
        { hooks: [ { type: "command", command: ($repo + "/plugins/session/hooks/session-end.sh") } ] }
      ],
      Stop: [
        { hooks: [
            { type: "command", command: ($repo + "/plugins/session/hooks/stop-audit.sh") },
            { type: "command", command: "/home/user/.claude/hooks/console-log-audit.sh" }
        ] }
      ],
      UserPromptSubmit: [
        { hooks: [ { type: "command", command: "/somewhere/else/entirely.sh", source: "asha:session" } ] }
      ]
    }
  }' > "$SANDBOX/.claude/settings.json"
}

run_uninstall() { # extra args forwarded
  env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
    bash "$REPO_ROOT/uninstall.sh" --target all "$@"
}

repo_links() { # count symlinks in sandbox still resolving into the repo
  # tr strips BSD/macOS wc's left-padding so string-equality asserts hold.
  find "$SANDBOX/.claude" "$SANDBOX/.codex" "$SANDBOX/.copilot" "$SANDBOX/.local" \
    -type l -lname "$REPO_ROOT*" 2>/dev/null | wc -l | tr -d '[:space:]'
}

asha_hooks_left() { # path-prefix OR tag, same predicate as the fix
  jq -r --arg prefix "$REPO_ROOT/plugins/" '
    [.hooks // {} | .[] | .[]? | .hooks[]?
     | select(((.command // "") | startswith($prefix))
              or ((.source // "") | test("^(asha|marketplace):")))] | length
  ' "$SANDBOX/.claude/settings.json"
}

# ---------------------------------------------------------------------------
# Test 1: dry-run must not mutate anything
# ---------------------------------------------------------------------------
echo "--- test 1: dry-run is read-only ---"
build_sandbox
before_links="$(repo_links)"
if run_uninstall --dry-run >/dev/null 2>&1; then
  ok "dry-run exits 0"
else
  fail "dry-run exits 0 (got $?)"
fi
assert_eq "dry-run leaves symlinks in place" "$before_links" "$(repo_links)"
assert_eq "dry-run leaves settings.json hooks in place" "4" "$(asha_hooks_left)"

# ---------------------------------------------------------------------------
# Test 2: live uninstall --target all completes past a non-empty cache dir
# (gap 1) and sweeps every harness including copilot
# ---------------------------------------------------------------------------
echo "--- test 2: live uninstall survives non-empty ~/.cache/asha and sweeps all harnesses ---"
build_sandbox
if out="$(run_uninstall 2>&1)"; then
  ok "uninstall --target all exits 0"
else
  fail "uninstall --target all exits 0 (got $?; output: $(tail -3 <<<"$out"))"
fi
grep -q "total symlinks removed" <<<"$out" \
  && ok "run reached the final summary (did not die mid-chain)" \
  || fail "run reached the final summary (did not die mid-chain)"
assert_eq "all repo-pointing symlinks removed (incl. copilot)" "0" "$(repo_links)"
[[ -L "$SANDBOX/.copilot/skills/foreign-tool" ]] \
  && ok "foreign symlink preserved" \
  || fail "foreign symlink preserved"
[[ -f "$SANDBOX/.cache/asha/leftover-dir/keep.txt" ]] \
  && ok "unrelated cache content preserved" \
  || fail "unrelated cache content preserved"

# ---------------------------------------------------------------------------
# Test 3 (gap 2): untagged, path-prefixed hooks are stripped; foreign kept
# ---------------------------------------------------------------------------
echo "--- test 3: hook strip matches path-prefix OR tag ---"
assert_eq "asha hooks removed (untagged prefix + tagged legacy)" "0" "$(asha_hooks_left)"
foreign_count="$(jq -r '[.hooks // {} | .[] | .[]? | .hooks[]?
  | select((.command // "") | startswith("/home/user/.claude/hooks/"))] | length' \
  "$SANDBOX/.claude/settings.json")"
assert_eq "foreign hooks preserved (incl. survivor of mixed group)" "2" "$foreign_count"
empty_events="$(jq -r '[.hooks // {} | .[] | select(length == 0)] | length' "$SANDBOX/.claude/settings.json")"
assert_eq "no empty hook events left behind" "0" "$empty_events"
jq empty "$SANDBOX/.claude/settings.json" 2>/dev/null \
  && ok "settings.json still valid JSON" \
  || fail "settings.json still valid JSON"

# ---------------------------------------------------------------------------
# Test 4: idempotency — second run is a clean no-op
# ---------------------------------------------------------------------------
echo "--- test 4: re-run is a clean no-op ---"
if run_uninstall >/dev/null 2>&1; then
  ok "second uninstall exits 0"
else
  fail "second uninstall exits 0 (got $?)"
fi

# ---------------------------------------------------------------------------
# Test 5: missing settings.json is benign — symlinks still swept, no failure
# (codex/copilot-only machines; die() here used to strand everything after
# claude under --target all)
# ---------------------------------------------------------------------------
echo "--- test 5: missing settings.json sweeps symlinks, exits 0 ---"
build_sandbox
rm -f "$SANDBOX/.claude/settings.json"
if run_uninstall >/dev/null 2>&1; then
  ok "uninstall without settings.json exits 0"
else
  fail "uninstall without settings.json exits 0 (got $?)"
fi
assert_eq "symlinks swept without settings.json" "0" "$(repo_links)"

# ---------------------------------------------------------------------------
# Test 6: corrupt settings.json fails the claude harness LOUDLY but does not
# strand codex/copilot — per-harness isolation, non-zero overall exit
# ---------------------------------------------------------------------------
echo "--- test 6: corrupt settings.json fails claude, still sweeps codex+copilot ---"
build_sandbox
echo '{ this is not json' > "$SANDBOX/.claude/settings.json"
rc=0
out="$(run_uninstall 2>&1)" || rc=$?
if [[ $rc -ne 0 ]]; then
  ok "corrupt settings.json yields non-zero exit ($rc)"
else
  fail "corrupt settings.json yields non-zero exit (got 0 — failure masked)"
fi
grep -q "uninstall incomplete for: claude" <<<"$out" \
  && ok "failure attributed to claude harness in summary" \
  || fail "failure attributed to claude harness in summary"
codex_copilot_left="$(find "$SANDBOX/.codex" "$SANDBOX/.copilot" -type l -lname "$REPO_ROOT*" 2>/dev/null | wc -l | tr -d '[:space:]')"
assert_eq "codex+copilot swept despite claude failure" "0" "$codex_copilot_left"

# ---------------------------------------------------------------------------
# Test 7: matcher-only hook group (no `hooks` key) does not error the strip
# filter; asha hooks still removed
# ---------------------------------------------------------------------------
echo "--- test 7: matcher-only hook group tolerated ---"
build_sandbox
jq --arg repo "$REPO_ROOT" '.hooks.PreToolUse = [ { matcher: "Edit" } ]' \
  "$SANDBOX/.claude/settings.json" > "$SANDBOX/.claude/settings.json.new"
mv "$SANDBOX/.claude/settings.json.new" "$SANDBOX/.claude/settings.json"
if run_uninstall >/dev/null 2>&1; then
  ok "uninstall with matcher-only group exits 0"
else
  fail "uninstall with matcher-only group exits 0 (got $?)"
fi
assert_eq "asha hooks removed despite matcher-only group" "0" "$(asha_hooks_left)"

# ---------------------------------------------------------------------------
# Test 8: unwritable TMPDIR must not fail a successful uninstall — the count
# handoff is cosmetic, the exit status is load-bearing
# ---------------------------------------------------------------------------
echo "--- test 8: broken TMPDIR does not fake a failure ---"
build_sandbox
if env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" TMPDIR=/nonexistent \
     bash "$REPO_ROOT/uninstall.sh" --target all >/dev/null 2>&1; then
  ok "uninstall with unwritable TMPDIR exits 0"
else
  fail "uninstall with unwritable TMPDIR exits 0 (got $?)"
fi
assert_eq "symlinks swept despite broken TMPDIR" "0" "$(repo_links)"

echo ""
echo "test-uninstall: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
