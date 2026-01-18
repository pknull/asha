#!/usr/bin/env python3
"""
Unit tests for memory_index.py

Tests the utility functions that don't require chromadb/ollama.
Run with: python -m pytest tests/python/test_memory_index.py -v
Or:       python tests/python/test_memory_index.py
"""

import os
import sys
import json
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add tools directory to path
TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "asha" / "tools"
sys.path.insert(0, str(TOOLS_DIR))


class TestMemoryIndexUtilities(unittest.TestCase):
    """Test utility functions that don't require external dependencies"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="memory_index_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.vector_db_dir = cls.memory_dir / "vector_db"
        cls.vector_db_dir.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _import_with_mock_project_root(self):
        """Import memory_index with mocked project root"""
        if "memory_index" in sys.modules:
            del sys.modules["memory_index"]
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.temp_dir}):
            import memory_index
            memory_index.init_paths()
            return memory_index

    def test_check_dependencies_structure(self):
        """Test that check_dependencies returns expected structure"""
        mi = self._import_with_mock_project_root()
        status = mi.check_dependencies()

        self.assertIn("chromadb", status)
        self.assertIn("requests", status)
        self.assertIn("ollama", status)
        self.assertIn("ready", status)
        self.assertIsInstance(status["chromadb"], bool)
        self.assertIsInstance(status["requests"], bool)
        self.assertIsInstance(status["ollama"], bool)
        self.assertIsInstance(status["ready"], bool)

    def test_get_content_type_memory(self):
        """Test content type detection for Memory files"""
        mi = self._import_with_mock_project_root()

        self.assertEqual(mi.get_content_type("Memory/activeContext.md"), "memory")
        self.assertEqual(mi.get_content_type("Memory/projectbrief.md"), "memory")

    def test_get_content_type_code(self):
        """Test content type detection for code files"""
        mi = self._import_with_mock_project_root()

        self.assertEqual(mi.get_content_type("Tools/script.py"), "code")
        self.assertEqual(mi.get_content_type("Tools/helper.sh"), "code")

    def test_get_content_type_unknown(self):
        """Test content type detection for unknown paths"""
        mi = self._import_with_mock_project_root()

        self.assertEqual(mi.get_content_type("random/path/file.txt"), "unknown")
        self.assertEqual(mi.get_content_type("src/main.py"), "unknown")

    def test_extract_section_headers_basic(self):
        """Test section header extraction from markdown"""
        mi = self._import_with_mock_project_root()

        content = """# Main Title

Some content here.

## Section One

More content.

### Subsection

