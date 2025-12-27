#!/bin/bash
# Rule: File Header Structure
# Severity: LOW
# Detects: Script and module files missing structured documentation headers
#
# OUTCOME: Encourage consistent documentation structure across codebase
# PATTERN: Key files should declare OUTCOME, PATTERN, CONSTRAINT
# CONSTRAINT: Log violation, do not block - gradual adoption

check_violation() {
    local tool_name="$1"
    local file_path="$2"
    local project_dir="$3"

    # Only applies to Write operations (new files)
    [[ "$tool_name" != "Write" ]] && return 1

    # Skip if file doesn't exist yet (can't check content in PreToolUse context)
    # This rule runs PostToolUse, so file exists
    [[ ! -f "$file_path" ]] && return 1

    # Target patterns: shell scripts, Asha modules, CLAUDE.md files
    local should_check=false
    local required_sections=""

    # Shell scripts in .claude/
    if [[ "$file_path" =~ ^$project_dir/\.claude/.*\.sh$ ]]; then
        should_check=true
        required_sections="OUTCOME PATTERN CONSTRAINT"
    fi

    # Asha modules
    if [[ "$file_path" =~ ^$project_dir/Asha/modules/.*\.md$ ]]; then
        should_check=true
        required_sections="Applies to"
    fi

    # CLAUDE.md files anywhere
    if [[ "$file_path" =~ CLAUDE\.md$ ]]; then
        should_check=true
        required_sections="OUTCOME"
    fi

    [[ "$should_check" == "false" ]] && return 1

    # Check for required sections in first 50 lines
    local header_content
    header_content=$(head -50 "$file_path" 2>/dev/null || echo "")

    local missing=""
    for section in $required_sections; do
        if ! echo "$header_content" | grep -qi "$section"; then
            missing="$missing $section"
        fi
    done

    if [[ -n "$missing" ]]; then
        local rel_path="${file_path#$project_dir/}"
        echo "Missing header sections in $rel_path:$missing"
        return 0
    fi

    return 1
}

export -f check_violation 2>/dev/null || true
