#!/usr/bin/env python3
"""
Unit tests for reasoning_bank.py

Run with: python -m pytest tests/python/test_reasoning_bank.py -v
Or:       python tests/python/test_reasoning_bank.py
"""

import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

# Add tools directory to path
TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "asha" / "tools"
sys.path.insert(0, str(TOOLS_DIR))


class TestReasoningBank(unittest.TestCase):
    """Test cases for reasoning_bank module"""

    @classmethod
    def setUpClass(cls):
        """Create a temporary directory structure for tests"""
        cls.temp_dir = tempfile.mkdtemp(prefix="reasoning_bank_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.reasoning_bank_dir = cls.memory_dir / "reasoning_bank"
        cls.reasoning_bank_dir.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        """Reset database before each test"""
        db_path = self.reasoning_bank_dir / "patterns.db"
        if db_path.exists():
            db_path.unlink()

    def _import_with_mock_project_root(self):
        """Import reasoning_bank with mocked project root"""
        # Remove cached module if present
        if "reasoning_bank" in sys.modules:
            del sys.modules["reasoning_bank"]

        # Mock detect_project_root to use our temp directory
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.temp_dir}):
            import reasoning_bank
            return reasoning_bank

    def test_init_db_creates_tables(self):
        """Test that init_db creates all required tables"""
        rb = self._import_with_mock_project_root()

        conn = rb.init_db()
        cursor = conn.cursor()

        # Check tables exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        self.assertIn("patterns", tables)
        self.assertIn("error_resolutions", tables)
        self.assertIn("tool_effectiveness", tables)

        conn.close()

    def test_record_pattern_valid(self):
        """Test recording a valid pattern"""
        rb = self._import_with_mock_project_root()

        result = rb.record_pattern(
            pattern_type="code",
            context="refactoring function",
            action="extract method",
            outcome="improved readability",
            score=0.9,
            tags=["refactor", "clean-code"]
        )

        self.assertEqual(result["status"], "recorded")
        self.assertIn("id", result)
        self.assertEqual(result["pattern_type"], "code")
        self.assertEqual(result["success_score"], 0.9)

    def test_record_pattern_invalid_type(self):
        """Test that invalid pattern types are rejected"""
        rb = self._import_with_mock_project_root()

        result = rb.record_pattern(
            pattern_type="invalid_type",
            context="test",
            action="test"
        )

        self.assertIn("error", result)
        self.assertIn("Invalid pattern type", result["error"])

    def test_record_pattern_invalid_score(self):
        """Test that scores outside 0-1 range are rejected"""
        rb = self._import_with_mock_project_root()

        result = rb.record_pattern(
            pattern_type="code",
            context="test",
            action="test",
            score=1.5  # Invalid: > 1.0
        )

        self.assertIn("error", result)
        self.assertIn("Score must be between", result["error"])

    def test_record_error_resolution_new(self):
        """Test recording a new error resolution"""
        rb = self._import_with_mock_project_root()

        result = rb.record_error_resolution(
            error_type="ImportError",
            signature="No module named 'requests'",
            resolution="pip install requests",
            prevention="Add requests to requirements.txt"
        )

        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["error_type"], "ImportError")
        self.assertEqual(result["occurrence_count"], 1)

    def test_record_error_resolution_duplicate_increments_count(self):
        """Test that recording same error increments occurrence count"""
        rb = self._import_with_mock_project_root()

        # Record first time
        rb.record_error_resolution(
            error_type="TypeError",
            signature="'NoneType' has no attribute 'strip'",
            resolution="Add null check"
        )

        # Record same error again
        result = rb.record_error_resolution(
            error_type="TypeError",
            signature="'NoneType' has no attribute 'strip'",
            resolution="Add null check"
        )

        self.assertEqual(result["occurrence_count"], 2)

    def test_record_tool_usage_success(self):
        """Test recording successful tool usage"""
        rb = self._import_with_mock_project_root()

        result = rb.record_tool_usage(
            tool_name="Grep",
            use_case="finding function definitions",
            success=True,
            duration_ms=150.5
        )

        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["tool_name"], "Grep")
        self.assertTrue(result["success"])

    def test_record_tool_usage_tracks_success_rate(self):
        """Test that tool usage tracks success/failure counts"""
        rb = self._import_with_mock_project_root()

        # Record 3 successes, 2 failures
        for _ in range(3):
            rb.record_tool_usage("Task", "code review", success=True)
        for _ in range(2):
            rb.record_tool_usage("Task", "code review", success=False)

        # Query to verify counts
        conn = rb.init_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT success_count, failure_count
            FROM tool_effectiveness
            WHERE tool_name = 'Task' AND use_case = 'code review'
        """)
        row = cursor.fetchone()
        conn.close()

        self.assertEqual(row["success_count"], 3)
        self.assertEqual(row["failure_count"], 2)

    def test_query_patterns_by_type(self):
        """Test querying patterns by type"""
        rb = self._import_with_mock_project_root()

        # Add patterns of different types
        rb.record_pattern("code", "context1", "action1", score=0.8)
        rb.record_pattern("workflow", "context2", "action2", score=0.7)
        rb.record_pattern("code", "context3", "action3", score=0.9)

        response = rb.query_patterns(pattern_type="code")
        results = response.get("results", [])

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["pattern_type"], "code")

    def test_query_patterns_by_min_score(self):
        """Test querying patterns with minimum score filter"""
        rb = self._import_with_mock_project_root()

        rb.record_pattern("code", "context1", "action1", score=0.3)
        rb.record_pattern("code", "context2", "action2", score=0.7)
        rb.record_pattern("code", "context3", "action3", score=0.9)

        response = rb.query_patterns(min_score=0.6)
        results = response.get("results", [])

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertGreaterEqual(r["success_score"], 0.6)


class TestProjectRootDetection(unittest.TestCase):
    """Test project root detection logic"""

    def test_detect_via_env_variable(self):
        """Test detection via CLAUDE_PROJECT_DIR environment variable"""
        temp_dir = tempfile.mkdtemp()
        memory_dir = Path(temp_dir) / "Memory"
        memory_dir.mkdir()

        try:
            with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": temp_dir}):
                # Remove cached module
                if "reasoning_bank" in sys.modules:
                    del sys.modules["reasoning_bank"]

                import reasoning_bank
                # Project root should be the temp dir
                self.assertEqual(str(reasoning_bank.PROJECT_ROOT), temp_dir)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
