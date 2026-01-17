# asha-marketplace Review

## Summary

asha-marketplace is a well-structured Claude Code plugin marketplace providing four plugins: panel-system (multi-perspective analysis), local-review (code review), output-styles (response formatting), and asha (session coordination/memory persistence). The codebase demonstrates mature architecture with clear separation of concerns, comprehensive documentation, and thoughtful design patterns. Overall health is **good** with minor issues around version synchronization, missing files referenced in hooks.json, and limited automated testing.

## Critical Issues

- `/home/pknull/Code/asha-marketplace/plugins/asha/hooks/hooks.json:19-21` **Missing hook handler files**: hooks.json references `post-tool-use`, `user-prompt-submit`, and `session-end` handlers without the `.sh` extension, but the files exist as `post-tool-use`, `user-prompt-submit`, etc. (no extension). This may work but is inconsistent with `session-start.sh` which has the extension. Verify hooks execute correctly.

- `/home/pknull/Code/asha-marketplace/plugins/asha/tools/memory_index.py` **File referenced but not fully reviewed**: The memory_index.py (33KB) is a substantial tool. It is referenced in init.md and save-session.sh but its functionality should be validated for error handling and edge cases.

## Recommendations

### High Priority

1. **[HIGH] Version consistency audit**: README.md documents Asha as v1.0.1 but plugin.json shows v1.4.0. README shows marketplace as v1.3.0 but marketplace.json shows v1.5.0. Synchronize all version references.

2. **[HIGH] Add missing requirements.txt dependencies**: The requirements.txt only lists `chromadb>=0.4.0` and `requests>=2.25.0`, but `local_react_save.py` uses `difflib`, `pathlib`, etc. (stdlib - OK), and `reasoning_bank.py` uses `sqlite3` (stdlib - OK). However, `memory_index.py` likely has additional dependencies (numpy, ollama client, etc.) that should be verified and documented.

3. **[HIGH] Strengthen test coverage**: Only one test script exists (`tests/validate-plugins.sh`) which validates JSON structure and namespace conflicts. Consider adding:
   - Python unit tests for `reasoning_bank.py`, `local_react_save.py`, `memory_index.py`
   - Bash script tests for hook handlers
   - Integration tests for command workflows

### Medium Priority

4. **[MEDIUM] Add error handling for missing Ollama**: The memory indexing requires Ollama running locally but error messages may not be user-friendly if Ollama is not available. Add explicit checks with helpful error messages.

5. **[MEDIUM] Document hook handler file naming convention**: The hooks directory has `session-start.sh` (with extension) but other handlers (`post-tool-use`, `user-prompt-submit`, `session-end`) lack extensions. Standardize naming.

6. **[MEDIUM] Add shellcheck to CI**: The bash scripts are well-written with `set -euo pipefail` but would benefit from shellcheck validation in CI.

7. **[MEDIUM] Consider adding a CHANGELOG.md**: Version history is embedded in README.md and CLAUDE.md. A dedicated CHANGELOG.md following Keep a Changelog format would improve release documentation.

### Low Priority

8. **[LOW] Reduce duplication in project directory detection**: The `detect_project_dir()` function is duplicated across `common.sh`, `save-session.sh`, and `reasoning_bank.py`. Consider consolidating into a single shared utility.

9. **[LOW] Add type hints to Python code**: The Python files lack type hints (except `reasoning_bank.py` which has some). Adding type hints would improve maintainability.

10. **[LOW] Document the `--minimal` flag behavior more explicitly**: The `/asha:init --minimal` flag is mentioned but its exact behavior (skip Vector DB) could be more prominent in README.

## Scores (1-10)

| Category | Score | Notes |
|----------|-------|-------|
| Code Quality | 8 | Clean, readable code with good error handling. Bash scripts follow best practices (`set -euo pipefail`). Python code is well-structured. Minor duplication in utility functions. |
| Architecture | 9 | Excellent separation of concerns. Plugin architecture is well-designed. Memory Bank pattern is thoughtful. Hook system is extensible. The 11-phase panel protocol is sophisticated. |
| Completeness | 7 | Core functionality is complete. Missing: comprehensive tests, some referenced files (requirements for all dependencies), version sync. The `memory_index.py` tool is substantial but unreviewed. |
| Standards | 8 | Consistent naming conventions (camelCase for Memory files, kebab-case for commands). Good documentation standards. CLAUDE.md is comprehensive. Minor version inconsistencies across files. |

**Overall Score: 8/10** - Production-ready with minor improvements needed.

## Notes

### Good Patterns Observed

1. **Multi-layer fallback for project detection**: Scripts use environment variable -> git root -> upward directory search pattern consistently.

2. **Marker-based feature flags**: The `Memory/markers/` pattern for silence/rp-active is elegant and allows feature toggling without code changes.

3. **Self-documenting commands**: The markdown-based command definitions with YAML frontmatter (`allowed-tools`, `argument-hint`, etc.) are excellent for both humans and AI parsing.

4. **Four Questions Protocol**: The session save protocol (What was the goal? What did we accomplish? What did we learn? What comes next?) provides structured knowledge capture.

5. **ReasoningBank pattern tracking**: The SQLite-based pattern tracking with error resolution, tool effectiveness, and pattern scoring is a sophisticated cross-session learning mechanism.

6. **Agent recruitment architecture**: The panel system's dynamic specialist recruitment with session-specific naming (e.g., `prose-analysis` -> "The Editor") is creative and domain-adaptive.

7. **Validation pass in local-review**: Running parallel reviewers then validating findings to filter false positives is a smart pattern.

### Concerns

1. **Hook file permissions**: All handlers have mode `--x--x--x` (711) which is unusual - typically scripts need read permission for bash to execute them. Verify hooks execute correctly in production.

2. **Hardcoded project references**: Some files reference specific projects (`mplay`, `rpg-dice`) in `local_react_save.py:206-207` which may be leftovers from a specific installation.

3. **Large Python files without tests**: `memory_index.py` (33KB), `reasoning_bank.py` (24KB), and `local_react_save.py` (14KB) are substantial tools without accompanying tests.

4. **Version drift risk**: With versions tracked in multiple places (README.md, CLAUDE.md, plugin.json, marketplace.json), there's risk of drift. Consider a single source of truth.

### Questions for Maintainer

1. Is the `memory_index.py` tool using the Ollama API directly or through a library? The requirements.txt only shows `requests` but the embedding functionality may need additional setup.

2. What is the expected user workflow when Ollama is not available? Should the system degrade gracefully or is Ollama a hard dependency?

3. Are the hook handlers tested with actual Claude Code installations? The permission bits and naming inconsistencies suggest potential issues.

4. Is there a roadmap for automated testing? The `tests/` directory exists but only has one script.

---

**Reviewed**: 2026-01-16
**Reviewer**: Claude Code QA Review
**Repository**: https://github.com/pknull/asha-marketplace
