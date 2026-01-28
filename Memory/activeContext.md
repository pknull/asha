---
version: "2.0"
lastUpdated: "2026-01-28 12:30 UTC"
lifecycle: "maintenance"
stakeholder: "all"
changeTrigger: "Session save - schedule plugin created and deployed"
validatedBy: "ai"
dependencies: ["communicationStyle.md", "keeperProfile.md"]
---

# activeContext

## Current Project Status

**Primary Focus**: Claude Code plugin marketplace providing domain-focused plugins for research, development, creative writing, automation, and session coordination.

**Active Work**:
- Marketplace v1.8.0 with 7 plugins (added schedule plugin)
- Schedule plugin fully tested end-to-end with systemd timer execution

**Recent Activities** (last 7 days):
- **2026-01-28**: Schedule plugin created - Full cron-style task automation plugin from research through deployment. Natural language time parser (20+ expressions), task management with security validation (rate limiting, dangerous command blocking, duplicate detection), systemd/cron backend with auto-detection, execution wrapper with logging. End-to-end tested: tasks execute on schedule, Claude responds correctly. Marketplace bumped to v1.8.0.
- **2026-01-27**: Redis whitepaper analysis - Analyzed "Build an AI app in 5 steps" whitepaper; concluded Memory Bank architecture is sound; created `keeperProfile.md` for persistent user preferences
- **2026-01-26 PM**: Agent migration and standards audit - Migrated 9 agents from REDACTED99; added image plugin; standards compliance audit
- **2026-01-26 AM**: Domain restructuring complete - Created code plugin; created write plugin; moved ACE cycle to asha/cognitive.md

## Critical Reference Information

- **Plugins**: panel-system (v4.2.0), code (v1.0.1), write (v1.1.1), output-styles (v1.0.2), asha (v1.7.1), image (v1.0.0), schedule (v0.1.0)
- **Domain separation**: panel=research, code=development, write=creative, image=generation, schedule=automation, asha=core scaffold
- **Main docs**: CLAUDE.md contains comprehensive repository guide
- **Vector DB**: Indexed, ollama + chromadb operational
- **Shared files**: communicationStyle.md → /home/pknull/Obsidian/REDACTED99/Memory/communicationStyle.md
- **Test suite**: `./tests/run-tests.sh` runs all tests

## Next Steps

**Immediate**:
- [x] Update CLAUDE.md version history with v1.7.0 and v1.8.0 changes (done)
- [ ] Run test suite to validate all changes

**Blocked**:
- None

**Deferred**:
- Add CHANGELOG.md (low priority per audit)
- Consider scoped Bash permissions (e.g., `Bash(git:*)`) per blog post recommendation
- Add "## Key Facts" section to activeContext.md when atomic facts accumulate
- Consider reranking enhancement for memory_index.py (recency/source weighting)

## Session Learnings

**Redis Whitepaper Analysis (2026-01-27)**:
- Semantic caching doesn't apply to Asha (not a persistent service)
- Agentic memory slots = already have via Memory Bank files
- "Facts" don't need a new system - a section in activeContext.md suffices
- Preference persistence solved via keeperProfile.md + bootstrap dependency chain

**Standards Reference**: https://leehanchung.github.io/blogs/2025/10/26/claude-skills-deep-dive/

Key compliance requirements:
- All commands/skills need YAML frontmatter (name, description, allowed-tools)
- Use `{baseDir}` not hardcoded paths for portability
- Keep tool permissions specific, not overly broad
- SKILL.md should stay under 5000 words
