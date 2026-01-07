#!/bin/bash
# Rule: Memory Protection
# Severity: HIGH
# Detects: Direct writes to immutable Memory/ files
#
# OUTCOME: Flag modifications to core identity/config files
# PATTERN: Mutable files (activeContext, sessions) are expected to change
# CONSTRAINT: Log violation, do not block (graduated enforcement)

# Mutable files - expected to change during normal operation
MUTABLE_PATTERNS=(
    "/sessions/"
    "/activeContext.md"
    "/techEnvironment.md"
    "/workflowProtocols.md"
    "/vector_db/"
)

check_violation() {
    local tool_name="$1"
    local file_path="$2"
    local project_dir="$3"

    # Only applies to Write/Edit operations
    [[ "$tool_name" != "Write" && "$tool_name" != "Edit" ]] && return 1

    # Check if targeting Memory/ directory
    if [[ "$file_path" =~ ^$project_dir/Memory/ ]]; then
        # Check against mutable patterns (no violation)
        for pattern in "${MUTABLE_PATTERNS[@]}"; do
            if [[ "$file_path" == *"$pattern"* ]]; then
                return 1
            fi
        done

        # Flag: Immutable Memory file modification detected
        echo "Immutable Memory file modified: ${file_path#$project_dir/}"
        return 0
    fi

    return 1
}

# Export for sourcing
export -f check_violation 2>/dev/null || true
