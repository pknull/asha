#!/bin/bash
# nudge-engine.sh — declarative guidance-nudge engine for Asha.
#
# Advisory counterpart to policy-guard.sh: policies constrain (deny/ask),
# nudges inform (context injection; a nudge can never block a tool call or a
# turn). Reads rows from:
#   <plugin>/hooks/nudges/rules.json   (repo defaults)
#   ~/.asha/nudges.json                (optional user layer; merged by id, user wins)
# The event is taken from the stdin payload's hook_event_name (present on
# every hook event, every harness — argument-free registration survives hook
# runners that do not shell-split command strings); $1 overrides it for tests
# and manual runs. The engine is event-agnostic; registering a new event is a
# hooks.json entry.
#
# Row schema and gate semantics: see the _comment field in nudges/rules.json.
# Payload: exactly one of handler (allowlisted function in nudge-builtins.sh),
# inject (inline text), inject_file (path relative to hooks/nudges/; absolute
# and ~/ paths allowed for user-layer rows). Rows are evaluated in ascending
# priority; all firing fragments merge into ONE output per event, because a
# hook may emit only a single response object.
#
# Output contracts (kept alongside harness-response.sh's enforcement shapes):
#   UserPromptSubmit -> raw fragment text (context on Claude; Codex accepts
#                       raw fragments); "{}" when nothing fires
#   PreToolUse       -> hookSpecificOutput.additionalContext JSON on Claude;
#                       raw text on codex/opencode; silent when nothing fires
#   PostToolUse      -> raw fragment text + trailing "{}" (legacy
#                       suggest-compact shape); "{}" when nothing fires
#   other events     -> raw fragment text; "{}" when nothing fires
#
# FAIL-OPEN: advisory layer — any internal error must neither block nor delay
# the session. Malformed rows are skipped; a builtin crash is contained by the
# dispatch guard; a missing registry is a silent no-op.

# fail-open by design: no set -e — a handler crash must never block the session
set -uo pipefail

EVENT="${1:-}"

noop() {
    case "$EVENT" in
        PreToolUse) ;;      # legacy memory_nudge contract: silence, not "{}"
        *) echo "{}" ;;
    esac
    exit 0
}

SELF_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
command -v jq >/dev/null 2>&1 || noop

# Consume stdin first (hook contract), then resolve the event from it unless
# an explicit $1 override was given.
NUDGE_INPUT="$(cat 2>/dev/null || true)"
if [[ -z "$EVENT" ]]; then
    EVENT="$(printf '%s' "$NUDGE_INPUT" | jq -r '.hook_event_name // empty' 2>/dev/null || true)"
fi
[[ -n "$EVENT" ]] || noop

[[ -f "$SELF_DIR/common.sh" ]] && source "$SELF_DIR/common.sh" 2>/dev/null || noop
[[ -f "$SELF_DIR/harness-response.sh" ]] && source "$SELF_DIR/harness-response.sh" 2>/dev/null || noop

NUDGES_DIR="$SELF_DIR/../nudges"
REPO_RULES="$NUDGES_DIR/rules.json"
USER_RULES="$HOME/.asha/nudges.json"

