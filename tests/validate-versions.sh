#!/usr/bin/env bash
# validate-versions.sh
#
# Plugin README files are the sole plugin-version authority. This validator
# checks that every plugin is covered by namespaces.json and that every
# versioned plugin README carries one strict semver declaration. It does not
# compare against duplicated tables in root documentation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
NAMESPACES="$REPO_ROOT/namespaces.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASSED=0
FAILED=0

pass() {
    echo -e "${GREEN}PASS${NC}${1:+ ($1)}"
    PASSED=$((PASSED + 1))
}

fail() {
    echo -e "${RED}FAIL${NC}"
    FAILED=$((FAILED + 1))
}

# The installer canary intentionally has no release lifecycle or version.
is_unversioned_canary() {
    [[ "$1" == "test" ]]
}

echo "=== Plugin README Authority Validator ==="
echo "Repository: $REPO_ROOT"
echo ""

echo -n "Test 1: namespaces.json covers every plugin README exactly once... "
NAMESPACE_ERRORS=()
PLUGIN_COUNT=0

if ! jq -e 'type == "object"' "$NAMESPACES" >/dev/null 2>&1; then
    NAMESPACE_ERRORS+=("namespaces.json is missing or is not a JSON object")
else
    for plugin_dir in "$REPO_ROOT"/plugins/*/; do
        [[ -d "$plugin_dir" ]] || continue
        plugin="$(basename "$plugin_dir")"
        PLUGIN_COUNT=$((PLUGIN_COUNT + 1))

        if [[ ! -f "$plugin_dir/README.md" ]]; then
            NAMESPACE_ERRORS+=("plugins/$plugin is missing README.md")
        fi

        namespace="$(jq -r --arg plugin "$plugin" '.[$plugin] // empty' "$NAMESPACES")"
        if [[ -z "$namespace" ]]; then
            NAMESPACE_ERRORS+=("plugins/$plugin has no namespaces.json entry")
        elif [[ ! "$namespace" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
            NAMESPACE_ERRORS+=("plugins/$plugin maps to malformed namespace '$namespace'")
        fi
    done

    while IFS=$'\t' read -r plugin namespace; do
        [[ -n "$plugin" ]] || continue
        if [[ ! -d "$REPO_ROOT/plugins/$plugin" ]]; then
            NAMESPACE_ERRORS+=("namespaces.json contains stale plugin entry '$plugin'")
        elif [[ ! -f "$REPO_ROOT/plugins/$plugin/README.md" ]]; then
            NAMESPACE_ERRORS+=("namespaces.json entry '$plugin' has no plugin README")
        fi
        if [[ -z "$namespace" ]]; then
            NAMESPACE_ERRORS+=("namespaces.json entry '$plugin' has an empty namespace")
        fi
    done < <(jq -r 'to_entries[] | select(.key | startswith("_") | not) | [.key, .value] | @tsv' "$NAMESPACES")

    duplicate_namespaces="$(
        jq -r 'to_entries[] | select(.key | startswith("_") | not) | .value' "$NAMESPACES" \
            | sort | uniq -d
    )"
    if [[ -n "$duplicate_namespaces" ]]; then
        while IFS= read -r namespace; do
            [[ -n "$namespace" ]] && NAMESPACE_ERRORS+=("namespace '$namespace' is assigned more than once")
        done <<< "$duplicate_namespaces"
    fi
fi

if [[ ${#NAMESPACE_ERRORS[@]} -eq 0 ]]; then
    pass "$PLUGIN_COUNT plugins"
else
    fail
    printf '  %s\n' "${NAMESPACE_ERRORS[@]}"
fi

echo -n "Test 2: Plugin README version declarations are strict semver... "
VERSION_ERRORS=()
VERSIONED_COUNT=0
UNVERSIONED_COUNT=0

for plugin_readme in "$REPO_ROOT"/plugins/*/README.md; do
    [[ -f "$plugin_readme" ]] || continue
    plugin="$(basename "$(dirname "$plugin_readme")")"
    declaration_count="$(awk '/^\*\*Version\*\*:/ { count++ } END { print count + 0 }' "$plugin_readme")"

    if is_unversioned_canary "$plugin"; then
        UNVERSIONED_COUNT=$((UNVERSIONED_COUNT + 1))
        if [[ "$declaration_count" -ne 0 ]]; then
            VERSION_ERRORS+=("plugins/$plugin/README.md is the unversioned installer canary but declares a version")
        fi
        continue
    fi

    if [[ "$declaration_count" -ne 1 ]]; then
        VERSION_ERRORS+=("plugins/$plugin/README.md must contain exactly one **Version** declaration (found $declaration_count)")
        continue
    fi

    version="$(awk '
        /^\*\*Version\*\*:/ {
            sub(/^\*\*Version\*\*:[[:space:]]*/, "")
            sub(/[[:space:]]+$/, "")
            print
            exit
        }
    ' "$plugin_readme")"
    if [[ ! "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
        VERSION_ERRORS+=("plugins/$plugin/README.md has malformed version '$version' (expected X.Y.Z)")
        continue
    fi
    VERSIONED_COUNT=$((VERSIONED_COUNT + 1))
done

if [[ ${#VERSION_ERRORS[@]} -eq 0 ]]; then
    pass "$VERSIONED_COUNT versioned; $UNVERSIONED_COUNT canary"
else
    fail
    printf '  %s\n' "${VERSION_ERRORS[@]}"
fi

echo ""
echo "=== Test Summary ==="
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
echo ""

if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}✓ Plugin README authority checks passed${NC}"
    exit 0
fi

echo -e "${RED}✗ Plugin README authority checks failed${NC}"
exit 1
