---
version: "2.7"
lastUpdated: "2026-03-08 20:15 UTC"
lifecycle: "maintenance"
stakeholder: "all"
changeTrigger: "ECC review complete - learnings system, secret scrubbing"
validatedBy: "ai"
dependencies: ["~/.asha/communicationStyle.md", "~/.asha/keeper.md"]
---

# activeContext

## Current Project Status

**Primary Focus**: Claude Code plugin marketplace providing domain-focused plugins for research, development, creative writing, automation, and session coordination.

**Active Work**:

- Marketplace v1.14.0 with 7 plugins
- Asha v1.18.0: Confidence-tracked learnings, secret scrubbing, automatic pattern detection
- Code v1.11.0: Unified verification, framework skills (Django/Spring Boot/Go), parallel orchestration

**Recent Activities** (last 7 days):

- **2026-03-08**: ECC review complete — systematic improvement session (Part 2)
  - Added `learnings_manager.py` for structured learnings with confidence tracking (0.3-0.9)
  - Migrated `~/.asha/learnings.md` to structured format with trigger/action/evidence
  - Added secret scrubbing to `event_store.py` (API keys, JWTs, AWS keys, GitHub tokens)
  - Added automatic learning detection to `pattern_analyzer.py` (error→resolution, agent sequences, tool preference)
  - Updated CORE.md bootstrap to reference confidence-tracked learnings
  - Compared ECC vs Asha — concluded Asha now has parity on valuable patterns, diverges on philosophy (Claude-native vs multi-provider)
- **2026-03-08**: ECC repository review — systematic improvement session (Part 1)
  - Added `pattern_analyzer.py` for automatic learning (pattern extraction, session eval, calibration detection)
  - Simplified `/save` command — Four Questions auto-populated by synthesis
  - Added orphan session recovery to session-start hook
  - Created `verify.py` with pluggable checkers (TypeScript, Python, Go, Java, Rust)
  - Added framework skills: Django, Spring Boot, Go
  - Added parallel orchestration notation `[agent1, agent2]` to `/orchestrate`
  - Added `suggest-compact.sh` hook (100 tool calls or 200 events triggers suggestion)
- **2026-02-13**: Version sync + hook hardening + repo cleanup

## Critical Reference Information

- **Plugins**: panel-system (v5.0.0), code (v1.11.0), write (v1.2.0), output-styles (v1.0.2), asha (v1.18.0), image (v1.1.0), scheduler (v0.1.0)
- **Domain separation**: panel=research, code=development, write=creative, image=generation, scheduler=automation, asha=core scaffold
- **Identity layer**: `~/.asha/` (cross-project, user-scope) contains soul.md, voice.md, keeper.md, learnings.md
- **Main docs**: CLAUDE.md contains comprehensive repository guide
- **Vector DB**: Indexed, ollama + chromadb operational
- **Test suite**: `./tests/run-tests.sh` runs all tests

## Next Steps

**Immediate**:

- None — ECC review complete

**Blocked**:

- None

**Deferred**:

- Session aliasing (minor UX, low priority)
- Cost tracking (requires Claude Code API exposure)

## Session Learnings

**ECC Review Learnings (2026-03-08)**:

- Atomic instincts vs aggregated context: Per-pattern confidence enables decay and promotion
- Secret scrubbing belongs at event emission, not at analysis time
- Automatic learning detection closes the observe→detect→store→apply loop
- ECC's two-tier instinct system (project→global) unnecessary when learnings are global from start
- Calibration signals (voice/keeper) can be extracted from events.jsonl at session-end
- Unified verification with pluggable language checkers avoids duplicate type-check/lint systems
- Bracket notation `[a, b]` for parallel phases is cleaner than complex workflow DSL

**Standards Reference**: https://leehanchung.github.io/blogs/2025/10/26/claude-skills-deep-dive/

Key compliance requirements:

- All commands/skills need YAML frontmatter (name, description, allowed-tools)
- Use `{baseDir}` not hardcoded paths for portability
- Keep tool permissions specific, not overly broad
- SKILL.md should stay under 5000 words
