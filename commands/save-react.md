---
tool: bash
command: cd "${PROJECT_DIR}" && ./asha/commands/save-react
description: "Experimental ReAct-enhanced save command that analyzes sessions for patterns, redundancies, and cross-project opportunities"
---

# /save-react

Experimental ReAct-enhanced save command that uses intelligent analysis to:
- Detect code patterns and repetitions
- Identify redundancies with existing memory
- Extract novel insights
- Suggest abstractions and refactoring opportunities
- Find cross-project sharing opportunities

## Usage

```
/save-react
```

This command will:
1. Analyze the current session using local pattern matching
2. Display recommendations and insights
3. Request AI analysis for deeper pattern understanding
4. Allow you to then run regular `/save` if desired

## Differences from /save

- `/save` - Traditional linear compression and storage
- `/save-react` - Intelligent analysis with actionable recommendations

Both commands work together - use `/save-react` for analysis, then `/save` to persist.