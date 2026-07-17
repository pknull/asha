#!/bin/bash
# save-commit-gate.sh — PreToolUse enforcement: no git commit may touch
# Memory/ until the save preflight gates have passed.
#
# The old contract was convention ("save.md says STOP on hard fail") plus a
# post-commit Stop-hook net that gives up after 3 attempts. This handler makes
# the refusal mechanical: a `git commit` whose command references Memory/ (or
# whose staged set includes Memory/ paths) is DENIED unless
# Work/markers/save-gates-ok exists AND its stored activeContext.md sha256
# matches disk. The marker is written only by save-preflight-env.sh after all
# hard gates pass, and any later mutation of activeContext.md invalidates it
# automatically — you cannot pass the gates and then commit something else.
#
# FAIL-OPEN on internal errors (missing jq, unparseable input, no project):
# a guard that fails closed bricks every commit, strictly worse than the gap.
# Override escape hatch: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1.
set -uo pipefail   # deliberately NOT -e: we own every exit code

SELF_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
[[ -f "$SELF_DIR/harness-response.sh" ]] && source "$SELF_DIR/harness-response.sh" 2>/dev/null || exit 0
command -v jq >/dev/null 2>&1 || exit 0

INPUT="$(cat 2>/dev/null || true)"
[[ -n "$INPUT" ]] || exit 0

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)"
[[ "$TOOL_NAME" == "Bash" ]] || exit 0

CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[[ -n "$CMD" ]] || exit 0

# Only git-commit commands are in scope.
printf '%s' "$CMD" | grep -Eq 'git([[:space:]]+--?[A-Za-z][^|;&]*)?[[:space:]]+commit\b' || exit 0

# Override escape hatch (mirrors policy-guard convention).
[[ "${ASHA_ALLOW_UNGATED_MEMORY_COMMIT:-}" == "1" ]] && exit 0

CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"
PROJECT_DIR="${CWD:-${CLAUDE_PROJECT_DIR:-$(pwd)}}"
[[ -d "$PROJECT_DIR" ]] || exit 0

# Does this commit touch Memory/? Two signals, either suffices:
#   (a) the command itself references Memory/ (e.g. `git add Memory/ && git commit`)
#   (b) the staged set already contains Memory/ paths (add ran in a prior block)
#
# Signal (a) inspects ONLY the portion of the command before the `commit`
# subcommand. A commit MESSAGE that merely mentions Memory/ — as every commit
# describing this very gate does — is not a staging operation and must never
# trigger; grepping the whole command string (message included) is friendly
# fire. `git add Memory/ && git commit …` still matches: the add precedes the
# commit token.
TOUCHES_MEMORY=0
CMD_HEAD="${CMD%%commit*}"
printf '%s' "$CMD_HEAD" | grep -q 'Memory/' && TOUCHES_MEMORY=1
if [[ $TOUCHES_MEMORY -eq 0 ]]; then
    if git -C "$PROJECT_DIR" diff --cached --name-only 2>/dev/null | grep -q '^Memory/'; then
        TOUCHES_MEMORY=1
    fi
fi
[[ $TOUCHES_MEMORY -eq 1 ]] || exit 0

# Nothing to gate before the project has an activeContext (e.g. /session:init's
# very first Memory commit).
AC_FILE="$PROJECT_DIR/Memory/activeContext.md"
[[ -f "$AC_FILE" ]] || exit 0

# Silence marker: Memory persistence is disabled — a Memory commit under
# silence is a policy violation regardless of gates.
if [[ -f "$PROJECT_DIR/Work/markers/silence" ]]; then
    pretooluse_policy_deny "save-commit-gate" \
        "Memory/ commit refused: Work/markers/silence is active (Memory persistence disabled). Run /session:restore first." \
        " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
    exit $?
fi

MARKER="$PROJECT_DIR/Work/markers/save-gates-ok"
REMEDY="Run: \"\$ASHA_ROOT/plugins/session/tools/save-preflight-env.sh\" — it resolves the environment, verifies the save plugin, checks Memory notes against disk, and opens this gate only when all continuity gates pass."

if [[ ! -f "$MARKER" ]]; then
    pretooluse_policy_deny "save-commit-gate" \
        "Memory/ commit refused: save preflight gates have not passed (no Work/markers/save-gates-ok). $REMEDY" \
        " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
    exit $?
fi

MARKER_SHA="$(jq -r '.ac_sha256 // empty' "$MARKER" 2>/dev/null || true)"
DISK_SHA="$(sha256sum "$AC_FILE" 2>/dev/null | cut -d' ' -f1 || true)"
if [[ -z "$MARKER_SHA" || -z "$DISK_SHA" || "$MARKER_SHA" != "$DISK_SHA" ]]; then
    rm -f "$MARKER" 2>/dev/null || true
    pretooluse_policy_deny "save-commit-gate" \
        "Memory/ commit refused: activeContext.md changed AFTER the gates passed (marker hash ${MARKER_SHA:0:12}… != disk ${DISK_SHA:0:12}…) — gates are stale. $REMEDY" \
        " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
    exit $?
fi

# Gates passed against exactly this activeContext — allow the commit. The
# marker is left in place for the turn; the Stop-hook net still runs after.
exit 0