# Merge repo + user rules (user overrides by id) — same contract as policy-guard.
RULES=""
if [[ -f "$REPO_RULES" && -f "$USER_RULES" ]]; then
    RULES="$(jq -s '
      (.[0].rules // []) as $base | (.[1].rules // []) as $user
      | ($user | map(.id)) as $uids
      | { rules: (($base | map(select((.id) as $i | ($uids | index($i)) | not))) + $user) }
    ' "$REPO_RULES" "$USER_RULES" 2>/dev/null || true)"
elif [[ -f "$REPO_RULES" ]]; then
    RULES="$(jq '{rules: (.rules // [])}' "$REPO_RULES" 2>/dev/null || true)"
elif [[ -f "$USER_RULES" ]]; then
    RULES="$(jq '{rules: (.rules // [])}' "$USER_RULES" 2>/dev/null || true)"
fi
[[ -n "$RULES" ]] || noop

ROWS="$(printf '%s' "$RULES" | jq -c --arg ev "$EVENT" \
    '[.rules[]? | select(.event == $ev)] | sort_by(.priority // 50) | .[]' 2>/dev/null || true)"
[[ -n "$ROWS" ]] || noop

TOOL_NAME="$(printf '%s' "$NUDGE_INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)"
MATCH_TEXT="$(printf '%s' "$NUDGE_INPUT" | jq -r '
    [.prompt?, .tool_input.command?, .tool_input.file_path?,
     .tool_input.pattern?, .tool_input.query?]
    | map(select(type == "string" and length > 0)) | join("\n")' 2>/dev/null || true)"
HARNESS="$(asha_harness)"

# One resolution for all rows; empty is valid (rows without project gates run).
NUDGE_PROJECT_DIR="$(detect_project_dir 2>/dev/null || true)"
# shellcheck disable=SC2034  # consumed by nudge-builtins.sh, sourced below
NUDGE_HANDLERS_DIR="$SELF_DIR"

# Builtins are optional: without them, handler rows skip and static rows work.
[[ -f "$SELF_DIR/nudge-builtins.sh" ]] && source "$SELF_DIR/nudge-builtins.sh" 2>/dev/null

