# Platform Adaptation Guide

## Overview

The Memory+Session Manager plugin is designed for Claude Code but the core protocol can be adapted to any AI platform. This guide shows how to implement Memory Bank maintenance and session capture across different platforms.

## Core Protocol (Platform-Agnostic)

**Foundation Principles** (work anywhere):
1. Memory Bank structure (required + optional files)
2. Frontmatter schema for all Memory files
3. Session watching capture triggers
4. Four Questions Protocol for synthesis
5. Memory update decision logic

**Platform-Specific Enhancements**:
- Automation level varies by platform capabilities
- Graceful degradation: manual workflows as fallback
- Cross-platform compatibility: same Memory structure everywhere

## Platform Comparison Matrix

| Feature | Claude Code | Claude.ai | ChatGPT | Gemini |
|---------|-------------|-----------|---------|--------|
| **Automatic Session Watching** | ✅ Yes (hooks) | ❌ Manual | ❌ Manual | ❌ Manual |
| **Memory File Storage** | ✅ Filesystem | ✅ Projects | ⚠️ Knowledge | ❌ None |
| **Git Integration** | ✅ Native | ❌ Manual | ❌ Manual | ❌ Manual |
| **Slash Commands** | ✅ Yes | ❌ No | ❌ No | ❌ No |
| **Skills (Auto-Invoke)** | ✅ Yes | ❌ No | ⚠️ GPT Actions | ❌ No |
| **Session Persistence** | ✅ File-based | ⚠️ Project-based | ⚠️ Conversation | ❌ None |
| **Recommended Approach** | Full Automation | Manual Protocol | Hybrid | Manual |

**Legend**:
- ✅ Native support
- ⚠️ Limited/workaround available
- ❌ Not supported

---

## Claude Code Adaptation (Full Automation)

**Installation Method**: Plugin marketplace

### Setup

1. **Add marketplace**:
   ```bash
   /plugin marketplace add ~/Code/asha-marketplace
   ```

2. **Install plugin**:
   ```bash
   /plugin install memory-session-manager@asha-marketplace
   ```

3. **Verify installation**:
   ```bash
   /plugin list
   ```

### Provided Features

**Commands**:
- `/save` - Interactive session synthesis workflow

**Skills** (auto-invoked):
- `memory-maintenance` - Frontmatter schema guidance when updating Memory files

**Hooks** (automatic):
- `PostToolUse` - Captures file mods, agent deployments, errors
- `UserPromptSubmit` - Logs user prompts and decisions
- `SessionEnd` - Archives watching file on clean exit

**Scripts**:
- `save-session.sh` - Portable bash automation

### Workflow

**During Session** (automatic):
- Operations captured to `Memory/sessions/current-session.md`
- Errors logged with context
- Marker overrides respected (silence/rp-active)

**End of Session** (manual):
1. `/save` - Display summary + Four Questions guidance
2. Update Memory files per guidance
3. `save-session.sh --archive-only` - Archive watching file
4. `git add Memory/ && git commit && git push`

### Advantages

- Zero cognitive overhead (automatic capture)
- Crash recovery (watching file persists)
- Audit trail (git-tracked archives)
- Systematic synthesis (guided workflow)

### Limitations

- Requires bash and filesystem access
- Git integration manual step
- Hooks may not capture all edge cases

---

## Claude.ai Adaptation (Manual Protocol)

**Installation Method**: Custom Instructions + Project Knowledge

### Setup

1. **Create Project**: "Memory Bank" or project-specific name

2. **Add Foundation Files** (via Projects Knowledge):
   - `Memory/activeContext.md`
   - `Memory/projectbrief.md`
   - `Memory/communicationStyle.md`

3. **Add Custom Instructions**:
   ```markdown
   # Memory Bank Protocol

   At session start, read:
   - Memory/activeContext.md (current status, next steps)
   - Memory/projectbrief.md (scope, objectives)
   - Memory/communicationStyle.md (voice, persona)

   Memory File Frontmatter (all files):
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

   Update Memory files when: ≥25% code impact, pattern discovery, user request, context ambiguity
   ```

4. **Create Session Template** (manual copy-paste):
   ```markdown
   # Session [Date] - [Title]

   ## Significant Operations
   - [timestamp] [Description]

   ## Decisions & Clarifications
   - [timestamp] [User decision]

   ## Discoveries & Patterns
   - [timestamp] [Insight]

   ## Errors & Anomalies
   - [timestamp] [Error with context]

   ## Candidates for Next Steps
   - [Follow-up task]
   ```

### Workflow

**Session Start**:
1. Claude automatically reads Memory foundation files
2. Begin working on task

