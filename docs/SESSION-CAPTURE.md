# Session Capture Protocol

## Overview

Session watching automatically captures significant operations during a session, creating a progressive event log that prevents end-of-session recall failures. This document specifies what to capture, when to capture it, and how to structure captured data.

## Purpose

**Problem**: End-of-session synthesis relies on memory recall, which fails for complex sessions with many operations.

**Solution**: Progressive capture during the session creates concrete event log for synthesis.

**Benefits**:
- Eliminates recall failures
- Enables crash recovery (watching file persists across sessions)
- Provides concrete evidence for Memory synthesis
- Tracks error patterns for system monitoring
- Reduces cognitive load (automatic vs manual documentation)

## Session Watching File

### Location

`Memory/sessions/current-session.md`

### Structure

```markdown
---
sessionStart: [timestamp]
sessionID: [random-hash]
---

## Significant Operations
[Auto-populated agent/file/panel operations]

## Decisions & Clarifications
[Auto-populated user decisions]

## Errors & Anomalies
[Auto-populated tool failures]
```

### Lifecycle

1. **Creation**: Auto-created on first significant operation each session
2. **Persistence**: Survives until `/save` completes (crash recovery)
3. **Synthesis**: At `/save`, content synthesized to:
   - `activeContext.md` (operational context, decisions)
   - `systemMonitoring.md` (errors and system health)
4. **Archive**: Post-synthesis, moved to `Memory/sessions/archive/session-[timestamp].md`
5. **Limits**: 3000 lines maximum (prevent unbounded growth)

## Marker Override Protocol

Before ANY capture operation, check for marker files that disable logging:

```python
def should_capture_to_session():
    if exists("Memory/markers/silence"):
        return False  # Silence: disables Memory logging (hooks, session watching)
                      # NOTE: RP transcripts continue (roleplay-gm agent independent)

    if exists("Memory/markers/rp-active"):
        return False  # RP mode: hooks disabled, no session watching

    # Default: log everything
    return True
```

**Silence Mode** (`Memory/markers/silence`):
- Disables Memory synthesis
- Disables session watching
- RP transcripts continue (independent system)
- Use for: Sessions that shouldn't be remembered

**RP Mode** (`Memory/markers/rp-active`):
- Disables session watching
- RP transcripts continue in separate files
- Use for: Roleplay sessions (logged separately)

## Capture Triggers

Capture operations automatically after completion, only if `should_capture_to_session()` returns True:

### 1. Agent Deployments (Task Tool)

**Trigger**: Task tool invocation completes

**Capture Format**:
```
- [timestamp] Agent: {agent-name} → {output-summary-first-paragraph}
```

**Example**:
```
- [2025-11-15 12:30] Agent: code-reviewer → Identified 3 security vulnerabilities in auth module
```

**Section**: Significant Operations

### 2. File Modifications

**Trigger**: Edit/Write tool to Vault/Memory/Tools directories completes

**Capture Format**:
```
- [timestamp] Modified: {file-path} → {change-summary}
```

**Example**:
```
- [2025-11-15 12:35] Modified: Vault/World/Bestiary/Thorne Broodling.md → Created moss parasite creature
```

**Section**: Significant Operations

### 3. User Decisions

**Trigger**: AskUserQuestion response received

**Capture Format**:
```
- [timestamp] Decision: {question-header} → {user-selection}
```

**Example**:
```
- [2025-11-15 12:40] Decision: Auth method → OAuth 2.0 with PKCE
```

**Section**: Decisions & Clarifications

### 4. Panel Sessions

**Trigger**: Panel tool invocation completes

**Capture Format**:
```
- [timestamp] Panel: {topic} → {decision}
```

**Example**:
```
- [2025-11-15 13:00] Panel: Copyright clearance strategy → Consensus: Replace 3 creatures, rewrite 77 descriptions
```

**Section**: Significant Operations

### 5. RP Sessions

**Trigger**: roleplay-gm agent activity

**Capture Format**:
```
- [timestamp] RP: {scene-context} → {key-developments-or-outcomes}
```

**Example**:
```
- [2025-11-15 13:30] RP: Crowley-Lysander resonance crystal test → Discovered bidirectional feedback loops
```

**Section**: Significant Operations

**Note**: RP sessions also logged separately in dedicated RP transcripts

### 6. Errors & Failures

**Trigger**: Tool result contains `<error>` tags

**Capture Format**:
```
- [timestamp] ERROR: {tool-name} → {error-message} | Context: {what-was-being-attempted}
```

