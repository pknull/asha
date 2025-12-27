#!/bin/bash
# Rule: Destructive Git Operations
# Severity: HIGH
# Detects: Force pushes, hard resets, branch deletions on protected branches
#
# OUTCOME: Flag dangerous git operations before they cause data loss
# PATTERN: Certain git operations are irreversible or affect shared state
# CONSTRAINT: Log violation, do not block

check_violation() {
    local tool_name="$1"
    local command="$2"
    local project_dir="$3"

    # Only applies to Bash operations
    [[ "$tool_name" != "Bash" ]] && return 1

    # Pattern: force push
    if [[ "$command" =~ git[[:space:]]+(push[[:space:]]+-f|push[[:space:]]+--force) ]]; then
        echo "Force push detected: $command"
        return 0
    fi

    # Pattern: hard reset
    if [[ "$command" =~ git[[:space:]]+reset[[:space:]]+--hard ]]; then
        echo "Hard reset detected: $command"
        return 0
    fi

    # Pattern: branch deletion on main/master
    if [[ "$command" =~ git[[:space:]]+branch[[:space:]]+-[dD][[:space:]]+(main|master) ]]; then
        echo "Protected branch deletion: $command"
        return 0
    fi

    # Pattern: force push to main/master
    if [[ "$command" =~ git[[:space:]]+push.*-f.*(main|master) ]] || \
       [[ "$command" =~ git[[:space:]]+push.*(main|master).*-f ]]; then
        echo "Force push to protected branch: $command"
        return 0
    fi

    return 1
}

export -f check_violation 2>/dev/null || true
