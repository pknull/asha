#!/usr/bin/env python3
"""
Memory Index - Unified interface for semantic search over project files

Uses ChromaDB + Ollama embeddings for document indexing and retrieval.
Requires: chromadb, requests, ollama running locally

INDEXING APPROACH:
    Blacklist-based: indexes ALL text files except explicitly excluded paths.
    Override defaults via Memory/vector_db/index_config.json:

    {
        "exclude_dirs": ["custom", "dirs"],           // replaces defaults
        "additional_exclude_dirs": ["more", "dirs"],  // extends defaults
        "exclude_extensions": [".custom"],            // replaces defaults
        "additional_exclude_extensions": [".more"],   // extends defaults
        "exclude_files": ["custom.file"],             // replaces defaults
        "additional_exclude_files": ["more.file"]     // extends defaults
    }

Usage:
    python memory_index.py search "what is the creator's name?"
    python memory_index.py search --fallback "who tells Redacted124 to go"
    python memory_index.py ingest              # Full reindex
    python memory_index.py ingest --changed    # Incremental (git-diff based)
    python memory_index.py stats               # Show index statistics
    python memory_index.py check               # Verify dependencies
"""

import os
import sys
import json
import atexit
import fcntl
import faulthandler
import signal
import subprocess
import traceback
import hashlib
import re
from typing import Optional
import time
from pathlib import Path
from datetime import datetime

# Graceful dependency handling
CHROMADB_AVAILABLE = False
REQUESTS_AVAILABLE = False

try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    pass

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    pass


def check_dependencies() -> dict:
    """Check all dependencies and return status"""
    status = {
        "chromadb": CHROMADB_AVAILABLE,
        "requests": REQUESTS_AVAILABLE,
        "ollama": False,
        "ready": False
    }

    if REQUESTS_AVAILABLE:
        try:
            response = requests.get("http://localhost:11434/api/version", timeout=2)
            status["ollama"] = response.status_code == 200
        except Exception:
            pass

    status["ready"] = all([status["chromadb"], status["requests"], status["ollama"]])
    return status


def require_dependencies(for_operation: str = "this operation"):
    """Exit with helpful message if dependencies missing"""
    status = check_dependencies()

    missing = []
    if not status["chromadb"]:
        missing.append("chromadb (pip install chromadb)")
    if not status["requests"]:
        missing.append("requests (pip install requests)")
    if not status["ollama"]:
        missing.append("ollama running (ollama serve)")

    if missing:
        print(f"❌ Cannot perform {for_operation}. Missing dependencies:", file=sys.stderr)
        for dep in missing:
            print(f"   - {dep}", file=sys.stderr)
        sys.exit(1)


def detect_project_root() -> Path:
    """Find project root via environment, git, or upward search for Memory/"""
    # Layer 1: Use CLAUDE_PROJECT_DIR if set (hook invocation)
    claude_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if claude_project_dir:
        project_path = Path(claude_project_dir)
        if (project_path / "Memory").is_dir():
            return project_path

    # Layer 2: Try git root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True
        )
        git_root = Path(result.stdout.strip())
        if (git_root / "Memory").is_dir():
            return git_root
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Layer 3: Upward search for Memory/ directory
    search_dir = Path(__file__).parent.resolve()
    while search_dir != search_dir.parent:
        if (search_dir / "Memory").is_dir():
            return search_dir
        search_dir = search_dir.parent

    raise RuntimeError(
        "Cannot detect project root. Ensure you're in a project with Memory/ directory "
        "or a git repository."
    )


# Dynamic paths
PROJECT_ROOT = None
MEMORY_DIR = None
VECTOR_DB_PATH = None
INDEX_STATE_FILE = None

CURRENT_FILE = None
DEBUG_LOG_PATH = os.environ.get("INDEXER_DEBUG_LOG", "/tmp/redacted99-indexer-debug.log")
_FAULTHANDLER_FILE = None

def log_debug(message: str):
    timestamp = datetime.now().isoformat()
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} {message}\n")
    except Exception:
        pass

def setup_diagnostics():
    global _FAULTHANDLER_FILE
    try:
        _FAULTHANDLER_FILE = open(DEBUG_LOG_PATH, "a", encoding="utf-8")
        faulthandler.enable(_FAULTHANDLER_FILE)
    except Exception:
        pass

    def _handle_signal(signum, frame):
        log_debug(f"signal {signum} received, last_file={CURRENT_FILE}")
        sys.exit(1)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGABRT):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    atexit.register(lambda: log_debug(f"exit, last_file={CURRENT_FILE}"))

