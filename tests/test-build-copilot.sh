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
for p in asha-code asha-devops asha-security asha-prompt; do
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
while read -r path; do
  [[ -d "$OUT/$path" ]] || fail "marketplace path missing on disk: $path"
done < <(jq -r '.plugins[].path' "$OUT/marketplace.json")
ok "all marketplace paths exist on disk"
sn="$(jq -r '.enabledPlugins | sort | join(" ")' "$OUT/settings-snippet.json")"
assert_eq "settings snippet lists the built set" "$(echo "$dir_names" | xargs)" "$sn"

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
if build --force --only output-styles >/dev/null 2>&1; then
  fail "--only output-styles is refused (Claude-only)"
else
  ok "--only output-styles is refused (Claude-only)"
fi
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

echo ""
echo "test-build-copilot: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
