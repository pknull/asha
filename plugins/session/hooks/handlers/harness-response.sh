#!/bin/bash
# harness-response.sh — shared hook output contracts across harnesses.
#
# This file is sourced by hook handlers from the source tree. It is not copied
# or generated during install. Keep helpers event-specific: hook response
# contracts differ by event and by harness, so generic formatting hides bugs.

asha_harness() {
    echo "${ASHA_HARNESS:-claude}"
}

hook_noop() {
    echo "{}"
}

user_prompt_submit_noop() {
    hook_noop
}

user_prompt_submit_correction() {
    local refined="$1"
    cat <<EOF
<system-reminder>
User's prompt has been corrected. Interpret as: "$refined"
</system-reminder>
EOF
}

# Per-turn RP routing directive, injected while Work/markers/rp-active exists
# (and Work/markers/rp-hook-off does not). Mechanism-generic: names the
# orchestrator and validator agents, no setting content. Bash cannot force the
# spawn — the main loop performs it; this re-assertion is the un-scroll-away
# replacement for the one-shot /rp setup.
user_prompt_submit_rp_routing() {
    cat <<'EOF'
<system-reminder>
RP session active. Routing directive for this turn:
- Do not voice profiled NPCs yourself: the main loop's accumulated context drifts them off-sheet.
- For any beat where a profiled NPC acts or speaks, spawn `roleplay-gm` (Task) with TRIGGER=<the user's input> plus a complete inline SCENE_STATE (location / time / present / observable / character_state + recent register-stack) so no agent reads the full session file. roleplay-gm consults character agents only for the NPCs acting this beat.
- Validator off by default: run `continuity-reviewer MODE:live_roleplay` only on key beats (a reveal or a threshold) or when the Keeper tags [validate].
- Relay roleplay-gm's prose; drive 2-3 beats; pause only at genuine decisions.
- PC-only beats (sit/sleep/wait, pure PC-internal action, [meta]): handle directly, no spawn. Drift happens at NPC voicing; do not pay the spawn where no NPC acts.
</system-reminder>
EOF
}

# Codex accepts raw prompt fragments for UserPromptSubmit but rejects the
# Claude-only {prompt: ...} passthrough as invalid JSON, so a raw fragment
# must be the handler's final output there. Shared by the correction and
# RP-routing injection paths.
user_prompt_submit_stops_after_injection() {
    [[ "$(asha_harness)" == "codex" ]]
}

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
        codex)
            # Codex has no hook-mediated ask channel here. Preserve safety by
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
        codex)
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
