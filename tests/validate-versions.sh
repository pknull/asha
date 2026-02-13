#!/usr/bin/env bash
# validate-versions.sh
# Validates version consistency between plugin.json files and README.md

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASSED=0
FAILED=0
WARNINGS=0

echo "=== Version Consistency Validator ==="
echo "Repository: $REPO_ROOT"
echo ""

# Test 1: Marketplace version consistency
echo -n "Test 1: Marketplace version (marketplace.json vs README.md)... "
MARKETPLACE_JSON_VERSION=$(jq -r '.metadata.version' "$REPO_ROOT/.claude-plugin/marketplace.json")
README_MARKETPLACE_VERSION=$(grep -oP '^\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/README.md" | head -1)

if [[ "$MARKETPLACE_JSON_VERSION" == "$README_MARKETPLACE_VERSION" ]]; then
    echo -e "${GREEN}PASS${NC} (v$MARKETPLACE_JSON_VERSION)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  marketplace.json: v$MARKETPLACE_JSON_VERSION"
    echo "  README.md: v$README_MARKETPLACE_VERSION"
    FAILED=$((FAILED + 1))
fi

# Test 2: Plugin version consistency
echo ""
echo "Test 2: Plugin versions (plugin.json vs README.md)..."

declare -A PLUGIN_VERSIONS
PLUGIN_VERSIONS=(
    ["panel-system"]=""
    ["code"]=""
    ["write"]=""
    ["output-styles"]=""
    ["asha"]=""
)

# Extract versions from plugin.json files
for plugin_dir in "$REPO_ROOT"/plugins/*; do
    if [[ -d "$plugin_dir" ]]; then
        plugin_json="$plugin_dir/.claude-plugin/plugin.json"
        if [[ -f "$plugin_json" ]]; then
            plugin_name=$(jq -r '.name' "$plugin_json")
            plugin_version=$(jq -r '.version' "$plugin_json")
            PLUGIN_VERSIONS["$plugin_name"]="$plugin_version"
        fi
    fi
done

# Check each plugin
ALL_PLUGINS_MATCH=true
for plugin_name in "${!PLUGIN_VERSIONS[@]}"; do
    json_version="${PLUGIN_VERSIONS[$plugin_name]}"

    # Extract version from README (look for **Version**: X.Y.Z after the plugin section)
    # This is complex because README has multiple version entries per section
    case "$plugin_name" in
        "panel-system")
            readme_version=$(awk '/### Panel System/,/---/' "$REPO_ROOT/README.md" | grep -oP '\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' | head -1)
            ;;
        "code")
            readme_version=$(awk '/### Code$/,/---/' "$REPO_ROOT/README.md" | grep -oP '\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' | head -1)
            ;;
        "write")
            readme_version=$(awk '/### Write$/,/---/' "$REPO_ROOT/README.md" | grep -oP '\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' | head -1)
            ;;
        "output-styles")
            readme_version=$(awk '/### Output Styles/,/---/' "$REPO_ROOT/README.md" | grep -oP '\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' | head -1)
            ;;
        "asha")
            readme_version=$(awk '/### Asha$/,/---/' "$REPO_ROOT/README.md" | grep -oP '\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' | head -1)
            ;;
        "image")
            readme_version=$(grep -oP '^\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/plugins/image/README.md" | head -1)
            ;;
        "scheduler")
            readme_version=$(grep -oP '^\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/plugins/schedule/README.md" | head -1)
            ;;
        *)
            readme_version=""
            ;;
    esac

    echo -n "  $plugin_name: "
    if [[ -z "$json_version" ]]; then
        echo -e "${YELLOW}SKIP${NC} (no plugin.json)"
        WARNINGS=$((WARNINGS + 1))
    elif [[ -z "$readme_version" ]]; then
        echo -e "${YELLOW}WARN${NC} (not found in README)"
        WARNINGS=$((WARNINGS + 1))
    elif [[ "$json_version" == "$readme_version" ]]; then
        echo -e "${GREEN}PASS${NC} (v$json_version)"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "    plugin.json: v$json_version"
        echo "    README.md: v$readme_version"
        FAILED=$((FAILED + 1))
        ALL_PLUGINS_MATCH=false
    fi
done

# Test 3: Version in CLAUDE.md
echo ""
echo -n "Test 3: Marketplace version in CLAUDE.md... "
CLAUDE_MD_VERSION=$(grep -oP '^\*\*Version\*\*: \K[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/CLAUDE.md" 2>/dev/null | head -1 || echo "")

if [[ -z "$CLAUDE_MD_VERSION" ]]; then
    echo -e "${YELLOW}SKIP${NC} (no version found in CLAUDE.md)"
    WARNINGS=$((WARNINGS + 1))
elif [[ "$MARKETPLACE_JSON_VERSION" == "$CLAUDE_MD_VERSION" ]]; then
    echo -e "${GREEN}PASS${NC} (v$CLAUDE_MD_VERSION)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  marketplace.json: v$MARKETPLACE_JSON_VERSION"
    echo "  CLAUDE.md: v$CLAUDE_MD_VERSION"
    FAILED=$((FAILED + 1))
fi

# Summary
echo ""
echo "=== Version Summary ==="
echo "Marketplace: v$MARKETPLACE_JSON_VERSION (source of truth: marketplace.json)"
echo ""
echo "Plugins:"
for plugin_name in "${!PLUGIN_VERSIONS[@]}"; do
    version="${PLUGIN_VERSIONS[$plugin_name]}"
    if [[ -n "$version" ]]; then
        echo "  $plugin_name: v$version"
    fi
done

echo ""
echo "=== Test Summary ==="
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
echo -e "Warnings: ${YELLOW}$WARNINGS${NC}"
echo ""

if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}✓ All version checks passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Version inconsistencies detected${NC}"
    echo "Fix: Update README.md and CLAUDE.md to match plugin.json versions"
    exit 1
fi