FRAGMENTS=""
while IFS= read -r rule; do
    [[ -n "$rule" && "$rule" != "null" ]] || continue

    r_id="$(printf '%s' "$rule" | jq -r '.id // empty' 2>/dev/null || true)"
    [[ -n "$r_id" ]] || continue
    r_tool="$(printf '%s' "$rule" | jq -r '.tool // empty' 2>/dev/null || true)"
    r_regex="$(printf '%s' "$rule" | jq -r '.match_regex // empty' 2>/dev/null || true)"
    r_denv="$(printf '%s' "$rule" | jq -r '.disable_env // empty' 2>/dev/null || true)"
    r_mreq="$(printf '%s' "$rule" | jq -r '.marker_required // empty' 2>/dev/null || true)"
    r_moff="$(printf '%s' "$rule" | jq -r '.marker_off // empty' 2>/dev/null || true)"
    r_sil="$(printf '%s' "$rule" | jq -r 'if .silence_gated == false then "0" else "1" end' 2>/dev/null || echo 1)"
    r_init="$(printf '%s' "$rule" | jq -r 'if .requires_init == false then "0" else "1" end' 2>/dev/null || echo 1)"
    r_cool="$(printf '%s' "$rule" | jq -r '.cooldown_hours // empty' 2>/dev/null || true)"
    r_handler="$(printf '%s' "$rule" | jq -r '.handler // empty' 2>/dev/null || true)"
    r_inject="$(printf '%s' "$rule" | jq -r '.inject // empty' 2>/dev/null || true)"
    r_ifile="$(printf '%s' "$rule" | jq -r '.inject_file // empty' 2>/dev/null || true)"

    # Harness allowlist (absent = all harnesses).
    r_harnesses="$(printf '%s' "$rule" | jq -r '(.harnesses // []) | join(" ")' 2>/dev/null || true)"
    if [[ -n "$r_harnesses" ]]; then
        read -ra harness_list <<< "$r_harnesses"
        harness_ok=0
        for h in "${harness_list[@]}"; do
            [[ "$h" == "$HARNESS" ]] && harness_ok=1
        done
        [[ $harness_ok -eq 1 ]] || continue
    fi

    # Env kill switch: <disable_env>=0 disables the row (ASHA_NUDGE convention).
    if [[ -n "$r_denv" ]]; then
        [[ "$(printenv "$r_denv" 2>/dev/null || true)" == "0" ]] && continue
    fi

    # Tool and text gates (evaluated against this event's stdin payload).
    if [[ -n "$r_tool" ]]; then
        printf '%s' "$TOOL_NAME" | grep -Eq -- "^($r_tool)\$" 2>/dev/null || continue
    fi
    if [[ -n "$r_regex" ]]; then
        printf '%s' "$MATCH_TEXT" | grep -Eq -- "$r_regex" 2>/dev/null || continue
    fi

    # Project-scoped gates. A row that needs them is skipped outside a project;
    # a row without them (e.g. memory-lexical) runs anywhere.
    needs_project=0
    [[ -n "$r_mreq" || -n "$r_moff" || "$r_sil" == "1" || "$r_init" == "1" || -n "$r_cool" ]] && needs_project=1
    if [[ $needs_project -eq 1 ]]; then
        [[ -n "$NUDGE_PROJECT_DIR" ]] || continue
    fi
    if [[ -n "$NUDGE_PROJECT_DIR" ]]; then
        markers="$NUDGE_PROJECT_DIR/Work/markers"
        [[ -f "$markers/nudge-${r_id}-off" ]] && continue
        [[ "$r_init" == "1" && ! -f "$NUDGE_PROJECT_DIR/.asha/config.json" ]] && continue
        [[ "$r_sil" == "1" && -f "$markers/silence" ]] && continue
        [[ -n "$r_mreq" && ! -f "$markers/$r_mreq" ]] && continue
        [[ -n "$r_moff" && -f "$markers/$r_moff" ]] && continue
        if [[ -n "$r_cool" && "$r_cool" =~ ^[0-9]+$ ]]; then
            cmark="$markers/nudge-${r_id}-cooldown"
            if [[ -f "$cmark" ]]; then
                last="$(cat "$cmark" 2>/dev/null || echo 0)"
                [[ "$last" =~ ^[0-9]+$ ]] || last=0
                now="$(date +%s)"
                (( now - last < r_cool * 3600 )) && continue
            fi
        fi
    fi

    # Payload: builtin handler, fragment file, or inline text.
    frag=""
    if [[ -n "$r_handler" ]]; then
        if command -v nudge_dispatch >/dev/null 2>&1; then
            frag="$(nudge_dispatch "$r_handler" 2>/dev/null || true)"
        fi
    elif [[ -n "$r_ifile" ]]; then
        fpath="$r_ifile"
        fpath="${fpath/#\~\//$HOME/}"
        [[ "$fpath" == /* ]] || fpath="$NUDGES_DIR/$fpath"
        [[ -f "$fpath" ]] && frag="$(cat "$fpath" 2>/dev/null || true)"
    elif [[ -n "$r_inject" ]]; then
        frag="$r_inject"
    fi
    [[ -n "$frag" ]] || continue

    FRAGMENTS="${FRAGMENTS:+$FRAGMENTS$'\n\n'}$frag"

    # Stamp the engine-managed cooldown only when the row actually fired.
    if [[ -n "$r_cool" && "$r_cool" =~ ^[0-9]+$ && -n "$NUDGE_PROJECT_DIR" ]]; then
        mkdir -p "$NUDGE_PROJECT_DIR/Work/markers" 2>/dev/null
        date +%s > "$NUDGE_PROJECT_DIR/Work/markers/nudge-${r_id}-cooldown" 2>/dev/null
    fi
done <<< "$ROWS"

[[ -n "$FRAGMENTS" ]] || noop

case "$EVENT" in
    PreToolUse)
        case "$HARNESS" in
            codex|opencode) printf '%s\n' "$FRAGMENTS" ;;
            *) jq -n --arg ctx "$FRAGMENTS" \
                 '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $ctx}}' ;;
        esac
        ;;
    PostToolUse)
        printf '%s\n' "$FRAGMENTS"
        echo "{}"
        ;;
    *)
        printf '%s\n' "$FRAGMENTS"
        ;;
esac
exit 0
