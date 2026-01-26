---
description: "Show status of automatic prompt correction via LanguageTool"
argument-hint: "status"
allowed-tools: ["Bash"]
---

# Automatic Prompt Correction (Always Active)

**Status**: ALWAYS RUNNING - Intelligent overhead management

LanguageTool automatically checks all prompts but only adds token overhead when corrections are significant (≥10% change).

## How It Works

The `user-prompt-submit` hook **automatically**:
1. Sends every prompt (≥5 words) to LanguageTool (localhost:8081)
2. Calculates correction difference percentage
3. **If correction <10% change**: Silently ignored (zero overhead)
4. **If correction ≥10% change**: Injects corrected version via system-reminder
5. Logs significant corrections to `Work/sessions/current-session.md`

**Smart Overhead**: Most prompts have <10% typos → zero token cost. Only heavy typo clusters trigger 1.5x overhead.

## Usage

```bash
/refine status             # Show recent corrections this session
```

## Benefits

- **Zero manual intervention** - Always active, intelligent filtering
- **Minimal overhead** - Only pays token cost for significant corrections
- **Graceful degradation** - Silently continues if LanguageTool server unavailable
- **Conservative corrections** - No rewriting, preserves your intent
- **Fast** - <100ms typical, 3-second timeout

## Requirements

**LanguageTool server must be running:**
- Local server on `http://localhost:8081`
- If not running, corrections will be skipped gracefully

## Behavior

**Automatically corrects when:**
- Prompt is ≥5 words
- Correction is enabled (marker file exists)
- LanguageTool server is running
- Not in silence mode
- Not during RP sessions

**Skips correction when:**
- Very short prompts (<5 words)
- Silence mode active
- RP session active
- LanguageTool timeout (3 seconds)

## Configuration

Correction enabled via: `Work/markers/prompt-refine`
- Marker file enables correction (content doesn't matter)
- Created by `/refine on`
- Deleted by `/refine off`

**Timeout:** 3 seconds (falls back to original if LanguageTool too slow)

---

**Implementation**: Execute bash logic below.

```bash
#!/bin/bash

# Show recent corrections from current session
SESSION_FILE="$(git rev-parse --show-toplevel)/Work/sessions/current-session.md"

echo "Automatic Prompt Correction: ALWAYS ACTIVE"
echo "  Engine: LanguageTool (localhost:8081)"
echo "  Threshold: 10% difference required for injection"
echo "  Minimum prompt length: 5 words"
echo "  Timeout: 3 seconds"
echo ""

if [[ -f "$SESSION_FILE" ]]; then
    CORRECTIONS=$(grep "Prompt corrected" "$SESSION_FILE" 2>/dev/null || true)

    if [[ -n "$CORRECTIONS" ]]; then
        echo "Significant corrections this session (≥10% change):"
        echo ""
        echo "$CORRECTIONS" | tail -5
        echo ""

        # Count total corrections
        CORRECTION_COUNT=$(echo "$CORRECTIONS" | wc -l)
        echo "Total significant corrections: $CORRECTION_COUNT"
        echo ""
    else
        echo "No significant corrections this session"
        echo "(Prompts with <10% changes are silently corrected without overhead)"
        echo ""
    fi
else
    echo "(Session file not found)"
    echo ""
fi

echo "How it works:"
echo "  • Every prompt checked by LanguageTool"
echo "  • <10% change: Silently ignored (zero token overhead)"
echo "  • ≥10% change: System-reminder injection (~1.5x overhead)"
echo ""
echo "View all corrections: Work/sessions/current-session.md"
```
