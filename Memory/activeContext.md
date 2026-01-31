---
version: "2.3"
lastUpdated: "2026-01-31 09:30 UTC"
lifecycle: "maintenance"
stakeholder: "all"
changeTrigger: "v1.9.0 validation complete - test suite hardening"
validatedBy: "ai"
dependencies: ["~/.asha/communicationStyle.md", "~/.asha/keeper.md"]
---

# activeContext

## Current Project Status

**Primary Focus**: Claude Code plugin marketplace providing domain-focused plugins for research, development, creative writing, automation, and session coordination.

**Active Work**:
- Marketplace v1.9.0 with 7 plugins (validated)
- Panel System v5.0.0: Full persistence and panel management
- Asha v1.9.0: Cross-project identity layer (`~/.asha/`)

**Recent Activities** (last 7 days):
- **2026-01-31**: v1.9.0 validation complete - Ran full test suite, fixed 3 validation failures (block-secrets strict mode, version mismatches, schedule→scheduler namespace conflict). Fixed flaky Test 75 (grep→bash string match), Test 84 (empty hooks.json handling). Cleaned 14 shellcheck warnings. All 5 test suites now pass including shell linting.
- **2026-01-30**: Housekeeping - Verified git clean, version tracking correct. Confirmed asha 1.8.0→1.8.1 bump tracked memory script fix (orphaned indexer process). Clarified marketplace update policy: only for structural changes (new plugins, removals, major features), not patch bumps.
- **2026-01-29**: v1.9.0 release - Major features:
  - **Panel System v5.0.0**: State persistence with `--resume <id>`, `--list`, `--show <id>`, `--abandon <id>`. Output moved to `Work/panels/` with per-phase state files.
  - **Asha v1.8.0**: Cross-project identity via `~/.asha/` directory (user-scope, not committed). `communicationStyle.md` defines who Asha is; `keeper.md` captures calibration signals via `/save`. Session-start hook auto-injects identity.
- **2026-01-28**: Schedule plugin created - Full cron-style task automation plugin. Natural language time parser (20+ expressions), task management with security validation, systemd/cron backend. End-to-end tested.
- **2026-01-27**: Redis whitepaper analysis - Concluded Memory Bank architecture is sound; created `keeperProfile.md` (now superseded by `~/.asha/keeper.md`)
- **2026-01-26**: Domain restructuring + agent migration - Created code/write plugins; migrated 9 agents from REDACTED99; standards compliance audit

## Critical Reference Information

- **Plugins**: panel-system (v5.0.0), code (v1.0.1), write (v1.1.1), output-styles (v1.0.2), asha (v1.9.0), image (v1.0.0), scheduler (v0.1.0)
- **Domain separation**: panel=research, code=development, write=creative, image=generation, scheduler=automation, asha=core scaffold
- **Identity layer**: `~/.asha/` (cross-project, user-scope) contains communicationStyle.md + keeper.md
- **Main docs**: CLAUDE.md contains comprehensive repository guide
- **Vector DB**: Indexed, ollama + chromadb operational
- **Test suite**: `./tests/run-tests.sh` runs all tests

## Next Steps

**Immediate**:
- [x] Run test suite to validate v1.9.0 changes ✓
- [ ] Test panel persistence (`/panel --resume`, `--list`, `--show`)

**Blocked**:
- None

**Deferred**:
- Add CHANGELOG.md (low priority per audit)
- Consider scoped Bash permissions (e.g., `Bash(git:*)`) per blog post recommendation
- Consider reranking enhancement for memory_index.py (recency/source weighting)
- ai-detector: Check API credits before scanning (see TODO.md)

## Session Learnings

**Redis Whitepaper Analysis (2026-01-27)**:
- Semantic caching doesn't apply to Asha (not a persistent service)
- Agentic memory slots = already have via Memory Bank files
- "Facts" don't need a new system - a section in activeContext.md suffices
- Preference persistence solved via `~/.asha/keeper.md` + bootstrap dependency chain

**Identity Layer (2026-01-29)**:
- Cross-project identity moved to `~/.asha/` (user-scope, not committed)
- `communicationStyle.md`: Who Asha is (voice, persona, constraints)
- `keeper.md`: Who The Keeper is (calibration signals accumulated via `/save`)
- Session-start hook auto-injects both files, ensuring identity persists across all projects

**Test Suite Hardening (2026-01-31)**:
- Shellcheck can't follow dynamic source paths (SC1090/SC1091) - exclude from test runner
- `grep | wc -l` with no matches returns exit 1 under `set -e pipefail` - use `|| true`
- Bash string matching (`[[ "$var" == *"pattern"* ]]`) more reliable than piped grep in tests
- Parameter expansions need quoting: `${var#"$other"}` not `${var#$other}`
- Plugin names must differ from command names to avoid namespace conflicts

**Standards Reference**: https://leehanchung.github.io/blogs/2025/10/26/claude-skills-deep-dive/

Key compliance requirements:
- All commands/skills need YAML frontmatter (name, description, allowed-tools)
- Use `{baseDir}` not hardcoded paths for portability
- Keep tool permissions specific, not overly broad
- SKILL.md should stay under 5000 words
