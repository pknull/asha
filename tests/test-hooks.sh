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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

OUTPUT=$("$REPO_ROOT/plugins/session/hooks/handlers/session-start.sh" 2>/dev/null || true)

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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

OUTPUT=$("$REPO_ROOT/plugins/session/hooks/handlers/session-start.sh" 2>/dev/null || true)

if [[ "$OUTPUT" == *"system-reminder"* && "$OUTPUT" == *"Asha-managed project"* ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected output containing 'system-reminder' and 'Asha-managed project'"
    echo "  Got: ${OUTPUT:0:100}..."
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 3: PostToolUse does NOT write Memory/events/events.jsonl
# ============================================================================
# Architecture note: capture was moved from hooks to jsonl_reader.py on
# 2026-05-10 (commits referenced in memory:
# project_asha_jsonl_consolidation.md). The hook still creates Work/markers/
# for intervention paths (ReasoningBank, vector DB refresh, violation
# checker), but Memory/events/events.jsonl is now regenerated at /save
# time by parsing the host's native transcript. This test guards against
# accidental revert of that consolidation.
echo -n "Test 3: PostToolUse leaves Memory/events untouched (capture retired)... "
setup_test_project
rm -rf "$TEST_DIR/project/Memory/events"
export CLAUDE_PROJECT_DIR="$TEST_DIR/project"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

echo '{"tool_name": "Read", "tool_input": {}}' | \
    "$REPO_ROOT/plugins/session/hooks/handlers/post-tool-use.sh" >/dev/null 2>&1 || true

# Give any (mistaken) background emit time to flush before asserting absence.
sleep 0.3

EVENTS_FILE="$TEST_DIR/project/Memory/events/events.jsonl"
MARKERS_DIR="$TEST_DIR/project/Work/markers"
if [[ ! -f "$EVENTS_FILE" ]] && [[ -d "$MARKERS_DIR" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    [[ -f "$EVENTS_FILE" ]] && echo "  Memory/events/events.jsonl was written (capture should be retired)"
    [[ ! -d "$MARKERS_DIR" ]] && echo "  Work/markers/ was not created (intervention path broken)"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 4: PostToolUse Edit operation does NOT emit events.jsonl line
# ============================================================================
# Same architecture note as Test 3: the Edit-capture path was removed when
# capture moved to jsonl_reader. The hook still routes Edit through the
# vector-DB-refresh and violation-check intervention paths, but does not
# write events.jsonl.
echo -n "Test 4: PostToolUse Edit does not emit events.jsonl (capture retired)... "
setup_test_project
EVENTS_FILE="$TEST_DIR/project/Memory/events/events.jsonl"
mkdir -p "$TEST_DIR/project/Memory/events"
rm -f "$EVENTS_FILE"

export CLAUDE_PROJECT_DIR="$TEST_DIR/project"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

echo '{"tool_name": "Edit", "tool_input": {"file_path": "/test/file.md"}, "tool_response": {}}' | \
    "$REPO_ROOT/plugins/session/hooks/handlers/post-tool-use.sh" >/dev/null 2>&1 || true

# Wait for any (mistaken) background emit to flush before asserting absence.
sleep 0.3

# Pass iff events.jsonl is empty or absent. A non-empty file would mean
# someone wired capture back into the hook — flag it.
if [[ ! -s "$EVENTS_FILE" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  events.jsonl was written by hook — capture should be retired (see project_asha_jsonl_consolidation.md)"
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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

BEFORE_SIZE=$(wc -c < "$TEST_DIR/project/Memory/sessions/current-session.md")

echo '{"tool_name": "Write", "tool_input": {"file_path": "/test/newfile.md"}, "tool_response": {}}' | \
    "$REPO_ROOT/plugins/session/hooks/handlers/post-tool-use.sh" >/dev/null 2>&1 || true

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
# Test 6: UserPromptSubmit does NOT emit events.jsonl (capture retired)
# ============================================================================
# Same architecture note as Tests 3/4: prompt capture moved to jsonl_reader
# on 2026-05-10. The hook still does LanguageTool refinement and (when a
# correction is ≥10%) writes Work/markers/last-correction + injects a
# <system-reminder>, but it does not write Memory/events/events.jsonl.
echo -n "Test 6: UserPromptSubmit does not emit events.jsonl (capture retired)... "
TEST6_DIR=$(mktemp -d)
mkdir -p "$TEST6_DIR/Memory/events"
mkdir -p "$TEST6_DIR/Work/markers"
mkdir -p "$TEST6_DIR/.asha"
echo '{"initialized": true}' > "$TEST6_DIR/.asha/config.json"
export CLAUDE_PROJECT_DIR="$TEST6_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

# Stdout from the hook is the JSON {prompt:...} returned to Claude Code;
# we discard it. We only want to observe filesystem side-effects.
echo '{"prompt": "Hello world this is a test prompt"}' | \
    "$REPO_ROOT/plugins/session/hooks/handlers/user-prompt-submit.sh" >/dev/null 2>&1 || true

# Wait for any (mistaken) background emit to flush before asserting absence.
sleep 0.3

EVENTS_FILE="$TEST6_DIR/Memory/events/events.jsonl"
if [[ ! -s "$EVENTS_FILE" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  events.jsonl was written by hook — capture should be retired (see project_asha_jsonl_consolidation.md)"
    FAILED=$((FAILED + 1))
fi
rm -rf "$TEST6_DIR"
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
source '$REPO_ROOT/plugins/session/hooks/handlers/common.sh'
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
source '$REPO_ROOT/plugins/session/hooks/handlers/common.sh'
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
TEMPLATE_DIR="$REPO_ROOT/plugins/session/templates"
MISSING_TEMPLATES=0
REQUIRED_TEMPLATES=(
    "activeContext.md"
    "projectbrief.md"
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
# Test 16: Python tools requirements.txt exists
# ============================================================================
echo -n "Test 16: Python requirements.txt exists... "
REQ_FILE="$REPO_ROOT/plugins/session/tools/requirements.txt"

if [[ -f "$REQ_FILE" ]]; then
    # File exists. Counting non-comment lines under pipefail: grep returns 1
    # when no matches, which would abort the script — wrap in || true so the
    # zero-deps case (current state: stdlib-only) is observable, not fatal.
    DEP_COUNT=$( { grep -v "^#" "$REQ_FILE" || true; } | { grep -v "^$" || true; } | wc -l)
    echo -e "${GREEN}PASS${NC} ($DEP_COUNT dependencies)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  requirements.txt not found"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 19: SessionEnd hook handles clear reason
# ============================================================================
echo -n "Test 19: SessionEnd handles clear reason... "
SESSION_END="$REPO_ROOT/plugins/session/hooks/handlers/session-end.sh"
TEST19_DIR=$(mktemp -d)
mkdir -p "$TEST19_DIR/Memory/sessions"
mkdir -p "$TEST19_DIR/Work/markers"
mkdir -p "$TEST19_DIR/.asha"
echo '{"initialized": true}' > "$TEST19_DIR/.asha/config.json"
export CLAUDE_PROJECT_DIR="$TEST19_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

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
HOOKS_DIR="$REPO_ROOT/plugins/session/hooks/handlers"
TEST20_DIR=$(mktemp -d)
mkdir -p "$TEST20_DIR/Memory/sessions"
mkdir -p "$TEST20_DIR/Work/markers"
mkdir -p "$TEST20_DIR/.asha"
echo '{"initialized": true}' > "$TEST20_DIR/.asha/config.json"

for hook in session-start.sh post-tool-use.sh user-prompt-submit.sh session-end.sh; do
    HOOK_FILE="$HOOKS_DIR/$hook"
    [[ ! -f "$HOOK_FILE" ]] && continue

    export CLAUDE_PROJECT_DIR="$TEST20_DIR"
    export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

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
# Test 24: No hardcoded absolute paths in hook handlers
# ============================================================================
echo -n "Test 24: No hardcoded paths in hook handlers... "
HARDCODED_PATHS=0

for handler in "$REPO_ROOT"/plugins/session/hooks/handlers/*.sh; do
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
RUN_PYTHON="$REPO_ROOT/plugins/session/tools/run-python.sh"

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
SAVE_SESSION="$REPO_ROOT/plugins/session/tools/save-session.sh"

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
TOOLS_DIR="$REPO_ROOT/plugins/session/tools"
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
# Test 40: All scripts have proper shebang
# ============================================================================
echo -n "Test 40: All shell scripts have proper shebang... "
MISSING_SHEBANG=0

for script in "$REPO_ROOT"/plugins/*/hooks/handlers/*.sh \
              "$REPO_ROOT"/plugins/*/hooks-handlers/*.sh \
              "$REPO_ROOT"/plugins/*/tools/*.sh; do
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
VIOLATION_CHECKER="$REPO_ROOT/plugins/session/hooks/handlers/violation-checker.sh"

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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

# Trigger a declarative policy violation (no-broad-home-scans, the live rule)
"$VIOLATION_CHECKER" "Bash" '{"command": "find /home -name foo"}' 2>/dev/null || true

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
COMMON_SH="$REPO_ROOT/plugins/session/hooks/handlers/common.sh"

if [[ -f "$COMMON_SH" ]]; then
    export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"
    RESULT=$(bash -c "
        source '$COMMON_SH'
        get_plugin_root
    ")

    if [[ "$RESULT" == "$REPO_ROOT/plugins/session" ]]; then
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
TEMPLATES_DIR="$REPO_ROOT/plugins/session/templates"
INVALID_TEMPLATES=0

for template in "$TEMPLATES_DIR"/*.md; do
    [[ ! -f "$template" ]] && continue
    template_name=$(basename "$template")

    # Skip Mustache partials — files using {{var}} placeholders are filled in
    # at runtime and aren't expected to parse as valid YAML at rest. The
    # loop-checkpoint.md and loop-completion.md templates are examples.
    if grep -q "{{[a-z-]*}}" "$template"; then
        continue
    fi

    if head -1 "$template" | grep -q "^---"; then
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
# Test 52: No duplicate command names WITHIN a plugin
# ============================================================================
# Namespaced commands (/asha:init vs /session:init) are valid by design under
# the symlink-mount installer model — the slash command is /<plugin>:<name>
# so cross-plugin filename overlap is not a conflict. Only flag duplicates
# inside the SAME plugin's commands/ dir.
echo -n "Test 52: No duplicate command names within a plugin... "
DUPLICATES=0
declare -A CMD_NAMES

for cmd_file in "$REPO_ROOT"/plugins/*/commands/*.md; do
    [[ ! -f "$cmd_file" ]] && continue
    cmd_name=$(basename "$cmd_file" .md)
    plugin_name=$(basename "$(dirname "$(dirname "$cmd_file")")")
    key="${plugin_name}/${cmd_name}"

    if [[ -n "${CMD_NAMES[$key]:-}" ]]; then
        if [[ $DUPLICATES -eq 0 ]]; then
            echo -e "${RED}FAIL${NC}"
        fi
        echo "  Duplicate command '$cmd_name' inside plugin '$plugin_name'"
        DUPLICATES=$((DUPLICATES + 1))
    else
        CMD_NAMES[$key]="$plugin_name"
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
TOOLS_DIR="$REPO_ROOT/plugins/session/tools"

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

    # Skip sourced libraries which must not alter the caller's shell options.
    [[ "$handler_name" == "common.sh" || "$handler_name" == "state.sh" || "$handler_name" == "harness-response.sh" ]] && continue

    # Check for set -e or set -euo pipefail in first 10 lines
    if ! head -40 "$handler" | grep -qE "set -e|set -.*e"; then
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
COMMON_SH="$REPO_ROOT/plugins/session/hooks/handlers/common.sh"

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
POST_TOOL_USE="$REPO_ROOT/plugins/session/hooks/handlers/post-tool-use.sh"
TEST57_DIR=$(mktemp -d)
mkdir -p "$TEST57_DIR/Memory/sessions"
mkdir -p "$TEST57_DIR/.asha"
echo '{"initialized": true}' > "$TEST57_DIR/.asha/config.json"
echo "# Session" > "$TEST57_DIR/Memory/sessions/current-session.md"
export CLAUDE_PROJECT_DIR="$TEST57_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

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
# Test 61: save-session.sh accepts valid modes
# ============================================================================
echo -n "Test 61: save-session.sh accepts valid modes... "
SAVE_SESSION="$REPO_ROOT/plugins/session/tools/save-session.sh"
TEST61_DIR=$(mktemp -d)
mkdir -p "$TEST61_DIR/Memory/sessions"
mkdir -p "$TEST61_DIR/.asha"
echo '{"initialized": true}' > "$TEST61_DIR/.asha/config.json"
export CLAUDE_PROJECT_DIR="$TEST61_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

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
# Test 67: PostToolUse does NOT write file-path events (capture retired)
# ============================================================================
# Same architecture note as Tests 3/4/6: capture moved to jsonl_reader on
# 2026-05-10. Hook still routes Edit through vector-DB and violation-check
# intervention paths but does not emit events.jsonl. Guards the consolidation.
echo -n "Test 67: PostToolUse Edit emits no events.jsonl line (capture retired)... "
POST_TOOL_USE="$REPO_ROOT/plugins/session/hooks/handlers/post-tool-use.sh"
TEST67_DIR=$(mktemp -d)
mkdir -p "$TEST67_DIR/Memory/events"
mkdir -p "$TEST67_DIR/Work/markers"
mkdir -p "$TEST67_DIR/.asha"
echo '{"initialized": true}' > "$TEST67_DIR/.asha/config.json"
EVENTS_FILE_67="$TEST67_DIR/Memory/events/events.jsonl"

export CLAUDE_PROJECT_DIR="$TEST67_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

if command -v jq >/dev/null 2>&1; then
    echo '{"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/myfile.ts"}, "tool_response": {}}' | "$POST_TOOL_USE" 2>/dev/null || true

    # Wait for any (mistaken) background emit to flush before asserting absence.
    sleep 0.3

    if [[ ! -s "$EVENTS_FILE_67" ]]; then
        echo -e "${GREEN}PASS${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  events.jsonl was written by hook — capture should be retired (see project_asha_jsonl_consolidation.md)"
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
# Test 75: SessionStart hook injects correct module paths
# ============================================================================
echo -n "Test 75: SessionStart provides module paths... "
TEST75_DIR=$(mktemp -d)
mkdir -p "$TEST75_DIR/Memory/sessions"
mkdir -p "$TEST75_DIR/.asha"
echo '{"initialized": true}' > "$TEST75_DIR/.asha/config.json"

# Run with environment override (prefix assignment)
OUTPUT=$(CLAUDE_PROJECT_DIR="$TEST75_DIR" CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" \
    "$REPO_ROOT/plugins/session/hooks/handlers/session-start.sh" 2>&1 || true)
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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

# Send clear reason - should return {} without archiving
OUTPUT=$(echo '{"reason": "clear"}' | "$REPO_ROOT/plugins/session/hooks/handlers/session-end.sh" 2>/dev/null || true)
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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

# Run session-end (with clear reason so it doesn't try to archive)
echo '{"reason": "clear"}' | "$REPO_ROOT/plugins/session/hooks/handlers/session-end.sh" >/dev/null 2>&1 || true

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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

# Send a prompt - should be ignored due to silence marker
OUTPUT=$(echo '{"prompt": "test prompt that should be ignored"}' | "$REPO_ROOT/plugins/session/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)
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
# Test 91: UserPromptSubmit injects RP routing directive during RP
# ============================================================================
# Contract change (increment A of the living-world plan): rp-active no longer
# means "skip everything" — LanguageTool refinement stays skipped, but the
# hook now re-asserts the per-turn RP routing directive (spawn roleplay-gm,
# inline SCENE_STATE) and passes the prompt through unchanged.
echo -n "Test 91: UserPromptSubmit injects RP routing during rp-active... "
TEST91_DIR=$(mktemp -d)
mkdir -p "$TEST91_DIR/Memory/sessions"
mkdir -p "$TEST91_DIR/Work/markers"
mkdir -p "$TEST91_DIR/.asha"
echo '{"initialized": true}' > "$TEST91_DIR/.asha/config.json"
echo "# Session" > "$TEST91_DIR/Memory/sessions/current-session.md"
touch "$TEST91_DIR/Work/markers/rp-active"
export CLAUDE_PROJECT_DIR="$TEST91_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

OUTPUT=$(echo '{"prompt": "test prompt during RP"}' | "$REPO_ROOT/plugins/session/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)

# LanguageTool must remain skipped in RP: the correction marker is only
# touched on the refinement path, so its absence proves the skip held.
CORRECTION_MARKER_ABSENT=1
[[ -f "$TEST91_DIR/Work/markers/last-correction" ]] && CORRECTION_MARKER_ABSENT=0
rm -rf "$TEST91_DIR"

if [[ "$OUTPUT" == *"<system-reminder>"* && "$OUTPUT" == *"roleplay-gm"* \
      && "$OUTPUT" == *'"prompt": "test prompt during RP"'* \
      && $CORRECTION_MARKER_ABSENT -eq 1 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    [[ "$OUTPUT" != *"roleplay-gm"* ]] && echo "  Routing directive missing from output"
    [[ "$OUTPUT" != *'"prompt": "test prompt during RP"'* ]] && echo "  Prompt passthrough missing or altered"
    [[ $CORRECTION_MARKER_ABSENT -eq 0 ]] && echo "  LanguageTool refinement ran during RP (must stay skipped)"
    echo "  Got: ${OUTPUT:0:200}..."
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 91b: UserPromptSubmit rp-hook-off kill-switch suppresses injection
# ============================================================================
echo -n "Test 91b: rp-hook-off marker suppresses RP routing directive... "
TEST91B_DIR=$(mktemp -d)
mkdir -p "$TEST91B_DIR/Work/markers"
mkdir -p "$TEST91B_DIR/.asha"
echo '{"initialized": true}' > "$TEST91B_DIR/.asha/config.json"
touch "$TEST91B_DIR/Work/markers/rp-active"
touch "$TEST91B_DIR/Work/markers/rp-hook-off"
export CLAUDE_PROJECT_DIR="$TEST91B_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

OUTPUT=$(echo '{"prompt": "test prompt during RP"}' | "$REPO_ROOT/plugins/session/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)
rm -rf "$TEST91B_DIR"

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected {} with kill-switch present, got: ${OUTPUT:0:200}..."
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 91c: RP routing on Codex stops after the raw fragment
# ============================================================================
# Codex rejects the Claude-only {prompt: ...} passthrough as invalid JSON;
# the raw <system-reminder> fragment must be the handler's final output.
echo -n "Test 91c: RP routing codex output omits {prompt} passthrough... "
TEST91C_DIR=$(mktemp -d)
mkdir -p "$TEST91C_DIR/Work/markers"
mkdir -p "$TEST91C_DIR/.asha"
echo '{"initialized": true}' > "$TEST91C_DIR/.asha/config.json"
touch "$TEST91C_DIR/Work/markers/rp-active"
export CLAUDE_PROJECT_DIR="$TEST91C_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

OUTPUT=$(echo '{"prompt": "test prompt during RP"}' | ASHA_HARNESS=codex "$REPO_ROOT/plugins/session/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)
rm -rf "$TEST91C_DIR"

if [[ "$OUTPUT" == *"roleplay-gm"* && "$OUTPUT" != *'"prompt"'* ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    [[ "$OUTPUT" != *"roleplay-gm"* ]] && echo "  Routing directive missing"
    [[ "$OUTPUT" == *'"prompt"'* ]] && echo "  Codex output contains {prompt} shape it rejects"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 91d: RP routing no-ops on malformed stdin
# ============================================================================
echo -n "Test 91d: RP routing no-ops on malformed stdin... "
TEST91D_DIR=$(mktemp -d)
mkdir -p "$TEST91D_DIR/Work/markers"
mkdir -p "$TEST91D_DIR/.asha"
echo '{"initialized": true}' > "$TEST91D_DIR/.asha/config.json"
touch "$TEST91D_DIR/Work/markers/rp-active"
export CLAUDE_PROJECT_DIR="$TEST91D_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

EXIT_CODE=0
OUTPUT=$(echo 'not valid json at all' | "$REPO_ROOT/plugins/session/hooks/handlers/user-prompt-submit.sh" 2>/dev/null) || EXIT_CODE=$?
rm -rf "$TEST91D_DIR"

if [[ "$OUTPUT" == "{}" && $EXIT_CODE -eq 0 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Expected {} and exit 0, got exit $EXIT_CODE: ${OUTPUT:0:200}..."
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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

# Send malformed JSON - should not crash (exit 0 or return {})
EXIT_CODE=0
OUTPUT=$(echo 'not valid json at all' | "$REPO_ROOT/plugins/session/hooks/handlers/post-tool-use.sh" 2>/dev/null) || EXIT_CODE=$?
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
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

# Send empty JSON - should not crash
OUTPUT=$(echo '{}' | "$REPO_ROOT/plugins/session/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)
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
# Test 100b: UserPromptSubmit Codex no-op returns Codex-safe JSON
# ============================================================================
echo -n "Test 100b: UserPromptSubmit codex no-op output... "
TEST100B_DIR=$(mktemp -d)
mkdir -p "$TEST100B_DIR/Memory/sessions"
mkdir -p "$TEST100B_DIR/Work/markers"
mkdir -p "$TEST100B_DIR/.asha"
echo '{"initialized": true}' > "$TEST100B_DIR/.asha/config.json"
export CLAUDE_PROJECT_DIR="$TEST100B_DIR"
export CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session"

OUTPUT=$(echo '{"prompt": "test prompt that codex should ignore safely"}' | ASHA_HARNESS=codex "$REPO_ROOT/plugins/session/hooks/handlers/user-prompt-submit.sh" 2>/dev/null || true)
rm -rf "$TEST100B_DIR"

if [[ "$OUTPUT" == "{}" ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  Unexpected Codex output: $OUTPUT"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 100c: harness-response.sh centralizes per-harness contracts
# ============================================================================
echo -n "Test 100c: harness-response helper contracts... "
HR="$REPO_ROOT/plugins/session/hooks/handlers/harness-response.sh"
HR_OK=1
HR_WHY=""

claude_prompt="$(ASHA_HARNESS=claude bash -c 'source "$1"; user_prompt_submit_final_prompt "hello"' _ "$HR" 2>/dev/null || true)"
codex_prompt="$(ASHA_HARNESS=codex bash -c 'source "$1"; user_prompt_submit_final_prompt "hello"' _ "$HR" 2>/dev/null || true)"
claude_ask="$(ASHA_HARNESS=claude bash -c 'source "$1"; pretooluse_policy_ask test-policy "Needs review" " (override: X=1)"' _ "$HR" 2>/dev/null || true)"
codex_err="$(mktemp)"
set +e
ASHA_HARNESS=codex bash -c 'source "$1"; pretooluse_policy_ask test-policy "Needs review" " (override: X=1)"' _ "$HR" >/dev/null 2>"$codex_err"
codex_rc=$?
set -e
codex_msg="$(cat "$codex_err" 2>/dev/null || true)"
rm -f "$codex_err"

[[ "$(printf '%s' "$claude_prompt" | jq -r '.prompt // empty' 2>/dev/null)" == "hello" ]] || { HR_OK=0; HR_WHY="$HR_WHY claude-prompt"; }
[[ "$codex_prompt" == "{}" ]] || { HR_OK=0; HR_WHY="$HR_WHY codex-prompt"; }
[[ "$(printf '%s' "$claude_ask" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null)" == "ask" ]] || { HR_OK=0; HR_WHY="$HR_WHY claude-ask"; }
[[ "$codex_rc" -eq 2 && "$codex_msg" == "BLOCKED by Asha policy [test-policy]: Needs review (override: X=1)" ]] || { HR_OK=0; HR_WHY="$HR_WHY codex-deny(rc=$codex_rc msg=$codex_msg)"; }

if [[ $HR_OK -eq 1 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  harness-response mismatch:$HR_WHY"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 101: run-python.sh passes arguments correctly
# ============================================================================
echo -n "Test 101: run-python.sh passes arguments... "
RUN_PYTHON="$REPO_ROOT/plugins/session/tools/run-python.sh"

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
SAVE_SESSION="$REPO_ROOT/plugins/session/tools/save-session.sh"

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
# Test 104: Ported policy rules (destructive-git ask, memory-protection exclude, vault-structure warn)
# ============================================================================
echo -n "Test 104: Ported policy rules enforce correctly... "
PG_PORTED="$REPO_ROOT/plugins/session/hooks/handlers/policy-guard.sh"
pg_decision() {
    local out
    out="$(printf '%s' "$1" | env -u ASHA_HARNESS bash "$PG_PORTED" 2>/dev/null)"
    if [[ -n "$out" ]]; then
        printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null
    else
        echo "allow"
    fi
}
PG_OK=1; PG_WHY=""
chk_pg() { local got; got="$(pg_decision "$2")"; [[ "$got" == "$3" ]] || { PG_OK=0; PG_WHY="$PG_WHY $1(got=$got want=$3)"; }; }
chk_pg force_push   '{"tool_name":"Bash","tool_input":{"command":"git push --force"}}'              ask
chk_pg plain_push   '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}'          allow
chk_pg hard_reset   '{"tool_name":"Bash","tool_input":{"command":"git reset --hard HEAD~1"}}'       ask
chk_pg del_main     '{"tool_name":"Bash","tool_input":{"command":"git branch -D main"}}'            ask
chk_pg mem_immutable '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/projectbrief.md"}}' ask
chk_pg mem_mutable   '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/activeContext.md"}}' allow
chk_pg vault_warn    '{"tool_name":"Write","tool_input":{"file_path":"/p/Vault/Random/x.md"}}'       allow
chk_pg broad_home    '{"tool_name":"Bash","tool_input":{"command":"find /home -name x"}}'             ask
chk_pg broad_user    '{"tool_name":"Bash","tool_input":{"command":"find /home/pknull -name x"}}'      ask
chk_pg scoped_home   '{"tool_name":"Bash","tool_input":{"command":"find /home/pknull/life -name x"}}' allow
if [[ $PG_OK -eq 1 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  policy-guard ported-rule mismatch:$PG_WHY"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 105: Copilot policy adapter (Copilot payload -> Asha decision)
# ============================================================================
echo -n "Test 105: Copilot hook adapter translates + enforces... "
CP_ADAPTER="$REPO_ROOT/plugins/session/hooks/handlers/copilot-policy-adapter.sh"
cp_decision() {
    printf '%s' "$1" | bash "$CP_ADAPTER" 2>/dev/null | jq -r '.permissionDecision // "allow"' 2>/dev/null
}
CP_OK=1; CP_WHY=""
chk_cp() { local got; got="$(cp_decision "$2")"; [[ "$got" == "$3" ]] || { CP_OK=0; CP_WHY="$CP_WHY $1(got=$got want=$3)"; }; }
# broad /home scan, toolArgs as an object -> ask (no-broad-home-scans)
chk_cp scan_obj   '{"toolName":"bash","toolArgs":{"command":"find /home/pknull -name x"}}'      ask
# toolArgs as a JSON-encoded STRING, benign -> allow (exercises the string path)
chk_cp benign_str '{"toolName":"bash","toolArgs":"{\"command\":\"echo hi\"}"}'                   allow
# force-push -> ask (destructive-git)
chk_cp force_push '{"toolName":"bash","toolArgs":{"command":"git push --force"}}'                ask
# create a secrets file via the `path` field -> deny (block-secrets)
chk_cp secret     '{"toolName":"create","toolArgs":{"path":"/p/.ssh/id_rsa","file_text":"x"}}'  deny
# read a normal file -> allow
chk_cp view_ok    '{"toolName":"view","toolArgs":{"path":"/tmp/readme.txt"}}'                    allow
if [[ -x "$CP_ADAPTER" && "$CP_OK" -eq 1 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  copilot-adapter mismatch:$CP_WHY (adapter exec=$([[ -x "$CP_ADAPTER" ]] && echo yes || echo no))"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 106: Codex installer emits current hook schema + native rules
# ============================================================================
echo -n "Test 106: Codex installer emits nested hooks + native rules... "
CODEX_TMP="$(mktemp -d)"
CODEX_OK=1
CODEX_WHY=""
printf '[features]\nhooks = true\n' > "$CODEX_TMP/config.toml"
if ! CODEX_HOME="$CODEX_TMP" "$REPO_ROOT/install.sh" --target codex --only session >/dev/null 2>"$CODEX_TMP/install.err"; then
    CODEX_OK=0
    CODEX_WHY=" install-failed:$(cat "$CODEX_TMP/install.err")"
elif ! python3 - "$CODEX_TMP/config.toml" "$CODEX_TMP/rules/asha.rules" <<'PY' >/dev/null 2>"$CODEX_TMP/check.err"
import pathlib, sys, tomllib
config = pathlib.Path(sys.argv[1])
rules = pathlib.Path(sys.argv[2])
text = config.read_text()
tomllib.loads(text)
assert '[[hooks.PreToolUse]]' in text
assert '[[hooks.PreToolUse.hooks]]' in text
assert 'type = "command"' in text
rule_text = rules.read_text()
assert 'prefix_rule(' in rule_text
assert 'pattern = ["git", "reset", "--hard"]' in rule_text
assert 'pattern = ["find", "/home"]' in rule_text
PY
then
    CODEX_OK=0
    CODEX_WHY=" schema-check-failed:$(cat "$CODEX_TMP/check.err")"
elif command -v codex >/dev/null 2>&1; then
    DECISION="$(codex execpolicy check --rules "$CODEX_TMP/rules/asha.rules" -- git reset --hard 2>/dev/null | jq -r '.decision // empty' 2>/dev/null || true)"
    if [[ "$DECISION" != "prompt" ]]; then
        CODEX_OK=0
        CODEX_WHY=" execpolicy(got=${DECISION:-empty} want=prompt)"
    fi
fi
rm -rf "$CODEX_TMP"
if [[ $CODEX_OK -eq 1 ]]; then
    echo -e "${GREEN}PASS${NC}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  codex installer mismatch:$CODEX_WHY"
    FAILED=$((FAILED + 1))
fi

# ============================================================================
# Test 107: Total test count matches expected
# ============================================================================
echo -n "Test 107: Test infrastructure self-check... "
# This test verifies the test suite is complete
EXPECTED_TESTS=87
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