Even more content.
"""
        headers = mi.extract_section_headers(content)

        self.assertEqual(len(headers), 3)
        self.assertEqual(headers[0][1], "h1:Main Title")
        self.assertEqual(headers[1][1], "h2:Section One")
        self.assertEqual(headers[2][1], "h3:Subsection")

    def test_extract_section_headers_empty(self):
        """Test section header extraction from content without headers"""
        mi = self._import_with_mock_project_root()

        content = "Just some plain text without any headers."
        headers = mi.extract_section_headers(content)

        self.assertEqual(headers, [])

    def test_get_section_for_position(self):
        """Test getting section header for a given position"""
        mi = self._import_with_mock_project_root()

        headers = [(0, "h1:Title"), (50, "h2:Section"), (100, "h3:Subsection")]

        self.assertEqual(mi.get_section_for_position(headers, 25), "h1:Title")
        self.assertEqual(mi.get_section_for_position(headers, 75), "h2:Section")
        self.assertEqual(mi.get_section_for_position(headers, 150), "h3:Subsection")

    def test_chunk_text_small(self):
        """Test chunking of text smaller than chunk size"""
        mi = self._import_with_mock_project_root()

        content = "This is a small piece of text."
        chunks = mi.chunk_text(content, chunk_size=1000)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], content)
        self.assertEqual(chunks[0][1], 0)  # Start position

    def test_chunk_text_large(self):
        """Test chunking of text larger than chunk size"""
        mi = self._import_with_mock_project_root()

        # Create content larger than chunk size
        paragraph = "This is a paragraph of text that contains some words. " * 10
        content = "\n\n".join([paragraph] * 20)

        chunks = mi.chunk_text(content, chunk_size=500, overlap=50)

        self.assertGreater(len(chunks), 1)
        # Each chunk should be <= chunk_size (allowing for paragraph boundary handling)
        for chunk_text, _ in chunks:
            self.assertLessEqual(len(chunk_text), 600)  # Allow some margin

    def test_extract_keywords_basic(self):
        """Test keyword extraction from query"""
        mi = self._import_with_mock_project_root()

        query = "What is the authentication method for the API?"
        keywords = mi.extract_keywords(query)

        self.assertIn("authentication", keywords)
        self.assertIn("method", keywords)
        # Stop words should be removed
        self.assertNotIn("the", keywords)
        self.assertNotIn("is", keywords)
        self.assertNotIn("for", keywords)

    def test_extract_keywords_preserves_case(self):
        """Test that keyword extraction preserves original case"""
        mi = self._import_with_mock_project_root()

        query = "Find the UserProfile component in React"
        keywords = mi.extract_keywords(query)

        self.assertIn("UserProfile", keywords)
        self.assertIn("React", keywords)


class TestExclusionConfig(unittest.TestCase):
    """Test file exclusion configuration"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="memory_index_excl_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.vector_db_dir = cls.memory_dir / "vector_db"
        cls.vector_db_dir.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        """Reset exclusion config and module before each test"""
        # Remove module to force reimport with fresh state
        if "memory_index" in sys.modules:
            del sys.modules["memory_index"]
        # Clean up any config files from previous tests
        config_file = self.vector_db_dir / "index_config.json"
        if config_file.exists():
            config_file.unlink()

    def _import_with_mock_project_root(self):
        """Import memory_index with mocked project root"""
        if "memory_index" in sys.modules:
            del sys.modules["memory_index"]
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.temp_dir}):
            import memory_index
            memory_index.EXCLUDE_CONFIG = None  # Reset cached config
            memory_index.init_paths()
            return memory_index

    def test_should_exclude_git(self):
        """Test that .git directory is excluded"""
        mi = self._import_with_mock_project_root()

        # Create a path that includes .git as a directory component
        git_dir = Path(self.temp_dir) / ".git"
        git_dir.mkdir(exist_ok=True)
        path = git_dir / "config"
        self.assertTrue(mi.should_exclude(path))

    def test_should_exclude_node_modules(self):
        """Test that node_modules is excluded"""
        mi = self._import_with_mock_project_root()

        # Create path structure
        node_modules = Path(self.temp_dir) / "node_modules"
        node_modules.mkdir(exist_ok=True)
        package_dir = node_modules / "some-package"
        package_dir.mkdir(exist_ok=True)
        path = package_dir / "index.js"
        self.assertTrue(mi.should_exclude(path))

    def test_should_exclude_binary_extensions(self):
        """Test that binary file extensions are excluded"""
        mi = self._import_with_mock_project_root()

        # Binary extensions should be excluded regardless of path
        # The function checks file.suffix.lower()
        test_cases = [".png", ".db", ".zip", ".pyc"]
        for ext in test_cases:
            path = Path(self.temp_dir) / f"file{ext}"
            self.assertTrue(mi.should_exclude(path), f"Should exclude {ext}")

    def test_should_not_exclude_text_files(self):
        """Test that text files are not excluded"""
        mi = self._import_with_mock_project_root()

        text_files = [
            Path(self.temp_dir) / "readme.md",
            Path(self.temp_dir) / "script.py",
            Path(self.temp_dir) / "config.json",
            Path(self.temp_dir) / "style.css",
        ]
        for path in text_files:
            self.assertFalse(mi.should_exclude(path), f"Should not exclude {path}")

    def test_should_exclude_lockfiles(self):
        """Test that lock files are excluded"""
        mi = self._import_with_mock_project_root()

        lock_files = ["package-lock.json", "yarn.lock", "poetry.lock"]
        for filename in lock_files:
            path = Path(self.temp_dir) / filename
            self.assertTrue(mi.should_exclude(path), f"Should exclude {filename}")

    def test_custom_config_replaces_defaults(self):
        """Test that custom config can replace default exclusions"""
        mi = self._import_with_mock_project_root()

        # Create custom config
        config_file = self.vector_db_dir / "index_config.json"
        config_file.write_text(json.dumps({
            "exclude_dirs": ["custom_excluded"],
            "exclude_extensions": [".custom"],
            "exclude_files": ["custom.lock"]
        }))

        # Reset and reload config
        mi.EXCLUDE_CONFIG = None
        config = mi.load_exclude_config()

        self.assertIn("custom_excluded", config["exclude_dirs"])
        self.assertIn(".custom", config["exclude_extensions"])
        self.assertIn("custom.lock", config["exclude_files"])
        # Defaults should be replaced
        self.assertNotIn(".git", config["exclude_dirs"])

    def test_custom_config_extends_defaults(self):
        """Test that custom config can extend default exclusions"""
        mi = self._import_with_mock_project_root()

        # Create custom config with additional exclusions
        config_file = self.vector_db_dir / "index_config.json"
        config_file.write_text(json.dumps({
            "additional_exclude_dirs": ["my_custom_dir"],
            "additional_exclude_extensions": [".myext"],
            "additional_exclude_files": ["my.lock"]
        }))

        # Reset and reload config
        mi.EXCLUDE_CONFIG = None
        config = mi.load_exclude_config()

        # Defaults should still be present
        self.assertIn(".git", config["exclude_dirs"])
        self.assertIn(".png", config["exclude_extensions"])
        # Additional should be included
        self.assertIn("my_custom_dir", config["exclude_dirs"])
        self.assertIn(".myext", config["exclude_extensions"])
        self.assertIn("my.lock", config["exclude_files"])