def init_paths():
    """Initialize paths - call before any file operations"""
    global PROJECT_ROOT, MEMORY_DIR, VECTOR_DB_PATH, INDEX_STATE_FILE
    if PROJECT_ROOT is None:
        PROJECT_ROOT = detect_project_root()
        MEMORY_DIR = PROJECT_ROOT / "Memory"
        VECTOR_DB_PATH = MEMORY_DIR / "vector_db"
        INDEX_STATE_FILE = VECTOR_DB_PATH / ".index_state.json"


# Ollama config
OLLAMA_BASE_URL = "http://localhost:11434"
EMBEDDING_MODEL = "nomic-embed-text:latest"
OLLAMA_EMBED_TIMEOUT = float(os.environ.get("OLLAMA_EMBED_TIMEOUT", "30"))
OLLAMA_EMBED_MAX_RETRIES = int(os.environ.get("OLLAMA_EMBED_RETRIES", "2"))
OLLAMA_EMBED_RETRY_BACKOFF = float(os.environ.get("OLLAMA_EMBED_RETRY_BACKOFF", "1.5"))

# =============================================================================
# EXCLUSION CONFIGURATION (Blacklist approach - index everything except these)
# =============================================================================

# Directories to exclude (matches any path segment)
DEFAULT_EXCLUDE_DIRS = [
    # Version control
    ".git",
    ".git-rewrite",  # git filter-branch artifacts
    ".jj",  # jujutsu VCS internals
    # Python
    ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    # Node
    "node_modules",
    # Asha internals
    ".asha",
    "Memory/vector_db",
    "Memory/reasoning_bank",
    # Session logs (raw, already synthesized)
    "sessions",
    # Build outputs
    "dist", "build", ".next", ".nuxt",
    # IDE
    ".idea", ".vscode",
    # Obsidian internals (plugins, themes, config)
    ".obsidian",
]

# File extensions to exclude (binary/non-textual)
DEFAULT_EXCLUDE_EXTENSIONS = [
    # Database/binary
    ".db", ".sqlite", ".sqlite3", ".bin", ".exe", ".dll", ".so", ".dylib",
    ".pkl", ".pickle", ".parquet",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tiff",
    # Archives
    ".zip", ".tar", ".gz", ".7z", ".rar", ".bz2",
    # Media
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".flac",
    # Documents (no text extraction)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Other binary
    ".pyc", ".pyo", ".class", ".o", ".a",
    # Map editors and specialized formats
    ".dungeondraft_map", ".wonderdraft_map", ".canvas",
]

# Specific filenames to exclude
DEFAULT_EXCLUDE_FILES = [
    ".DS_Store",
    "Thumbs.db",
    ".index_state.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
]

# Loaded exclusion config (can be overridden via config file)
EXCLUDE_CONFIG = None

def load_exclude_config() -> dict:
    """Load exclusion config from file or use defaults"""
    global EXCLUDE_CONFIG
    if EXCLUDE_CONFIG is not None:
        return EXCLUDE_CONFIG

    init_paths()
    config_file = VECTOR_DB_PATH / "index_config.json"

    # Start with defaults
    EXCLUDE_CONFIG = {
        "exclude_dirs": DEFAULT_EXCLUDE_DIRS.copy(),
        "exclude_extensions": DEFAULT_EXCLUDE_EXTENSIONS.copy(),
        "exclude_files": DEFAULT_EXCLUDE_FILES.copy(),
    }

    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                # Override with config values if present
                if "exclude_dirs" in config:
                    EXCLUDE_CONFIG["exclude_dirs"] = config["exclude_dirs"]
                if "exclude_extensions" in config:
                    EXCLUDE_CONFIG["exclude_extensions"] = config["exclude_extensions"]
                if "exclude_files" in config:
                    EXCLUDE_CONFIG["exclude_files"] = config["exclude_files"]
                # Allow adding to defaults instead of replacing
                if "additional_exclude_dirs" in config:
                    EXCLUDE_CONFIG["exclude_dirs"].extend(config["additional_exclude_dirs"])
                if "additional_exclude_extensions" in config:
                    EXCLUDE_CONFIG["exclude_extensions"].extend(config["additional_exclude_extensions"])
                if "additional_exclude_files" in config:
                    EXCLUDE_CONFIG["exclude_files"].extend(config["additional_exclude_files"])
        except (json.JSONDecodeError, IOError):
            pass

    return EXCLUDE_CONFIG


# Chunking config
MAX_CHUNK_SIZE = 4000
CHUNK_OVERLAP = 400  # Characters of overlap between chunks

