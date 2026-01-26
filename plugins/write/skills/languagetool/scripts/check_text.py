#!/usr/bin/env python3
"""
Check text using local LanguageTool server.
Usage: python check_text.py "Text to check"
"""

import sys
import requests
import json

def check_text(text, language='en-US', server='http://localhost:8081'):
    """Check text for grammar and style issues."""
    url = f'{server}/v2/check'
    data = {
        'text': text,
        'language': language
    }

    try:
        response = requests.post(url, data=data, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to LanguageTool server: {e}", file=sys.stderr)
        sys.exit(1)

def format_matches(result):
    """Format matches for display."""
    matches = result.get('matches', [])

    if not matches:
        print("✓ No issues found!")
        return

    print(f"Found {len(matches)} issue(s):\n")

    for i, match in enumerate(matches, 1):
        print(f"{i}. {match['message']}")
        print(f"   Category: {match['rule']['category']['name']}")
        print(f"   Context: ...{match['context']['text']}...")

        if match['replacements']:
            suggestions = ', '.join([r['value'] for r in match['replacements'][:3]])
            print(f"   Suggestions: {suggestions}")

        print()

def main():
    if len(sys.argv) < 2:
        print("Usage: python check_text.py 'Text to check'")
        print("       python check_text.py 'Text to check' en-GB")
        sys.exit(1)

    text = sys.argv[1]
    language = sys.argv[2] if len(sys.argv) > 2 else 'en-US'

    result = check_text(text, language)
    format_matches(result)

if __name__ == '__main__':
    main()
