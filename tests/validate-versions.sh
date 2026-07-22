#!/usr/bin/env bash
# validate-versions.sh
#
# Validates version consistency between README.md, CLAUDE.md, and the
# per-plugin README.md files humans read for authoritative repo state.
#
# The legacy 4-way cross-check between marketplace.json + per-plugin
# plugin.json + README + CLAUDE was retired with the symlink-mount
# installer migration: marketplace.json and plugin.json no longer exist.
# Per-plugin versions are cross-checked against both top-level plugin tables.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASSED=0
FAILED=0

echo "=== Version Consistency Validator ==="
echo "Repository: $REPO_ROOT"
echo ""

# Extract top-level versions from README and CLAUDE.md.
# Both files are expected to declare `**Version**: X.Y.Z` near the top.
README_VERSION=$(grep -m1 -oP '^\*\*Version\*\*:\s*\K[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/README.md" || true)
CLAUDE_MD_VERSION=$(grep -m1 -oP '^\*\*Version\*\*:\s*\K[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/CLAUDE.md" || true)

# Test 1: README version is present and well-formed.
echo -n "Test 1: README.md has top-level **Version** in semver form... "
if [[ -n "$README_VERSION" ]]; then
    echo -e "${GREEN}PASS${NC} (v$README_VERSION)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  README.md missing or malformed top-level **Version** line"
    FAILED=$((FAILED + 1))
fi

# Test 2: CLAUDE.md version is present and well-formed.
echo -n "Test 2: CLAUDE.md has top-level **Version** in semver form... "
if [[ -n "$CLAUDE_MD_VERSION" ]]; then
    echo -e "${GREEN}PASS${NC} (v$CLAUDE_MD_VERSION)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  CLAUDE.md missing or malformed top-level **Version** line"
    FAILED=$((FAILED + 1))
fi

# Note on version coupling: README.md and CLAUDE.md track different
# things (README = repo/marketplace face, CLAUDE.md = AI-assistant guide
# revision). Drift between them is not necessarily a defect — both
# evolve on independent cadences. We surface both numbers for review
# but do not enforce equality. Per-plugin versions (Panel 5.0.0,
# Asha 1.18.0, etc.) are documented in README's plugin sections and
# tracked there as the source of truth.

readme_table_version() {
    local namespace="$1"
    awk -F'|' -v expected="\`$namespace\`" '
        function trim(s) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", s); return s }
        NF >= 4 && trim($3) == expected { print trim($4); exit }
    ' "$REPO_ROOT/README.md"
}

claude_table_version() {
    local namespace="$1"
    awk -F'|' -v expected="$namespace" '
        function trim(s) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", s); return s }
        /^### Current Plugins[[:space:]]*$/ { in_table=1; next }
        in_table && /^###[[:space:]]/ { exit }
        in_table && NF >= 4 {
            name=trim($2)
            gsub(/\*\*/, "", name)
            name=tolower(name)
            gsub(/[[:space:]]+/, "-", name)
            if (name == expected) { print trim($3); exit }
        }
    ' "$REPO_ROOT/CLAUDE.md"
}

PLUGIN_COUNT=0
CLAUDE_PLUGIN_COUNT=0
README_TABLE_MISMATCHES=0
CLAUDE_TABLE_MISMATCHES=0

echo -n "Test 3: Per-plugin versions match README.md plugin table... "
for plugin_readme in "$REPO_ROOT"/plugins/*/README.md; do
    plugin_version=$(grep -m1 -oP '^\*\*Version\*\*:\s*\K[0-9]+\.[0-9]+\.[0-9]+' "$plugin_readme" || true)
    [[ -n "$plugin_version" ]] || continue

    plugin_dir="$(basename "$(dirname "$plugin_readme")")"
    namespace=$(jq -r --arg plugin "$plugin_dir" '.[$plugin] // $plugin' "$REPO_ROOT/namespaces.json")
    table_cell=$(readme_table_version "$namespace")
    [[ "$table_cell" == "—" ]] && continue
    table_version="${table_cell#v}"
    PLUGIN_COUNT=$((PLUGIN_COUNT + 1))

    if [[ "$table_version" != "$plugin_version" ]]; then
        if [[ $README_TABLE_MISMATCHES -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_dir (namespace \`$namespace\`): plugins/$plugin_dir/README.md is $plugin_version; README.md table is ${table_cell:-<missing>}"
        README_TABLE_MISMATCHES=$((README_TABLE_MISMATCHES + 1))
    fi
done
if [[ $README_TABLE_MISMATCHES -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC} ($PLUGIN_COUNT plugins)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

echo -n "Test 4: Per-plugin versions match CLAUDE.md Current Plugins table... "
for plugin_readme in "$REPO_ROOT"/plugins/*/README.md; do
    plugin_version=$(grep -m1 -oP '^\*\*Version\*\*:\s*\K[0-9]+\.[0-9]+\.[0-9]+' "$plugin_readme" || true)
    [[ -n "$plugin_version" ]] || continue

    plugin_dir="$(basename "$(dirname "$plugin_readme")")"
    namespace=$(jq -r --arg plugin "$plugin_dir" '.[$plugin] // $plugin' "$REPO_ROOT/namespaces.json")
    table_cell=$(claude_table_version "$namespace")
    [[ "$table_cell" == "—" ]] && continue
    table_version="${table_cell#v}"
    CLAUDE_PLUGIN_COUNT=$((CLAUDE_PLUGIN_COUNT + 1))

    if [[ "$table_version" != "$plugin_version" ]]; then
        if [[ $CLAUDE_TABLE_MISMATCHES -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_dir (namespace \`$namespace\`): plugins/$plugin_dir/README.md is $plugin_version; CLAUDE.md table is ${table_cell:-<missing>}"
        CLAUDE_TABLE_MISMATCHES=$((CLAUDE_TABLE_MISMATCHES + 1))
    fi
done
if [[ $CLAUDE_TABLE_MISMATCHES -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC} ($CLAUDE_PLUGIN_COUNT plugins)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

echo ""
echo "=== Test Summary ==="
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
echo ""

if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}✓ All version checks passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Version inconsistencies detected${NC}"
    echo "Fix: Synchronize the reported version source and its top-level plugin table row."
    exit 1
fi