# Content type detection
CONTENT_TYPE_MAP = {
    "Memory/": "memory",
    "Vault/World/Characters/": "character",
    "Vault/World/Locations/": "location",
    "Vault/World/Groups/": "group",
    "Vault/Books/": "narrative",
    "Tools/": "code",
    ".claude/agents/": "agent",
    ".claude/commands/": "command",
    ".claude/docs/": "documentation",
}


def get_content_type(rel_path: str) -> str:
    """Determine content type from file path"""
    for prefix, content_type in CONTENT_TYPE_MAP.items():
        if rel_path.startswith(prefix):
            return content_type
    return "unknown"


def extract_section_headers(content: str) -> list[tuple[int, str]]:
    """Extract markdown section headers with their character positions"""
    headers = []
    pos = 0
    for line in content.split('\n'):
        if line.startswith('#'):
            # Extract header level and text
            match = re.match(r'^(#+)\s+(.+)$', line)
            if match:
                level = len(match.group(1))
                text = match.group(2).strip()
                headers.append((pos, f"h{level}:{text}"))
        pos += len(line) + 1  # +1 for newline
    return headers


def get_section_for_position(headers: list[tuple[int, str]], pos: int) -> str:
    """Get the most recent section header for a given position"""
    current_section = ""
    for header_pos, header_text in headers:
        if header_pos <= pos:
            current_section = header_text
        else:
            break
    return current_section


def get_embedding(text: str) -> list[float]:
    """Get embedding from Ollama. Returns None on failure."""
    if not REQUESTS_AVAILABLE:
        return None

    url = f"{OLLAMA_BASE_URL}/api/embeddings"
    payload = {"model": EMBEDDING_MODEL, "prompt": text}
    for attempt in range(1, OLLAMA_EMBED_MAX_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, timeout=OLLAMA_EMBED_TIMEOUT)
            response.raise_for_status()
            embedding = response.json().get("embedding")
            if embedding is None or (hasattr(embedding, '__len__') and len(embedding) == 0):
                raise ValueError("empty embedding")
            return embedding
        except Exception as exc:
            if attempt < OLLAMA_EMBED_MAX_RETRIES:
                print(
                    f"  ⚠️  Ollama embedding failed (attempt {attempt}/{OLLAMA_EMBED_MAX_RETRIES}), retrying...",
                    file=sys.stderr,
                    flush=True
                )
                time.sleep(OLLAMA_EMBED_RETRY_BACKOFF * attempt)
            else:
                print(
                    f"  ⚠️  Ollama embedding failed after {attempt} attempts: {exc}",
                    file=sys.stderr,
                    flush=True
                )

    return None


def init_db():
    """Initialize ChromaDB"""
    init_paths()
    VECTOR_DB_PATH.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(VECTOR_DB_PATH),
        settings=Settings(anonymized_telemetry=False)
    )
    # Get or create collection
    collection = client.get_or_create_collection(
        name="project_index",
        metadata={"hnsw:space": "cosine"}
    )
    return client, collection


def chunk_text(text: str, chunk_size: int = MAX_CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[tuple[str, int]]:
    """
    Split text into chunks with overlap, trying to break at paragraph boundaries.
    Returns list of (chunk_text, start_position) tuples.
    """
    if len(text) <= chunk_size:
        return [(text, 0)]

    chunks = []
    paragraphs = text.split('\n\n')
    current_chunk = ""
    current_start = 0
    char_pos = 0

    for para in paragraphs:
        para_with_sep = para + "\n\n"

        if len(current_chunk) + len(para_with_sep) <= chunk_size:
            current_chunk += para_with_sep
        else:
            if current_chunk:
                chunks.append((current_chunk.strip(), current_start))

                # Calculate overlap start position
                overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
                # Find a good break point in overlap (paragraph or sentence)
                overlap_break = overlap_text.rfind('\n\n')
                if overlap_break == -1:
                    overlap_break = overlap_text.rfind('. ')
                if overlap_break == -1:
                    overlap_break = 0
                else:
                    overlap_break += 2  # Include the break characters

                # Start new chunk with overlap
                overlap_content = overlap_text[overlap_break:]
                current_start = char_pos - len(overlap_content)
                current_chunk = overlap_content

            # Handle paragraphs longer than chunk_size
            if len(para) > chunk_size:
                words = para.split()
                temp_chunk = current_chunk
                for word in words:
                    if len(temp_chunk) + len(word) + 1 <= chunk_size:
                        temp_chunk += word + " "
                    else:
                        chunks.append((temp_chunk.strip(), current_start))
                        # Overlap for word-split chunks
                        overlap_words = temp_chunk.split()[-20:]  # Last ~20 words
                        current_chunk = " ".join(overlap_words) + " " + word + " "
                        current_start = char_pos - len(current_chunk) + len(word) + 1
                        temp_chunk = current_chunk
                current_chunk = temp_chunk + "\n\n"
            else:
                current_chunk += para_with_sep

        char_pos += len(para_with_sep)

    if current_chunk.strip():
        chunks.append((current_chunk.strip(), current_start))

    return chunks


def extract_keywords(query: str) -> list[str]:
    """Extract significant keywords from query for fallback search"""
    # Remove common stop words
    stop_words = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
        'ought', 'used', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by',
        'from', 'as', 'into', 'through', 'during', 'before', 'after', 'above',
        'below', 'between', 'under', 'again', 'further', 'then', 'once', 'here',
        'there', 'when', 'where', 'why', 'how', 'all', 'each', 'few', 'more',
        'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own',
        'same', 'so', 'than', 'too', 'very', 'just', 'and', 'but', 'if', 'or',
        'because', 'until', 'while', 'what', 'who', 'which', 'this', 'that',
        'these', 'those', 'am', 'it', 'its', 'he', 'she', 'they', 'them', 'his',
        'her', 'their', 'my', 'your', 'our', 'i', 'you', 'we', 'me', 'him',
    }

    # Tokenize and filter - preserve original case for proper nouns
    words = re.findall(r'\b[a-zA-Z]{3,}\b', query)
    # Filter stop words (compare lowercase but keep original)
    keywords = [w for w in words if w.lower() not in stop_words]

    return keywords


