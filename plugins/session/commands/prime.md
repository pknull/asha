---
description: "Interactive codebase exploration through guided conversation"
argument-hint: "Optional: project-name or directory-path"
allowed-tools: ["Read", "Glob", "Grep", "Bash"]
---

# Prime Command - Interactive Codebase Discovery

Execute structured codebase exploration through guided conversation.

## Usage

```bash
/prime [optional: project-name or directory-path]
```

## Behavior

**Phase 1: Discovery**
- If no argument: List available projects from Work/projects/ and ask which to explore
- If project name: Start exploration of that project
- If directory path: Explore that directory structure

**Phase 2: Structure Analysis**
- Read project README.md if exists
- List main directories and key files
- Identify file types and purposes
- Present exploration options to user

**Phase 3: Interactive Exploration**
- User selects area of interest
- Deep dive into selected files/directories
- Build knowledge graph of relationships
- Offer next exploration options based on findings

**Phase 4: Summary**
- Synthesize understanding of explored areas
- Document key findings
- Suggest next areas for exploration or work

## Example Session

```
User: /prime callum
Assistant: Exploring Work/projects/callum/
          Found: chapters/ (22 files), editing-guidelines.md,
                 character-knowledge-timeline.md, reference-conversations/

          What should I explore?
          1. Chapter structure and progression
          2. Editing guidelines and constraints
          3. Character knowledge tracking
          4. Reference conversations

User: 1
Assistant: [Reads chapter files, analyzes structure]
          Found 22 chapters (Ch01-Ch22), ~8-12 KB each
          Story progression: transformation narrative

          Should I:
          1. Analyze specific chapter content
          2. Review chapter transitions
          3. Check consistency across chapters

User: 3
Assistant: [Runs consistency analysis across chapters]
          ...
```

## Integration

- Works with Work/projects/ structure
- Can explore Vault/ directories
- Respects workspace markers (silence, rp-active)
- Uses existing file reading tools (Read, Glob, Grep)
- Builds on Memory context when available

## Notes

- Progressive token usage (only reads what's needed)
- User-guided exploration (not automated dump)
- Iterative deepening based on user interest
- Can be interrupted and resumed
- Findings can inform Memory updates
