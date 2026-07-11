#!/usr/bin/env bash
# test-build-copilot.sh — regression tests for `asha build copilot` (issue #3).
#
# Validates the generated Copilot plugin dist tree: manifest validity, the
# load-bearing hooks exclusion (Claude-schema mismatch + copilot-cli#2540),
# frontmatter conversion, path-rewrite integrity, marketplace consistency,
# and rebuild idempotence. Builds into a temp dir; the repo's dist/ and the
# user's HOME are never touched.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PASS=0
FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }
assert_eq() { # desc expected actual
  if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 (expected: $2, got: $3)"; fi
}

command -v jq      >/dev/null 2>&1 || { echo "SKIP: jq not available" >&2; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not available" >&2; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
OUT="$WORK/dist"

build() { bash "$REPO_ROOT/bin/asha" build copilot --out "$OUT" "$@"; }

# ---------------------------------------------------------------------------
echo "--- test 1: default build succeeds and emits the expected top level ---"
if build >/dev/null 2>&1; then
  ok "default build exits 0"
else
  fail "default build exits 0 (got $?)"
fi
for f in marketplace.json settings-snippet.json README.md; do
  [[ -f "$OUT/$f" ]] && ok "emits $f" || fail "emits $f"
done
for p in asha-code asha-security asha-session asha-write; do
  [[ -f "$OUT/plugins/$p/plugin.json" ]] && ok "emits plugins/$p/plugin.json" || fail "emits plugins/$p/plugin.json"
done

# ---------------------------------------------------------------------------
echo "--- test 2: manifests are valid and versions match source READMEs ---"
for p in "$OUT"/plugins/*/plugin.json; do
  jq -e '.name and .version and .description and (.name|startswith("asha-"))' "$p" >/dev/null \
    && ok "$(basename "$(dirname "$p")")/plugin.json valid" \
    || fail "$(basename "$(dirname "$p")")/plugin.json valid"
done
src_ver="$(grep -m1 -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' <(grep -m1 '\*\*Version\*\*' "$REPO_ROOT/plugins/code/README.md"))"
assert_eq "asha-code version matches plugins/code/README.md" \
  "$src_ver" "$(jq -r .version "$OUT/plugins/asha-code/plugin.json")"

# ---------------------------------------------------------------------------
echo "--- test 3: hooks are excluded everywhere (copilot-cli#2540 + schema) ---"
hooks_found="$(find "$OUT" \( -name 'hooks' -o -name 'hooks.json' \) | wc -l | tr -d '[:space:]' || true)"
assert_eq "no hooks file or dir anywhere in dist" "0" "$hooks_found"
claude_manifests="$(find "$OUT" -name '.claude-plugin' | wc -l | tr -d '[:space:]' || true)"
assert_eq "no .claude-plugin manifests in dist" "0" "$claude_manifests"

# ---------------------------------------------------------------------------
echo "--- test 4: frontmatter conversion ---"
bad_keys="$(grep -rlE '^(argument-hint|allowed-tools):' "$OUT"/plugins/*/skills/*/SKILL.md 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
assert_eq "no Claude-only keys in generated SKILL.md files" "0" "$bad_keys"
bare_agents="$(find "$OUT"/plugins/*/agents -name '*.md' ! -name '*.agent.md' 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
assert_eq "all agents use .agent.md extension" "0" "$bare_agents"
agent_bad="$(grep -rlE '^(tools|model|memory|ownership|dispatch_priority|trigger):' "$OUT"/plugins/*/agents/*.agent.md 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
assert_eq "no Claude-vocabulary frontmatter in agents" "0" "$agent_bad"
# every agent retains a description (name may fall back to filename)
no_desc="$(grep -rLE '^description:' "$OUT"/plugins/*/agents/*.agent.md 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
assert_eq "every agent keeps description frontmatter" "0" "$no_desc"

# ---------------------------------------------------------------------------
echo "--- test 5: path rewrites resolve inside the plugin ---"
skills_residue="$(grep -rl '\$ASHA_ROOT' "$OUT"/plugins/*/skills 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
assert_eq "no \$ASHA_ROOT residue in skills" "0" "$skills_residue"
# the code-verify tool reference must resolve on disk
if grep -q 'python3 "\.\./\.\./tools/verify.py"' "$OUT/plugins/asha-code/skills/code-verify/SKILL.md" \
   && [[ -f "$OUT/plugins/asha-code/tools/verify.py" ]]; then
  ok "code-verify tool path rewritten and target exists in plugin"
else
  fail "code-verify tool path rewritten and target exists in plugin"
fi
# relative md links gained one level and resolve
if [[ -f "$OUT/plugins/asha-code/modules/complexity-routing.md" ]] \
   && grep -q '](\.\./\.\./modules/complexity-routing.md)' "$OUT/plugins/asha-code/skills/code-orchestrate/SKILL.md"; then
  ok "orchestrate module link rewritten and target exists"
else
  fail "orchestrate module link rewritten and target exists"
fi

# ---------------------------------------------------------------------------
echo "--- test 6: marketplace + snippet match the built set ---"
mk_names="$(jq -r '.plugins[].name' "$OUT/marketplace.json" | sort | tr '\n' ' ')"
dir_names="$(ls "$OUT/plugins" | sort | tr '\n' ' ')"
assert_eq "marketplace entries == built plugin dirs" "$dir_names" "$mk_names"
jq -e '.owner.name and (.plugins | all(.source and (.source|startswith("./"))))' \
  "$OUT/marketplace.json" >/dev/null \
  && ok "marketplace has owner + relative plugin sources (verified 1.0.65 schema)" \
  || fail "marketplace has owner + relative plugin sources (verified 1.0.65 schema)"
while read -r src; do
  [[ -d "$OUT/${src#./}" ]] || fail "marketplace source missing on disk: $src"
done < <(jq -r '.plugins[].source' "$OUT/marketplace.json")
ok "all marketplace sources exist on disk"
sn="$(jq -r '.enabledPlugins | keys | sort | join(" ")' "$OUT/settings-snippet.json")"
expected_keys="$(ls "$OUT/plugins" | sort | sed 's/$/@asha/' | tr '\n' ' ' | xargs)"
assert_eq "settings snippet maps plugin@marketplace keys to true" "$expected_keys" "$sn"
jq -e '.enabledPlugins | all(. == true)' "$OUT/settings-snippet.json" >/dev/null \
  && ok "settings snippet values are true (object-map form)" \
  || fail "settings snippet values are true (object-map form)"

# ---------------------------------------------------------------------------
echo "--- test 7: rebuild over --force is byte-identical ---"
cp -R "$OUT" "$WORK/first"
build --force >/dev/null 2>&1 || fail "rebuild exits 0"
if diff -r "$WORK/first" "$OUT" >/dev/null 2>&1; then
  ok "second build is byte-identical"
else
  fail "second build is byte-identical"
fi

# ---------------------------------------------------------------------------
echo "--- test 8: selection guards ---"
if build --force --only test --version 0.0.1 >/dev/null 2>&1 \
   && [[ -f "$OUT/plugins/asha-test/plugin.json" ]]; then
  ok "--only test builds the canary namespace"
else
  fail "--only test builds the canary namespace"
fi
if build --force --only nonexistent >/dev/null 2>&1; then
  fail "--only nonexistent is refused"
else
  ok "--only nonexistent is refused"
fi

# ---------------------------------------------------------------------------
echo "--- test 9: non-empty --out without --force is refused ---"
if build >/dev/null 2>&1; then
  fail "non-empty --out refused without --force"
else
  ok "non-empty --out refused without --force"
fi

# ---------------------------------------------------------------------------
echo "--- test 10: review-finding regressions ---"
# a refused build (--only test has no Version line, no --version) must leave
# the existing dist INTACT even with --force (validation precedes the wipe)
build --force >/dev/null 2>&1 || fail "test-10 baseline rebuild exits 0"
rc=0; build --force --only test >/dev/null 2>&1 || rc=$?
if [[ $rc -eq 2 && -f "$OUT/marketplace.json" && -d "$OUT/plugins/asha-code" ]]; then
  ok "refused build leaves prior dist intact (exit 2, no wipe)"
else
  fail "refused build leaves prior dist intact (rc=$rc)"
fi
# duplicate --only refused
rc=0; build --force --only code,code >/dev/null 2>&1 || rc=$?
[[ $rc -eq 2 ]] && ok "duplicate --only refused (exit 2)" || fail "duplicate --only refused (got $rc)"
# missing flag value dies loudly with exit 2, not silently
rc=0; out="$(build --force --out 2>&1)" || rc=$?
if [[ $rc -eq 2 && "$out" == *"--out requires a value"* ]]; then
  ok "missing flag value dies loudly (exit 2)"
else
  fail "missing flag value dies loudly (rc=$rc, out=$out)"
fi
# --only validation exit code propagates as 2 (not rewritten to 1)
rc=0; build --force --only nonexistent >/dev/null 2>&1 || rc=$?
[[ $rc -eq 2 ]] && ok "--only validation exits 2" || fail "--only validation exits 2 (got $rc)"
# --force preserves foreign plugins in a shared dist tree
mkdir -p "$OUT/plugins/acme-internal" && echo x > "$OUT/plugins/acme-internal/plugin.json"
build --force >/dev/null 2>&1 || fail "rebuild with foreign plugin exits 0"
[[ -f "$OUT/plugins/acme-internal/plugin.json" ]] \
  && ok "--force preserves foreign plugins/ entries" \
  || fail "--force preserves foreign plugins/ entries"
rm -rf "$OUT/plugins/acme-internal"
# agent relative md links are NOT bumped (depth unchanged for agents)
probe_agent="$(mktemp -d)/a.md"
printf -- '---\nname: probe\ndescription: d\n---\nsee [x](../modules/x.md)\n' > "$probe_agent"
( DRY_RUN=0 VERBOSE=0
  source "$REPO_ROOT/lib/build.sh"
  _copilot_emit_agent_md "$probe_agent" "${probe_agent%.md}.agent.md"
  _build_rewrite_paths "${probe_agent%.md}.agent.md" code "../"
) >/dev/null 2>&1
grep -q '](\.\./modules/x.md)' "${probe_agent%.md}.agent.md" \
  && ok "agent relative links keep their depth" \
  || fail "agent relative links keep their depth"

# ---------------------------------------------------------------------------
echo "--- test 11: non-git source tree gets no false dirty marker ---"
EXPORT="$WORK/export"
mkdir -p "$EXPORT"
cp -a "$REPO_ROOT"/. "$EXPORT"/ 2>/dev/null
rm -rf "$EXPORT/.git" "$EXPORT/dist"
if bash "$EXPORT/bin/asha" build copilot --out "$WORK/export-dist" >/dev/null 2>&1 \
   && ! grep -q "uncommitted changes" "$WORK/export-dist/README.md" \
   && grep -q "Source commit\*\*: unknown" "$WORK/export-dist/README.md"; then
  ok "tarball-export build: no false '(+ uncommitted changes)' provenance"
else
  fail "tarball-export build: no false '(+ uncommitted changes)' provenance"
fi

echo ""
echo "test-build-copilot: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
