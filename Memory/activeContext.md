---
version: "2.4"
lastUpdated: "2026-02-11 03:50 UTC"
lifecycle: "maintenance"
stakeholder: "all"
changeTrigger: "v1.11.0 - facet auto-ingestion into ReasoningBank"
validatedBy: "ai"
dependencies: ["~/.asha/communicationStyle.md", "~/.asha/keeper.md"]
---

# activeContext

## Current Project Status

**Primary Focus**: Claude Code plugin marketplace providing domain-focused plugins for research, development, creative writing, automation, and session coordination.

**Active Work**:

- Marketplace v1.11.0 with 7 plugins
- Asha v1.11.0: Facet auto-ingestion, cross-project identity layer

**Recent Activities** (last 7 days):

- **2026-02-11**: v1.11.0 - Facet auto-ingestion into ReasoningBank. New `facet_ingest.py` tool reads `~/.claude/usage-data/facets/`, maps to project via `history.jsonl`, records workflow/friction/success patterns. Runs background at session start, synchronous during `/save`. Code review caught SQLite locking bug (fixed with busy_timeout + WAL mode + connection lifecycle), overly broad pgrep guard, hardcoded python3 reference, and primary_success score floor for failed sessions.
- **2026-02-09**: v1.10.0 - Added verify-app agent and verification template.
- **2026-01-31**: v1.9.0 validation complete - All 5 test suites pass including shell linting.
- **2026-01-29**: v1.9.0 release - Major features:
  - **Panel System v5.0.0**: State persistence with `--resume <id>`, `--list`, `--show <id>`, `--abandon <id>`. Output moved to `Work/panels/` with per-phase state files.
  - **Asha v1.8.0**: Cross-project identity via `~/.asha/` directory (user-scope, not committed). `communicationStyle.md` defines who Asha is; `keeper.md` captures calibration signals via `/save`. Session-start hook auto-injects identity.
- **2026-01-28**: Schedule plugin created - Full cron-style task automation plugin. Natural language time parser (20+ expressions), task management with security validation, systemd/cron backend. End-to-end tested.
- **2026-01-27**: Redis whitepaper analysis - Concluded Memory Bank architecture is sound; created `keeperProfile.md` (now superseded by `~/.asha/keeper.md`)
- **2026-01-26**: Domain restructuring + agent migration - Created code/write plugins; migrated 9 agents from REDACTED99; standards compliance audit

## Critical Reference Information

- **Plugins**: panel-system (v5.0.0), code (v1.1.0), write (v1.2.0), output-styles (v1.0.2), asha (v1.11.1), image (v1.1.0), scheduler (v0.1.0)
- **Domain separation**: panel=research, code=development, write=creative, image=generation, scheduler=automation, asha=core scaffold
- **Identity layer**: `~/.asha/` (cross-project, user-scope) contains communicationStyle.md + keeper.md
- **Main docs**: CLAUDE.md contains comprehensive repository guide
- **Vector DB**: Indexed, ollama + chromadb operational
- **Test suite**: `./tests/run-tests.sh` runs all tests

## Next Steps

**Immediate**:

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
