#!/usr/bin/env bash
# test-uninstall.sh — regression tests for issue #4 (uninstall gaps).
#
# Gap 1: a failing `rmdir` of the shared ~/.asha/cache dir inside
#         codex_uninstall died silently under `set -e`, so copilot_uninstall
#         never ran and every ~/.copilot symlink was stranded.
# Gap 2: claude_uninstall stripped settings.json hooks by "source" tag only,
#         but Claude Code drops that non-standard key on re-serialize, so
#         live (untagged) hooks were never removed.
#
# Strategy: build a sandbox HOME with all three harness mounts symlinked into
# THIS repo, a tag-stripped hooks fixture, and a non-empty ~/.asha/cache, then
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

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq not available" >&2; exit 1; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

# ---------------------------------------------------------------------------
# Fixture: fake HOME wired to this repo
# ---------------------------------------------------------------------------
build_sandbox() {
  rm -rf "$SANDBOX"/.claude "$SANDBOX"/.codex "$SANDBOX"/.copilot \
         "$SANDBOX"/.local "$SANDBOX"/.cache "$SANDBOX"/claude-styles-target
  mkdir -p "$SANDBOX/.claude/skills" "$SANDBOX/.claude/commands/session" \
           "$SANDBOX/.claude/agents/session" "$SANDBOX/.claude/hooks" \
           "$SANDBOX/.codex/skills" "$SANDBOX/.codex/agents" \
           "$SANDBOX/.copilot/skills" "$SANDBOX/.copilot/agents" \
           "$SANDBOX/.local/bin" \
           "$SANDBOX/.asha/cache/leftover-dir" \
           "$SANDBOX/claude-styles-target"

  # Previous releases installed this canary. The destination root is itself a
  # symlink to cover dotfiles-backed Claude directories during retirement.
  ln -s "$SANDBOX/claude-styles-target" "$SANDBOX/.claude/output-styles"
  ln -s "$REPO_ROOT/plugins/test/styles/debug.md" \
        "$SANDBOX/claude-styles-target/test-debug.md"

  # Symlink mounts into the real repo (targets must resolve inside the market
  # root for remove_symlinks_under to claim them).
  ln -s "$REPO_ROOT/plugins/session/skills/memory-maintenance" "$SANDBOX/.claude/skills/session-memory-maintenance"
  ln -s "$REPO_ROOT/plugins/session/commands/save.md"          "$SANDBOX/.claude/commands/session/save.md"
  ln -s "$REPO_ROOT/plugins/session/skills/memory-maintenance" "$SANDBOX/.codex/skills/session-memory-maintenance"
  ln -s "$REPO_ROOT/plugins/session/skills/memory-maintenance" "$SANDBOX/.copilot/skills/session-memory-maintenance"
  ln -s "$REPO_ROOT/plugins/write/skills/book-export"          "$SANDBOX/.copilot/skills/write-book-export"
  ln -s "$REPO_ROOT/bin/asha"                                  "$SANDBOX/.local/bin/asha"

  # A foreign symlink that must survive every sweep.
  ln -s /usr/bin/env "$SANDBOX/.copilot/skills/foreign-tool"

  # Gap 1 trigger: shared cache dir that stays non-empty after codex removes
  # its own files — the unguarded rmdir here is what killed the old code.
  touch "$SANDBOX/.asha/cache/instructions.md" \
        "$SANDBOX/.asha/cache/instructions-codex.md" \
        "$SANDBOX/.asha/cache/leftover-dir/keep.txt"

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
[[ -L "$SANDBOX/claude-styles-target/test-debug.md" ]] \
  && ok "dry-run preserves the retired output-style link" \
  || fail "dry-run preserves the retired output-style link"
assert_eq "dry-run leaves settings.json hooks in place" "4" "$(asha_hooks_left)"

# ---------------------------------------------------------------------------
# Test 2: live uninstall --target all completes past a non-empty cache dir
# (gap 1) and sweeps every harness including copilot
# ---------------------------------------------------------------------------
echo "--- test 2: live uninstall survives non-empty ~/.asha/cache and sweeps all harnesses ---"
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
[[ ! -e "$SANDBOX/claude-styles-target/test-debug.md" \
   && ! -L "$SANDBOX/claude-styles-target/test-debug.md" ]] \
  && ok "legacy output-style link is retired through a symlinked root" \
  || fail "legacy output-style link is retired through a symlinked root"
[[ -L "$SANDBOX/.copilot/skills/foreign-tool" ]] \
  && ok "foreign symlink preserved" \
  || fail "foreign symlink preserved"
[[ -f "$SANDBOX/.asha/cache/leftover-dir/keep.txt" ]] \
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

# ---------------------------------------------------------------------------
# Test 9: pre-manifest generated files must never be silently stranded.
# ---------------------------------------------------------------------------
echo "--- test 9: legacy generated artifacts require explicit adoption ---"
build_sandbox
mkdir -p "$SANDBOX/.codex/skills/session-save"
printf '%s\n' '## Codex harness adapter' > "$SANDBOX/.codex/skills/session-save/SKILL.md"
rc=0
out="$(env -i HOME="$SANDBOX" PATH="$PATH" USER="${USER:-test}" \
  bash "$REPO_ROOT/uninstall.sh" --target codex 2>&1)" || rc=$?
if [[ $rc -ne 0 && "$out" == *"pre-manifest Codex artifacts detected"* ]]; then
  ok "legacy Codex uninstall fails loudly with migration instruction"
else
  fail "legacy Codex uninstall fails loudly with migration instruction (rc=$rc)"
fi
[[ -f "$SANDBOX/.codex/skills/session-save/SKILL.md" ]] \
  && ok "legacy generated file preserved until explicit adoption" \
  || fail "legacy generated file preserved until explicit adoption"

echo ""
# U8 parent outcomes: real uninstall engines never sweep foreign bin links or
# remove a failed adapter's shim simply because --target all was requested.
if python3 - "$REPO_ROOT" "$SANDBOX" <<'PY_U8_UNINSTALL'
import json, os, pathlib, site, subprocess, sys, tempfile, unittest
ROOT, WORK = map(pathlib.Path, sys.argv[1:])
ENV = {'PATH':os.environ['PATH'], 'USER':os.environ.get('USER', 'test'),
       'PYTHONPATH':site.getusersitepackages()}

def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.lstat().st_uid,
            p.lstat().st_gid, os.readlink(p) if p.is_symlink() else
            None if p.is_dir() else p.read_bytes()) for p in root.rglob('*')}

class UninstallLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        self.bin = self.home/'.local/bin'
        self.bin.mkdir(parents=True)
        (self.bin/'asha').symlink_to(ROOT/'bin/asha')
        for h in ('claude','codex','copilot','opencode'):
            (self.bin/('asha-'+h)).symlink_to('asha')
        (self.home/'.codex/skills').mkdir(parents=True)
        (self.home/'.codex/skills/owned').symlink_to(ROOT/'plugins/test/skills/ping')
        self.config = self.home/'.codex/config.toml'
        self.config.write_bytes(b'features.hooks = false\n[mcp_servers.playwright]\ncommand="foreign"\n')
        self.config.chmod(0o640)
        (self.home/'.claude').mkdir()
        (self.home/'.claude/settings.json').write_bytes(b'{}\n')

    def run_uninstall(self, *args):
        return subprocess.run([str(ROOT/'uninstall.sh'), *args], cwd=ROOT,
            env=dict(ENV, HOME=str(self.home)), capture_output=True, timeout=120)

    def test_mixed_failure_keeps_codex_dispatcher_and_foreign(self):
        self.config.write_bytes(b'bad = [')
        (self.bin/'foreign').symlink_to(ROOT/'plugins/test/skills/ping')
        (self.bin/'asha-unknown').write_bytes(b'unknown consumer\n')
        before = snapshot(self.home/'.codex')
        p = self.run_uninstall('--target','all')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertEqual(snapshot(self.home/'.codex'), before)
        for name in ('asha','asha-codex','foreign'):
            self.assertTrue((self.bin/name).is_symlink(), name)
        self.assertEqual((self.bin/'asha-unknown').read_bytes(), b'unknown consumer\n')
        for h in ('claude','copilot','opencode'):
            self.assertFalse((self.bin/('asha-'+h)).is_symlink(), h)
        state = snapshot(self.home)
        p = self.run_uninstall('--target','all')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertEqual(snapshot(self.home), state)

    def test_successful_all_removes_only_owned_and_preserves_config(self):
        before = self.config.read_bytes(), self.config.stat().st_mode
        p = self.run_uninstall('--target','all')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mode), before)
        self.assertEqual(list(self.bin.iterdir()), [])
        p = self.run_uninstall('--target','all')
        self.assertEqual(p.returncode, 0, p.stderr.decode())

    def test_foreign_stale_and_broken_routing_never_claimed(self):
        for target in ('/usr/bin/env', str(self.home/'stale/bin/asha'), 'missing-dispatcher'):
            with self.subTest(target=target):
                dispatcher = self.bin/'asha'
                dispatcher.unlink()
                dispatcher.symlink_to(target)
                before = snapshot(self.bin)
                p = self.run_uninstall('--target','all')
                self.assertEqual(p.returncode, 0, p.stderr.decode())
                self.assertEqual(snapshot(self.bin), before)

    def test_unknown_and_foreign_consumers_retain_owned_dispatcher(self):
        foreign = self.bin/'asha-codex'
        foreign.unlink()
        foreign.symlink_to('/usr/bin/env')
        (self.bin/'unknown-name').symlink_to('asha')
        p = self.run_uninstall('--target','all')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(os.readlink(foreign), '/usr/bin/env')
        self.assertEqual(os.readlink(self.bin/'asha'), str(ROOT/'bin/asha'))
        self.assertEqual(os.readlink(self.bin/'unknown-name'), 'asha')

    def test_dry_run_all_failed_and_sourced_repeat(self):
        before = snapshot(self.home)
        p = self.run_uninstall('--target','all','--dry-run')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)
        self.config.write_bytes(b'bad = [')
        before = snapshot(self.home)
        p = self.run_uninstall('--target','codex')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)
        p = subprocess.run(['bash','-c',
            'set -uo pipefail; source "$1/lib/uninstall.sh"; '
            'asha_uninstall_main --target codex; [[ $? == 1 ]] || exit 90; '
            'asha_uninstall_main --target claude', 'u8', str(ROOT)], cwd=ROOT,
            env=dict(ENV, HOME=str(self.home)), capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertTrue((self.bin/'asha-codex').is_symlink())
        self.assertFalse((self.bin/'asha-claude').is_symlink())

    def test_hidden_survivor_alone_retains_dispatcher_after_all_uninstall(self):
        for name in ('.custom-wrapper', '..custom-wrapper'):
            with self.subTest(name=name):
                alias = self.bin/name
                alias.symlink_to('asha')
                before = snapshot(self.home)
                p = self.run_uninstall('--target','all','--dry-run')
                self.assertEqual(p.returncode, 0, p.stderr.decode())
                self.assertEqual(snapshot(self.home), before)
                p = self.run_uninstall('--target','all')
                self.assertEqual(p.returncode, 0, p.stderr.decode())
                self.assertEqual(snapshot(self.bin), {key: before['.local/bin/'+key]
                    for key in ('asha', name)})
                self.assertEqual(self.config.read_bytes(), before['.codex/config.toml'][-1])
                self.assertEqual(self.config.stat().st_mode, before['.codex/config.toml'][0])
                p = subprocess.run(['bash','-c',
                    'set -euo pipefail; for mode in -u -s; do shopt "$mode" dotglob; '
                    'before=$(shopt -p); source "$1/lib/uninstall.sh"; '
                    'asha_uninstall_main --target all; '
                    '[[ "$(shopt -p)" == "$before" ]] || exit 90; done',
                    'u8', str(ROOT)], cwd=ROOT, env=dict(ENV, HOME=str(self.home)),
                    capture_output=True, timeout=120)
                self.assertEqual(p.returncode, 0, p.stderr.decode())
                self.assertEqual(snapshot(self.bin), {key: before['.local/bin/'+key]
                    for key in ('asha', name)})
                alias.unlink()

unittest.main(argv=['u8-uninstall'], verbosity=2)
PY_U8_UNINSTALL
then ok "U8 uninstall successful-selected ownership and protected routing"
else fail "U8 uninstall successful-selected ownership and protected routing"
fi

echo "test-uninstall: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