class TestIndexState(unittest.TestCase):
    """Test index state persistence"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="memory_index_state_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.vector_db_dir = cls.memory_dir / "vector_db"
        cls.vector_db_dir.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        """Clean state file before each test"""
        state_file = self.vector_db_dir / ".index_state.json"
        if state_file.exists():
            state_file.unlink()

    def _import_with_mock_project_root(self):
        """Import memory_index with mocked project root"""
        if "memory_index" in sys.modules:
            del sys.modules["memory_index"]
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.temp_dir}):
            import memory_index
            memory_index.init_paths()
            return memory_index

    def test_load_index_state_empty(self):
        """Test loading index state when no state file exists"""
        mi = self._import_with_mock_project_root()

        state = mi.load_index_state()

        self.assertEqual(state["git_hash"], "")
        self.assertEqual(state["files"], {})

    def test_save_and_load_index_state(self):
        """Test saving and loading index state"""
        mi = self._import_with_mock_project_root()

        test_state = {
            "git_hash": "abc123",
            "files": {
                "Memory/test.md": "hash123",
                "Memory/other.md": "hash456"
            },
            "last_ingest": "2025-01-01T00:00:00",
            "config": {"chunk_size": 4000}
        }

        mi.save_index_state(test_state)
        loaded_state = mi.load_index_state()

        self.assertEqual(loaded_state["git_hash"], "abc123")
        self.assertEqual(loaded_state["files"]["Memory/test.md"], "hash123")
        self.assertEqual(loaded_state["config"]["chunk_size"], 4000)


class TestProjectRootDetection(unittest.TestCase):
    """Test project root detection"""

    def test_detect_via_env_variable(self):
        """Test detection via CLAUDE_PROJECT_DIR environment variable"""
        temp_dir = tempfile.mkdtemp()
        memory_dir = Path(temp_dir) / "Memory"
        memory_dir.mkdir()

        try:
            if "memory_index" in sys.modules:
                del sys.modules["memory_index"]

            with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": temp_dir}):
                import memory_index
                detected = memory_index.detect_project_root()
                self.assertEqual(str(detected), temp_dir)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestFileContentHash(unittest.TestCase):
    """Test file content hashing"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="memory_index_hash_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _import_with_mock_project_root(self):
        """Import memory_index with mocked project root"""
        if "memory_index" in sys.modules:
            del sys.modules["memory_index"]
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.temp_dir}):
            import memory_index
            memory_index.init_paths()
            return memory_index

    def test_file_content_hash_consistent(self):
        """Test that same content produces same hash"""
        mi = self._import_with_mock_project_root()

        test_file = Path(self.temp_dir) / "test.md"
        test_file.write_text("Test content")

        hash1 = mi.file_content_hash(test_file)
        hash2 = mi.file_content_hash(test_file)

        self.assertEqual(hash1, hash2)

    def test_file_content_hash_different_content(self):
        """Test that different content produces different hash"""
        mi = self._import_with_mock_project_root()

        test_file1 = Path(self.temp_dir) / "test1.md"
        test_file2 = Path(self.temp_dir) / "test2.md"
        test_file1.write_text("Content A")
        test_file2.write_text("Content B")

        hash1 = mi.file_content_hash(test_file1)
        hash2 = mi.file_content_hash(test_file2)

        self.assertNotEqual(hash1, hash2)