def search(
    query: str,
    limit: int = 5,
    content_type: Optional[str] = None,
    source_path: Optional[str] = None,
    time_after: Optional[str] = None,
    time_before: Optional[str] = None
) -> dict:
    """
    Search for relevant content using semantic similarity with optional filters.

    Args:
        query: Search query text
        limit: Maximum results to return
        content_type: Filter by content type (memory, character, code, etc.)
        source_path: Filter by source file path prefix
        time_after: Filter by ingested_at >= this ISO8601 timestamp
        time_before: Filter by ingested_at <= this ISO8601 timestamp
    """
    require_dependencies("search")
    _, collection = init_db()

    # Get query embedding
    query_embedding = get_embedding(query)
    if query_embedding is None or (hasattr(query_embedding, '__len__') and len(query_embedding) == 0):
        print("❌ Failed to get embedding from Ollama", file=sys.stderr, flush=True)
        return {"results": [], "method": "semantic", "error": "embedding_failed"}

    # Build where clause for filters
    where_clause = None
    where_conditions = []

    if content_type:
        where_conditions.append({"content_type": content_type})
    if source_path:
        where_conditions.append({"source_file": {"$contains": source_path}})
    if time_after:
        where_conditions.append({"ingested_at": {"$gte": time_after}})
    if time_before:
        where_conditions.append({"ingested_at": {"$lte": time_before}})

    if len(where_conditions) == 1:
        where_clause = where_conditions[0]
    elif len(where_conditions) > 1:
        where_clause = {"$and": where_conditions}

    # Search
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=limit,
        where=where_clause,
        include=["documents", "metadatas", "distances"]
    )

    # Format results
    formatted = []
    if results['ids'] and results['ids'][0]:
        for i, doc_id in enumerate(results['ids'][0]):
            metadata = results['metadatas'][0][i]
            formatted.append({
                "id": doc_id,
                "source": metadata.get('source_file', 'unknown'),
                "title": metadata.get('title', 'unknown'),
                "content_type": metadata.get('content_type', 'unknown'),
                "section": metadata.get('section_header', ''),
                "chunk": metadata.get('chunk_index', 0),
                "distance": results['distances'][0][i],
                "content": results['documents'][0][i][:500] + "..." if len(results['documents'][0][i]) > 500 else results['documents'][0][i]
            })

    return {"results": formatted, "method": "semantic"}


