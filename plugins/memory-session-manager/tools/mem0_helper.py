#!/usr/bin/env python3
"""
Mem0 Helper - Interface for semantic memory storage and retrieval

Usage:
    python mem0_helper.py search "what is the creator's name?"
    python mem0_helper.py add "New fact about the project"
    python mem0_helper.py get-all
"""

import os
import sys
import json
from pathlib import Path
from mem0 import Memory

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / "Memory"
VECTOR_DB_PATH = MEMORY_DIR / "vector_db"

# Mem0 configuration (Chroma file-based + Ollama for embeddings/LLM)
CONFIG = {
    "vector_store": {
        "provider": "chroma",
        "config": {
            "collection_name": "asha_memory",
            "path": str(VECTOR_DB_PATH)
        }
    },
    "llm": {
        "provider": "ollama",
        "config": {
            "model": "mistral-nemo:latest",
            "temperature": 0,
            "max_tokens": 2000,
            "ollama_base_url": "http://localhost:11434"
        }
    },
    "embedder": {
        "provider": "ollama",
        "config": {
            "model": "nomic-embed-text:latest",
            "ollama_base_url": "http://localhost:11434"
        }
    },
    "version": "v1.1"
}

def init_memory():
    """Initialize Mem0 with Chroma file-based vector store + Ollama"""
    # Ensure vector_db directory exists
    VECTOR_DB_PATH.mkdir(parents=True, exist_ok=True)
    
    return Memory.from_config(CONFIG)

def search(query: str, limit: int = 3):
    """Search for relevant memories"""
    m = init_memory()
    results = m.search(query=query, user_id="pk", limit=limit)
    return results

def add(content: str, metadata: dict | None = None):
    """Add new memory"""
    m = init_memory()
    metadata_dict = metadata if metadata is not None else {}
    result = m.add(content, user_id="pk", metadata=metadata_dict)
    return result

def get_all():
    """Get all memories"""
    m = init_memory()
    memories = m.get_all(user_id="pk")
    return memories

def main():
    if len(sys.argv) < 2:
        print("Usage: mem0_helper.py <command> [args]")
        print("Commands:")
        print("  search <query>     - Search for relevant memories")
        print("  add <content>      - Add new memory")
        print("  get-all            - Get all memories")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "search":
        if len(sys.argv) < 3:
            print("Error: search requires a query", file=sys.stderr)
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        results = search(query)
        print(json.dumps(results, indent=2))
    
    elif command == "add":
        if len(sys.argv) < 3:
            print("Error: add requires content", file=sys.stderr)
            sys.exit(1)
        content = " ".join(sys.argv[2:])
        result = add(content)
        print(json.dumps(result, indent=2))
    
    elif command == "get-all":
        memories = get_all()
        print(json.dumps(memories, indent=2))
    
    else:
        print(f"Error: Unknown command '{command}'", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
