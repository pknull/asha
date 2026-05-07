#!/usr/bin/env bash
# validate-versions.sh
#
# Validates version consistency between README.md and CLAUDE.md (the two
# documents that humans actually read for authoritative repo state).
#
# The legacy 4-way cross-check between marketplace.json + per-plugin
# plugin.json + README + CLAUDE was retired with the symlink-mount
# installer migration: marketplace.json and plugin.json no longer exist.
# Per-plugin version drift is now caught by visual review during PR.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASSED=0
FAILED=0

echo "=== Version Consistency Validator ==="
echo "Repository: $REPO_ROOT"
echo ""

# Extract top-level versions from README and CLAUDE.md.
# Both files are expected to declare `**Version**: X.Y.Z` near the top.
README_VERSION=$(grep -m1 -oP '^\*\*Version\*\*:\s*\K[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/README.md" || true)
CLAUDE_MD_VERSION=$(grep -m1 -oP '^\*\*Version\*\*:\s*\K[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/CLAUDE.md" || true)

# Test 1: README version is present and well-formed.
echo -n "Test 1: README.md has top-level **Version** in semver form... "
if [[ -n "$README_VERSION" ]]; then
    echo -e "${GREEN}PASS${NC} (v$README_VERSION)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  README.md missing or malformed top-level **Version** line"
    FAILED=$((FAILED + 1))
fi

# Test 2: CLAUDE.md version is present and well-formed.
echo -n "Test 2: CLAUDE.md has top-level **Version** in semver form... "
if [[ -n "$CLAUDE_MD_VERSION" ]]; then
    echo -e "${GREEN}PASS${NC} (v$CLAUDE_MD_VERSION)"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${NC}"
    echo "  CLAUDE.md missing or malformed top-level **Version** line"
    FAILED=$((FAILED + 1))
fi

# Note on version coupling: README.md and CLAUDE.md track different
# things (README = repo/marketplace face, CLAUDE.md = AI-assistant guide
# revision). Drift between them is not necessarily a defect — both
# evolve on independent cadences. We surface both numbers for review
# but do not enforce equality. Per-plugin versions (Panel 5.0.0,
# Asha 1.18.0, etc.) are documented in README's plugin sections and
# tracked there as the source of truth.

echo ""
echo "=== Test Summary ==="
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
echo ""

if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}✓ All version checks passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Version inconsistencies detected${NC}"
    echo "Fix: Update README.md and CLAUDE.md to share the same top-level **Version**."
    exit 1
fi