**During Session** (manual logging):
- When significant operation completes, copy-paste template and add entry
- Paste accumulated session log into conversation
- Claude can see session context for synthesis

**End of Session**:
1. User: "Synthesize session using Four Questions Protocol"
2. Claude: Displays Four Questions guidance
3. User answers questions (or delegates to Claude based on session log)
4. Claude updates Memory files via Projects interface
5. User manually copies updated Memory files back to filesystem (if git-tracked)

### Advantages

- Works on mobile/web without setup
- Projects feature provides persistent storage
- No technical dependencies (bash, git)

### Limitations

- Manual session watching (cognitive overhead)
- No crash recovery (session log in conversation)
- No automatic archiving
- File sync to git requires manual export
- No automatic error synthesis

### Optimization Tips

**Session Log Macro**:
Create text expansion shortcut:
```
;session
→ - [HH:MM] [DESCRIPTION]
```

**Four Questions Template**:
Saved chat message:
```
Please synthesize this session:

1. What was the goal?
2. What did we accomplish?
3. What did we learn?
4. What comes next?
```

**Memory Export Workflow**:
1. Projects → Download Memory files as zip
2. Extract to local repository
3. `git add Memory/ && git commit`

---

## ChatGPT Adaptation (Hybrid Approach)

**Installation Method**: Custom GPT + Knowledge Files

### Setup

1. **Create Custom GPT**: "Memory Bank Assistant"

2. **Upload Knowledge Files**:
   - `Memory-Structure-Spec.md` (from plugin docs)
   - `Session-Capture-Protocol.md` (from plugin docs)
   - `Four-Questions-Template.md` (create minimal version)

3. **GPT Instructions**:
   ```markdown
   You are a Memory Bank Assistant maintaining persistent context across sessions.

   # Memory Access Protocol
   At conversation start, ask user for:
   - Memory/activeContext.md (current status)
   - Memory/projectbrief.md (scope, objectives)
   - Memory/communicationStyle.md (voice, persona)

   # Memory File Standards
   [Paste frontmatter schema from MEMORY-STRUCTURE.md]

   # Session Capture
   During conversation, maintain running log:
   - Significant operations
   - User decisions
   - Discoveries & patterns
   - Errors & anomalies

   # Session Synthesis
   At user request, apply Four Questions Protocol:
   [Paste protocol from SESSION-SAVE.md]

   # Memory Updates
   Propose Memory file updates with:
   - Version increment
   - Timestamp update
   - Session summary formatted per spec
   ```

4. **GPT Actions** (optional, advanced):
   - API integration to read/write Memory files from external storage
   - GitHub API for direct git commits
   - Webhook for session archiving

### Workflow

**Session Start**:
1. User pastes Memory/activeContext.md content into chat
2. GPT reads and confirms context loaded

**During Session**:
- GPT maintains session log in conversation
- User can ask "show session log" for current state

**End of Session**:
1. User: "Synthesize session and update Memory"
2. GPT: Shows Four Questions, proposes Memory updates
3. User: Copies proposed updates to local Memory files
4. User: `git add Memory/ && git commit`

**Alternative** (with Actions):
1. GPT uses Actions to commit directly to git
2. Full automation (closest to Claude Code experience)

### Advantages

- Custom GPT provides persistent instructions
- Knowledge files ensure protocol consistency
- Actions enable automation (if configured)
- Conversation export provides session archive

### Limitations

- Memory files must be pasted manually each session
- No automatic session watching (GPT tracks in conversation)
- Actions require technical setup
- Knowledge files have size limits (2MB per file)

### Optimization Tips

**Memory Sync Shortcut**:
Create bash alias:
```bash
alias memory-sync='cat Memory/activeContext.md | pbcopy && echo "Memory context copied to clipboard"'
```

**Conversation Archive**:
- Export conversation as markdown after `/save`
- Store in `Memory/sessions/archive/chatgpt-session-[timestamp].md`
- Provides historical evidence like Claude Code archives

**GPT Actions Example** (Python):
```python
# GitHub API integration for Memory file updates
import requests

def update_memory_file(file_path, content, commit_message):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    data = {
        "message": commit_message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": get_file_sha(file_path)  # Get current file SHA
    }
    response = requests.put(url, headers=headers, json=data)
    return response.json()
```

---

## Gemini Adaptation (Manual Protocol)

**Installation Method**: Manual instructions (no persistent storage)

### Current Limitations

Gemini (as of 2025-11) lacks:
- Persistent file storage
- Projects feature
- Custom instructions
- Conversation memory across sessions

**Implication**: Memory files must be pasted EVERY session

