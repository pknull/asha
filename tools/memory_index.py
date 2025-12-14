#!/usr/bin/env python3
"""
Memory Index - Unified interface for semantic search over project files

Uses ChromaDB + Ollama embeddings for document indexing and retrieval.
Requires: chromadb, requests, ollama running locally

Usage:
    python memory_index.py search "what is the creator's name?"
    python memory_index.py search --fallback "who tells Aldric to go"
    python memory_index.py ingest              # Full reindex
    python memory_index.py ingest --changed    # Incremental (git-diff based)
    python memory_index.py stats               # Show index statistics
    python memory_index.py check               # Verify dependencies
"""

import sys
import json
import subprocess
import hashlib
import re
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
    """Find project root via git or upward search for Memory/"""
    # Try git root first
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

    # Upward search for Memory/ directory
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

# Default index patterns (can be overridden via config file)
DEFAULT_INDEX_PATTERNS = [
    # Memory files (high priority - project context)
    "Memory/*.md",
    # Asha framework
    "Asha/**/*.md",
    # Common code locations
    "Tools/*.py",
    ".claude/agents/*.md",
    ".claude/commands/*.md",
    ".claude/docs/*.md",
]

# Project-specific patterns loaded from config
INDEX_PATTERNS = None

def load_index_patterns() -> list[str]:
    """Load index patterns from config or use defaults"""
    global INDEX_PATTERNS
    if INDEX_PATTERNS is not None:
        return INDEX_PATTERNS

    init_paths()
    config_file = VECTOR_DB_PATH / "index_config.json"

    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                INDEX_PATTERNS = config.get("patterns", DEFAULT_INDEX_PATTERNS)
                return INDEX_PATTERNS
        except (json.JSONDecodeError, IOError):
            pass

    INDEX_PATTERNS = DEFAULT_INDEX_PATTERNS
    return INDEX_PATTERNS


# Chunking config
MAX_CHUNK_SIZE = 4000
CHUNK_OVERLAP = 400  # Characters of overlap between chunks

# Content type detection
CONTENT_TYPE_MAP = {
    "Memory/": "memory",
    "Asha/": "framework",
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
    """Get embedding from Ollama"""
    response = requests.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        json={"model": EMBEDDING_MODEL, "prompt": text}
    )
    response.raise_for_status()
    return response.json()["embedding"]


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


def search(query: str, limit: int = 5) -> dict:
    """Search for relevant content using semantic similarity"""
    require_dependencies("search")
    _, collection = init_db()

    # Get query embedding
    query_embedding = get_embedding(query)

    # Search
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=limit,
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


def get_files_to_index() -> list[Path]:
    """Get all files matching index patterns"""
    init_paths()
    patterns = load_index_patterns()
    files = []
    for pattern in patterns:
        matches = list(PROJECT_ROOT.glob(pattern))
        files.extend(matches)
    return sorted(set(files))


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
        print(f"  ⚠️  Skipping {file_path.name} (not found)")
        return 0

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        print(f"  ⚠️  Skipping {file_path.name} (binary or encoding error)")
        return 0

    if not content.strip():
        print(f"  ⚠️  Skipping {file_path.name} (empty)")
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

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas
    )

    chunk_info = f" ({len(chunks)} chunks)" if len(chunks) > 1 else ""
    print(f"  📄 {rel_path}{chunk_info} [{content_type}]")
    return len(chunks)


def ingest(changed_only: bool = False):
    """Ingest files into vector DB"""
    require_dependencies("ingest")
    init_paths()

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

    for file_path in files_to_ingest:
        chunks = ingest_file(collection, file_path)
        if chunks > 0:
            total_chunks += chunks
            rel_path = str(file_path.relative_to(PROJECT_ROOT))
            new_state["files"][rel_path] = file_content_hash(file_path)

    save_index_state(new_state)

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
        print("Requires: chromadb, requests, ollama running locally")
        sys.exit(1)

    command = sys.argv[1]

    if command == "search":
        if len(sys.argv) < 3:
            print("Error: search requires a query", file=sys.stderr)
            sys.exit(1)

        use_fallback = "--fallback" in sys.argv
        # Remove --fallback from args for query construction
        args = [a for a in sys.argv[2:] if a != "--fallback"]
        query = " ".join(args)

        if use_fallback:
            results = search_with_fallback(query)
        else:
            results = search(query)

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
