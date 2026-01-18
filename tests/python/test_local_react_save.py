#!/usr/bin/env python3
"""
Unit tests for local_react_save.py

Tests the LocalReActSave pattern analysis without external dependencies.
Run with: python -m pytest tests/python/test_local_react_save.py -v
Or:       python tests/python/test_local_react_save.py
"""

import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

# Add tools directory to path
TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "asha" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from local_react_save import LocalReActSave


class TestErrorPatternDetection(unittest.TestCase):
    """Test error pattern detection in session content"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="local_react_save_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.session_file = Path(cls.temp_dir) / "test_session.md"

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_find_try_catch_blocks(self):
        """Test detection of try-catch blocks"""
        content = """
Some code here:
try {
    doSomething();
} catch (error) {
    handleError(error);
}
More code.
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        patterns = agent._find_error_patterns(content)

        try_catch = [p for p in patterns if p['type'] == 'try-catch-block']
        self.assertEqual(len(try_catch), 1)
        self.assertEqual(try_catch[0]['count'], 1)

    def test_find_promise_catch(self):
        """Test detection of promise catch patterns"""
        content = """
fetch('/api/data')
    .then(response => response.json())
    .catch(err => console.error(err));

anotherPromise().catch(handleError);
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        patterns = agent._find_error_patterns(content)

        promise_catch = [p for p in patterns if p['type'] == 'promise-catch']
        self.assertEqual(len(promise_catch), 1)
        self.assertEqual(promise_catch[0]['count'], 2)

    def test_find_error_throw(self):
        """Test detection of throw new Error patterns"""
        content = """
if (invalid) {
    throw new ValidationError('Invalid input');
}
if (missing) {
    throw new TypeError('Missing required field');
}
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        patterns = agent._find_error_patterns(content)

        error_throw = [p for p in patterns if p['type'] == 'error-throw']
        self.assertEqual(len(error_throw), 1)
        self.assertEqual(error_throw[0]['count'], 2)


class TestCodePatternDetection(unittest.TestCase):
    """Test code pattern detection"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="local_react_save_code_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.session_file = Path(cls.temp_dir) / "test_session.md"

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_detect_async_functions(self):
        """Test detection of async function patterns"""
        # The regex is: async\s+\w+\s*\([^)]*\)\s*{
        # Requires: async, whitespace, word chars, optional whitespace, parens, brace
        content = """
async fetchData() {
    return await api.get('/data');
}

async handleSubmit(event) {
    await processForm(event);
}
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        patterns = agent._extract_code_patterns(content)

        async_func = [p for p in patterns if p['pattern'] == 'async-function']
        self.assertEqual(len(async_func), 1)
        self.assertEqual(async_func[0]['frequency'], 2)

    def test_detect_react_hooks(self):
        """Test detection of React hook patterns"""
        content = """
useEffect(() => {
    fetchData();
}, [dependencies]);

useEffect(() => {
    cleanup();
    return () => dispose();
}, []);
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        patterns = agent._extract_code_patterns(content)

        hooks = [p for p in patterns if p['pattern'] == 'react-hook']
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]['frequency'], 2)

    def test_detect_typescript_interfaces(self):
        """Test detection of TypeScript interface patterns"""
        content = """
interface User {
    id: string;
    name: string;
}

interface ApiResponse {
    data: any;
    status: number;
}
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        patterns = agent._extract_code_patterns(content)

        interfaces = [p for p in patterns if p['pattern'] == 'typescript-interface']
        self.assertEqual(len(interfaces), 1)
        self.assertEqual(interfaces[0]['frequency'], 2)

    def test_detect_module_exports(self):
        """Test detection of export default patterns"""
        content = """
export default function Component() {
    return <div>Hello</div>;
}

export default UserService;
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        patterns = agent._extract_code_patterns(content)

        exports = [p for p in patterns if p['pattern'] == 'module-export']
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]['frequency'], 2)


class TestRepetitionDetection(unittest.TestCase):
    """Test repetition detection in session content"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="local_react_save_rep_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.session_file = Path(cls.temp_dir) / "test_session.md"

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_find_repeated_lines(self):
        """Test detection of repeated lines"""
        content = """This is a unique line
This line appears multiple times
Another unique line
This line appears multiple times
Something else
This line appears multiple times
Final line"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        repetitions = agent._find_repetitions(content)

        self.assertEqual(len(repetitions), 1)
        self.assertIn("This line appears multiple times", repetitions[0])
        self.assertIn("3 times", repetitions[0])

    def test_ignore_short_lines(self):
        """Test that short lines are not flagged as repetitions"""
        content = """a
a
a
a
a
Some longer content"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        repetitions = agent._find_repetitions(content)

        # Short lines (< 10 chars) should be ignored
        self.assertEqual(len(repetitions), 0)


class TestFileChangeAnalysis(unittest.TestCase):
    """Test file change analysis"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="local_react_save_file_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.session_file = Path(cls.temp_dir) / "test_session.md"

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_analyze_file_changes(self):
        """Test extraction of file change information"""
        content = """