class TestGetFilesToIndex(unittest.TestCase):
    """Test file discovery for indexing"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory with Memory structure"""
        cls.temp_dir = tempfile.mkdtemp(prefix="memory_index_files_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.vector_db_dir = cls.memory_dir / "vector_db"
        cls.vector_db_dir.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        """Clean up test files before each test"""
        for f in self.memory_dir.glob("*.md"):
            f.unlink()

    def _import_with_mock_project_root(self):
        """Import memory_index with mocked project root"""
        if "memory_index" in sys.modules:
            del sys.modules["memory_index"]
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.temp_dir}):
            import memory_index
            memory_index.init_paths()
            return memory_index

    def test_get_files_to_index_finds_md_files(self):
        """Test that get_files_to_index finds markdown files in Memory/"""
        mi = self._import_with_mock_project_root()

        # Create test markdown files
        (self.memory_dir / "test1.md").write_text("# Test 1")
        (self.memory_dir / "test2.md").write_text("# Test 2")

        files = mi.get_files_to_index()
        filenames = [f.name for f in files]

        self.assertIn("test1.md", filenames)
        self.assertIn("test2.md", filenames)

    def test_get_files_to_index_excludes_vector_db(self):
        """Test that vector_db directory is excluded"""
        mi = self._import_with_mock_project_root()

        # Create file in Memory/
        (self.memory_dir / "good.md").write_text("# Good")
        # Create file in vector_db/ (should be excluded)
        (self.vector_db_dir / "bad.md").write_text("# Bad")

        files = mi.get_files_to_index()
        filenames = [f.name for f in files]

        self.assertIn("good.md", filenames)
        self.assertNotIn("bad.md", filenames)

    def test_get_files_to_index_handles_subdirs(self):
        """Test that subdirectories in Memory/ are included"""
        mi = self._import_with_mock_project_root()

        # Create subdirectory
        subdir = self.memory_dir / "subdir"
        subdir.mkdir(exist_ok=True)
        (subdir / "nested.md").write_text("# Nested")
        (self.memory_dir / "root.md").write_text("# Root")

        files = mi.get_files_to_index()
        filenames = [f.name for f in files]

        self.assertIn("root.md", filenames)
        self.assertIn("nested.md", filenames)


class TestCheckFunction(unittest.TestCase):
    """Test check_dependencies output"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="memory_index_check_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.vector_db_dir = cls.memory_dir / "vector_db"
        cls.vector_db_dir.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _import_with_mock_project_root(self):
        """Import memory_index with mocked project root"""
        if "memory_index" in sys.modules:
            del sys.modules["memory_index"]
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.temp_dir}):
            import memory_index
            memory_index.init_paths()
            return memory_index

    def test_check_dependencies_returns_dict(self):
        """Test that check_dependencies returns a dictionary with expected keys"""
        mi = self._import_with_mock_project_root()

        result = mi.check_dependencies()

        self.assertIsInstance(result, dict)
        self.assertIn("chromadb", result)
        self.assertIn("ollama", result)

    def test_check_dependencies_has_boolean_values(self):
        """Test that dependency check has boolean values"""
        mi = self._import_with_mock_project_root()

        result = mi.check_dependencies()

        # Each dependency should have boolean value
        self.assertIsInstance(result["chromadb"], bool)
        self.assertIsInstance(result["ollama"], bool)
        self.assertIsInstance(result["ready"], bool)


if __name__ == "__main__":
    unittest.main(verbosity=2)