def search_with_fallback(query: str, limit: int = 5, semantic_threshold: float = 0.35) -> dict:
    """
    Hybrid search: semantic first, then keyword fallback if results are weak.

    Args:
        query: Search query
        limit: Max results to return
        semantic_threshold: Distance threshold - results above this trigger fallback
    """
    require_dependencies("search")
    _, collection = init_db()

    # Phase 1: Semantic search
    query_embedding = get_embedding(query)
    if query_embedding is None or (hasattr(query_embedding, '__len__') and len(query_embedding) == 0):
        print("❌ Failed to get embedding from Ollama", file=sys.stderr, flush=True)
        return {"results": [], "method": "semantic+keyword", "error": "embedding_failed"}
    semantic_results = collection.query(
        query_embeddings=[query_embedding],
        n_results=limit * 2,  # Get more for filtering
        include=["documents", "metadatas", "distances"]
    )

    # Check if semantic results are strong enough
    has_strong_results = False
    if semantic_results['distances'] and semantic_results['distances'][0]:
        best_distance = min(semantic_results['distances'][0])
        has_strong_results = best_distance < semantic_threshold

    # Phase 2: Keyword fallback if semantic results are weak
    keyword_results = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
    if not has_strong_results:
        keywords = extract_keywords(query)
        if keywords:
            # Search for documents containing keywords
            for keyword in keywords[:3]:  # Limit to top 3 keywords
                try:
                    kw_results = collection.query(
                        query_embeddings=[query_embedding],  # Still use embedding for ranking
                        n_results=limit * 3,  # Get more results per keyword to avoid missing relevant matches
                        where_document={"$contains": keyword},
                        include=["documents", "metadatas", "distances"]
                    )
                    if kw_results['ids'] and kw_results['ids'][0]:
                        keyword_results['ids'][0].extend(kw_results['ids'][0])
                        keyword_results['documents'][0].extend(kw_results['documents'][0])
                        keyword_results['metadatas'][0].extend(kw_results['metadatas'][0])
                        keyword_results['distances'][0].extend(kw_results['distances'][0])
                except Exception:
                    pass  # ChromaDB may not support all filter operations

    # Merge and deduplicate results (Reciprocal Rank Fusion simplified)
    # First, build set of IDs that matched keywords for bonus application
    keyword_matched_ids = set(keyword_results['ids'][0]) if keyword_results['ids'] and keyword_results['ids'][0] else set()

    seen_ids = set()
    merged = []

    # Add semantic results first (apply bonus if also matched keywords)
    if semantic_results['ids'] and semantic_results['ids'][0]:
        for i, doc_id in enumerate(semantic_results['ids'][0]):
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                base_distance = semantic_results['distances'][0][i]
                # Apply 0.2 distance bonus if this result also matched keywords
                if doc_id in keyword_matched_ids:
                    adjusted_distance = max(0.01, base_distance - 0.2)
                    match_type = "semantic+keyword"
                else:
                    adjusted_distance = base_distance
                    match_type = "semantic"
                merged.append({
                    "id": doc_id,
                    "source": semantic_results['metadatas'][0][i].get('source_file', 'unknown'),
                    "title": semantic_results['metadatas'][0][i].get('title', 'unknown'),
                    "content_type": semantic_results['metadatas'][0][i].get('content_type', 'unknown'),
                    "section": semantic_results['metadatas'][0][i].get('section_header', ''),
                    "chunk": semantic_results['metadatas'][0][i].get('chunk_index', 0),
                    "distance": adjusted_distance,
                    "content": semantic_results['documents'][0][i][:500] + "..." if len(semantic_results['documents'][0][i]) > 500 else semantic_results['documents'][0][i],
                    "match_type": match_type
                })

    # Add keyword results not already in semantic results
    if keyword_results['ids'] and keyword_results['ids'][0]:
        for i, doc_id in enumerate(keyword_results['ids'][0]):
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                # Apply 0.2 distance bonus to keyword matches (lower = better)
                adjusted_distance = max(0.01, keyword_results['distances'][0][i] - 0.2)
                merged.append({
                    "id": doc_id,
                    "source": keyword_results['metadatas'][0][i].get('source_file', 'unknown'),
                    "title": keyword_results['metadatas'][0][i].get('title', 'unknown'),
                    "content_type": keyword_results['metadatas'][0][i].get('content_type', 'unknown'),
                    "section": keyword_results['metadatas'][0][i].get('section_header', ''),
                    "chunk": keyword_results['metadatas'][0][i].get('chunk_index', 0),
                    "distance": adjusted_distance,
                    "content": keyword_results['documents'][0][i][:500] + "..." if len(keyword_results['documents'][0][i]) > 500 else keyword_results['documents'][0][i],
                    "match_type": "keyword"
                })

    # Sort by distance and limit
    merged.sort(key=lambda x: x['distance'])

    method = "semantic" if has_strong_results else "hybrid"
    return {"results": merged[:limit], "method": method, "keywords_used": extract_keywords(query) if not has_strong_results else []}


def get_current_git_hash() -> str:
    """Get current git HEAD hash"""
    init_paths()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def get_changed_files_since(commit_hash: str) -> list[str]:
    """Get list of files changed since given commit"""
    init_paths()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", commit_hash, "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True
        )
        return [f for f in result.stdout.strip().split('\n') if f]
    except subprocess.CalledProcessError:
        return []


