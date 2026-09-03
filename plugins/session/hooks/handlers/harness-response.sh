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

nudge_fragment() {
    local name="$1"
    local fragment="${BASH_SOURCE[0]%/*}/../nudges/fragments/$name.md"
    [[ -f "$fragment" ]] || return 0
    cat "$fragment" 2>/dev/null || true
}

# Copilot CLI currently discards postToolUse additionalContext
# (github/copilot-cli#2980). Keep this queue specific to style-audit rather
# than reviving the retired general nudge engine: post-tool-use.sh writes one
# private, session-scoped file per finding and user-prompt-submit.sh drains it
# through Copilot's verified userPromptSubmitted response seam.
copilot_queue_style_audit_nudge() {
    local project_dir="$1" session_id="${2:-unknown}" context="$3"
    local safe dir file
    safe="${session_id//[^[:alnum:]_.-]/_}"
    safe="${safe:0:80}"
    [[ -n "$safe" ]] || safe=unknown
    safe="session-$safe"
    dir="$project_dir/Work/markers/style-audit/$safe"
    umask 077
    mkdir -p "$dir" 2>/dev/null || return 0
    file="$(mktemp "$dir/finding.XXXXXX" 2>/dev/null || true)"
    [[ -n "$file" ]] || return 0
    printf '%s\n' "$context" > "$file" 2>/dev/null || rm -f "$file" 2>/dev/null || true
    return 0
}

copilot_drain_style_audit_nudges() {
    local project_dir="$1" session_id="${2:-unknown}"
    local safe dir file combined=""
    safe="${session_id//[^[:alnum:]_.-]/_}"
    safe="${safe:0:80}"
    [[ -n "$safe" ]] || safe=unknown
    safe="session-$safe"
    dir="$project_dir/Work/markers/style-audit/$safe"
    [[ -d "$dir" ]] || return 0
    for file in "$dir"/*; do
        [[ -f "$file" && ! -L "$file" ]] || continue
        local item
        item="$(cat "$file" 2>/dev/null || true)"
        [[ -z "$item" ]] || combined="${combined:+$combined$'\n\n'}$item"
        rm -f "$file" 2>/dev/null || true
    done
    rmdir "$dir" "$project_dir/Work/markers/style-audit" 2>/dev/null || true
    [[ -z "$combined" ]] || printf '%s\n' "$combined"
    return 0
}

posttooluse_nudge() {
    local context="$1" project_dir="${2:-}" session_id="${3:-unknown}"
    case "$(asha_harness)" in
        claude|codex)
            # Both native hook schemas accept PostToolUse additionalContext.
            jq -n --arg ctx "$context" '{
              hookSpecificOutput: {
                hookEventName: "PostToolUse",
                additionalContext: $ctx
              }
            }' 2>/dev/null || hook_noop
            ;;
        copilot)
            [[ -z "$project_dir" ]] \
                || copilot_queue_style_audit_nudge "$project_dir" "$session_id" "$context"
            hook_noop
            ;;
        opencode)
            # The generated bridge appends this stdout to its pending system
            # context and injects it at the next transform.
            printf '%s\n' "$context"
            ;;
        *) hook_noop ;;
    esac
}

verify_pass_nudge() {
    local context="$1"
    case "$(asha_harness)" in
        claude|codex)
            # Codex Stop must receive JSON only; plain text is not a valid
            # response and can obscure the fail-open retry contract.
            jq -nc --arg reason "$context" '{decision:"block", reason:$reason}' \
                2>/dev/null || hook_noop
            ;;
        copilot)
            jq -n --arg ctx "$context" '{additionalContext:$ctx}' \
                2>/dev/null || hook_noop
            ;;
        opencode)
            printf '%s\n' "$context"
            ;;
        *) hook_noop ;;
    esac
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
            # FINDING (Codex hooks docs, 2026-09-02): PreToolUse ask is "parsed but not supported yet. Codex marks the hook run as failed, reports the error, and continues the tool call".
            # "PermissionRequest accepts only allow|deny." Do not emit
            # the inert ask shape. Preserve the existing conservative contract
            # by degrading ask -> deny with the same message on stderr.
            # OpenCode likewise has no verified hook-mediated ask response.
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