### Setup

1. **Create Prompt Template** (save locally):
   ```markdown
   You are operating with a Memory Bank system.

   # Memory Files (read these first)

   ## Memory/activeContext.md
   [PASTE CONTENT]

   ## Memory/projectbrief.md
   [PASTE CONTENT]

   ## Memory/communicationStyle.md
   [PASTE CONTENT]

   # Memory Frontmatter Schema
   [PASTE SCHEMA]

   # Session Capture Instructions
   Maintain session log during conversation:
   - Significant operations
   - Decisions & clarifications
   - Discoveries & patterns
   - Errors & anomalies

   # Four Questions Protocol
   [PASTE PROTOCOL]

   Now proceeding with user request...
   ```

2. **Create Session Template** (save locally):
   ```markdown
   # Session Log

   ## Significant Operations
   ## Decisions & Clarifications
   ## Discoveries & Patterns
   ## Errors & Anomalies
   ## Candidates for Next Steps
   ```

### Workflow

**Session Start**:
1. Paste prompt template with Memory file contents
2. Provide user task/question
3. Gemini reads context and proceeds

**During Session**:
- Manually track operations in local session template
- Paste session log periodically to keep Gemini aware

**End of Session**:
1. Paste session log
2. Request: "Synthesize session using Four Questions Protocol"
3. Gemini proposes Memory updates
4. User copies updates to local Memory files
5. `git add Memory/ && git commit`

### Advantages

- No setup required
- Works immediately
- Full control over Memory files

### Limitations

- Highest cognitive overhead (manual everything)
- No crash recovery
- No session persistence
- Token limit constraints on large Memory files
- Must re-paste Memory every session

### Optimization Tips

**Macro Expansion**:
Use text expander for prompt template:
```
;geminimem
→ [Full prompt template with placeholders]
```

**Memory Compression**:
If Memory files exceed token limits:
1. Create `Memory/activeContext-summary.md` (compressed version)
2. Paste summary instead of full file
3. Keep full file in git, use summary for Gemini sessions

**Session Batching**:
- Minimize Gemini sessions (work in batches)
- Reduce frequency of Memory pasting
- Use for specific tasks rather than general work

---

## Cross-Platform Best Practices

### Unified Memory Structure

**Use identical file structure across all platforms**:
```
Memory/
├── activeContext.md         # Required
├── projectbrief.md          # Required
├── communicationStyle.md    # Required
├── workflowProtocols.md     # Optional
├── techEnvironment.md       # Optional
├── productContext.md        # Optional
└── sessions/                # Platform-specific
    ├── current-session.md   # (Claude Code only)
    └── archive/             # Platform-specific archives
```

**Why**: Portability - can switch platforms without restructuring

### Version Control Integration

**Git-track Memory across all platforms**:

**Claude Code**:
- Native git integration
- Automated commits via hooks

**Claude.ai**:
- Manual export from Projects
- Sync to local repository
- Git commit locally

**ChatGPT**:
- Local Memory files (git-tracked)
- Custom GPT Actions for direct commits (optional)

**Gemini**:
- Local Memory files (git-tracked)
- Manual updates only

**Benefits**:
- Unified history across platforms
- Conflict resolution when switching
- Backup and recovery
- Team collaboration

### Session Archive Strategy

**Platform-Specific Archives**:

```
Memory/sessions/archive/
├── claude-code-session-2025-11-15_12-30.md
├── claude-ai-session-2025-11-16_09-00.md
├── chatgpt-session-2025-11-17_14-15.md
└── gemini-session-2025-11-18_11-45.md
```

**Naming Convention**: `{platform}-session-{timestamp}.md`

**Why**: Track which platform created which context

### Frontmatter Consistency

**Use identical frontmatter across platforms**:

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

**Enforcement**:
- Claude Code: memory-maintenance skill (automatic)
- Other platforms: Manual validation or linter script

### Migration Path

**Scenario**: Starting on Platform A, moving to Platform B

**Steps**:
1. Export Memory files from Platform A
2. Commit to git
3. Import to Platform B storage
4. Continue with Platform B workflow
5. Memory structure unchanged

**Example: Claude.ai → Claude Code**:
1. Projects → Download Memory files
2. Move to local repository
3. `git add Memory/ && git commit`
4. Install memory-session-manager plugin
5. Continue with automated workflow

---

## Implementation Examples

### Claude Code Full Automation