def should_exclude(file_path: Path) -> bool:
    """
    Check if file should be excluded from indexing.

    Exclusion layers:
    1. Directory exclusions (any path segment matches)
    2. Extension exclusions (file suffix)
    3. Filename exclusions (exact match)
    """
    config = load_exclude_config()
    path_str = str(file_path)

    # Layer 1: Directory exclusions
    path_parts = file_path.parts
    for exclude_dir in config["exclude_dirs"]:
        # Check if any path segment matches
        if exclude_dir in path_parts:
            return True
        # Also check for nested paths like "Memory/vector_db"
        if "/" in exclude_dir and exclude_dir in path_str:
            return True

    # Layer 2: Extension exclusions
    suffix = file_path.suffix.lower()
    if suffix in config["exclude_extensions"]:
        return True

    # Layer 3: Filename exclusions
    if file_path.name in config["exclude_files"]:
        return True

    return False


def get_files_to_index() -> list[Path]:
    """
    Walk project tree and return all indexable files.

    Uses blacklist approach: index everything except excluded paths.
    """
    init_paths()
    files = []

    for root, dirs, filenames in os.walk(PROJECT_ROOT):
        root_path = Path(root)

        # Prune excluded directories from walk (modifies dirs in-place)
        config = load_exclude_config()
        dirs[:] = [d for d in dirs if d not in config["exclude_dirs"]]

        # Also prune nested exclusion patterns
        rel_root = root_path.relative_to(PROJECT_ROOT)
        dirs[:] = [
            d for d in dirs
            if not any(
                excl in str(rel_root / d)
                for excl in config["exclude_dirs"]
                if "/" in excl
            )
        ]

        for filename in filenames:
            file_path = root_path / filename
            if not should_exclude(file_path):
                files.append(file_path)

    return sorted(files)


def file_content_hash(file_path: Path) -> str:
    """Calculate MD5 hash of file content"""
    with open(file_path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def load_index_state() -> dict:
    """Load previous index state"""
    init_paths()
    if INDEX_STATE_FILE.exists():
        with open(INDEX_STATE_FILE, 'r') as f:
            return json.load(f)
    return {"git_hash": "", "files": {}}


def save_index_state(state: dict):
    """Save index state"""
    init_paths()
    INDEX_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def ingest_file(collection, file_path: Path) -> int:
    """Ingest a single file into vector DB. Returns number of chunks ingested."""
    init_paths()
    if not file_path.exists():
        print(f"  ⚠️  Skipping {file_path.name} (not found)", flush=True)
        return 0

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        print(f"  ⚠️  Skipping {file_path.name} (binary or encoding error)", flush=True)
        return 0

    if not content.strip():
        print(f"  ⚠️  Skipping {file_path.name} (empty)", flush=True)
        return 0

    # Extract title from first header or use filename
    title = file_path.stem
    for line in content.split('\n'):
        if line.startswith('# '):
            title = line.lstrip('# ').strip()
            break

    rel_path = str(file_path.relative_to(PROJECT_ROOT))
    content_hash = file_content_hash(file_path)
    content_type = get_content_type(rel_path)
    parent_path = str(file_path.parent.relative_to(PROJECT_ROOT))

    # Extract section headers for context
    section_headers = extract_section_headers(content)

    # Delete existing chunks for this file (for updates)
    existing = collection.get(where={"source_file": rel_path})
    if existing['ids']:
        collection.delete(ids=existing['ids'])

    # Chunk with overlap
    chunks = chunk_text(content)

    ids = []
    embeddings = []
    documents = []
    metadatas = []

    for i, (chunk_text_content, chunk_start_pos) in enumerate(chunks):
        chunk_id = f"{rel_path}::{i}"
        embedding = get_embedding(chunk_text_content)

        # Skip chunks that produce empty embeddings (e.g., from malformed content)
        if embedding is None or (hasattr(embedding, '__len__') and len(embedding) == 0):
            print(f"  ⚠️  Skipping chunk {i} of {file_path.name} (empty embedding)", flush=True)
            continue

        # Get section header for this chunk's position
        section_header = get_section_for_position(section_headers, chunk_start_pos)

        ids.append(chunk_id)
        embeddings.append(embedding)
        documents.append(chunk_text_content)
        metadatas.append({
            "source_file": rel_path,
            "parent_path": parent_path,
            "title": title,
            "content_type": content_type,
            "section_header": section_header,
            "chunk_index": i,
            "chunk_start_pos": chunk_start_pos,
            "total_chunks": len(chunks),
            "content_hash": content_hash,
            "ingested_at": datetime.now().isoformat()
        })

    # Guard against empty embedding lists (all chunks failed)
    if not embeddings:
        print(f"  ⚠️  Skipping {file_path.name} (no valid embeddings)", flush=True)
        return 0

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas
    )

    chunk_info = f" ({len(ids)} chunks)" if len(ids) > 1 else ""
    print(f"  📄 {rel_path}{chunk_info} [{content_type}]", flush=True)
    return len(ids)


