#!/usr/bin/env bash
# Canary Stop-lifecycle hook for the asha-marketplace installer.
# Side effect: touches a marker file with a timestamp when the hook fires.
# Purpose: proves settings.json registration succeeded and the merged
# command path resolves.

set -euo pipefail

MARKER="/tmp/asha-marketplace-test-hook-fired"
date -u +"%Y-%m-%dT%H:%M:%SZ fired ${BASH_SOURCE[0]}" >> "$MARKER"
# Empty JSON object so Claude Code doesn't misinterpret the hook's stdout.
echo '{}'
