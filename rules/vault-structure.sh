#!/bin/bash
# Rule: Vault Structure Integrity
# Severity: MEDIUM
# Detects: Files created outside expected Vault hierarchy
#
# OUTCOME: Flag files created in unexpected locations within Vault/
# PATTERN: Vault has defined structure - random files indicate confusion
# CONSTRAINT: Log violation, do not block

check_violation() {
    local tool_name="$1"
    local file_path="$2"
    local project_dir="$3"

    # Only applies to Write operations (new file creation)
    [[ "$tool_name" != "Write" ]] && return 1

    # Only check Vault/ directory
    [[ ! "$file_path" =~ ^$project_dir/Vault/ ]] && return 1

    # Extract subdirectory after Vault/
    local rel_path="${file_path#$project_dir/Vault/}"
    local first_dir="${rel_path%%/*}"

    # Expected top-level directories in Vault
    local expected_dirs="World Books Sessions Characters Templates Resources"

    # Check if first directory is expected
    for dir in $expected_dirs; do
        [[ "$first_dir" == "$dir" ]] && return 1
    done

    # File created outside expected structure
    echo "Vault file outside expected structure: Vault/$rel_path"
    return 0
}

export -f check_violation 2>/dev/null || true