def ingest(changed_only: bool = False):
    """Ingest files into vector DB"""
    global CURRENT_FILE
    require_dependencies("ingest")
    init_paths()
    setup_diagnostics()

    # Acquire exclusive lock to prevent concurrent ingestion
    lock_file_path = VECTOR_DB_PATH / ".ingest.lock"
    VECTOR_DB_PATH.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_file_path, 'w')
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_debug("ingest skipped: another instance is running")
        print("⏭️  Skipping: another indexer is already running", file=sys.stderr)
        lock_file.close()
        return

    # Ensure lock is released on exit
    atexit.register(lambda: lock_file.close())

    log_debug(f"ingest start: changed_only={changed_only}")

    print("=" * 60)
    print("Memory Index - Semantic Search Database")
    print("=" * 60)
    print(f"Project: {PROJECT_ROOT}")
    print()

    # Get files to index
    all_files = get_files_to_index()
    print(f"📁 Found {len(all_files)} indexable files")
    print()

    # Determine which files to actually ingest
    state = load_index_state()
    files_to_ingest = []

    if changed_only and state["git_hash"]:
        print(f"🔄 Incremental mode (since {state['git_hash'][:8]})")
        changed_paths = get_changed_files_since(state["git_hash"])

        for file_path in all_files:
            rel_path = str(file_path.relative_to(PROJECT_ROOT))
            current_hash = file_content_hash(file_path)
            stored_hash = state["files"].get(rel_path, "")

            if rel_path in changed_paths or current_hash != stored_hash:
                files_to_ingest.append(file_path)

        if not files_to_ingest:
            print("✓ No changes detected, index is current")
            print()
            return

        print(f"📝 {len(files_to_ingest)} files changed")
    else:
        print("🔄 Full reindex mode")
        files_to_ingest = all_files

    print()

    # Initialize ChromaDB
    print("🔧 Initializing ChromaDB...")
    _, collection = init_db()
    print("✓ ChromaDB initialized")
    print()

    # Ingest files
    print("📥 Ingesting files:")
    total_chunks = 0
    new_state = {
        "git_hash": get_current_git_hash(),
        "files": state.get("files", {}),
        "last_ingest": datetime.now().isoformat(),
        "config": {
            "chunk_size": MAX_CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
            "embedding_model": EMBEDDING_MODEL
        }
    }

    total_files = len(files_to_ingest)
    for index, file_path in enumerate(files_to_ingest, start=1):
        remaining = total_files - index
        rel_path = str(file_path.relative_to(PROJECT_ROOT))
        CURRENT_FILE = rel_path
        log_debug(f"start {index}/{total_files} {rel_path}")
        print(f"➡️  [{index}/{total_files}] {remaining} remaining - {rel_path}", flush=True)
        try:
            chunks = ingest_file(collection, file_path)
            log_debug(f"done {rel_path} chunks={chunks}")
        except Exception as exc:
            print(f"  ⚠️  Failed to ingest {rel_path}: {exc}", file=sys.stderr, flush=True)
            log_debug(f"error {rel_path}: {exc}\n{traceback.format_exc()}")
            continue
        if chunks > 0:
            total_chunks += chunks
            new_state["files"][rel_path] = file_content_hash(file_path)

    save_index_state(new_state)
    log_debug(f"ingest complete: files={len(files_to_ingest)} chunks={total_chunks}")

    print()
    print("=" * 60)
    print(f"✓ Ingestion complete: {len(files_to_ingest)} files, {total_chunks} chunks")
    print(f"📊 Chunk overlap: {CHUNK_OVERLAP} chars")
    print(f"📊 Index state saved: {INDEX_STATE_FILE.name}")
    print("=" * 60)


