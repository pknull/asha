---
description: "Index project files for semantic search"
argument-hint: "Optional: --full (complete reindex) or --check (verify dependencies)"
allowed-tools: ["Bash", "Read"]
---

# Index Memory for Semantic Search

Run the memory index tool to enable semantic search across project files.

Arguments: $ARGUMENTS

## Protocol

### Step 1: Locate Asha Directory

Find the asha directory relative to the project root (typically `./asha/`).

### Step 2: Determine Mode

Based on arguments:
- `--full` → Full reindex of all files
- `--check` → Verify dependencies only (Ollama running, packages installed)
- No arguments → Incremental update (changed files only, faster)

### Step 3: Run Indexer

Use the run-python.sh wrapper which auto-detects the virtual environment:

```bash
# Incremental (default - changed files only)
./asha/tools/run-python.sh ./asha/tools/memory_index.py ingest --changed

# Full reindex
./asha/tools/run-python.sh ./asha/tools/memory_index.py ingest

# Check dependencies
./asha/tools/run-python.sh ./asha/tools/memory_index.py check
```

### Step 4: Report Results

Summarize:
- Number of files indexed and chunks created
- Any errors or warnings encountered
- If dependencies are missing, provide install instructions

## Requirements

- Ollama running locally (`ollama serve`)
- Embedding model available (`ollama pull nomic-embed-text`)
- Python packages installed (handled by `./asha/install.sh`)
