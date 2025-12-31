#!/usr/bin/env python3
"""
Local ReAct save - no external LLM needed
Uses pattern matching, embeddings, and heuristics
"""

import re
import json
from pathlib import Path
from typing import Dict, List, Set, Any
from collections import Counter
import difflib

class LocalReActSave:
    """ReAct-style save using local analysis only"""
    
    def __init__(self, session_file: str, memory_dir: str = "Memory/"):
        self.session_file = session_file
        self.memory_dir = Path(memory_dir)
        self.patterns_db = self.memory_dir / "reasoning_bank/patterns.db"
        
    def think(self) -> Dict[str, Any]:
        """Analyze session using pattern matching and heuristics"""
        session_content = Path(self.session_file).read_text()
        
        thoughts = {
            "error_patterns": self._find_error_patterns(session_content),
            "code_patterns": self._extract_code_patterns(session_content),
            "repeated_actions": self._find_repetitions(session_content),
            "file_changes": self._analyze_file_changes(session_content),
            "command_patterns": self._extract_commands(session_content)
        }
        
        return thoughts
    
    def _find_error_patterns(self, content: str) -> List[Dict]:
        """Find error handling patterns using regex"""
        patterns = []
        
        # Common error patterns
        error_regexes = [
            (r'try\s*{[^}]+}\s*catch', 'try-catch-block'),
            (r'\.catch\([^)]+\)', 'promise-catch'),
            (r'if\s*\(.*error.*\)', 'error-check'),
            (r'throw\s+new\s+\w+Error', 'error-throw'),
            (r'console\.error\(', 'error-log')
        ]
        
        for pattern, name in error_regexes:
            matches = re.findall(pattern, content, re.MULTILINE | re.DOTALL)
            if matches:
                patterns.append({
                    'type': name,
                    'count': len(matches),
                    'examples': matches[:3]
                })
                
        return patterns
    
    def _extract_code_patterns(self, content: str) -> List[Dict]:
        """Extract common code patterns"""
        patterns = []
        
        # Look for common patterns
        pattern_checks = [
            (r'async\s+\w+\s*\([^)]*\)\s*{', 'async-function'),
            (r'useEffect\s*\(', 'react-hook'),
            (r'@\w+\s*\n\s*class', 'decorator-pattern'),
            (r'interface\s+\w+\s*{', 'typescript-interface'),
            (r'export\s+default', 'module-export')
        ]
        
        for pattern, name in pattern_checks:
            count = len(re.findall(pattern, content))
            if count > 0:
                patterns.append({'pattern': name, 'frequency': count})
                
        return patterns
    
    def act_search_similar(self, thoughts: Dict) -> Dict[str, List]:
        """Search for similar patterns in existing memory"""
        similar_patterns = {
            'exact_matches': [],
            'partial_matches': [],
            'frequency_analysis': {}
        }
        
        # Search existing memory files
        for memory_file in self.memory_dir.glob("*.md"):
            if memory_file.name == "personalInfo.md":
                continue
                
            content = memory_file.read_text()
            
            # Check for exact pattern matches
            for error_pattern in thoughts.get('error_patterns', []):
                if error_pattern['type'] in content:
                    similar_patterns['exact_matches'].append({
                        'file': memory_file.name,
                        'pattern': error_pattern['type'],
                        'context': self._extract_context(content, error_pattern['type'])
                    })
            
            # Use difflib for fuzzy matching
            for code_pattern in thoughts.get('code_patterns', []):
                pattern_name = code_pattern['pattern']
                if pattern_name in content:
                    similar_patterns['partial_matches'].append({
                        'file': memory_file.name,
                        'pattern': pattern_name,
                        'similarity': self._calculate_similarity(pattern_name, content)
                    })
        
        return similar_patterns
    
    def act_detect_redundancy(self, session_content: str, similar_patterns: Dict) -> Dict:
        """Detect redundant information"""
        redundancy_report = {
            'redundant_sections': [],
            'merge_candidates': [],
            'unique_content': []
        }
        
        # Check each similar pattern for redundancy
        for match in similar_patterns.get('exact_matches', []):
            file_path = self.memory_dir / match['file']
            existing_content = file_path.read_text()
            
            # Use sequence matcher to find similar blocks
            sm = difflib.SequenceMatcher(None, session_content, existing_content)
            
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == 'equal' and (i2 - i1) > 50:  # Substantial match
                    redundancy_report['redundant_sections'].append({
                        'size': i2 - i1,
                        'content_preview': session_content[i1:i1+100],
                        'found_in': match['file'],
                        'recommendation': 'merge' if (i2 - i1) > 200 else 'reference'
                    })
        
        return redundancy_report
    
    def act_extract_insights(self, thoughts: Dict, redundancy: Dict) -> List[str]:
        """Extract novel insights using heuristics"""
        insights = []
        
        # Novel patterns (not found in redundancy check)
        redundant_patterns = {r['content_preview'] for r in redundancy.get('redundant_sections', [])}
        
        # High-frequency new patterns
        for pattern in thoughts.get('code_patterns', []):
            if pattern['frequency'] > 3:
                insights.append(f"Frequently used pattern: {pattern['pattern']} ({pattern['frequency']} times)")
        
        # New error handling approaches
        error_types = [e['type'] for e in thoughts.get('error_patterns', [])]
        if len(set(error_types)) > 3:
            insights.append("Comprehensive error handling implemented")
        
        # Command patterns that suggest new workflows
        commands = thoughts.get('command_patterns', [])
        if any('test' in cmd for cmd in commands):
            insights.append("Testing workflow established")
            
        return insights
    
    def observe_and_decide(self, thoughts: Dict, actions_results: Dict) -> Dict:
        """Make decisions based on observations"""
        decisions = {
            'memory_updates': [],
            'new_abstractions': [],
            'cross_project_suggestions': []
        }
        
        # Decide on memory updates
        redundancy = actions_results.get('redundancy', {})
        for section in redundancy.get('redundant_sections', []):
            if section['recommendation'] == 'merge':
                decisions['memory_updates'].append({
                    'action': 'merge',
                    'target': section['found_in'],
                    'method': 'consolidate_pattern'
                })
        
        # Decide on abstractions
        insights = actions_results.get('insights', [])
        pattern_frequency = Counter()
        
        for pattern in thoughts.get('code_patterns', []):
            pattern_frequency[pattern['pattern']] = pattern['frequency']
        
        # If pattern used >5 times, suggest abstraction
        for pattern, count in pattern_frequency.items():
            if count > 5:
                decisions['new_abstractions'].append({
                    'pattern': pattern,
                    'usage_count': count,
                    'suggested_file': f"asha/patterns/{pattern}.md"
                })
        
        # Cross-project suggestions (based on pattern types)
        if 'async-function' in pattern_frequency and pattern_frequency['async-function'] > 3:
            decisions['cross_project_suggestions'].append({
                'pattern': 'async-error-handling',
                'projects': ['mplay', 'rpg-dice'],
                'reason': 'Common async patterns detected'
            })
        
        return decisions
    
    def execute(self) -> Dict:
        """Execute the full ReAct loop"""
        print("🧠 Local ReAct Save Analysis")
        print("=" * 40)
        
        # Think
        print("\n[THINK] Analyzing session content...")
        thoughts = self.think()
        print(f"Found: {len(thoughts['error_patterns'])} error patterns")
        print(f"Found: {len(thoughts['code_patterns'])} code patterns")
        
        # Act - Search
        print("\n[ACT] Searching for similar patterns...")
        similar = self.act_search_similar(thoughts)
        print(f"Found: {len(similar['exact_matches'])} exact matches")
        
        # Act - Redundancy
        print("\n[ACT] Checking for redundancy...")
        session_content = Path(self.session_file).read_text()
        redundancy = self.act_detect_redundancy(session_content, similar)
        print(f"Found: {len(redundancy['redundant_sections'])} redundant sections")
        
        # Act - Insights
        print("\n[ACT] Extracting novel insights...")
        insights = self.act_extract_insights(thoughts, redundancy)
        print(f"Found: {len(insights)} new insights")
        
        # Observe & Decide
        print("\n[OBSERVE & DECIDE] Making recommendations...")
        actions_results = {
            'similar': similar,
            'redundancy': redundancy,
            'insights': insights
        }
        decisions = self.observe_and_decide(thoughts, actions_results)
        
        # Generate report
        report = {
            'analysis': {
                'patterns_found': thoughts,
                'similar_patterns': len(similar['exact_matches']),
                'redundancy_percentage': self._calculate_redundancy_percentage(redundancy),
                'novel_insights': insights
            },
            'recommendations': decisions,
            'summary': self._generate_summary(decisions)
        }
        
        return report
    
    def _extract_context(self, content: str, pattern: str, context_size: int = 100) -> str:
        """Extract context around a pattern"""
        idx = content.find(pattern)
        if idx == -1:
            return ""
        start = max(0, idx - context_size)
        end = min(len(content), idx + len(pattern) + context_size)
        return content[start:end]
    
    def _calculate_similarity(self, pattern: str, content: str) -> float:
        """Calculate similarity score"""
        # Simple occurrence-based similarity
        occurrences = content.count(pattern)
        return min(1.0, occurrences / 10.0)
    
    def _find_repetitions(self, content: str) -> List[str]:
        """Find repeated actions in session"""
        lines = content.split('\n')
        repetitions = []
        
        # Look for repeated commands or actions
        line_counter = Counter(lines)
        for line, count in line_counter.items():
            if count > 2 and len(line) > 10:
                repetitions.append(f"{line} (repeated {count} times)")
                
        return repetitions
    
    def _analyze_file_changes(self, content: str) -> Dict:
        """Analyze which files were changed"""
        file_pattern = r'(?:modified|created|deleted):\s+(\S+)'
        files = re.findall(file_pattern, content)
        
        return {
            'total_files': len(set(files)),
            'file_types': Counter([f.split('.')[-1] for f in files if '.' in f]),
            'most_modified': Counter(files).most_common(5)
        }
    
    def _extract_commands(self, content: str) -> List[str]:
        """Extract commands run during session"""
        # Look for bash command patterns
        cmd_pattern = r'\$\s+([^\n]+)'
        commands = re.findall(cmd_pattern, content)
        return commands
    
    def _calculate_redundancy_percentage(self, redundancy: Dict) -> float:
        """Calculate overall redundancy percentage"""
        if not redundancy['redundant_sections']:
            return 0.0
            
        total_redundant_size = sum(r['size'] for r in redundancy['redundant_sections'])
        # Rough estimate - would need full session size for accurate percentage
        return min(100.0, (total_redundant_size / 1000) * 100)
    
    def _generate_summary(self, decisions: Dict) -> str:
        """Generate human-readable summary"""
        summary_parts = []
        
        if decisions['memory_updates']:
            summary_parts.append(f"📝 {len(decisions['memory_updates'])} memory updates recommended")
            
        if decisions['new_abstractions']:
            summary_parts.append(f"🎯 {len(decisions['new_abstractions'])} patterns ready for abstraction")
            
        if decisions['cross_project_suggestions']:
            summary_parts.append(f"🔗 {len(decisions['cross_project_suggestions'])} cross-project opportunities")
            
        return " | ".join(summary_parts) if summary_parts else "✅ Session captured, no special actions needed"


if __name__ == "__main__":
    # Example usage
    agent = LocalReActSave("Memory/sessions/current-session.md")
    report = agent.execute()
    
    print("\n" + "="*40)
    print("📊 Final Report")
    print("="*40)
    print(json.dumps(report, indent=2))