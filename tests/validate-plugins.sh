#!/usr/bin/env bash
# validate-plugins.sh
# Tests plugin configuration for namespace conflicts and structural issues

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASSED=0
FAILED=0

echo "=== Plugin Configuration Validator ==="
echo "Repository: $REPO_ROOT"
echo ""

# Test 1: Validate marketplace.json
echo -n "Test 1: Validating marketplace.json... "
if jq empty "$REPO_ROOT/.claude-plugin/marketplace.json" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    FAILED=$((FAILED + 1))
fi

# Test 2: Validate plugin.json files
echo -n "Test 2: Validating plugin.json files... "
PLUGIN_JSON_VALID=true
for plugin_json in "$REPO_ROOT"/plugins/*/.claude-plugin/plugin.json; do
    if [ -f "$plugin_json" ]; then
        if ! jq empty "$plugin_json" 2>/dev/null; then
            echo -e "${RED}FAIL${NC}"
            echo "  Error: $plugin_json is not valid JSON"
            PLUGIN_JSON_VALID=false
        fi
    fi
done

if [ "$PLUGIN_JSON_VALID" = true ]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# Test 3: Check for namespace conflicts
echo -n "Test 3: Checking for namespace conflicts... "
CONFLICTS=false
for plugin_dir in "$REPO_ROOT"/plugins/*; do
    if [ -d "$plugin_dir" ]; then
        plugin_json="$plugin_dir/.claude-plugin/plugin.json"
        if [ -f "$plugin_json" ]; then
            plugin_name=$(jq -r '.name' "$plugin_json")
            commands_dir="$plugin_dir/commands"

            if [ -d "$commands_dir" ] && [ -f "$commands_dir/${plugin_name}.md" ]; then
                echo -e "${RED}FAIL${NC}"
                echo "  Error: Plugin '$plugin_name' has command '${plugin_name}.md'"
                echo "  This creates namespace conflict: /$plugin_name becomes ambiguous"
                echo "  Solution: Rename plugin or command to be different"
                CONFLICTS=true
            fi
        fi
    fi
done

if [ "$CONFLICTS" = false ]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# Test 4: Plugin name consistency
echo -n "Test 4: Verifying plugin name consistency... "
INCONSISTENT=false
marketplace_json="$REPO_ROOT/.claude-plugin/marketplace.json"

# Read marketplace plugins
while IFS= read -r marketplace_name; do
    source=$(jq -r ".plugins[] | select(.name == \"$marketplace_name\") | .source" "$marketplace_json")
    plugin_json="$REPO_ROOT/$source/.claude-plugin/plugin.json"

    if [ -f "$plugin_json" ]; then
        actual_name=$(jq -r '.name' "$plugin_json")
        if [ "$marketplace_name" != "$actual_name" ]; then
            echo -e "${RED}FAIL${NC}"
            echo "  Error: Name mismatch - marketplace: '$marketplace_name', plugin.json: '$actual_name'"
            INCONSISTENT=true
        fi
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Error: plugin.json not found at $plugin_json"
        INCONSISTENT=true
    fi
done < <(jq -r '.plugins[].name' "$marketplace_json")

if [ "$INCONSISTENT" = false ]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# Test 5: Command files exist
echo -n "Test 5: Verifying command files exist... "
MISSING=false
for plugin_dir in "$REPO_ROOT"/plugins/*; do
    if [ -d "$plugin_dir" ]; then
        plugin_json="$plugin_dir/.claude-plugin/plugin.json"
        if [ -f "$plugin_json" ]; then
            plugin_name=$(jq -r '.name' "$plugin_json")
            commands=$(jq -r '.commands // empty' "$plugin_json")

            if [ -n "$commands" ]; then
                if [[ "$commands" == ./* ]]; then
                    # Directory format
                    if [ ! -d "$plugin_dir/$commands" ]; then
                        echo -e "${RED}FAIL${NC}"
                        echo "  Error: Commands directory missing: $plugin_dir/$commands"
                        MISSING=true
                    fi
                fi
            fi
        fi
    fi
done

if [ "$MISSING" = false ]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# List all commands
echo ""
echo "=== Registered Commands ==="
for plugin_dir in "$REPO_ROOT"/plugins/*; do
    if [ -d "$plugin_dir" ]; then
        plugin_json="$plugin_dir/.claude-plugin/plugin.json"
        if [ -f "$plugin_json" ]; then
            plugin_name=$(jq -r '.name' "$plugin_json")
            commands_path=$(jq -r '.commands // empty' "$plugin_json")

            echo ""
            echo "Plugin: $plugin_name"

            if [ -n "$commands_path" ] && [ -d "$plugin_dir/$commands_path" ]; then
                for cmd_file in "$plugin_dir/$commands_path"/*.md; do
                    if [ -f "$cmd_file" ]; then
                        cmd_name=$(basename "$cmd_file" .md)
                        echo "  ✓ /$plugin_name:$cmd_name (full form)"
                        echo "    /$cmd_name (short form, works if no conflict)"
                    fi
                done
            else
                echo "  (no commands)"
            fi
        fi
    fi
done

# Summary
echo ""
echo "=== Test Summary ==="
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Some tests failed${NC}"
    exit 1
fi
