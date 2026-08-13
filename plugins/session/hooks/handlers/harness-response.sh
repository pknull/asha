#!/bin/bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
# harness-response.sh — shared hook output contracts across harnesses.
#
# This file is sourced by hook handlers from the source tree. It is not copied
# or generated during install. Keep helpers event-specific: hook response
# contracts differ by event and by harness, so generic formatting hides bugs.

asha_harness() {
    # Explicit wins; otherwise recognize Copilot from the env it stamps on its
    # own hook processes (COPILOT_CLI=1, verified live on 1.0.68, 2026-07-26)
    # so bare `copilot` launches — without the asha wrapper — still get the
    # right response shapes. Default remains claude.
    if [[ -n "${ASHA_HARNESS:-}" ]]; then
        echo "$ASHA_HARNESS"
    elif [[ "${COPILOT_CLI:-}" == "1" ]]; then
        echo "copilot"
    elif [[ "${OPENCODE:-}" == "1" ]]; then
        echo "opencode"
    else
        echo "claude"
    fi
}

hook_noop() {
    echo "{}"
}

user_prompt_submit_noop() {
    hook_noop
}

# RP prompt routing is read directly by user-prompt-submit.sh. Codex rejects
# Claude's {prompt: ...} passthrough, so it uses the portable empty response.

user_prompt_submit_final_prompt() {
    local prompt="$1"
    case "$(asha_harness)" in
        codex)
            # Codex rejects Claude's {"prompt": ...} response shape for this
            # event. Empty JSON is the portable no-op.
            hook_noop
            ;;
        *)
            jq -n --arg prompt "$prompt" '{prompt: $prompt}'
            ;;
    esac
}

pretooluse_ask() {
    local reason="$1"
    case "$(asha_harness)" in
        codex|opencode)
            # These harnesses have no verified hook-mediated ask response.
            # Preserve safety by
            # degrading ask -> deny with the same message on stderr.
            printf '%s\n' "$reason" >&2
            return 2
            ;;
        *)
            jq -n --arg reason "$reason" '{
              hookSpecificOutput: {
                hookEventName: "PreToolUse",
                permissionDecision: "ask",
                permissionDecisionReason: $reason
              }
            }'
            ;;
    esac
}

pretooluse_deny() {
    local reason="$1"
    printf '%s\n' "$reason" >&2
    return 2
}

pretooluse_policy_ask() {
    local policy_id="$1"
    local reason="$2"
    local override_hint="${3:-}"
    case "$(asha_harness)" in
        codex|opencode)
            pretooluse_deny "BLOCKED by Asha policy [$policy_id]: ${reason}${override_hint}"
            ;;
        *)
            pretooluse_ask "${reason}${override_hint}"
            ;;
    esac
}

pretooluse_policy_deny() {
    local policy_id="$1"
    local reason="$2"
    local override_hint="${3:-}"
    pretooluse_deny "BLOCKED by Asha policy [$policy_id]: ${reason}${override_hint}"
}
