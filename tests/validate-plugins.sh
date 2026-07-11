#!/usr/bin/env bash
# validate-plugins.sh
#
# Validates plugin structure under the symlink-mount installer model.
# The legacy marketplace.json + plugin.json metadata layer has been retired
# (see top-of-CLAUDE.md migration notice); this suite tests what remains:
# directory layout, command frontmatter, and required artifacts per plugin.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASSED=0
FAILED=0

echo "=== Plugin Structure Validator ==="
echo "Repository: $REPO_ROOT"
echo ""

# Test 1: Every plugin directory has at least one primitive.
# A plugin with no commands, agents, skills, hooks, or modules has nothing
# to install — it's dead weight. Catch this before it ships.
echo -n "Test 1: Every plugin has at least one primitive... "
EMPTY_PLUGINS=()
for plugin_dir in "$REPO_ROOT"/plugins/*/; do
    plugin_name=$(basename "$plugin_dir")
    has_primitive=false
    for primitive in commands agents skills hooks modules templates rules; do
        if [[ -d "$plugin_dir$primitive" ]] && [[ -n "$(ls -A "$plugin_dir$primitive" 2>/dev/null)" ]]; then
            has_primitive=true
            break
        fi
    done
    if [[ "$has_primitive" == false ]]; then
        EMPTY_PLUGINS+=("$plugin_name")
    fi
done
if [[ ${#EMPTY_PLUGINS[@]} -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    for p in "${EMPTY_PLUGINS[@]}"; do
        echo "  $p has no commands/agents/skills/hooks/modules/templates/rules"
    done
    FAILED=$((FAILED + 1))
fi

# Test 2: Every command markdown file has a name + description in frontmatter.
# Without these, the slash command can't render in /help and Claude can't
# decide when to invoke it.
echo -n "Test 2: Command files have name + description frontmatter... "
MISSING_META=()
for cmd_file in "$REPO_ROOT"/plugins/*/commands/*.md; do
    [[ ! -f "$cmd_file" ]] && continue
    plugin_name=$(basename "$(dirname "$(dirname "$cmd_file")")")
    cmd_name=$(basename "$cmd_file")

    head -20 "$cmd_file" | grep -q "^name:" || MISSING_META+=("$plugin_name/$cmd_name (no name:)")
    head -20 "$cmd_file" | grep -q "^description:" || MISSING_META+=("$plugin_name/$cmd_name (no description:)")
done
if [[ ${#MISSING_META[@]} -eq 0 ]]; then
    CMD_COUNT=$( { ls "$REPO_ROOT"/plugins/*/commands/*.md 2>/dev/null || true; } | wc -l)
    echo -e "${GREEN}PASS${NC} ($CMD_COUNT commands)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    for m in "${MISSING_META[@]}"; do echo "  $m"; done
    FAILED=$((FAILED + 1))
fi

# Test 2b: Every SKILL.md has parseable YAML frontmatter with name + description.
echo -n "Test 2b: Skill files have valid name + description frontmatter... "
SKILL_ERRORS=()
while IFS= read -r skill_file; do
    [[ -f "$skill_file" ]] || continue
    if ! python3 - "$skill_file" <<'PY' 2>/tmp/asha-validate-skill.err; then
import sys, yaml
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
if not text.startswith("---\n"):
    raise SystemExit("missing opening frontmatter")
end = text.find("\n---\n", 4)
if end == -1:
    raise SystemExit("missing closing frontmatter")
data = yaml.safe_load(text[4:end]) or {}
if not isinstance(data, dict):
    raise SystemExit("frontmatter is not a mapping")
for key in ("name", "description"):
    if not data.get(key):
        raise SystemExit(f"missing {key}")
PY
        SKILL_ERRORS+=("$skill_file ($(cat /tmp/asha-validate-skill.err))")
    fi
done < <(find "$REPO_ROOT/plugins" -path '*/skills/*/SKILL.md' -type f | sort)
rm -f /tmp/asha-validate-skill.err
if [[ ${#SKILL_ERRORS[@]} -eq 0 ]]; then
    SKILL_COUNT=$(find "$REPO_ROOT/plugins" -path '*/skills/*/SKILL.md' -type f | wc -l)
    echo -e "${GREEN}PASS${NC} ($SKILL_COUNT skills)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    for m in "${SKILL_ERRORS[@]}"; do echo "  $m"; done
    FAILED=$((FAILED + 1))
fi

# Test 3: Every plugin has README.md and LICENSE.
echo -n "Test 3: Every plugin has README.md and LICENSE... "
MISSING_DOCS=()
for plugin_dir in "$REPO_ROOT"/plugins/*/; do
    plugin_name=$(basename "$plugin_dir")
    [[ -f "$plugin_dir/README.md" ]] || MISSING_DOCS+=("$plugin_name (README.md)")
    [[ -f "$plugin_dir/LICENSE" ]] || MISSING_DOCS+=("$plugin_name (LICENSE)")
done
if [[ ${#MISSING_DOCS[@]} -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    for m in "${MISSING_DOCS[@]}"; do echo "  $m"; done
    FAILED=$((FAILED + 1))
fi

# List registered slash commands (sourced from on-disk file structure).
echo ""
echo "=== Registered Commands ==="
for plugin_dir in "$REPO_ROOT"/plugins/*/; do
    plugin_name=$(basename "$plugin_dir")
    cmd_dir="$plugin_dir/commands"
    if [[ -d "$cmd_dir" ]] && [[ -n "$(ls "$cmd_dir"/*.md 2>/dev/null)" ]]; then
        echo ""
        echo "Plugin: $plugin_name"
        for cmd_file in "$cmd_dir"/*.md; do
            cmd_name=$(basename "$cmd_file" .md)
            # Extract frontmatter name if present, else use filename
            display_name=$(head -20 "$cmd_file" | grep "^name:" | head -1 | sed 's/^name: *//; s/"//g' || echo "$cmd_name")
            echo "  /$plugin_name:$cmd_name (display name: $display_name)"
        done
    fi
done

echo ""
echo "=== Test Summary ==="
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
echo ""

if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Some tests failed${NC}"
    exit 1
fi
