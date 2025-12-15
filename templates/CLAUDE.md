# Project Context

This project uses the **Asha** framework for session coordination and memory persistence.

## Quick Reference

**Framework:** Read `@asha/CORE.md` for full operational protocols.

**Memory Bank:** Project context stored in `Memory/*.md` files.

## Commands

| Command | Purpose |
|---------|---------|
| `/save` | Save session context to Memory Bank, archive session, refresh index, commit |
| `/index` | Index files for semantic search (use `--full` for complete reindex, `--check` for dependency verification) |

## Tools

```bash
# Semantic search (requires Ollama running)
./asha/tools/run-python.sh ./asha/tools/memory_index.py search "your query"

# Pattern lookup from ReasoningBank
./asha/tools/run-python.sh ./asha/tools/reasoning_bank.py query --context "situation"

# Check vector DB dependencies
./asha/tools/run-python.sh ./asha/tools/memory_index.py check
```

## Memory Files

| File | Purpose | Update Frequency |
|------|---------|------------------|
| `Memory/activeContext.md` | Current project state, recent activities | Every session |
| `Memory/projectbrief.md` | Project scope, objectives, constraints | Rarely |
| `Memory/communicationStyle.md` | Voice, persona, authority hierarchy | Rarely |
| `Memory/workflowProtocols.md` | Validated patterns, anti-patterns | When patterns discovered |
| `Memory/techEnvironment.md` | Technical stack, conventions | When stack changes |

## Session Workflow

1. **Start:** Read `Memory/activeContext.md` for context
2. **Work:** Operations logged automatically via hooks
3. **End:** Run `/save` to synthesize and persist learnings

## Code Style

- Follow existing patterns in the codebase
- Use authority markers when uncertain: `[Inference]`, `[Speculation]`, `[Unverified]`
- Reference code locations as `file_path:line_number`

## Testing

```bash
# Run Python tool tests (when available)
pytest tests/

# Verify installation
./asha/tools/run-python.sh ./asha/tools/memory_index.py check
```