def stats():
    """Show index statistics"""
    init_paths()

    # Check if we can show stats (need chromadb but not ollama)
    if not CHROMADB_AVAILABLE:
        print("❌ Cannot show stats: chromadb not installed", file=sys.stderr)
        print("   Install with: pip install chromadb", file=sys.stderr)
        sys.exit(1)

    _, collection = init_db()
    count = collection.count()

    state = load_index_state()
    config = state.get('config', {})

    print("=" * 60)
    print("Memory Index Statistics")
    print("=" * 60)
    print(f"Project: {PROJECT_ROOT}")
    print(f"Total chunks indexed: {count}")
    print(f"Files tracked: {len(state.get('files', {}))}")
    print(f"Last git hash: {state.get('git_hash', 'N/A')[:8] if state.get('git_hash') else 'N/A'}")
    print(f"Last ingest: {state.get('last_ingest', 'N/A')}")
    print()
    print("Configuration:")
    print(f"  Chunk size: {config.get('chunk_size', 'N/A')}")
    print(f"  Chunk overlap: {config.get('chunk_overlap', 'N/A')}")
    print(f"  Embedding model: {config.get('embedding_model', 'N/A')}")
    print()

    # Show dependency status
    dep_status = check_dependencies()
    print("Dependencies:")
    print(f"  chromadb: {'✓' if dep_status['chromadb'] else '✗'}")
    print(f"  requests: {'✓' if dep_status['requests'] else '✗'}")
    print(f"  ollama:   {'✓' if dep_status['ollama'] else '✗'}")
    print("=" * 60)


def check():
    """Check dependencies and show status"""
    print("=" * 60)
    print("Memory Index - Dependency Check")
    print("=" * 60)
    print()

    status = check_dependencies()

    print("Python packages:")
    print(f"  chromadb: {'✓ installed' if status['chromadb'] else '✗ not installed (pip install chromadb)'}")
    print(f"  requests: {'✓ installed' if status['requests'] else '✗ not installed (pip install requests)'}")
    print()
    print("Services:")
    print(f"  ollama:   {'✓ running' if status['ollama'] else '✗ not running (ollama serve)'}")
    print()

    if status['ready']:
        print("✓ All dependencies satisfied. Ready for indexing.")
    else:
        print("✗ Some dependencies missing. Install them to use vector search.")

    print("=" * 60)

    sys.exit(0 if status['ready'] else 1)


def main():
    if len(sys.argv) < 2:
        print("Usage: memory_index.py <command> [args]")
        print()
        print("Commands:")
        print("  search <query>           - Semantic search for relevant content")
        print("  search --fallback <query> - Hybrid search (semantic + keyword fallback)")
        print("  ingest                   - Full reindex of project files")
        print("  ingest --changed         - Incremental reindex (changed files only)")
        print("  stats                    - Show index statistics")
        print("  check                    - Verify dependencies are installed")
        print()
        print("Search filters:")
        print("  --content-type <type>    - Filter by content type (memory, character, code, etc.)")
        print("  --source-path <prefix>   - Filter by source file path prefix")
        print("  --time-after <ISO8601>   - Filter by ingested_at >= timestamp")
        print("  --time-before <ISO8601>  - Filter by ingested_at <= timestamp")
        print()
        print("Requires: chromadb, requests, ollama running locally")
        sys.exit(1)

    command = sys.argv[1]

    if command == "search":
        if len(sys.argv) < 3:
            print("Error: search requires a query", file=sys.stderr)
            sys.exit(1)

        # Parse flags
        use_fallback = "--fallback" in sys.argv
        content_type = None
        source_path = None
        time_after = None
        time_before = None

        args = sys.argv[2:]
        query_parts = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--fallback":
                i += 1
                continue
            elif arg == "--content-type" and i + 1 < len(args):
                content_type = args[i + 1]
                i += 2
                continue
            elif arg == "--source-path" and i + 1 < len(args):
                source_path = args[i + 1]
                i += 2
                continue
            elif arg == "--time-after" and i + 1 < len(args):
                time_after = args[i + 1]
                i += 2
                continue
            elif arg == "--time-before" and i + 1 < len(args):
                time_before = args[i + 1]
                i += 2
                continue
            else:
                query_parts.append(arg)
                i += 1

        query = " ".join(query_parts)
        if not query:
            print("Error: search requires a query", file=sys.stderr)
            sys.exit(1)

        if use_fallback:
            results = search_with_fallback(query)
        else:
            results = search(
                query,
                content_type=content_type,
                source_path=source_path,
                time_after=time_after,
                time_before=time_before
            )

        print(json.dumps(results, indent=2))

    elif command == "ingest":
        changed_only = "--changed" in sys.argv
        ingest(changed_only=changed_only)

    elif command == "stats":
        stats()

    elif command == "check":
        check()

    else:
        print(f"Error: Unknown command '{command}'", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
