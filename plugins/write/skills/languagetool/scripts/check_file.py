#!/usr/bin/env python3
"""
Check a file using local LanguageTool server.
Usage: python check_file.py path/to/file.txt
"""

import sys
import requests
from pathlib import Path

def check_file(filepath, language='en-US', server='http://localhost:8081'):
    """Check file for grammar and style issues."""
    path = Path(filepath)

    if not path.exists():
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()

    url = f'{server}/v2/check'
    data = {
        'text': text,
        'language': language
    }

    try:
        response = requests.post(url, data=data, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to LanguageTool server: {e}", file=sys.stderr)
        sys.exit(1)

def generate_report(filepath, result):
    """Generate detailed report."""
    matches = result.get('matches', [])

    print(f"Grammar Check Report")
    print("=" * 70)
    print(f"File: {filepath}")
    print(f"Issues found: {len(matches)}\n")

    if not matches:
        print("✓ No issues found!")
        return

    # Group by category
    by_category = {}
    for match in matches:
        cat = match['rule']['category']['name']
        by_category.setdefault(cat, []).append(match)

    for category, cat_matches in sorted(by_category.items()):
        print(f"\n{category} ({len(cat_matches)} issues)")
        print("-" * 70)

        for i, match in enumerate(cat_matches, 1):
            print(f"\n{i}. {match['message']}")
            print(f"   Rule: {match['rule']['id']}")

            # Show context
            context = match['context']['text']
            ctx_offset = match['context']['offset']
            ctx_length = match['context']['length']

            # Highlight the issue in context
            before = context[:ctx_offset]
            issue = context[ctx_offset:ctx_offset + ctx_length]
            after = context[ctx_offset + ctx_length:]
            print(f"   Context: {before}>>>{issue}<<<{after}")

            # Show suggestions
            if match['replacements']:
                suggestions = ', '.join([r['value'] for r in match['replacements'][:5]])
                print(f"   Suggestions: {suggestions}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python check_file.py path/to/file.txt [language]")
        print("Example: python check_file.py document.txt en-US")
        sys.exit(1)

    filepath = sys.argv[1]
    language = sys.argv[2] if len(sys.argv) > 2 else 'en-US'

    result = check_file(filepath, language)
    generate_report(filepath, result)

if __name__ == '__main__':
    main()