Session log:
modified: src/components/Button.tsx
created: src/utils/helpers.ts
modified: src/components/Button.tsx
deleted: old/deprecated.js
modified: src/api/client.ts
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        analysis = agent._analyze_file_changes(content)

        self.assertEqual(analysis['total_files'], 4)
        self.assertIn('tsx', analysis['file_types'])
        self.assertIn('ts', analysis['file_types'])
        self.assertIn('js', analysis['file_types'])

    def test_most_modified_files(self):
        """Test tracking of most frequently modified files"""
        content = """
modified: src/main.ts
modified: src/main.ts
modified: src/main.ts
modified: src/utils.ts
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        analysis = agent._analyze_file_changes(content)

        most_modified = analysis['most_modified']
        self.assertEqual(most_modified[0][0], 'src/main.ts')
        self.assertEqual(most_modified[0][1], 3)


class TestCommandExtraction(unittest.TestCase):
    """Test command extraction from session content"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="local_react_save_cmd_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.session_file = Path(cls.temp_dir) / "test_session.md"

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_extract_shell_commands(self):
        """Test extraction of shell commands"""
        content = """
Running tests:
$ npm test
Output shows 5 tests passing.

Building project:
$ npm run build
Build successful.

$ git status
No changes.
"""
        self.session_file.write_text(content)
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))
        commands = agent._extract_commands(content)

        self.assertEqual(len(commands), 3)
        self.assertIn('npm test', commands)
        self.assertIn('npm run build', commands)
        self.assertIn('git status', commands)


class TestInsightExtraction(unittest.TestCase):
    """Test insight extraction logic"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="local_react_save_insight_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.session_file = Path(cls.temp_dir) / "test_session.md"

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_high_frequency_pattern_insight(self):
        """Test insight for frequently used patterns"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        thoughts = {
            'code_patterns': [
                {'pattern': 'async-function', 'frequency': 5}
            ],
            'error_patterns': []
        }
        redundancy = {'redundant_sections': []}

        insights = agent.act_extract_insights(thoughts, redundancy)

        self.assertTrue(any('async-function' in i for i in insights))

    def test_comprehensive_error_handling_insight(self):
        """Test insight for comprehensive error handling"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        thoughts = {
            'code_patterns': [],
            'error_patterns': [
                {'type': 'try-catch-block', 'count': 2},
                {'type': 'promise-catch', 'count': 3},
                {'type': 'error-throw', 'count': 1},
                {'type': 'error-check', 'count': 2}
            ]
        }
        redundancy = {'redundant_sections': []}

        insights = agent.act_extract_insights(thoughts, redundancy)

        self.assertTrue(any('comprehensive error handling' in i.lower() for i in insights))

    def test_testing_workflow_insight(self):
        """Test insight for testing workflow detection"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        thoughts = {
            'code_patterns': [],
            'error_patterns': [],
            'command_patterns': ['npm test', 'jest']
        }
        redundancy = {'redundant_sections': []}

        insights = agent.act_extract_insights(thoughts, redundancy)

        self.assertTrue(any('testing' in i.lower() for i in insights))


class TestDecisionMaking(unittest.TestCase):
    """Test observation and decision-making logic"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="local_react_save_decision_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.session_file = Path(cls.temp_dir) / "test_session.md"

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_merge_recommendation_for_large_redundancy(self):
        """Test merge recommendation for large redundant sections"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        thoughts = {'code_patterns': []}
        actions_results = {
            'redundancy': {
                'redundant_sections': [
                    {'size': 300, 'recommendation': 'merge', 'found_in': 'existing.md'}
                ]
            },
            'insights': []
        }

        decisions = agent.observe_and_decide(thoughts, actions_results)

        self.assertEqual(len(decisions['memory_updates']), 1)
        self.assertEqual(decisions['memory_updates'][0]['action'], 'merge')

    def test_abstraction_suggestion_for_high_frequency(self):
        """Test abstraction suggestion for patterns used >5 times"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        thoughts = {
            'code_patterns': [
                {'pattern': 'async-function', 'frequency': 7}
            ]
        }
        actions_results = {
            'redundancy': {'redundant_sections': []},
            'insights': []
        }

        decisions = agent.observe_and_decide(thoughts, actions_results)

        self.assertEqual(len(decisions['new_abstractions']), 1)
        self.assertEqual(decisions['new_abstractions'][0]['pattern'], 'async-function')
        self.assertEqual(decisions['new_abstractions'][0]['usage_count'], 7)


class TestUtilityFunctions(unittest.TestCase):
    """Test utility functions"""

    @classmethod
    def setUpClass(cls):
        """Create temporary test directory"""
        cls.temp_dir = tempfile.mkdtemp(prefix="local_react_save_util_test_")
        cls.memory_dir = Path(cls.temp_dir) / "Memory"
        cls.memory_dir.mkdir(parents=True)
        cls.session_file = Path(cls.temp_dir) / "test_session.md"

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory"""
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_extract_context(self):
        """Test context extraction around a pattern"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        content = "prefix text TARGET_PATTERN suffix text"
        context = agent._extract_context(content, "TARGET_PATTERN", context_size=10)

        self.assertIn("TARGET_PATTERN", context)
        self.assertIn("text", context)

    def test_calculate_similarity(self):
        """Test similarity calculation"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        content = "pattern pattern pattern pattern pattern"
        similarity = agent._calculate_similarity("pattern", content)

        self.assertEqual(similarity, 0.5)  # 5 occurrences / 10 = 0.5

    def test_calculate_redundancy_percentage_empty(self):
        """Test redundancy percentage with no redundant sections"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        redundancy = {'redundant_sections': []}
        percentage = agent._calculate_redundancy_percentage(redundancy)

        self.assertEqual(percentage, 0.0)

    def test_generate_summary(self):
        """Test summary generation"""
        self.session_file.write_text("test content")
        agent = LocalReActSave(str(self.session_file), str(self.memory_dir))

        decisions = {
            'memory_updates': [{'action': 'merge'}],
            'new_abstractions': [{'pattern': 'async'}],
            'cross_project_suggestions': []
        }

        summary = agent._generate_summary(decisions)

        self.assertIn("1 memory updates", summary)
        self.assertIn("1 patterns", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
