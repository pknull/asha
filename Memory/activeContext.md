---
version: "1.8"
lastUpdated: "2026-01-26 19:40 UTC"
lifecycle: "maintenance"
stakeholder: "all"
changeTrigger: "Session save - v1.7.0 agent migration and standards compliance audit"
validatedBy: "ai"
dependencies: ["communicationStyle.md"]
---

# activeContext

## Current Project Status

**Primary Focus**: Claude Code plugin marketplace providing domain-focused plugins for research, development, creative writing, and session coordination.

**Active Work**:
- Marketplace v1.7.0 with 6 plugins (added image plugin)
- Standards compliance audit completed per blog post review
- All plugin versions incremented for upgrade path

**Recent Activities** (last 7 days):
- **2026-01-26 PM**: Agent migration and standards audit - Migrated 9 new agents from REDACTED99 (partner-sentiment, task-manager, architect, comfyui-prompt-engineer, ai-detector, fiction-writer, intimacy-designer, manuscript-editor, prose-analysis); added new image plugin; audited against Claude Code skills best practices blog post; fixed 10 hardcoded paths, added frontmatter to 6 files, added allowed-tools to 2 commands; bumped all affected plugin versions
- **2026-01-26 AM**: Domain restructuring complete - Created code plugin (codebase-historian, orchestration, recipes); created write plugin (5 agents, writing.md, recipes); absorbed local-review into code as /code:review; moved ACE cycle to asha/cognitive.md; cleaned up all stale references; panel approved structure with 100% consensus; code review identified and fixed 7 issues
- **2026-01-25**: memory_index.py robustness improvements - Added faulthandler/signal handling; Ollama retry logic; robust embedding handling; bumped Asha to v1.5.0
- **2026-01-18**: Ralph loop test expansion - Doubled test coverage from 91→182 tests
- **2026-01-17**: Audit remediation - Fixed hook permissions; synchronized versions; created test framework

## Critical Reference Information

- **Plugins**: panel-system (v4.2.0), code (v1.0.1), write (v1.1.1), output-styles (v1.0.2), asha (v1.7.1), image (v1.0.0)
- **Domain separation**: panel=research, code=development, write=creative, image=generation, asha=core scaffold
- **Main docs**: CLAUDE.md contains comprehensive repository guide
- **Vector DB**: Indexed, ollama + chromadb operational
- **Shared files**: communicationStyle.md → /home/pknull/Obsidian/REDACTED99/Memory/communicationStyle.md
- **Test suite**: `./tests/run-tests.sh` runs all tests

## Next Steps

**Immediate**:
- [ ] Run test suite to validate changes
- [ ] Update CLAUDE.md version history with v1.7.0 changes

**Blocked**:
- None

**Deferred**:
- Add CHANGELOG.md (low priority per audit)
- Consider scoped Bash permissions (e.g., `Bash(git:*)`) per blog post recommendation

## Session Learnings

**Standards Reference**: https://leehanchung.github.io/blogs/2025/10/26/claude-skills-deep-dive/

Key compliance requirements:
- All commands/skills need YAML frontmatter (name, description, allowed-tools)
- Use `{baseDir}` not hardcoded paths for portability
- Keep tool permissions specific, not overly broad
- SKILL.md should stay under 5000 words
