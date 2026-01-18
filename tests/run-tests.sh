#!/usr/bin/env bash
# run-tests.sh - Run all test suites for asha-marketplace
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

TOTAL_PASSED=0
TOTAL_FAILED=0
TOTAL_SKIPPED=0

echo -e "${BLUE}=== Asha Marketplace Test Suite ===${NC}"
echo "Repository: $REPO_ROOT"
echo ""

# Test Suite 1: Plugin Validation
echo -e "${BLUE}--- Test Suite 1: Plugin Validation ---${NC}"
if "$SCRIPT_DIR/validate-plugins.sh"; then
    echo -e "${GREEN}✓ Plugin validation passed${NC}"
    TOTAL_PASSED=$((TOTAL_PASSED + 1))
else
    echo -e "${RED}✗ Plugin validation failed${NC}"
    TOTAL_FAILED=$((TOTAL_FAILED + 1))
fi
echo ""

# Test Suite 2: Version Consistency
echo -e "${BLUE}--- Test Suite 2: Version Consistency ---${NC}"
if "$SCRIPT_DIR/validate-versions.sh"; then
    echo -e "${GREEN}✓ Version consistency passed${NC}"
    TOTAL_PASSED=$((TOTAL_PASSED + 1))
else
    echo -e "${RED}✗ Version consistency failed${NC}"
    TOTAL_FAILED=$((TOTAL_FAILED + 1))
fi
echo ""

# Test Suite 3: Python Unit Tests
echo -e "${BLUE}--- Test Suite 3: Python Unit Tests ---${NC}"
PYTHON_CMD=""
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
fi

if [[ -n "$PYTHON_CMD" ]]; then
    # Check if pytest is available
    if $PYTHON_CMD -c "import pytest" 2>/dev/null; then
        if $PYTHON_CMD -m pytest "$SCRIPT_DIR/python/" -v --tb=short; then
            echo -e "${GREEN}✓ Python unit tests passed${NC}"
            TOTAL_PASSED=$((TOTAL_PASSED + 1))
        else
            echo -e "${RED}✗ Python unit tests failed${NC}"
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
        fi
    else
        # Fall back to unittest
        echo -e "${YELLOW}pytest not available, using unittest${NC}"
        if $PYTHON_CMD -m unittest discover -s "$SCRIPT_DIR/python" -v; then
            echo -e "${GREEN}✓ Python unit tests passed${NC}"
            TOTAL_PASSED=$((TOTAL_PASSED + 1))
        else
            echo -e "${RED}✗ Python unit tests failed${NC}"
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
        fi
    fi
else
    echo -e "${YELLOW}⚠ Python not found, skipping Python tests${NC}"
    TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
fi
echo ""

# Test Suite 4: Hook Handler Tests
echo -e "${BLUE}--- Test Suite 4: Hook Handler Tests ---${NC}"
if "$SCRIPT_DIR/test-hooks.sh"; then
    echo -e "${GREEN}✓ Hook handler tests passed${NC}"
    TOTAL_PASSED=$((TOTAL_PASSED + 1))
else
    echo -e "${RED}✗ Hook handler tests failed${NC}"
    TOTAL_FAILED=$((TOTAL_FAILED + 1))
fi
echo ""

# Test Suite 5: Shellcheck (if available)
echo -e "${BLUE}--- Test Suite 5: Shell Script Linting ---${NC}"
if command -v shellcheck &>/dev/null; then
    SHELL_ERRORS=0
    while IFS= read -r -d '' script; do
        if head -1 "$script" | grep -qE "^#!.*bash"; then
            if ! shellcheck -x "$script" 2>/dev/null; then
                echo -e "${RED}  ✗ $script${NC}"
                SHELL_ERRORS=$((SHELL_ERRORS + 1))
            fi
        fi
    done < <(find "$REPO_ROOT/plugins" -type f \( -name "*.sh" -o -path "*/hooks/handlers/*" \) ! -name "*.json" -print0 2>/dev/null)

    if [[ $SHELL_ERRORS -eq 0 ]]; then
        echo -e "${GREEN}✓ Shell script linting passed${NC}"
        TOTAL_PASSED=$((TOTAL_PASSED + 1))
    else
        echo -e "${RED}✗ Shell script linting failed ($SHELL_ERRORS errors)${NC}"
        TOTAL_FAILED=$((TOTAL_FAILED + 1))
    fi
else
    echo -e "${YELLOW}⚠ shellcheck not installed, skipping shell linting${NC}"
    echo "  Install with: apt install shellcheck (Ubuntu) or brew install shellcheck (macOS)"
    TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
fi
echo ""

# Summary
echo -e "${BLUE}=== Test Summary ===${NC}"
echo -e "Passed:  ${GREEN}$TOTAL_PASSED${NC}"
echo -e "Failed:  ${RED}$TOTAL_FAILED${NC}"
echo -e "Skipped: ${YELLOW}$TOTAL_SKIPPED${NC}"
echo ""

if [[ $TOTAL_FAILED -eq 0 ]]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Some tests failed${NC}"
    exit 1
fi
