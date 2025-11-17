# Troubleshooting Guide

## Session File Not Auto-Creating

**Symptom**: `Memory/sessions/current-session.md` doesn't exist after conversation starts

**Diagnosis**:
```bash
# Check if plugin installed
cat ~/.claude/plugins/installed_plugins.json | grep memory-session-manager

# Check if hooks exist
ls -la ~/.claude/plugins/marketplaces/asha-marketplace/plugins/memory-session-manager/hooks/

# Test hook manually
CLAUDE_PROJECT_DIR=$(pwd) bash ~/.claude/plugins/marketplaces/asha-marketplace/plugins/memory-session-manager/hooks/post-tool-use <<< '{"tool_name": "Read", "tool_input": {}, "tool_response": {}}'
```

**Causes**:
1. Claude Code hasn't restarted since plugin installation
2. Hooks disabled in Claude Code settings
3. Environment variable `$CLAUDE_PROJECT_DIR` not set properly
4. Marker file blocking capture (`Memory/markers/silence` or `Memory/markers/rp-active`)

**Solutions**:

### Solution 1: Restart Claude Code
Exit and restart Claude Code to reload plugin hooks.

### Solution 2: Manual Session Initialization
If hooks still don't fire after restart, manually create session file:

```bash
cat > Memory/sessions/current-session.md <<'EOF'
---
sessionStart: $(date -u '+%Y-%m-%d %H:%M UTC')
sessionID: manual-$(head /dev/urandom | tr -dc a-f0-9 | head -c 8)
---

## Significant Operations
<!-- Manual logging until hooks activate -->

## Decisions & Clarifications
<!-- Manual logging until hooks activate -->

## Discoveries & Patterns
<!-- Manual logging until hooks activate -->

## Errors & Anomalies
<!-- Manual logging until hooks activate -->

## Candidates for Next Steps
<!-- Manual logging until hooks activate -->
EOF
```

Once created, manually append operations during session, then use `/save` normally at session end.

### Solution 3: Check Marker Files
```bash
# Remove silence mode if accidentally active
rm -f Memory/markers/silence

# Remove RP mode if not doing roleplay
rm -f Memory/markers/rp-active
```

### Solution 4: Verify Hook Permissions
```bash
# Ensure hooks are executable
chmod +x ~/.claude/plugins/marketplaces/asha-marketplace/plugins/memory-session-manager/hooks/*
```

## `/save` Command Not Found

**Symptom**: `/save` command doesn't execute

**Diagnosis**:
```bash
# Check if command exists
ls -la ~/.claude/plugins/marketplaces/asha-marketplace/plugins/memory-session-manager/commands/
```

**Solution**: Use SlashCommand tool to invoke:
```
SlashCommand("/save")
```

Or manually run the save script:
```bash
bash Code/asha-marketplace/plugins/memory-session-manager/scripts/save-session.sh
```

## Session File Growing Too Large

**Symptom**: Session file exceeds 3000 lines

**Solution**: Run `/save` to synthesize and archive, then continue with fresh session file.

**Prevention**: For very long sessions, run `/save` at natural breakpoints (e.g., after completing major tasks).

## Hooks Capturing Unwanted Operations

**Symptom**: Too much noise in session file from minor operations

**Solution**: Temporarily enable silence mode for low-signal work:
```bash
# Enable silence
touch Memory/markers/silence

# Do noisy work...

# Disable silence
rm Memory/markers/silence
```

Session watching resumes after removing marker.

## Git Commit Failures During `/save`

**Symptom**: `/save` completes but git commit fails

**Diagnosis**:
```bash
git status
git log -1
```

**Causes**:
1. Uncommitted changes blocking automated commit
2. Git credentials not configured
3. Detached HEAD state

**Solutions**:

### Manual Commit
```bash
git add Memory/sessions/archive/session-*.md Memory/activeContext.md
git commit -m "session: [description]"
git push
```

### Skip Git Integration
Edit session save script to remove git commands if not using version control.

## Memory File Frontmatter Errors

**Symptom**: Memory file updates rejected due to invalid frontmatter

**Solution**: The `memory-maintenance` skill auto-invokes when updating Memory files. Follow its schema guidance:

```yaml
---
version: "X.Y"
lastUpdated: "YYYY-MM-DD HH:MM UTC"
lifecycle: "initiation|planning|execution|maintenance"
stakeholder: "technical|business|regulatory|all"
changeTrigger: "≥25% code impact|pattern discovery|user request|context ambiguity"
validatedBy: "human|ai|system"
dependencies: ["file1.md", "file2.md"]
---
```

## Hooks Working But Missing Operations

**Symptom**: Some operations captured, others missing

**Check**: Hook filters in `post-tool-use` script only logs:
- Edit/Write to `Vault/`, `Memory/`, `Tools/` directories
- Task tool agent deployments
- Significant errors

**Expected Behavior**: Read operations, internal `.claude/` changes, and trivial Bash commands are intentionally excluded to reduce noise.

## Cross-Platform Issues

### Claude.ai (Projects)
- Hooks not available → Use manual capture protocol
- Session file must be manually created and updated
- `/save` workflow still works via SlashCommand

### ChatGPT (Custom GPT)
- No file system access → Use conversation export as session archive
- Manual synthesis to Memory Bank files

### Gemini
- TBD - platform capabilities under research

See `PLATFORM-ADAPTERS.md` for platform-specific implementation guides.

## Getting Help

1. Check plugin version: `cat ~/.claude/plugins/installed_plugins.json | grep memory-session-manager`
2. Review recent changes: `git log --oneline -- Code/asha-marketplace/plugins/memory-session-manager`
3. Test hook manually (see "Session File Not Auto-Creating" diagnosis)
4. Check Memory Bank structure: `ls -la Memory/`

If issues persist after troubleshooting, consider manual session logging workflow until hooks can be debugged.