**Example**:
```
- [2025-11-15 14:15] ERROR: Bash → pathspec did not match | Context: Attempting git commit of Memory/activeContext.md
```

**Section**: Errors & Anomalies

**Categories Tracked**:
- Tool failures (file operations, bash commands, MCP)
- Agent deployment issues
- Git operation errors
- Permission/access denials
- Validation failures

## Implementation Approaches

### Level 1: Claude Code Hooks (Automatic)

Use hooks to automatically capture operations:

**PostToolUse Hook**:
```json
{
  "event": "PostToolUse",
  "command": "${CLAUDE_PLUGIN_ROOT}/scripts/capture-operation.sh"
}
```

**UserPromptSubmit Hook**:
```json
{
  "event": "UserPromptSubmit",
  "command": "${CLAUDE_PLUGIN_ROOT}/scripts/check-markers.sh"
}
```

**SessionEnd Hook**:
```json
{
  "event": "SessionEnd",
  "command": "${CLAUDE_PLUGIN_ROOT}/scripts/remind-save.sh"
}
```

### Level 2: Manual Protocol (All Platforms)

Claude follows capture protocol based on instructions:
- Check markers before each capture
- Append to watching file after significant operations
- Format per capture trigger specifications
- Update systemMonitoring.md with error patterns

### Level 3: Hybrid Approach

Hooks for automation where available, manual protocol fallback:
- Claude Code: Full automation via hooks
- Claude.ai: Manual capture with reminder prompts
- ChatGPT: Conversation-based progressive logging

## Session Watching Benefits

1. **Eliminates Recall Failures**: Concrete event log vs memory-based recall
2. **Enables Crash Recovery**: Watching file persists across interrupted sessions
3. **Provides Evidence**: Operations documented with timestamps
4. **Reduces Cognitive Load**: Automatic capture, no "remember to document"
5. **Enables Empirical Monitoring**: Error patterns inform self-repair
6. **Tracks System Health**: Failure modes and operational patterns
7. **Supports Data-Driven Decisions**: Actual usage vs theoretical recommendations

## Validation

**Before Synthesis** (`/save`):
- Watching file exists and has content
- All sections populated appropriately
- Timestamps chronological
- No duplicate entries

**During Synthesis**:
- Extract key accomplishments → activeContext.md
- Extract decisions → activeContext.md
- Extract errors → systemMonitoring.md

**After Synthesis**:
- Archive watching file to `Memory/sessions/archive/`
- Reset with new sessionID
- Increment systemMonitoring.md session count

## File Size Management

**3000-Line Limit**:
- Prevents unbounded growth during long sessions
- Triggers warning at 2500 lines
- Forces synthesis or session break
- Rare in practice (typical sessions <500 lines)

**Size Monitoring**:
- Check line count on each append
- Warn at 2500 lines
- Hard stop at 3000 lines
- Recommend `/save` to synthesize

## Examples

### Example Session Watching File

```markdown
---
sessionStart: 2025-11-15 12:00 UTC
sessionID: fustian-unsavory
---

## Significant Operations
- [2025-11-15 12:15] Agent: intellectual-property → Analyzed 442 worldbuilding files, 367 approved for commercial use
- [2025-11-15 12:30] Modified: Vault/World/Bestiary/Wooden Companions.md → Created original AAS creature replacing Dark Young
- [2025-11-15 12:45] Modified: Vault/World/Bestiary/Thorne Broodling.md → Created moss parasite creature, pollution not offspring
- [2025-11-15 13:00] Panel: Copyright clearance → All commercial blockers resolved, 0 copyrighted creatures remaining

## Decisions & Clarifications
- [2025-11-15 12:20] Decision Point: Birthing Trees verification
- [2025-11-15 12:50] Decision Point: Broodling spread mechanics
- [2025-11-15 13:10] Decision Point: Campaign threat resolution

## Errors & Anomalies
- [2025-11-15 13:20] ERROR: Git → pathspec did not match | Context: Working directory not set correctly
```

## Platform-Specific Notes

**Claude Code**:
- Hooks automate capture
- Bash scripts handle file operations
- Git integration for archiving

**Claude.ai**:
- Manual capture via protocol instructions
- Projects feature stores watching file
- Manual synthesis workflow

**ChatGPT**:
- Conversation-based logging
- Custom GPT Instructions include capture protocol
- Export conversation as session archive

**Gemini**:
- TBD - research platform capabilities
