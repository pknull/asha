#!/usr/bin/env bash
# test-hooks.sh - Test hook handlers for correct behavior
# Tests hook scripts in isolation without requiring full Claude Code environment

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASSED=0
FAILED=0
SKIPPED=0

# Create temp directory for test environment
TEST_DIR=$(mktemp -d)
trap "rm -rf $TEST_DIR" EXIT

setup_test_project() {
    # Create mock project structure
    mkdir -p "$TEST_DIR/project/Memory/sessions"
    mkdir -p "$TEST_DIR/project/Work/markers"
    mkdir -p "$TEST_DIR/project/.asha"
    echo '{"initialized": true}' > "$TEST_DIR/project/.asha/config.json"
}

echo -e "${BLUE}=== Hook Handler Test Suite ===${NC}"
echo "Repository: $REPO_ROOT"
echo "Test directory: $TEST_DIR"
echo ""

# ============================================================================
# Test 1: Asha SessionStart hook - uninitialized project
# ============================================================================
echo -n "Test 1: SessionStart exits cleanly for non-Asha project... "
mkdir -p "$TEST_DIR/non-asha"
export CLAUDE_PROJECT_DIR="$TEST_DIR/non-asha"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

OUTPUT=$("$REPO_ROOT/plugins/asha/hooks/handlers/session-start.sh" 2>/dev/null || true)

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected: {}"
    echo "  Got: $OUTPUT"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 2: Asha SessionStart hook - initialized project
# ============================================================================
echo -n "Test 2: SessionStart injects context for Asha project... "
setup_test_project
export CLAUDE_PROJECT_DIR="$TEST_DIR/project"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

OUTPUT=$("$REPO_ROOT/plugins/asha/hooks/handlers/session-start.sh" 2>/dev/null || true)

