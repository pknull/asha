#!/usr/bin/env python3
"""
Ingest existing Memory/*.md files into Mem0 vector database

This is a one-time operation to populate the vector DB with existing knowledge.
Re-run when Memory files are significantly updated.

Usage:
    python ingest_memory.py
"""

import os
import sys
from pathlib import Path
from datetime import datetime
from mem0_helper import init_memory

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / "Memory"

# Files to ingest
MEMORY_FILES = [
    "creatorProfile.md",
    "projectbrief.md",
    "activeContext.md",
    "communicationStyle.md",
    "workflowProtocols.md",
    "techEnvironment.md",
]

def ingest_file(memory, file_path: Path):
    """Ingest a single Memory file"""
    if not file_path.exists():
        print(f"⚠️  Skipping {file_path.name} (not found)")
        return False
    
    print(f"📄 Reading {file_path.name}...")
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Extract title from first header or use filename
    title = file_path.stem
    lines = content.split('\n')
    for line in lines:
        if line.startswith('# '):
            title = line.lstrip('# ').strip()
            break
    
    metadata = {
        "source_file": file_path.name,
        "ingested_at": datetime.now().isoformat(),
        "title": title
    }
    
    print(f"💾 Ingesting {file_path.name} into Mem0...")
    memory.add(content, user_id="pk", metadata=metadata)
    print(f"✓ {file_path.name} ingested")
    return True

def main():
    print("=" * 60)
    print("Memory Ingestion - Loading existing Memory files into Mem0")
    print("=" * 60)
    print()
    
    # Check if Ollama is running
    import subprocess
    try:
        subprocess.run(["ollama", "list"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ Error: Ollama not running or not installed")
        print("Start it with: ollama serve")
        sys.exit(1)
    
    # Initialize Mem0
    print("🔧 Initializing Mem0...")
    memory = init_memory()
    print("✓ Mem0 initialized")
    print()
    
    # Ingest each file
    ingested_count = 0
    for filename in MEMORY_FILES:
        file_path = MEMORY_DIR / filename
        if ingest_file(memory, file_path):
            ingested_count += 1
        print()
    
    print("=" * 60)
    print(f"✓ Ingestion complete: {ingested_count}/{len(MEMORY_FILES)} files processed")
    print(f"📊 Vector DB location: {MEMORY_DIR / 'vector_db'}")
    print("=" * 60)
    print()
    print("Test retrieval with:")
    print("  python Tools/mem0_helper.py search \"what is the creator's name?\"")

if __name__ == "__main__":
    main()
