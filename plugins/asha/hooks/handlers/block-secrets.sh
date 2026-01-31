#!/bin/bash
# block-secrets.sh - PreToolUse hook for Asha
# Blocks access to sensitive credential files
#
# Override: CLAUDE_ALLOW_SECRETS=1 claude ...
# Exit codes: 0=allow, 2=block (stderr fed to Claude)
#
# Triggered by: Read, Edit, Write, MultiEdit operations

# Override check - explicit opt-in allows access
[[ "$CLAUDE_ALLOW_SECRETS" == "1" ]] && exit 0

# Patterns to block (regex)
BLOCKED_PATTERNS=(
  '\.env$'             # .env files
  '\.env\.'            # .env.local, .env.production, etc.
  'secrets\.json'      # Generic secrets file
  'credentials\.json'  # Generic credentials
  '\.pem$'             # SSL/TLS certificates
  '\.key$'             # Private keys
  'id_rsa'             # SSH private keys (RSA)
  'id_ed25519'         # SSH private keys (Ed25519)
  'id_ecdsa'           # SSH private keys (ECDSA)
  'id_dsa'             # SSH private keys (DSA)
  '\.p12$'             # PKCS#12 keystores
  '\.pfx$'             # Windows certificate stores
  '\.keystore$'        # Java keystores
  'htpasswd'           # Apache password files
  'shadow$'            # Unix shadow password file
  '\.npmrc$'           # npm auth tokens
  '\.pypirc$'          # PyPI auth tokens
  '\.netrc$'           # Network credentials
  '\.git-credentials$' # Git credential store
)

FILE_PATH="$CLAUDE_FILE_PATH"

# Skip if no file path provided
[[ -z "$FILE_PATH" ]] && exit 0

for pattern in "${BLOCKED_PATTERNS[@]}"; do
  if [[ "$FILE_PATH" =~ $pattern ]]; then
    echo "BLOCKED: Secrets file access denied: $FILE_PATH" >&2
    echo "Pattern matched: $pattern" >&2
    echo "" >&2
    echo "To override for this session:" >&2
    echo "  CLAUDE_ALLOW_SECRETS=1 claude ..." >&2
    exit 2
  fi
done

exit 0
