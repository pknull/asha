#!/usr/bin/env bash
# asha-env-bootstrap — load secrets and shared env vars used by Asha skills.
#
# Sourced by asha-claude and asha-codex before exec'ing their harness, so
# that ONLY Asha-launched sessions see these secrets. The user's bare shell
# stays clean; MCP servers spawned by other projects don't inherit Asha's
# tokens; rotation requires only an asha-prefixed-restart.
#
# Source of truth: $ASHA_SECRETS_FILE (default ~/.asha/secrets.env), gitignored,
# expected mode 0600. Standard dotenv format — KEY=VALUE per line, # comments
# allowed, no shell expansion of value RHS.
#
# Idempotent and silent on missing file. Skills that need a token surface
# their own "set $VAR" error message when they fail to read it; this script
# does not validate which keys are present.
#
# Future graduation path (not implemented yet): wrap this in `op run` /
# `infisical run` for a vault-backed source of truth without changing the
# dotenv shape. See docs/secrets.md.

set -u

ASHA_SECRETS_FILE="${ASHA_SECRETS_FILE:-$HOME/.asha/secrets.env}"

if [[ -r "$ASHA_SECRETS_FILE" ]]; then
    # Permission warning — secrets file should not be world-readable.
    perms="$(stat -c '%a' "$ASHA_SECRETS_FILE" 2>/dev/null || stat -f '%Lp' "$ASHA_SECRETS_FILE" 2>/dev/null)"
    if [[ -n "$perms" && "$perms" != "600" && "$perms" != "400" ]]; then
        echo "warn: $ASHA_SECRETS_FILE has mode $perms (expected 600). Run: chmod 600 $ASHA_SECRETS_FILE" >&2
    fi

    # Auto-export everything defined in the file.
    set -a
    # shellcheck source=/dev/null
    source "$ASHA_SECRETS_FILE"
    set +a
fi