```bash
# Install plugin
/plugin marketplace add ~/Code/asha-marketplace
/plugin install memory-session-manager@asha-marketplace

# Work on task...
# (hooks automatically capture operations)

# End of session
/save

# Review summary, answer Four Questions
# Update Memory files per guidance

# Archive session
./scripts/save-session.sh --archive-only

# Git commit
git add Memory/
git commit -m "session: Plugin architecture analysis"
git push
```

### Claude.ai Manual Protocol

```markdown
# Session Start
[Claude auto-reads Memory/activeContext.md from Projects]

User: "Implement user authentication"

# During Session
User: (mentally tracking operations)
- [12:30] Modified: src/auth/login.ts
- [12:45] Decision: Use JWT with refresh tokens
- [13:00] Pattern: Token rotation strategy

# End of Session
User: "Synthesize session using Four Questions Protocol"

Claude: [Shows Four Questions guidance]

User: [Answers questions or asks Claude to synthesize from log]

Claude: [Proposes Memory updates via Projects interface]

User: [Copies updated files to local git repo]

$ git add Memory/
$ git commit -m "session: Auth implementation"
```

### ChatGPT with GPT Actions

```python
# Custom GPT Action: update_memory
# Configured in GPT Actions settings

def update_memory(file_path: str, content: str, commit_msg: str):
    """Update Memory file and commit to GitHub"""
    # Base64 encode content
    encoded = base64.b64encode(content.encode()).decode()

    # Get current file SHA
    get_url = f"{github_api_base}/{file_path}"
    sha = requests.get(get_url, headers=headers).json()["sha"]

    # Update file
    put_url = f"{github_api_base}/{file_path}"
    data = {
        "message": commit_msg,
        "content": encoded,
        "sha": sha
    }
    response = requests.put(put_url, headers=headers, json=data)
    return response.json()

# In conversation:
User: "Synthesize session and update Memory"

GPT: [Calls update_memory Action with proposed changes]
GPT: "Memory updated and committed to GitHub"
```

---

## Troubleshooting

### Issue: Memory Files Out of Sync Across Platforms

**Symptom**: activeContext.md different on Claude Code vs Claude.ai

**Cause**: Concurrent edits without git sync

**Solution**:
1. Identify canonical version (git log timestamps)
2. Merge changes manually
3. Commit unified version
4. Re-sync to all platforms

### Issue: Frontmatter Schema Inconsistent

**Symptom**: Missing required fields or wrong format

**Cause**: Manual editing without validation

**Solution**:
1. Create frontmatter linter script:
   ```bash
   # validate-frontmatter.sh
   for file in Memory/*.md; do
     if ! grep -q "^version:" "$file"; then
       echo "Missing version: $file"
     fi
     # Check other required fields...
   done
   ```
2. Run before git commit
3. Fix violations

### Issue: Session Archive Size Growing Unbounded

**Symptom**: Memory/sessions/archive/ exceeds 100 files

**Cause**: Long-running project with many sessions

**Solution**:
1. Compress old archives:
   ```bash
   tar -czf sessions-2025-Q1.tar.gz Memory/sessions/archive/session-2025-0*
   rm Memory/sessions/archive/session-2025-0*
   ```
2. Git-track compressed archives
3. Keep last 20-30 sessions uncompressed

### Issue: Platform Token Limits

**Symptom**: "Context too long" errors when pasting Memory

**Cause**: Memory files exceed platform token limits

**Solution**:
1. Create compressed summary versions:
   - `activeContext-summary.md` (essential info only)
   - `projectbrief-summary.md` (scope + objectives only)
2. Use summaries for platforms with limits (Gemini)
3. Keep full files for platforms with larger contexts (Claude Code, Claude.ai)

---

## Future Platform Support

### Cursor

**Status**: Research needed

**Potential Features**:
- Similar to Claude Code (filesystem access)
- Hooks system (if available)
- Git integration

### GitHub Copilot Chat

**Status**: Limited Memory support

**Potential Approach**:
- Workspace files as Memory
- Comment-based frontmatter
- Session log in comments

### Augment Code

**Status**: Research needed

**Potential Features**:
- Multi-file context
- Custom instructions
- Automation capabilities

---

## Summary

| Use Case | Recommended Platform | Rationale |
|----------|---------------------|-----------|
| **Software Development** | Claude Code | Full automation, git integration, filesystem access |
| **Mobile/On-the-Go** | Claude.ai | Projects feature, no setup, works anywhere |
| **Team Collaboration** | ChatGPT (with Actions) | Custom GPT shareable, API automation possible |
| **Experimentation** | Gemini | Zero setup, immediate use, manual control |

**Key Takeaway**: The Memory Bank protocol is platform-agnostic. Choose automation level based on platform capabilities, but maintain identical Memory structure everywhere for portability.