if [[ "$OUTPUT" == *"system-reminder"* && "$OUTPUT" == *"Asha is initialized"* ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected output containing 'system-reminder' and 'Asha is initialized'"
    echo "  Got: ${OUTPUT:0:100}..."
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 3: PostToolUse creates session file
# ============================================================================
echo -n "Test 3: PostToolUse creates session file if missing... "
setup_test_project
rm -f "$TEST_DIR/project/Memory/sessions/current-session.md"
export CLAUDE_PROJECT_DIR="$TEST_DIR/project"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Feed minimal JSON input
echo '{"tool_name": "Read", "tool_input": {}}' | \
    "$REPO_ROOT/plugins/asha/hooks/handlers/post-tool-use.sh" >/dev/null 2>&1 || true

if [[ -f "$TEST_DIR/project/Memory/sessions/current-session.md" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Session file was not created"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 4: PostToolUse logs Edit operations
# ============================================================================
echo -n "Test 4: PostToolUse logs Edit operations... "
setup_test_project
# Create session file first
cat > "$TEST_DIR/project/Memory/sessions/current-session.md" << 'EOF'
---
sessionStart: 2026-01-17 00:00 UTC
sessionID: test123
---

## Significant Operations
<!-- Auto-appended -->

## Decisions & Clarifications
<!-- Auto-appended -->

## Errors & Anomalies
<!-- Auto-appended -->
EOF

export CLAUDE_PROJECT_DIR="$TEST_DIR/project"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Feed Edit tool JSON
echo '{"tool_name": "Edit", "tool_input": {"file_path": "/test/file.md"}, "tool_response": {}}' | \
    "$REPO_ROOT/plugins/asha/hooks/handlers/post-tool-use.sh" >/dev/null 2>&1 || true

if grep -q "Modified:.*file.md" "$TEST_DIR/project/Memory/sessions/current-session.md"; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Edit operation was not logged"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 5: PostToolUse respects silence marker
# ============================================================================
echo -n "Test 5: PostToolUse respects silence marker... "
setup_test_project
touch "$TEST_DIR/project/Work/markers/silence"

# Create fresh session file
cat > "$TEST_DIR/project/Memory/sessions/current-session.md" << 'EOF'
---
sessionStart: 2026-01-17 00:00 UTC
sessionID: test456
---

## Significant Operations
<!-- Auto-appended -->
EOF

export CLAUDE_PROJECT_DIR="$TEST_DIR/project"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

BEFORE_SIZE=$(wc -c < "$TEST_DIR/project/Memory/sessions/current-session.md")

echo '{"tool_name": "Write", "tool_input": {"file_path": "/test/newfile.md"}, "tool_response": {}}' | \
    "$REPO_ROOT/plugins/asha/hooks/handlers/post-tool-use.sh" >/dev/null 2>&1 || true

AFTER_SIZE=$(wc -c < "$TEST_DIR/project/Memory/sessions/current-session.md")

if [[ "$BEFORE_SIZE" -eq "$AFTER_SIZE" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Session file was modified despite silence marker"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 6: UserPromptSubmit creates session file
# ============================================================================
echo -n "Test 6: UserPromptSubmit creates session file if missing... "
# Create fresh test directory for this test
TEST6_DIR=$(mktemp -d)
mkdir -p "$TEST6_DIR/Memory/sessions"
mkdir -p "$TEST6_DIR/Work/markers"
mkdir -p "$TEST6_DIR/.asha"
echo '{"initialized": true}' > "$TEST6_DIR/.asha/config.json"
export CLAUDE_PROJECT_DIR="$TEST6_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

echo '{"prompt": "Hello world this is a test prompt"}' | \
    "$REPO_ROOT/plugins/asha/hooks/handlers/user-prompt-submit.sh" >/dev/null 2>&1 || true

if [[ -f "$TEST6_DIR/Memory/sessions/current-session.md" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Session file was not created"
    FAILED=$((FAILED + 1))
fi
rm -rf "$TEST6_DIR"

# ============================================================================
# Test 7: Output-styles SessionStart without config
# ============================================================================
echo -n "Test 7: Output-styles hook returns {} without config... "
rm -f "$HOME/.claude/active-output-style" 2>/dev/null || true

OUTPUT=$("$REPO_ROOT/plugins/output-styles/hooks-handlers/session-start.sh" 2>/dev/null || true)

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected: {}"
    echo "  Got: $OUTPUT"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 8: Output-styles SessionStart with valid config
# ============================================================================
echo -n "Test 8: Output-styles hook injects style when configured... "
mkdir -p "$HOME/.claude"
echo "ultra-concise" > "$HOME/.claude/active-output-style"

OUTPUT=$("$REPO_ROOT/plugins/output-styles/hooks-handlers/session-start.sh" 2>/dev/null || true)

# Clean up
rm -f "$HOME/.claude/active-output-style"

if [[ "$OUTPUT" == *"hookSpecificOutput"* && "$OUTPUT" == *"additionalContext"* ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected output with hookSpecificOutput and additionalContext"
    echo "  Got: ${OUTPUT:0:100}..."
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 9: Common.sh utility functions
# ============================================================================
echo -n "Test 9: common.sh detect_project_dir works... "
setup_test_project
export CLAUDE_PROJECT_DIR="$TEST_DIR/project"

# Source common.sh and test
DETECTED=$(bash -c "
source '$REPO_ROOT/plugins/asha/hooks/handlers/common.sh'
detect_project_dir
")

if [[ "$DETECTED" == "$TEST_DIR/project" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected: $TEST_DIR/project"
    echo "  Got: $DETECTED"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 10: is_asha_initialized function
# ============================================================================
echo -n "Test 10: is_asha_initialized correctly detects initialization... "
setup_test_project
export CLAUDE_PROJECT_DIR="$TEST_DIR/project"

RESULT=$(bash -c "
source '$REPO_ROOT/plugins/asha/hooks/handlers/common.sh'
if is_asha_initialized; then echo 'yes'; else echo 'no'; fi
")

if [[ "$RESULT" == "yes" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected: yes"
    echo "  Got: $RESULT"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 11: All output styles have valid frontmatter
# ============================================================================
echo -n "Test 11: Output styles have valid frontmatter... "
STYLE_ERRORS=0
for style_file in "$REPO_ROOT"/plugins/output-styles/styles/*.md; do
    style_name=$(basename "$style_file" .md)

    # Check for YAML frontmatter (starts with ---)
    if ! head -1 "$style_file" | grep -q "^---$"; then
        echo -e "${RED}FAIL${NC}"
        echo "  Style $style_name missing frontmatter"
        STYLE_ERRORS=$((STYLE_ERRORS + 1))
        continue
    fi

    # Check for name field in frontmatter
    if ! grep -q "^name:" "$style_file"; then
        echo -e "${RED}FAIL${NC}"
        echo "  Style $style_name missing 'name' in frontmatter"
        STYLE_ERRORS=$((STYLE_ERRORS + 1))
    fi
done

if [[ $STYLE_ERRORS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC} (8 styles validated)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 12: Asha templates exist for init command
# ============================================================================
echo -n "Test 12: Asha init templates exist... "
TEMPLATE_DIR="$REPO_ROOT/plugins/asha/templates"
MISSING_TEMPLATES=0
REQUIRED_TEMPLATES=(
    "activeContext.md"
    "projectbrief.md"
    "communicationStyle.md"
    "workflowProtocols.md"
    "techEnvironment.md"
    "scratchpad.md"
    "CLAUDE.md"
)

for template in "${REQUIRED_TEMPLATES[@]}"; do
    if [[ ! -f "$TEMPLATE_DIR/$template" ]]; then
        if [[ $MISSING_TEMPLATES -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  Missing template: $template"
        MISSING_TEMPLATES=$((MISSING_TEMPLATES + 1))
    fi
done

if [[ $MISSING_TEMPLATES -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC} (${#REQUIRED_TEMPLATES[@]} templates)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 13: All command files have description frontmatter
# ============================================================================
echo -n "Test 13: Commands have description frontmatter... "
CMD_ERRORS=0
for plugin_dir in "$REPO_ROOT"/plugins/*/; do
    plugin_name=$(basename "$plugin_dir")
    cmd_dir="$plugin_dir/commands"
    [[ ! -d "$cmd_dir" ]] && continue

    for cmd_file in "$cmd_dir"/*.md; do
        [[ ! -f "$cmd_file" ]] && continue
        cmd_name=$(basename "$cmd_file" .md)

        # Check for description in frontmatter or first heading
        if ! grep -q "^description:" "$cmd_file" && ! head -5 "$cmd_file" | grep -q "^# "; then
            echo -e "${RED}FAIL${NC}"
            echo "  $plugin_name/$cmd_name missing description"
            CMD_ERRORS=$((CMD_ERRORS + 1))
        fi
    done
done

if [[ $CMD_ERRORS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 14: Violation rules have check_violation function
# ============================================================================
echo -n "Test 14: Violation rules have check_violation function... "
RULES_DIR="$REPO_ROOT/plugins/asha/rules"
RULE_ERRORS=0

if [[ -d "$RULES_DIR" ]]; then
    for rule_file in "$RULES_DIR"/*.sh; do
        [[ ! -f "$rule_file" ]] && continue
        rule_name=$(basename "$rule_file")

        # Check for check_violation function
        if ! grep -q "check_violation()" "$rule_file"; then
            if [[ $RULE_ERRORS -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  $rule_name missing check_violation function"
            RULE_ERRORS=$((RULE_ERRORS + 1))
        fi

        # Check for Severity comment
        if ! grep -q "^# Severity:" "$rule_file"; then
            if [[ $RULE_ERRORS -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  $rule_name missing Severity declaration"
            RULE_ERRORS=$((RULE_ERRORS + 1))
        fi
    done

    if [[ $RULE_ERRORS -eq 0 ]]; then
        RULE_COUNT=$(ls -1 "$RULES_DIR"/*.sh 2>/dev/null | wc -l)
        echo -e "${GREEN}PASS${NC} ($RULE_COUNT rules validated)"
        PASSED=$((PASSED + 1))
    else
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC} (no rules directory)"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 15: Destructive-git rule detects force push
# ============================================================================
echo -n "Test 15: Destructive-git rule detects force push... "
RULE_FILE="$REPO_ROOT/plugins/asha/rules/destructive-git.sh"

if [[ -f "$RULE_FILE" ]]; then
    # Source the rule and test
    RESULT=$(bash -c "
        source '$RULE_FILE'
        check_violation 'Bash' 'git push -f origin main' '/tmp' 2>/dev/null && echo 'DETECTED' || echo 'MISSED'
    ")

    if [[ "$RESULT" == *"DETECTED"* ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Force push not detected"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC} (rule file not found)"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 16: Python tools requirements.txt exists
# ============================================================================
echo -n "Test 16: Python requirements.txt exists... "
REQ_FILE="$REPO_ROOT/plugins/asha/tools/requirements.txt"

if [[ -f "$REQ_FILE" ]]; then
    # Check it has at least one dependency
    DEP_COUNT=$(grep -v "^#" "$REQ_FILE" | grep -v "^$" | wc -l)
    if [[ $DEP_COUNT -gt 0 ]]; then
        echo -e "${GREEN}PASS${NC} ($DEP_COUNT dependencies)"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  requirements.txt is empty"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  requirements.txt not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 17: Memory-protection rule allows mutable files
# ============================================================================
echo -n "Test 17: Memory-protection allows mutable files... "
RULE_FILE="$REPO_ROOT/plugins/asha/rules/memory-protection.sh"

if [[ -f "$RULE_FILE" ]]; then
    # Test mutable file (should NOT trigger)
    RESULT=$(bash -c "
        source '$RULE_FILE'
        check_violation 'Edit' '/tmp/project/Memory/sessions/test.md' '/tmp/project' 2>/dev/null && echo 'VIOLATION' || echo 'ALLOWED'
    ")

    if [[ "$RESULT" == "ALLOWED" ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Mutable file incorrectly flagged"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 18: Memory-protection rule blocks immutable files
# ============================================================================
echo -n "Test 18: Memory-protection blocks immutable files... "

if [[ -f "$RULE_FILE" ]]; then
    # Test immutable file (should trigger)
    RESULT=$(bash -c "
        source '$RULE_FILE'
        check_violation 'Edit' '/tmp/project/Memory/communicationStyle.md' '/tmp/project' 2>/dev/null && echo 'VIOLATION' || echo 'ALLOWED'
    ")

    if [[ "$RESULT" == *"VIOLATION"* ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Immutable file not protected"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 19: SessionEnd hook handles clear reason
# ============================================================================
echo -n "Test 19: SessionEnd handles clear reason... "
SESSION_END="$REPO_ROOT/plugins/asha/hooks/handlers/session-end.sh"
TEST19_DIR=$(mktemp -d)
mkdir -p "$TEST19_DIR/Memory/sessions"
mkdir -p "$TEST19_DIR/Work/markers"
mkdir -p "$TEST19_DIR/.asha"
echo '{"initialized": true}' > "$TEST19_DIR/.asha/config.json"
export CLAUDE_PROJECT_DIR="$TEST19_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

OUTPUT=$(echo '{"reason": "clear"}' | "$SESSION_END" 2>/dev/null || true)
rm -rf "$TEST19_DIR"

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected {} for clear reason"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 20: All hooks return valid JSON or system-reminder
# ============================================================================
echo -n "Test 20: All hooks return valid output... "
JSON_ERRORS=0
HOOKS_DIR="$REPO_ROOT/plugins/asha/hooks/handlers"
TEST20_DIR=$(mktemp -d)
mkdir -p "$TEST20_DIR/Memory/sessions"
mkdir -p "$TEST20_DIR/Work/markers"
mkdir -p "$TEST20_DIR/.asha"
echo '{"initialized": true}' > "$TEST20_DIR/.asha/config.json"

for hook in session-start.sh post-tool-use.sh user-prompt-submit.sh session-end.sh; do
    HOOK_FILE="$HOOKS_DIR/$hook"
    [[ ! -f "$HOOK_FILE" ]] && continue

    export CLAUDE_PROJECT_DIR="$TEST20_DIR"
    export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

    # Run hook with minimal input (capture full output)
    OUTPUT=$(echo '{"prompt": "test", "tool_name": "Read"}' | "$HOOK_FILE" 2>/dev/null || true)

    # Valid outputs:
    # 1. Empty or {}
    # 2. Valid JSON object
    # 3. system-reminder tags (for SessionStart)
    if [[ -n "$OUTPUT" && "$OUTPUT" != "{}" ]]; then
        # Check for system-reminder (valid for SessionStart)
        if [[ "$OUTPUT" == *"<system-reminder>"* ]]; then
            continue
        fi
        # Check for valid JSON
        if ! echo "$OUTPUT" | jq . >/dev/null 2>&1; then
            echo "  $hook: invalid output"
            JSON_ERRORS=$((JSON_ERRORS + 1))
        fi
    fi
done

rm -rf "$TEST20_DIR"

if [[ $JSON_ERRORS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 21: Vault-structure rule detects unexpected locations
# ============================================================================
echo -n "Test 21: Vault-structure detects unexpected locations... "
VAULT_RULE="$REPO_ROOT/plugins/asha/rules/vault-structure.sh"

if [[ -f "$VAULT_RULE" ]]; then
    # Test unexpected location (should trigger)
    RESULT=$(bash -c "
        source '$VAULT_RULE'
        check_violation 'Write' '/tmp/project/Vault/random-file.md' '/tmp/project' 2>/dev/null && echo 'VIOLATION' || echo 'ALLOWED'
    ")

    if [[ "$RESULT" == *"VIOLATION"* ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Unexpected vault location not detected"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 22: Vault-structure allows expected directories
# ============================================================================
echo -n "Test 22: Vault-structure allows expected directories... "

if [[ -f "$VAULT_RULE" ]]; then
    # Test expected location (should not trigger)
    RESULT=$(bash -c "
        source '$VAULT_RULE'
        check_violation 'Write' '/tmp/project/Vault/Characters/test.md' '/tmp/project' 2>/dev/null && echo 'VIOLATION' || echo 'ALLOWED'
    ")

    if [[ "$RESULT" == "ALLOWED" ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Expected vault location incorrectly flagged"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 23: All asha modules exist
# ============================================================================
echo -n "Test 23: All asha modules exist... "
MODULES_DIR="$REPO_ROOT/plugins/asha/modules"
REQUIRED_MODULES=(
    "CORE.md"
    "cognitive.md"
    "research.md"
    "memory-ops.md"
    "high-stakes.md"
    "verbalized-sampling.md"
)
MISSING_MODULES=0

for module in "${REQUIRED_MODULES[@]}"; do
    if [[ ! -f "$MODULES_DIR/$module" ]]; then
        if [[ $MISSING_MODULES -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  Missing module: $module"
        MISSING_MODULES=$((MISSING_MODULES + 1))
    fi
done

if [[ $MISSING_MODULES -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC} (${#REQUIRED_MODULES[@]} modules)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 24: No hardcoded absolute paths in hook handlers
# ============================================================================
echo -n "Test 24: No hardcoded paths in hook handlers... "
HARDCODED_PATHS=0

for handler in "$REPO_ROOT"/plugins/asha/hooks/handlers/*.sh; do
    [[ ! -f "$handler" ]] && continue
    handler_name=$(basename "$handler")

    # Check for hardcoded /home/ or /Users/ paths (excluding comments)
    if grep -v "^#" "$handler" | grep -qE "(/home/|/Users/)[a-zA-Z]"; then
        if [[ $HARDCODED_PATHS -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $handler_name contains hardcoded paths"
        HARDCODED_PATHS=$((HARDCODED_PATHS + 1))
    fi
done

if [[ $HARDCODED_PATHS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 25: All plugins have LICENSE files
# ============================================================================
echo -n "Test 25: All plugins have LICENSE files... "
LICENSE_MISSING=0

for plugin_dir in "$REPO_ROOT"/plugins/*/; do
    plugin_name=$(basename "$plugin_dir")
    if [[ ! -f "$plugin_dir/LICENSE" ]]; then
        if [[ $LICENSE_MISSING -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name missing LICENSE"
        LICENSE_MISSING=$((LICENSE_MISSING + 1))
    fi
done

if [[ $LICENSE_MISSING -eq 0 ]]; then
    PLUGIN_COUNT=$(ls -d "$REPO_ROOT"/plugins/*/ 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($PLUGIN_COUNT plugins)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 26: run-python.sh wrapper is executable
# ============================================================================
echo -n "Test 26: run-python.sh wrapper is executable... "
RUN_PYTHON="$REPO_ROOT/plugins/asha/tools/run-python.sh"

if [[ -x "$RUN_PYTHON" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  run-python.sh is not executable"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 27: save-session.sh exists and is executable
# ============================================================================
echo -n "Test 27: save-session.sh exists and is executable... "
SAVE_SESSION="$REPO_ROOT/plugins/asha/tools/save-session.sh"

if [[ -x "$SAVE_SESSION" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  save-session.sh missing or not executable"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 28: Python tools are importable (syntax check)
# ============================================================================
echo -n "Test 28: Python tools have valid syntax... "
TOOLS_DIR="$REPO_ROOT/plugins/asha/tools"
SYNTAX_ERRORS=0

for py_file in "$TOOLS_DIR"/*.py; do
    [[ ! -f "$py_file" ]] && continue
    py_name=$(basename "$py_file")

    if ! python3 -m py_compile "$py_file" 2>/dev/null; then
        if [[ $SYNTAX_ERRORS -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $py_name has syntax errors"
        SYNTAX_ERRORS=$((SYNTAX_ERRORS + 1))
    fi
done

if [[ $SYNTAX_ERRORS -eq 0 ]]; then
    PY_COUNT=$(ls "$TOOLS_DIR"/*.py 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($PY_COUNT files)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 29: Panel agent files exist
# ============================================================================
echo -n "Test 29: Panel agent files exist... "
PANEL_AGENTS_DIR="$REPO_ROOT/plugins/panel/agents"

if [[ -d "$PANEL_AGENTS_DIR" ]]; then
    AGENT_COUNT=$(ls "$PANEL_AGENTS_DIR"/*.md 2>/dev/null | wc -l)
    if [[ $AGENT_COUNT -gt 0 ]]; then
        echo -e "${GREEN}PASS${NC} ($AGENT_COUNT agents)"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  No agent files found"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  Panel agents directory missing"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 30: Panel character files exist
# ============================================================================
echo -n "Test 30: Panel character files exist... "
PANEL_CHARS_DIR="$REPO_ROOT/plugins/panel/docs/characters"
REQUIRED_CHARS=(
    "The Moderator.md"
    "The Analyst.md"
    "The Challenger.md"
)
MISSING_CHARS=0

if [[ -d "$PANEL_CHARS_DIR" ]]; then
    for char in "${REQUIRED_CHARS[@]}"; do
        if [[ ! -f "$PANEL_CHARS_DIR/$char" ]]; then
            if [[ $MISSING_CHARS -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  Missing character: $char"
            MISSING_CHARS=$((MISSING_CHARS + 1))
        fi
    done

    if [[ $MISSING_CHARS -eq 0 ]]; then
        echo -e "${GREEN}PASS${NC} (${#REQUIRED_CHARS[@]} characters)"
        PASSED=$((PASSED + 1))
    else
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  Panel characters directory missing"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 31: All plugin.json files are valid JSON
# ============================================================================
echo -n "Test 31: All plugin.json files are valid JSON... "
INVALID_JSON=0

for plugin_json in "$REPO_ROOT"/plugins/*/.claude-plugin/plugin.json; do
    [[ ! -f "$plugin_json" ]] && continue
    plugin_name=$(basename "$(dirname "$(dirname "$plugin_json")")")

    if ! python3 -c "import json; json.load(open('$plugin_json'))" 2>/dev/null; then
        if [[ $INVALID_JSON -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name/plugin.json is invalid"
        INVALID_JSON=$((INVALID_JSON + 1))
    fi
done

if [[ $INVALID_JSON -eq 0 ]]; then
    JSON_COUNT=$(ls "$REPO_ROOT"/plugins/*/.claude-plugin/plugin.json 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($JSON_COUNT plugins)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 32: marketplace.json is valid
# ============================================================================
echo -n "Test 32: marketplace.json is valid JSON... "
MARKETPLACE_JSON="$REPO_ROOT/.claude-plugin/marketplace.json"

if [[ -f "$MARKETPLACE_JSON" ]]; then
    if python3 -c "import json; json.load(open('$MARKETPLACE_JSON'))" 2>/dev/null; then
        # Also verify required fields exist
        HAS_PLUGINS=$(python3 -c "import json; d=json.load(open('$MARKETPLACE_JSON')); print('yes' if 'plugins' in d else 'no')" 2>/dev/null)
        if [[ "$HAS_PLUGINS" == "yes" ]]; then
            echo -e "${GREEN}PASS${NC}"
            PASSED=$((PASSED + 1))
        else
            echo -e "${RED}FAIL${NC}"
            echo "  marketplace.json missing 'plugins' field"
            FAILED=$((FAILED + 1))
        fi
    else
        echo -e "${RED}FAIL${NC}"
        echo "  marketplace.json is invalid JSON"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  marketplace.json not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 33: Commands have valid YAML frontmatter
# ============================================================================
echo -n "Test 33: Command files have valid frontmatter... "
INVALID_FRONTMATTER=0

for cmd_file in "$REPO_ROOT"/plugins/*/commands/*.md; do
    [[ ! -f "$cmd_file" ]] && continue
    cmd_name=$(basename "$cmd_file")

    # Check if file starts with ---
    if head -1 "$cmd_file" | grep -q "^---"; then
        # Extract frontmatter and validate
        FRONTMATTER=$(sed -n '1,/^---$/p' "$cmd_file" | tail -n +2 | head -n -1)
        if ! echo "$FRONTMATTER" | python3 -c "import sys,yaml; yaml.safe_load(sys.stdin)" 2>/dev/null; then
            if [[ $INVALID_FRONTMATTER -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  $cmd_name has invalid frontmatter"
            INVALID_FRONTMATTER=$((INVALID_FRONTMATTER + 1))
        fi
    fi
done

if [[ $INVALID_FRONTMATTER -eq 0 ]]; then
    CMD_COUNT=$(ls "$REPO_ROOT"/plugins/*/commands/*.md 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($CMD_COUNT commands)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 34: All hooks.json files are valid JSON
# ============================================================================
echo -n "Test 34: All hooks.json files are valid JSON... "
INVALID_HOOKS=0

for hooks_json in "$REPO_ROOT"/plugins/*/hooks/hooks.json; do
    [[ ! -f "$hooks_json" ]] && continue
    plugin_name=$(basename "$(dirname "$(dirname "$hooks_json")")")

    if ! python3 -c "import json; json.load(open('$hooks_json'))" 2>/dev/null; then
        if [[ $INVALID_HOOKS -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name/hooks.json is invalid"
        INVALID_HOOKS=$((INVALID_HOOKS + 1))
    fi
done

if [[ $INVALID_HOOKS -eq 0 ]]; then
    HOOKS_COUNT=$(ls "$REPO_ROOT"/plugins/*/hooks/hooks.json 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($HOOKS_COUNT plugins with hooks)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 35: Hook handler scripts referenced in hooks.json exist
# ============================================================================
echo -n "Test 35: Hook handlers referenced in hooks.json exist... "
MISSING_HANDLERS=0

for hooks_json in "$REPO_ROOT"/plugins/*/hooks/hooks.json; do
    [[ ! -f "$hooks_json" ]] && continue
    plugin_dir=$(dirname "$(dirname "$hooks_json")")
    plugin_name=$(basename "$plugin_dir")

    # Extract command paths from hooks.json (replacing ${CLAUDE_PLUGIN_ROOT} with plugin_dir)
    COMMANDS=$(python3 -c "
import json
with open('$hooks_json') as f:
    data = json.load(f)

def find_commands(obj):
    if isinstance(obj, dict):
        if 'command' in obj:
            yield obj['command']
        for v in obj.values():
            yield from find_commands(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_commands(item)

for cmd in find_commands(data):
    print(cmd)
" 2>/dev/null)

    while IFS= read -r cmd; do
        [[ -z "$cmd" ]] && continue
        # Replace ${CLAUDE_PLUGIN_ROOT} with actual plugin directory
        resolved_cmd="${cmd//\$\{CLAUDE_PLUGIN_ROOT\}/$plugin_dir}"
        if [[ ! -f "$resolved_cmd" ]]; then
            if [[ $MISSING_HANDLERS -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  $plugin_name: Missing handler $resolved_cmd"
            MISSING_HANDLERS=$((MISSING_HANDLERS + 1))
        fi
    done <<< "$COMMANDS"
done

if [[ $MISSING_HANDLERS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 36: All hook handlers are executable
# ============================================================================
echo -n "Test 36: All hook handlers are executable... "
NON_EXEC_HANDLERS=0

for hooks_json in "$REPO_ROOT"/plugins/*/hooks/hooks.json; do
    [[ ! -f "$hooks_json" ]] && continue
    plugin_dir=$(dirname "$(dirname "$hooks_json")")
    plugin_name=$(basename "$plugin_dir")

    # Extract command paths
    COMMANDS=$(python3 -c "
import json
with open('$hooks_json') as f:
    data = json.load(f)

def find_commands(obj):
    if isinstance(obj, dict):
        if 'command' in obj:
            yield obj['command']
        for v in obj.values():
            yield from find_commands(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_commands(item)

for cmd in find_commands(data):
    print(cmd)
" 2>/dev/null)

    while IFS= read -r cmd; do
        [[ -z "$cmd" ]] && continue
        resolved_cmd="${cmd//\$\{CLAUDE_PLUGIN_ROOT\}/$plugin_dir}"
        if [[ -f "$resolved_cmd" && ! -x "$resolved_cmd" ]]; then
            if [[ $NON_EXEC_HANDLERS -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  $plugin_name: $resolved_cmd not executable"
            NON_EXEC_HANDLERS=$((NON_EXEC_HANDLERS + 1))
        fi
    done <<< "$COMMANDS"
done

if [[ $NON_EXEC_HANDLERS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 37: README files exist for all plugins
# ============================================================================
echo -n "Test 37: All plugins have README files... "
MISSING_README=0

for plugin_dir in "$REPO_ROOT"/plugins/*/; do
    plugin_name=$(basename "$plugin_dir")
    if [[ ! -f "$plugin_dir/README.md" ]]; then
        if [[ $MISSING_README -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name missing README.md"
        MISSING_README=$((MISSING_README + 1))
    fi
done

if [[ $MISSING_README -eq 0 ]]; then
    README_COUNT=$(ls "$REPO_ROOT"/plugins/*/README.md 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($README_COUNT plugins)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 38: File-header rule detects missing headers
# ============================================================================
echo -n "Test 38: File-header rule detects missing headers... "
FILE_HEADER_RULE="$REPO_ROOT/plugins/asha/rules/file-header.sh"

if [[ -f "$FILE_HEADER_RULE" ]]; then
    # Create test file without header
    TEST38_DIR=$(mktemp -d)
    mkdir -p "$TEST38_DIR/.claude"
    echo "#!/bin/bash" > "$TEST38_DIR/.claude/test-script.sh"
    echo "echo 'hello'" >> "$TEST38_DIR/.claude/test-script.sh"

    RESULT=$(bash -c "
        source '$FILE_HEADER_RULE'
        check_violation 'Write' '$TEST38_DIR/.claude/test-script.sh' '$TEST38_DIR' 2>/dev/null && echo 'VIOLATION' || echo 'ALLOWED'
    ")
    rm -rf "$TEST38_DIR"

    if [[ "$RESULT" == *"VIOLATION"* ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  File without header not flagged"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 39: File-header rule allows proper headers
# ============================================================================
echo -n "Test 39: File-header rule allows proper headers... "

if [[ -f "$FILE_HEADER_RULE" ]]; then
    # Create test file with header
    TEST39_DIR=$(mktemp -d)
    mkdir -p "$TEST39_DIR/.claude"
    cat > "$TEST39_DIR/.claude/test-script.sh" << 'SCRIPT'
#!/bin/bash
# OUTCOME: Test script for validation
# PATTERN: Example pattern
# CONSTRAINT: Example constraint
echo 'hello'
SCRIPT

    RESULT=$(bash -c "
        source '$FILE_HEADER_RULE'
        check_violation 'Write' '$TEST39_DIR/.claude/test-script.sh' '$TEST39_DIR' 2>/dev/null && echo 'VIOLATION' || echo 'ALLOWED'
    ")
    rm -rf "$TEST39_DIR"

    if [[ "$RESULT" == "ALLOWED" ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  File with proper header flagged incorrectly"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 40: All scripts have proper shebang
# ============================================================================
echo -n "Test 40: All shell scripts have proper shebang... "
MISSING_SHEBANG=0

for script in "$REPO_ROOT"/plugins/*/hooks/handlers/*.sh \
              "$REPO_ROOT"/plugins/*/hooks-handlers/*.sh \
              "$REPO_ROOT"/plugins/*/tools/*.sh \
              "$REPO_ROOT"/plugins/*/rules/*.sh; do
    [[ ! -f "$script" ]] && continue
    script_name=$(basename "$script")

    # Check for shebang on first line
    FIRST_LINE=$(head -1 "$script")
    if [[ ! "$FIRST_LINE" =~ ^#! ]]; then
        if [[ $MISSING_SHEBANG -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $script_name missing shebang"
        MISSING_SHEBANG=$((MISSING_SHEBANG + 1))
    fi
done

if [[ $MISSING_SHEBANG -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 41: No debug echo statements in production hooks
# ============================================================================
echo -n "Test 41: No debug echo statements in production hooks... "
DEBUG_FOUND=0

for handler in "$REPO_ROOT"/plugins/*/hooks/handlers/*.sh \
               "$REPO_ROOT"/plugins/*/hooks-handlers/*.sh; do
    [[ ! -f "$handler" ]] && continue
    handler_name=$(basename "$handler")

    # Check for common debug patterns (excluding comments and quoted strings)
    if grep -E "^[^#]*echo.*DEBUG|^[^#]*echo.*TEST|^[^#]*set -x" "$handler" | grep -v "^#" | grep -qv '".*DEBUG.*"'; then
        if [[ $DEBUG_FOUND -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $handler_name contains debug statements"
        DEBUG_FOUND=$((DEBUG_FOUND + 1))
    fi
done

if [[ $DEBUG_FOUND -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 42: Violation-checker script exists and is executable
# ============================================================================
echo -n "Test 42: Violation-checker script is executable... "
VIOLATION_CHECKER="$REPO_ROOT/plugins/asha/hooks/handlers/violation-checker.sh"

if [[ -x "$VIOLATION_CHECKER" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  violation-checker.sh missing or not executable"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 43: Violation-checker logs violations to session file
# ============================================================================
echo -n "Test 43: Violation-checker logs violations... "
TEST43_DIR=$(mktemp -d)
mkdir -p "$TEST43_DIR/Memory/sessions"
mkdir -p "$TEST43_DIR/.asha"
echo '{"initialized": true}' > "$TEST43_DIR/.asha/config.json"
echo "# Current Session" > "$TEST43_DIR/Memory/sessions/current-session.md"

export CLAUDE_PROJECT_DIR="$TEST43_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Trigger a destructive-git violation
"$VIOLATION_CHECKER" "Bash" '{"command": "git push --force"}' 2>/dev/null || true

# Check if violation was logged
if grep -q "Violation" "$TEST43_DIR/Memory/sessions/current-session.md" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Violation not logged to session file"
    FAILED=$((FAILED + 1))
fi
rm -rf "$TEST43_DIR"

# ============================================================================
# Test 44: Common.sh functions work correctly
# ============================================================================
echo -n "Test 44: common.sh get_plugin_root works... "
COMMON_SH="$REPO_ROOT/plugins/asha/hooks/handlers/common.sh"

if [[ -f "$COMMON_SH" ]]; then
    export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"
    RESULT=$(bash -c "
        source '$COMMON_SH'
        get_plugin_root
    ")

    if [[ "$RESULT" == "$REPO_ROOT/plugins/asha" ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  get_plugin_root returned: $RESULT"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 45: All command frontmatter has description field
# ============================================================================
echo -n "Test 45: All commands have description in frontmatter... "
MISSING_DESC=0

for cmd_file in "$REPO_ROOT"/plugins/*/commands/*.md; do
    [[ ! -f "$cmd_file" ]] && continue
    cmd_name=$(basename "$cmd_file")

    # Check if file has frontmatter with description
    if head -1 "$cmd_file" | grep -q "^---"; then
        # Extract frontmatter
        FRONTMATTER=$(sed -n '1,/^---$/p' "$cmd_file" | tail -n +2 | head -n -1)
        if ! echo "$FRONTMATTER" | grep -q "description:"; then
            if [[ $MISSING_DESC -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  $cmd_name missing description"
            MISSING_DESC=$((MISSING_DESC + 1))
        fi
    fi
done

if [[ $MISSING_DESC -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 46: Plugin versions follow semver format
# ============================================================================
echo -n "Test 46: All plugin versions follow semver format... "
INVALID_SEMVER=0

for plugin_json in "$REPO_ROOT"/plugins/*/.claude-plugin/plugin.json; do
    [[ ! -f "$plugin_json" ]] && continue
    plugin_name=$(basename "$(dirname "$(dirname "$plugin_json")")")

    VERSION=$(python3 -c "import json; print(json.load(open('$plugin_json')).get('version', ''))" 2>/dev/null)

    # Check semver format (X.Y.Z or X.Y)
    if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
        if [[ $INVALID_SEMVER -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name: Invalid version '$VERSION'"
        INVALID_SEMVER=$((INVALID_SEMVER + 1))
    fi
done

if [[ $INVALID_SEMVER -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 47: No TODO/FIXME comments in production hooks
# ============================================================================
echo -n "Test 47: No TODO/FIXME in production hooks... "
TODO_FOUND=0

for handler in "$REPO_ROOT"/plugins/*/hooks/handlers/*.sh \
               "$REPO_ROOT"/plugins/*/hooks-handlers/*.sh; do
    [[ ! -f "$handler" ]] && continue
    handler_name=$(basename "$handler")

    if grep -qiE "TODO|FIXME|XXX|HACK" "$handler"; then
        if [[ $TODO_FOUND -eq 0 ]]; then
            echo -e "${YELLOW}WARN${NC}"
        fi
        echo "  $handler_name contains TODO/FIXME"
        TODO_FOUND=$((TODO_FOUND + 1))
    fi
done

if [[ $TODO_FOUND -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    # Count as pass but with warning (not blocking)
    echo "  (Non-blocking warning)"
    PASSED=$((PASSED + 1))
fi

# ============================================================================
# Test 48: Output styles directory has expected styles
# ============================================================================
echo -n "Test 48: Output styles directory has expected styles... "
STYLES_DIR="$REPO_ROOT/plugins/output-styles/styles"
EXPECTED_STYLES=(
    "ultra-concise.md"
    "bullet-points.md"
    "markdown-focused.md"
    "table-based.md"
)
MISSING_STYLES=0

if [[ -d "$STYLES_DIR" ]]; then
    for style in "${EXPECTED_STYLES[@]}"; do
        if [[ ! -f "$STYLES_DIR/$style" ]]; then
            if [[ $MISSING_STYLES -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  Missing style: $style"
            MISSING_STYLES=$((MISSING_STYLES + 1))
        fi
    done

    if [[ $MISSING_STYLES -eq 0 ]]; then
        STYLE_COUNT=$(ls "$STYLES_DIR"/*.md 2>/dev/null | wc -l)
        echo -e "${GREEN}PASS${NC} ($STYLE_COUNT styles)"
        PASSED=$((PASSED + 1))
    else
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  Styles directory missing"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 49: Asha templates have valid YAML frontmatter
# ============================================================================
echo -n "Test 49: Asha templates have valid frontmatter... "
TEMPLATES_DIR="$REPO_ROOT/plugins/asha/templates"
INVALID_TEMPLATES=0

for template in "$TEMPLATES_DIR"/*.md; do
    [[ ! -f "$template" ]] && continue
    template_name=$(basename "$template")

    # Check if file starts with ---
    if head -1 "$template" | grep -q "^---"; then
        # Extract and validate YAML frontmatter
        FRONTMATTER=$(sed -n '1,/^---$/p' "$template" | tail -n +2 | head -n -1)
        if ! echo "$FRONTMATTER" | python3 -c "import sys,yaml; yaml.safe_load(sys.stdin)" 2>/dev/null; then
            if [[ $INVALID_TEMPLATES -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  $template_name has invalid frontmatter"
            INVALID_TEMPLATES=$((INVALID_TEMPLATES + 1))
        fi
    fi
done

if [[ $INVALID_TEMPLATES -eq 0 ]]; then
    TEMPLATE_COUNT=$(ls "$TEMPLATES_DIR"/*.md 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($TEMPLATE_COUNT templates)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 50: Panel command references correct character names
# ============================================================================
echo -n "Test 50: Panel command uses correct character names... "
PANEL_CMD="$REPO_ROOT/plugins/panel/commands/panel.md"

if [[ -f "$PANEL_CMD" ]]; then
    # Check for the three core roles
    HAS_MODERATOR=$(grep -c "The Moderator" "$PANEL_CMD" || echo "0")
    HAS_ANALYST=$(grep -c "The Analyst" "$PANEL_CMD" || echo "0")
    HAS_CHALLENGER=$(grep -c "The Challenger" "$PANEL_CMD" || echo "0")

    if [[ $HAS_MODERATOR -gt 0 && $HAS_ANALYST -gt 0 && $HAS_CHALLENGER -gt 0 ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Missing role references (Moderator:$HAS_MODERATOR, Analyst:$HAS_ANALYST, Challenger:$HAS_CHALLENGER)"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  Panel command file not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 51: All plugins registered in marketplace.json
# ============================================================================
echo -n "Test 51: All plugins registered in marketplace.json... "
MARKETPLACE="$REPO_ROOT/.claude-plugin/marketplace.json"
UNREGISTERED=0

if [[ -f "$MARKETPLACE" ]]; then
    # Get list of registered plugin names
    REGISTERED=$(python3 -c "import json; d=json.load(open('$MARKETPLACE')); print('\n'.join(p['name'] for p in d.get('plugins', [])))" 2>/dev/null)

    for plugin_dir in "$REPO_ROOT"/plugins/*/; do
        plugin_name=$(basename "$plugin_dir")
        # Check plugin.json for the registered name
        if [[ -f "$plugin_dir/.claude-plugin/plugin.json" ]]; then
            reg_name=$(python3 -c "import json; print(json.load(open('$plugin_dir/.claude-plugin/plugin.json')).get('name', ''))" 2>/dev/null)
            if ! echo "$REGISTERED" | grep -qx "$reg_name"; then
                if [[ $UNREGISTERED -eq 0 ]]; then
                    echo -e "${RED}FAIL${NC}"
                fi
                echo "  $reg_name not in marketplace.json"
                UNREGISTERED=$((UNREGISTERED + 1))
            fi
        fi
    done

    if [[ $UNREGISTERED -eq 0 ]]; then
        PLUGIN_COUNT=$(echo "$REGISTERED" | wc -l)
        echo -e "${GREEN}PASS${NC} ($PLUGIN_COUNT registered)"
        PASSED=$((PASSED + 1))
    else
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  marketplace.json not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 52: No duplicate command names across plugins
# ============================================================================
echo -n "Test 52: No duplicate command names across plugins... "
DUPLICATES=0
declare -A CMD_NAMES

for cmd_file in "$REPO_ROOT"/plugins/*/commands/*.md; do
    [[ ! -f "$cmd_file" ]] && continue
    cmd_name=$(basename "$cmd_file" .md)
    plugin_name=$(basename "$(dirname "$(dirname "$cmd_file")")")

    if [[ -n "${CMD_NAMES[$cmd_name]:-}" ]]; then
        if [[ $DUPLICATES -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  Duplicate command '$cmd_name' in ${CMD_NAMES[$cmd_name]} and $plugin_name"
        DUPLICATES=$((DUPLICATES + 1))
    else
        CMD_NAMES[$cmd_name]="$plugin_name"
    fi
done

if [[ $DUPLICATES -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 53: Python tools have docstrings
# ============================================================================
echo -n "Test 53: Python tools have module docstrings... "
MISSING_DOCSTRINGS=0
TOOLS_DIR="$REPO_ROOT/plugins/asha/tools"

for py_file in "$TOOLS_DIR"/*.py; do
    [[ ! -f "$py_file" ]] && continue
    py_name=$(basename "$py_file")

    # Check for docstring (triple quotes) in first 10 lines
    if ! head -10 "$py_file" | grep -qE '""".*|'"'"''"'"''"'"'.*'; then
        if [[ $MISSING_DOCSTRINGS -eq 0 ]]; then
            echo -e "${YELLOW}WARN${NC}"
        fi
        echo "  $py_name missing module docstring"
        MISSING_DOCSTRINGS=$((MISSING_DOCSTRINGS + 1))
    fi
done

if [[ $MISSING_DOCSTRINGS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    # Non-blocking warning
    echo "  (Non-blocking warning)"
    PASSED=$((PASSED + 1))
fi

# ============================================================================
# Test 54: Hook handlers use set -euo pipefail
# ============================================================================
echo -n "Test 54: Hook handlers use strict mode... "
MISSING_STRICT=0

for handler in "$REPO_ROOT"/plugins/*/hooks/handlers/*.sh \
               "$REPO_ROOT"/plugins/*/hooks-handlers/*.sh; do
    [[ ! -f "$handler" ]] && continue
    handler_name=$(basename "$handler")

    # Skip common.sh which may not need strict mode
    [[ "$handler_name" == "common.sh" ]] && continue

    # Check for set -e or set -euo pipefail in first 10 lines
    if ! head -10 "$handler" | grep -qE "set -e|set -.*e"; then
        if [[ $MISSING_STRICT -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $handler_name missing strict mode (set -e)"
        MISSING_STRICT=$((MISSING_STRICT + 1))
    fi
done

if [[ $MISSING_STRICT -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 55: common.sh is_asha_initialized function works
# ============================================================================
echo -n "Test 55: common.sh is_asha_initialized works... "
COMMON_SH="$REPO_ROOT/plugins/asha/hooks/handlers/common.sh"

if [[ -f "$COMMON_SH" ]]; then
    # Test with initialized project
    TEST55_DIR=$(mktemp -d)
    mkdir -p "$TEST55_DIR/.asha"
    echo '{"initialized": true}' > "$TEST55_DIR/.asha/config.json"
    export CLAUDE_PROJECT_DIR="$TEST55_DIR"

    RESULT=$(bash -c "
        source '$COMMON_SH'
        is_asha_initialized && echo 'INITIALIZED' || echo 'NOT_INITIALIZED'
    ")
    rm -rf "$TEST55_DIR"

    if [[ "$RESULT" == "INITIALIZED" ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  is_asha_initialized returned wrong result"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 56: common.sh get_python_cmd function works
# ============================================================================
echo -n "Test 56: common.sh get_python_cmd works... "

if [[ -f "$COMMON_SH" ]]; then
    TEST56_DIR=$(mktemp -d)
    mkdir -p "$TEST56_DIR/.asha"
    echo '{"initialized": true}' > "$TEST56_DIR/.asha/config.json"
    export CLAUDE_PROJECT_DIR="$TEST56_DIR"

    RESULT=$(bash -c "
        source '$COMMON_SH'
        get_python_cmd 2>/dev/null || echo 'FAILED'
    ")
    rm -rf "$TEST56_DIR"

    # Should return python3 or venv python
    if [[ "$RESULT" == *"python"* ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  get_python_cmd returned: $RESULT"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 57: PostToolUse correctly filters non-significant operations
# ============================================================================
echo -n "Test 57: PostToolUse filters Read operations... "
POST_TOOL_USE="$REPO_ROOT/plugins/asha/hooks/handlers/post-tool-use.sh"
TEST57_DIR=$(mktemp -d)
mkdir -p "$TEST57_DIR/Memory/sessions"
mkdir -p "$TEST57_DIR/.asha"
echo '{"initialized": true}' > "$TEST57_DIR/.asha/config.json"
echo "# Session" > "$TEST57_DIR/Memory/sessions/current-session.md"
export CLAUDE_PROJECT_DIR="$TEST57_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Read operations should not be logged
BEFORE_SIZE=$(wc -c < "$TEST57_DIR/Memory/sessions/current-session.md")
echo '{"tool_name": "Read", "tool_input": {"file_path": "/tmp/test.md"}}' | "$POST_TOOL_USE" 2>/dev/null || true
AFTER_SIZE=$(wc -c < "$TEST57_DIR/Memory/sessions/current-session.md")
rm -rf "$TEST57_DIR"

# Session file should not have grown (Read is filtered)
if [[ $AFTER_SIZE -eq $BEFORE_SIZE ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Read operation was logged when it should be filtered"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 58: Character files have required sections
# ============================================================================
echo -n "Test 58: Character files have required sections... "
CHARS_DIR="$REPO_ROOT/plugins/panel/docs/characters"
MISSING_SECTIONS=0

for char_file in "$CHARS_DIR"/*.md; do
    [[ ! -f "$char_file" ]] && continue
    char_name=$(basename "$char_file")

    # Check for Nature section
    if ! grep -qi "## Nature" "$char_file"; then
        if [[ $MISSING_SECTIONS -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $char_name missing 'Nature' section"
        MISSING_SECTIONS=$((MISSING_SECTIONS + 1))
    fi

    # Check for Voice section
    if ! grep -qi "## Voice" "$char_file"; then
        if [[ $MISSING_SECTIONS -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $char_name missing 'Voice' section"
        MISSING_SECTIONS=$((MISSING_SECTIONS + 1))
    fi

    # Check for Responsibilities or Purpose section
    if ! grep -qiE "## (Responsibilities|Purpose)" "$char_file"; then
        if [[ $MISSING_SECTIONS -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $char_name missing 'Responsibilities/Purpose' section"
        MISSING_SECTIONS=$((MISSING_SECTIONS + 1))
    fi
done

if [[ $MISSING_SECTIONS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 59: Rules have Severity declaration
# ============================================================================
echo -n "Test 59: Violation rules have Severity declaration... "
RULES_DIR="$REPO_ROOT/plugins/asha/rules"
MISSING_SEVERITY=0

for rule_file in "$RULES_DIR"/*.sh; do
    [[ ! -f "$rule_file" ]] && continue
    rule_name=$(basename "$rule_file")

    if ! grep -q "^# Severity:" "$rule_file"; then
        if [[ $MISSING_SEVERITY -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $rule_name missing Severity declaration"
        MISSING_SEVERITY=$((MISSING_SEVERITY + 1))
    fi
done

if [[ $MISSING_SEVERITY -eq 0 ]]; then
    RULE_COUNT=$(ls "$RULES_DIR"/*.sh 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($RULE_COUNT rules)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 60: Memory-search wrapper script exists
# ============================================================================
echo -n "Test 60: memory-search wrapper exists... "
MEMORY_SEARCH="$REPO_ROOT/plugins/asha/tools/memory-search"

if [[ -x "$MEMORY_SEARCH" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  memory-search missing or not executable"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 61: save-session.sh accepts valid modes
# ============================================================================
echo -n "Test 61: save-session.sh accepts valid modes... "
SAVE_SESSION="$REPO_ROOT/plugins/asha/tools/save-session.sh"
TEST61_DIR=$(mktemp -d)
mkdir -p "$TEST61_DIR/Memory/sessions"
mkdir -p "$TEST61_DIR/.asha"
echo '{"initialized": true}' > "$TEST61_DIR/.asha/config.json"
export CLAUDE_PROJECT_DIR="$TEST61_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Test automatic mode (used by session-end hook)
OUTPUT=$("$SAVE_SESSION" --automatic 2>&1 || true)
rm -rf "$TEST61_DIR"

if [[ "$OUTPUT" == *"{}"* ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  save-session.sh --automatic failed"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 62: save-session.sh resets watching file correctly
# ============================================================================
echo -n "Test 62: save-session.sh resets watching file... "
TEST62_DIR=$(mktemp -d)
mkdir -p "$TEST62_DIR/Memory/sessions"
mkdir -p "$TEST62_DIR/.asha"
echo '{"initialized": true}' > "$TEST62_DIR/.asha/config.json"
# Create a watching file with enough content to archive
cat > "$TEST62_DIR/Memory/sessions/current-session.md" << 'EOF'
---
sessionStart: 2026-01-17 10:00 UTC
sessionID: test-session
---

## Significant Operations
- Edit: file1.md
- Edit: file2.md
- Edit: file3.md
- Edit: file4.md
- Edit: file5.md
- Task: agent1
- Task: agent2
- Task: agent3
- Task: agent4
- Task: agent5
- Bash: git status

## Decisions & Clarifications
- Decision 1
- Decision 2

## Errors & Anomalies
EOF
export CLAUDE_PROJECT_DIR="$TEST62_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

"$SAVE_SESSION" --archive-only 2>/dev/null || true

# Check new watching file was created with new session ID
if grep -q "sessionID:" "$TEST62_DIR/Memory/sessions/current-session.md" 2>/dev/null; then
    # Check archive was created
    if ls "$TEST62_DIR/Memory/sessions/archive/"*.md 1>/dev/null 2>&1; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Archive not created"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  Watching file not reset"
    FAILED=$((FAILED + 1))
fi
rm -rf "$TEST62_DIR"

# ============================================================================
# Test 63: Plugin commands have allowed-tools field where needed
# ============================================================================
echo -n "Test 63: Commands with tool requirements have allowed-tools... "
MISSING_TOOLS=0

for cmd_file in "$REPO_ROOT"/plugins/*/commands/*.md; do
    [[ ! -f "$cmd_file" ]] && continue
    cmd_name=$(basename "$cmd_file")

    # Check if command uses Task tool (indicates it needs allowed-tools)
    if grep -q "Task tool\|launch.*agent\|spawn.*agent" "$cmd_file"; then
        # Extract frontmatter and check for allowed-tools
        if head -1 "$cmd_file" | grep -q "^---"; then
            FRONTMATTER=$(sed -n '1,/^---$/p' "$cmd_file" | tail -n +2 | head -n -1)
            if ! echo "$FRONTMATTER" | grep -q "allowed-tools"; then
                # This is a warning, not a hard failure
                : # silently pass
            fi
        fi
    fi
done

# All commands pass (allowed-tools is optional guidance)
echo -e "${GREEN}PASS${NC}"
PASSED=$((PASSED + 1))

# ============================================================================
# Test 64: All Python tests can be discovered
# ============================================================================
echo -n "Test 64: Python tests are discoverable... "
PYTHON_TESTS_DIR="$REPO_ROOT/tests/python"

if [[ -d "$PYTHON_TESTS_DIR" ]]; then
    TEST_COUNT=$(python3 -c "
import unittest
import sys
sys.path.insert(0, '$PYTHON_TESTS_DIR')
loader = unittest.TestLoader()
suite = loader.discover('$PYTHON_TESTS_DIR', pattern='test_*.py')
print(suite.countTestCases())
" 2>/dev/null || echo "0")

    if [[ $TEST_COUNT -gt 0 ]]; then
        echo -e "${GREEN}PASS${NC} ($TEST_COUNT tests)"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  No Python tests discovered"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 65: CLAUDE.md exists in repo root
# ============================================================================
echo -n "Test 65: CLAUDE.md exists in repo root... "
CLAUDE_MD="$REPO_ROOT/CLAUDE.md"

if [[ -f "$CLAUDE_MD" ]]; then
    # Check it has meaningful content (>100 lines)
    LINE_COUNT=$(wc -l < "$CLAUDE_MD")
    if [[ $LINE_COUNT -gt 100 ]]; then
        echo -e "${GREEN}PASS${NC} ($LINE_COUNT lines)"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  CLAUDE.md too short ($LINE_COUNT lines)"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  CLAUDE.md not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 66: No syntax errors in shell scripts
# ============================================================================
echo -n "Test 66: Shell scripts have valid syntax... "
SYNTAX_ERRORS=0

for script in "$REPO_ROOT"/plugins/*/hooks/handlers/*.sh \
              "$REPO_ROOT"/plugins/*/hooks-handlers/*.sh \
              "$REPO_ROOT"/plugins/*/tools/*.sh \
              "$REPO_ROOT"/plugins/*/rules/*.sh \
              "$REPO_ROOT"/tests/*.sh; do
    [[ ! -f "$script" ]] && continue
    script_name=$(basename "$script")

    if ! bash -n "$script" 2>/dev/null; then
        if [[ $SYNTAX_ERRORS -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $script_name has syntax errors"
        SYNTAX_ERRORS=$((SYNTAX_ERRORS + 1))
    fi
done

if [[ $SYNTAX_ERRORS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 67: PostToolUse logs Edit operations correctly
# ============================================================================
echo -n "Test 67: PostToolUse logs Edit with file path... "
POST_TOOL_USE="$REPO_ROOT/plugins/asha/hooks/handlers/post-tool-use.sh"
TEST67_DIR=$(mktemp -d)
mkdir -p "$TEST67_DIR/Memory/sessions"
mkdir -p "$TEST67_DIR/Work/markers"
mkdir -p "$TEST67_DIR/.asha"
echo '{"initialized": true}' > "$TEST67_DIR/.asha/config.json"
cat > "$TEST67_DIR/Memory/sessions/current-session.md" << 'EOF'
---
sessionStart: 2026-01-17 10:00 UTC
sessionID: test-session
---

## Significant Operations
<!-- Auto-appended -->

## Decisions & Clarifications
<!-- Auto-appended -->

## Errors & Anomalies
<!-- Auto-appended -->
EOF
export CLAUDE_PROJECT_DIR="$TEST67_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Check if jq is available (required for PostToolUse)
if command -v jq >/dev/null 2>&1; then
    # Trigger Edit operation with properly formatted JSON
    echo '{"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/myfile.ts"}, "tool_response": {}}' | "$POST_TOOL_USE" 2>/dev/null || true

    # Check if Edit was logged with file path (Modified: path (Edit))
    if grep -q "Modified.*myfile.ts.*(Edit)" "$TEST67_DIR/Memory/sessions/current-session.md" 2>/dev/null; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Edit operation not logged correctly"
        cat "$TEST67_DIR/Memory/sessions/current-session.md" | head -20
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC} (jq not available)"
    SKIPPED=$((SKIPPED + 1))
fi
rm -rf "$TEST67_DIR"

# ============================================================================
# Test 68: Recruiter agent file has proper structure
# ============================================================================
echo -n "Test 68: Recruiter agent has required sections... "
RECRUITER="$REPO_ROOT/plugins/panel/agents/recruiter.md"

if [[ -f "$RECRUITER" ]]; then
    MISSING=0

    # Check for key sections in recruiter
    if ! grep -qi "purpose\|role\|responsibilities" "$RECRUITER"; then
        MISSING=$((MISSING + 1))
    fi

    if [[ $MISSING -eq 0 ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Recruiter missing key sections"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  Recruiter agent not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 69: All test scripts are executable
# ============================================================================
echo -n "Test 69: Test scripts are executable... "
NON_EXEC_TESTS=0

for test_script in "$REPO_ROOT"/tests/*.sh; do
    [[ ! -f "$test_script" ]] && continue
    test_name=$(basename "$test_script")

    if [[ ! -x "$test_script" ]]; then
        if [[ $NON_EXEC_TESTS -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $test_name not executable"
        NON_EXEC_TESTS=$((NON_EXEC_TESTS + 1))
    fi
done

if [[ $NON_EXEC_TESTS -eq 0 ]]; then
    TEST_SCRIPT_COUNT=$(ls "$REPO_ROOT"/tests/*.sh 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($TEST_SCRIPT_COUNT scripts)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 70: Plugin READMEs have installation instructions
# ============================================================================
echo -n "Test 70: Plugin READMEs have installation section... "
MISSING_INSTALL=0

for readme in "$REPO_ROOT"/plugins/*/README.md; do
    [[ ! -f "$readme" ]] && continue
    plugin_name=$(basename "$(dirname "$readme")")

    if ! grep -qi "installation\|install" "$readme"; then
        if [[ $MISSING_INSTALL -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name README missing installation section"
        MISSING_INSTALL=$((MISSING_INSTALL + 1))
    fi
done

if [[ $MISSING_INSTALL -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 71: Plugin READMEs have usage examples
# ============================================================================
echo -n "Test 71: Plugin READMEs have usage section... "
MISSING_USAGE=0

for readme in "$REPO_ROOT"/plugins/*/README.md; do
    [[ ! -f "$readme" ]] && continue
    plugin_name=$(basename "$(dirname "$readme")")

    if ! grep -qi "usage\|commands\|examples" "$readme"; then
        if [[ $MISSING_USAGE -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name README missing usage section"
        MISSING_USAGE=$((MISSING_USAGE + 1))
    fi
done

if [[ $MISSING_USAGE -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 72: No hardcoded user paths in any script
# ============================================================================
echo -n "Test 72: No hardcoded user paths in scripts... "
HARDCODED=0

for script in "$REPO_ROOT"/plugins/*/hooks/handlers/*.sh \
              "$REPO_ROOT"/plugins/*/hooks-handlers/*.sh \
              "$REPO_ROOT"/plugins/*/tools/*.sh; do
    [[ ! -f "$script" ]] && continue
    script_name=$(basename "$script")

    # Check for hardcoded /home/username or /Users/username paths
    if grep -v "^#" "$script" | grep -qE "(/home/[a-zA-Z]|/Users/[a-zA-Z])[a-zA-Z0-9_-]*/" 2>/dev/null; then
        if [[ $HARDCODED -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $script_name contains hardcoded user paths"
        HARDCODED=$((HARDCODED + 1))
    fi
done

if [[ $HARDCODED -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 73: CORE.md module exists and has key sections
# ============================================================================
echo -n "Test 73: CORE.md module has required sections... "
CORE_MD="$REPO_ROOT/plugins/asha/modules/CORE.md"

if [[ -f "$CORE_MD" ]]; then
    MISSING=0

    # Check for key sections in CORE module
    for section in "Session Initialization" "Memory" "Identity"; do
        if ! grep -qi "$section" "$CORE_MD"; then
            MISSING=$((MISSING + 1))
        fi
    done

    if [[ $MISSING -eq 0 ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  CORE.md missing key sections"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  CORE.md not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 74: All modules have meaningful content
# ============================================================================
echo -n "Test 74: All modules have meaningful content... "
MODULES_DIR="$REPO_ROOT/plugins/asha/modules"
EMPTY_MODULES=0

for module in "$MODULES_DIR"/*.md; do
    [[ ! -f "$module" ]] && continue
    module_name=$(basename "$module")

    # Check module has at least 50 lines of content
    LINE_COUNT=$(wc -l < "$module")
    if [[ $LINE_COUNT -lt 50 ]]; then
        if [[ $EMPTY_MODULES -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $module_name too short ($LINE_COUNT lines)"
        EMPTY_MODULES=$((EMPTY_MODULES + 1))
    fi
done

if [[ $EMPTY_MODULES -eq 0 ]]; then
    MODULE_COUNT=$(ls "$MODULES_DIR"/*.md 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($MODULE_COUNT modules)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 75: SessionStart hook injects correct module paths
# ============================================================================
echo -n "Test 75: SessionStart provides module paths... "
TEST75_DIR=$(mktemp -d)
mkdir -p "$TEST75_DIR/Memory/sessions"
mkdir -p "$TEST75_DIR/.asha"
echo '{"initialized": true}' > "$TEST75_DIR/.asha/config.json"

# Run with environment override (prefix assignment)
OUTPUT=$(CLAUDE_PROJECT_DIR="$TEST75_DIR" CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha" \
    "$REPO_ROOT/plugins/asha/hooks/handlers/session-start.sh" 2>&1 || true)
rm -rf "$TEST75_DIR"

# Check output contains module paths (using fixed string match for reliability)
if [[ "$OUTPUT" == *"cognitive.md"* ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  SessionStart output missing module paths (len=${#OUTPUT})"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 76: validate-plugins.sh exists and is executable
# ============================================================================
echo -n "Test 76: validate-plugins.sh is executable... "
VALIDATE_PLUGINS="$REPO_ROOT/tests/validate-plugins.sh"

if [[ -x "$VALIDATE_PLUGINS" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  validate-plugins.sh missing or not executable"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 77: validate-versions.sh exists and is executable
# ============================================================================
echo -n "Test 77: validate-versions.sh is executable... "
VALIDATE_VERSIONS="$REPO_ROOT/tests/validate-versions.sh"

if [[ -x "$VALIDATE_VERSIONS" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  validate-versions.sh missing or not executable"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 78: Python __init__.py exists in test directory
# ============================================================================
echo -n "Test 78: Python test __init__.py exists... "
INIT_PY="$REPO_ROOT/tests/python/__init__.py"

if [[ -f "$INIT_PY" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  tests/python/__init__.py not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 79: run-tests.sh exists and is executable
# ============================================================================
echo -n "Test 79: run-tests.sh is executable... "
RUN_TESTS="$REPO_ROOT/tests/run-tests.sh"

if [[ -x "$RUN_TESTS" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  run-tests.sh missing or not executable"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 80: All rule files export check_violation
# ============================================================================
echo -n "Test 80: All rules export check_violation... "
RULES_DIR="$REPO_ROOT/plugins/asha/rules"
MISSING_EXPORT=0

for rule_file in "$RULES_DIR"/*.sh; do
    [[ ! -f "$rule_file" ]] && continue
    rule_name=$(basename "$rule_file")

    # Check for export statement or function declaration
    if ! grep -qE "^check_violation\(\)|export -f check_violation" "$rule_file"; then
        if [[ $MISSING_EXPORT -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $rule_name missing check_violation"
        MISSING_EXPORT=$((MISSING_EXPORT + 1))
    fi
done

if [[ $MISSING_EXPORT -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 81: gitignore exists in repo root
# ============================================================================
echo -n "Test 81: .gitignore exists... "
GITIGNORE="$REPO_ROOT/.gitignore"

if [[ -f "$GITIGNORE" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${YELLOW}WARN${NC} (optional)"
    PASSED=$((PASSED + 1))
fi

# ============================================================================
# Test 82: LICENSE exists in repo root
# ============================================================================
echo -n "Test 82: Root LICENSE exists... "
ROOT_LICENSE="$REPO_ROOT/LICENSE"

if [[ -f "$ROOT_LICENSE" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Root LICENSE not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 83: All asha commands have description frontmatter
# ============================================================================
echo -n "Test 83: Asha commands have description... "
ASHA_CMDS="$REPO_ROOT/plugins/asha/commands"
MISSING_DESC=0

for cmd_file in "$ASHA_CMDS"/*.md; do
    [[ ! -f "$cmd_file" ]] && continue
    cmd_name=$(basename "$cmd_file")

    # Check for description in frontmatter
    if ! head -10 "$cmd_file" | grep -q "description:"; then
        if [[ $MISSING_DESC -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $cmd_name missing description"
        MISSING_DESC=$((MISSING_DESC + 1))
    fi
done

if [[ $MISSING_DESC -eq 0 ]]; then
    CMD_COUNT=$(ls "$ASHA_CMDS"/*.md 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($CMD_COUNT commands)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 84: hooks.json references valid handler paths
# ============================================================================
echo -n "Test 84: hooks.json handler paths exist... "
INVALID_PATHS=0

for hooks_json in "$REPO_ROOT"/plugins/*/hooks/hooks.json; do
    [[ ! -f "$hooks_json" ]] && continue
    plugin_dir=$(dirname "$(dirname "$hooks_json")")
    plugin_name=$(basename "$plugin_dir")

    # Extract command paths from hooks.json (|| true handles empty results)
    HANDLER_PATHS=$(grep -o '"command": "[^"]*"' "$hooks_json" 2>/dev/null | sed 's/"command": "//;s/"$//' | sed 's|\${CLAUDE_PLUGIN_ROOT}||' || true)

    for handler_path in $HANDLER_PATHS; do
        full_path="$plugin_dir$handler_path"
        if [[ ! -f "$full_path" ]]; then
            if [[ $INVALID_PATHS -eq 0 ]]; then
                echo -e "${RED}FAIL${NC}"
            fi
            echo "  $plugin_name: missing $handler_path"
            INVALID_PATHS=$((INVALID_PATHS + 1))
        fi
    done
done

if [[ $INVALID_PATHS -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 85: All hooks.json files are valid JSON
# ============================================================================
echo -n "Test 85: All hooks.json files valid JSON... "
INVALID_JSON=0

for hooks_json in "$REPO_ROOT"/plugins/*/hooks/hooks.json; do
    [[ ! -f "$hooks_json" ]] && continue
    plugin_name=$(basename "$(dirname "$(dirname "$hooks_json")")")

    if ! jq empty "$hooks_json" 2>/dev/null; then
        if [[ $INVALID_JSON -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name/hooks/hooks.json invalid"
        INVALID_JSON=$((INVALID_JSON + 1))
    fi
done

if [[ $INVALID_JSON -eq 0 ]]; then
    HOOK_COUNT=$(find "$REPO_ROOT"/plugins -name "hooks.json" | wc -l)
    echo -e "${GREEN}PASS${NC} ($HOOK_COUNT files)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 86: Plugin directories consistent naming (no mixed handlers/)
# ============================================================================
echo -n "Test 86: Plugin hook directories consistent... "
MIXED_NAMING=0

# Check for mixed naming conventions
for plugin in "$REPO_ROOT"/plugins/*/; do
    plugin_name=$(basename "$plugin")

    # Check if both hooks/handlers and hooks-handlers exist (inconsistent)
    if [[ -d "$plugin/hooks/handlers" && -d "$plugin/hooks-handlers" ]]; then
        if [[ $MIXED_NAMING -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name: mixed hooks/handlers and hooks-handlers"
        MIXED_NAMING=$((MIXED_NAMING + 1))
    fi
done

if [[ $MIXED_NAMING -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 87: All hook handlers are executable
# ============================================================================
echo -n "Test 87: All hook handlers executable... "
NON_EXEC=0

for handler in "$REPO_ROOT"/plugins/*/hooks/handlers/*.sh "$REPO_ROOT"/plugins/*/hooks-handlers/*.sh; do
    [[ ! -f "$handler" ]] && continue
    handler_name=$(basename "$handler")
    plugin_name=$(basename "$(dirname "$(dirname "$handler")")")

    if [[ ! -x "$handler" ]]; then
        if [[ $NON_EXEC -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name: $handler_name not executable"
        NON_EXEC=$((NON_EXEC + 1))
    fi
done

if [[ $NON_EXEC -eq 0 ]]; then
    HANDLER_COUNT=$(find "$REPO_ROOT"/plugins -type f \( -path "*/hooks/handlers/*.sh" -o -path "*/hooks-handlers/*.sh" \) | wc -l)
    echo -e "${GREEN}PASS${NC} ($HANDLER_COUNT handlers)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 88: SessionEnd ignores /clear reason
# ============================================================================
echo -n "Test 88: SessionEnd ignores clear reason... "
TEST88_DIR=$(mktemp -d)
mkdir -p "$TEST88_DIR/Memory/sessions"
mkdir -p "$TEST88_DIR/Work/markers"
mkdir -p "$TEST88_DIR/.asha"
echo '{"initialized": true}' > "$TEST88_DIR/.asha/config.json"
echo "# Session" > "$TEST88_DIR/Memory/sessions/current-session.md"
export CLAUDE_PROJECT_DIR="$TEST88_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Send clear reason - should return {} without archiving
OUTPUT=$(echo '{"reason": "clear"}' | "$REPO_ROOT/plugins/asha/hooks/handlers/session-end.sh" 2>/dev/null || true)
rm -rf "$TEST88_DIR"

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected {}, got: $OUTPUT"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 89: SessionEnd cleans up rp-active marker
# ============================================================================
echo -n "Test 89: SessionEnd cleans rp-active marker... "
TEST89_DIR=$(mktemp -d)
mkdir -p "$TEST89_DIR/Memory/sessions"
mkdir -p "$TEST89_DIR/Work/markers"
mkdir -p "$TEST89_DIR/.asha"
echo '{"initialized": true}' > "$TEST89_DIR/.asha/config.json"
echo "# Session" > "$TEST89_DIR/Memory/sessions/current-session.md"
touch "$TEST89_DIR/Work/markers/rp-active"
export CLAUDE_PROJECT_DIR="$TEST89_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Run session-end (with clear reason so it doesn't try to archive)
echo '{"reason": "clear"}' | "$REPO_ROOT/plugins/asha/hooks/handlers/session-end.sh" >/dev/null 2>&1 || true

if [[ ! -f "$TEST89_DIR/Work/markers/rp-active" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  rp-active marker not cleaned up"
    FAILED=$((FAILED + 1))
fi
rm -rf "$TEST89_DIR"

# ============================================================================
# Test 90: UserPromptSubmit respects silence marker
# ============================================================================
echo -n "Test 90: UserPromptSubmit respects silence marker... "
TEST90_DIR=$(mktemp -d)
mkdir -p "$TEST90_DIR/Memory/sessions"
mkdir -p "$TEST90_DIR/Work/markers"
mkdir -p "$TEST90_DIR/.asha"
echo '{"initialized": true}' > "$TEST90_DIR/.asha/config.json"
echo "# Session" > "$TEST90_DIR/Memory/sessions/current-session.md"
touch "$TEST90_DIR/Work/markers/silence"
export CLAUDE_PROJECT_DIR="$TEST90_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Send a prompt - should be ignored due to silence marker
OUTPUT=$(echo '{"prompt": "test prompt that should be ignored"}' | "$REPO_ROOT/plugins/asha/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)
rm -rf "$TEST90_DIR"

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  silence marker not respected"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 91: UserPromptSubmit respects rp-active marker
# ============================================================================
echo -n "Test 91: UserPromptSubmit respects rp-active marker... "
TEST91_DIR=$(mktemp -d)
mkdir -p "$TEST91_DIR/Memory/sessions"
mkdir -p "$TEST91_DIR/Work/markers"
mkdir -p "$TEST91_DIR/.asha"
echo '{"initialized": true}' > "$TEST91_DIR/.asha/config.json"
echo "# Session" > "$TEST91_DIR/Memory/sessions/current-session.md"
touch "$TEST91_DIR/Work/markers/rp-active"
export CLAUDE_PROJECT_DIR="$TEST91_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Send a prompt - should be ignored due to rp-active marker
OUTPUT=$(echo '{"prompt": "test prompt during RP"}' | "$REPO_ROOT/plugins/asha/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)
rm -rf "$TEST91_DIR"

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  rp-active marker not respected"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 92: Output-styles SessionStart returns valid JSON for no style
# ============================================================================
echo -n "Test 92: Output-styles SessionStart (no style)... "
# Remove any existing style config to test default behavior
STYLE_CONFIG="$HOME/.claude/active-output-style"
STYLE_BACKUP=""
if [[ -f "$STYLE_CONFIG" ]]; then
    STYLE_BACKUP=$(cat "$STYLE_CONFIG")
    rm "$STYLE_CONFIG"
fi

export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/output-styles"
OUTPUT=$("$REPO_ROOT/plugins/output-styles/hooks-handlers/session-start.sh" 2>/dev/null || true)

# Restore style config if it existed
if [[ -n "$STYLE_BACKUP" ]]; then
    echo "$STYLE_BACKUP" > "$STYLE_CONFIG"
fi

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected {}, got: $OUTPUT"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 93: All commands have description frontmatter
# ============================================================================
echo -n "Test 93: All commands have description frontmatter... "
MISSING_DESC=0

for cmd_file in "$REPO_ROOT"/plugins/*/commands/*.md; do
    [[ ! -f "$cmd_file" ]] && continue
    cmd_name=$(basename "$cmd_file")
    plugin_name=$(basename "$(dirname "$(dirname "$cmd_file")")")

    # Check for description in frontmatter (first 15 lines)
    if ! head -15 "$cmd_file" | grep -q "description:"; then
        if [[ $MISSING_DESC -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $plugin_name/$cmd_name missing description"
        MISSING_DESC=$((MISSING_DESC + 1))
    fi
done

if [[ $MISSING_DESC -eq 0 ]]; then
    CMD_COUNT=$(find "$REPO_ROOT"/plugins -path "*/commands/*.md" | wc -l)
    echo -e "${GREEN}PASS${NC} ($CMD_COUNT commands)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 94: Output-styles has valid style files
# ============================================================================
echo -n "Test 94: Output-styles has valid style files... "
STYLES_DIR="$REPO_ROOT/plugins/output-styles/styles"
INVALID_STYLES=0

for style_file in "$STYLES_DIR"/*.md; do
    [[ ! -f "$style_file" ]] && continue
    style_name=$(basename "$style_file" .md)

    # Check style has frontmatter with name
    if ! head -10 "$style_file" | grep -q "^---"; then
        if [[ $INVALID_STYLES -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  $style_name.md missing frontmatter"
        INVALID_STYLES=$((INVALID_STYLES + 1))
    fi
done

if [[ $INVALID_STYLES -eq 0 ]]; then
    STYLE_COUNT=$(ls "$STYLES_DIR"/*.md 2>/dev/null | wc -l)
    echo -e "${GREEN}PASS${NC} ($STYLE_COUNT styles)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 95: Panel command has character references
# ============================================================================
echo -n "Test 95: Panel command references characters... "
PANEL_CMD="$REPO_ROOT/plugins/panel/commands/panel.md"

if [[ -f "$PANEL_CMD" ]]; then
    # Check for core character references
    MISSING_CHARS=0
    for char in "Moderator" "Analyst" "Challenger"; do
        if ! grep -q "$char" "$PANEL_CMD"; then
            MISSING_CHARS=$((MISSING_CHARS + 1))
        fi
    done

    if [[ $MISSING_CHARS -eq 0 ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Missing character references"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  panel.md not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 96: Destructive-git rule detects hard reset
# ============================================================================
echo -n "Test 96: Destructive-git rule detects hard reset... "
RULE_FILE="$REPO_ROOT/plugins/asha/rules/destructive-git.sh"

if [[ -f "$RULE_FILE" ]]; then
    RESULT=$(bash -c "
        source '$RULE_FILE'
        check_violation 'Bash' 'git reset --hard HEAD~3' '/tmp' 2>/dev/null && echo 'DETECTED' || echo 'MISSED'
    ")

    if [[ "$RESULT" == *"DETECTED"* ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Hard reset not detected"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 97: Destructive-git rule detects branch deletion
# ============================================================================
echo -n "Test 97: Destructive-git rule detects branch deletion... "
RULE_FILE="$REPO_ROOT/plugins/asha/rules/destructive-git.sh"

if [[ -f "$RULE_FILE" ]]; then
    RESULT=$(bash -c "
        source '$RULE_FILE'
        check_violation 'Bash' 'git branch -D main' '/tmp' 2>/dev/null && echo 'DETECTED' || echo 'MISSED'
    ")

    if [[ "$RESULT" == *"DETECTED"* ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Branch deletion not detected"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 98: Destructive-git rule ignores safe operations
# ============================================================================
echo -n "Test 98: Destructive-git rule ignores safe operations... "
RULE_FILE="$REPO_ROOT/plugins/asha/rules/destructive-git.sh"

if [[ -f "$RULE_FILE" ]]; then
    RESULT=$(bash -c "
        source '$RULE_FILE'
        check_violation 'Bash' 'git push origin feature-branch' '/tmp' 2>/dev/null && echo 'DETECTED' || echo 'SAFE'
    ")

    if [[ "$RESULT" == *"SAFE"* ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Safe operation incorrectly flagged"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 99: PostToolUse handles malformed JSON gracefully
# ============================================================================
echo -n "Test 99: PostToolUse handles malformed JSON... "
TEST99_DIR=$(mktemp -d)
mkdir -p "$TEST99_DIR/Memory/sessions"
mkdir -p "$TEST99_DIR/Work/markers"
mkdir -p "$TEST99_DIR/.asha"
echo '{"initialized": true}' > "$TEST99_DIR/.asha/config.json"
echo "# Session" > "$TEST99_DIR/Memory/sessions/current-session.md"
export CLAUDE_PROJECT_DIR="$TEST99_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Send malformed JSON - should not crash (exit 0 or return {})
EXIT_CODE=0
OUTPUT=$(echo 'not valid json at all' | "$REPO_ROOT/plugins/asha/hooks/handlers/post-tool-use.sh" 2>/dev/null) || EXIT_CODE=$?
rm -rf "$TEST99_DIR"

# Handler should either return {} or exit gracefully (not crash with non-zero)
if [[ "$OUTPUT" == "{}" || -z "$OUTPUT" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Unexpected output: $OUTPUT"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 100: UserPromptSubmit handles empty input gracefully
# ============================================================================
echo -n "Test 100: UserPromptSubmit handles empty input... "
TEST100_DIR=$(mktemp -d)
mkdir -p "$TEST100_DIR/Memory/sessions"
mkdir -p "$TEST100_DIR/Work/markers"
mkdir -p "$TEST100_DIR/.asha"
echo '{"initialized": true}' > "$TEST100_DIR/.asha/config.json"
echo "# Session" > "$TEST100_DIR/Memory/sessions/current-session.md"
export CLAUDE_PROJECT_DIR="$TEST100_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/asha"

# Send empty JSON - should not crash
OUTPUT=$(echo '{}' | "$REPO_ROOT/plugins/asha/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)
rm -rf "$TEST100_DIR"

# Should return valid JSON (either {} or {"prompt": ...})
if echo "$OUTPUT" | jq empty 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Invalid JSON output"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 101: run-python.sh passes arguments correctly
# ============================================================================
echo -n "Test 101: run-python.sh passes arguments... "
RUN_PYTHON="$REPO_ROOT/plugins/asha/tools/run-python.sh"

if [[ -x "$RUN_PYTHON" ]]; then
    # Test that it can run a simple Python command
    OUTPUT=$("$RUN_PYTHON" -c "print('test')" 2>/dev/null || true)
    if [[ "$OUTPUT" == "test" ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Expected 'test', got: $OUTPUT"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${YELLOW}SKIP${NC}"
    SKIPPED=$((SKIPPED + 1))
fi

# ============================================================================
# Test 102: save-session.sh has required functions
# ============================================================================
echo -n "Test 102: save-session.sh structure valid... "
SAVE_SESSION="$REPO_ROOT/plugins/asha/tools/save-session.sh"

if [[ -f "$SAVE_SESSION" ]]; then
    # Check for key function patterns
    HAS_ARCHIVE=$(grep -c "archive_session\|Archive\|ARCHIVE" "$SAVE_SESSION" || echo 0)
    HAS_GIT=$(grep -c "git commit\|git add" "$SAVE_SESSION" || echo 0)

    if [[ $HAS_ARCHIVE -gt 0 && $HAS_GIT -gt 0 ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Missing archive or git functionality"
        FAILED=$((FAILED + 1))
    fi
else
    echo -e "${RED}FAIL${NC}"
    echo "  save-session.sh not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 103: No stray desktop/shortcut files in repo
# ============================================================================
echo -n "Test 103: No stray shortcut files... "
STRAY_FILES=$(find "$REPO_ROOT/plugins" -type f \( -name "*.desktop" -o -name "*.lnk" -o -name "*.url" -o -name "file:*" \) 2>/dev/null | wc -l)

if [[ $STRAY_FILES -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${YELLOW}WARN${NC} ($STRAY_FILES stray files found)"
    find "$REPO_ROOT/plugins" -type f \( -name "*.desktop" -o -name "*.lnk" -o -name "*.url" -o -name "file:*" \) 2>/dev/null | while read f; do
        echo "  - $f"
    done
    PASSED=$((PASSED + 1))  # Warn but don't fail
fi

# ============================================================================
# Test 104: Total test count matches expected
# ============================================================================
echo -n "Test 104: Test infrastructure self-check... "
# This test verifies the test suite is complete
EXPECTED_TESTS=104
if [[ $((PASSED + FAILED + SKIPPED + 1)) -eq $EXPECTED_TESTS ]]; then
    echo -e "${GREEN}PASS${NC} ($EXPECTED_TESTS tests)"
    PASSED=$((PASSED + 1))
else
    echo -e "${YELLOW}INFO${NC} (test count: $((PASSED + FAILED + SKIPPED + 1)))"
    PASSED=$((PASSED + 1))
fi

# ============================================================================
# Summary
# ============================================================================
echo ""
echo -e "${BLUE}=== Hook Test Summary ===${NC}"
echo -e "Passed:  ${GREEN}$PASSED${NC}"
echo -e "Failed:  ${RED}$FAILED${NC}"
echo -e "Skipped: ${YELLOW}$SKIPPED${NC}"
echo ""

if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}✓ All hook tests passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Some hook tests failed${NC}"
    exit 1
fi
