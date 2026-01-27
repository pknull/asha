---
version: "1.9"
lastUpdated: "2026-01-27 08:15 UTC"
lifecycle: "maintenance"
stakeholder: "all"
changeTrigger: "Session save - Redis whitepaper analysis, keeperProfile.md creation"
validatedBy: "ai"
dependencies: ["communicationStyle.md", "keeperProfile.md"]
---

# activeContext

## Current Project Status

**Primary Focus**: Claude Code plugin marketplace providing domain-focused plugins for research, development, creative writing, and session coordination.

**Active Work**:
- Marketplace v1.7.0 with 6 plugins (added image plugin)
- Standards compliance audit completed per blog post review
- All plugin versions incremented for upgrade path

**Recent Activities** (last 7 days):
- **2026-01-27**: Redis whitepaper analysis - Analyzed "Build an AI app in 5 steps" whitepaper for Asha improvements; concluded existing Memory Bank architecture is sound; created `keeperProfile.md` for persistent user preferences; added to `communicationStyle.md` dependencies (v4.7); key insight: "facts" are best handled as a section in activeContext.md, not a new system
- **2026-01-26 PM**: Agent migration and standards audit - Migrated 9 new agents from REDACTED99; added new image plugin; audited against Claude Code skills best practices; fixed 10 hardcoded paths, added frontmatter to 6 files; bumped all affected plugin versions
- **2026-01-26 AM**: Domain restructuring complete - Created code plugin; created write plugin; absorbed local-review into code as /code:review; moved ACE cycle to asha/cognitive.md
- **2026-01-25**: memory_index.py robustness improvements - Added faulthandler/signal handling; Ollama retry logic; robust embedding handling; bumped Asha to v1.5.0

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
