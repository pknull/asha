#!/usr/bin/env python3
"""
Check a file using local LanguageTool server.
Usage: python check_file.py path/to/file.txt
"""

import sys
import requests
from pathlib import Path

# Preserve access for callers that already loaded the parser; plain-text mode
# never imports it or changes sys.path. Clean mode verifies its location first.
inline_review = sys.modules.get('inline_review')


def _load_inline_review():
    """Find the shared parser in source/symlinked or copied packaged skills."""
    import importlib.util

    global inline_review
    skills = Path(__file__).resolve().parents[2]
    searched = [skills / name / 'scripts' / 'inline_review.py'
                for name in ('inline-review', 'write-inline-review')]
    for path in searched:
        if path.is_file():
            cached = sys.modules.get('inline_review')
            if cached is not None and Path(cached.__file__).resolve() == path.resolve():
                inline_review = cached
            else:
                spec = importlib.util.spec_from_file_location('inline_review', path)
                module = importlib.util.module_from_spec(spec)
                sys.modules['inline_review'] = module
                try:
                    spec.loader.exec_module(module)
                except BaseException:
                    if cached is None:
                        sys.modules.pop('inline_review', None)
                    else:
                        sys.modules['inline_review'] = cached
                    raise
                inline_review = module
            return inline_review
    print('Error: shared inline_review parser not found; searched: '
          + ', '.join(map(str, searched)), file=sys.stderr)
    sys.exit(2)

def check_file(filepath, language='en-US', server='http://localhost:8081', manuscript=False):
    """Check file for grammar and style issues."""
    path = Path(filepath)

    if not path.exists():
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    projection = None
    if manuscript:
        parser = _load_inline_review()
        projection = parser.project(parser.read_source(path), str(path))
        text = projection.text
    else:
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
        result = response.json()
        if projection is not None:
            result['source_line_map'] = projection.line_map
            for match in result.get('matches', []):
                # LanguageTool offsets are UTF-16 code units (Java), not Python
                # code points; count lines after decoding the reported prefix.
                prefix = text.encode('utf-16-le')[:match['offset'] * 2]
                line = prefix.decode('utf-16-le', errors='ignore').count('\n') + 1
                if projection.line_map:
                    match['projected_line'] = line
                    match['source_line'] = projection.source_line(min(line, len(projection.line_map)))
        return result
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
            if 'source_line' in match:
                print(f"   Source: {filepath}:{match['source_line']}")

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
        print("Usage: python check_file.py path/to/file.txt [language] [--manuscript|--clean]")
        print("Example: python check_file.py document.txt en-US")
        sys.exit(1)

    manuscript = '--manuscript' in sys.argv or '--clean' in sys.argv
    arguments = [arg for arg in sys.argv[1:] if arg not in ('--manuscript', '--clean')]
    if not arguments:
        print('Error: file required', file=sys.stderr)
        sys.exit(2)
    filepath = arguments[0]
    language = arguments[1] if len(arguments) > 1 else 'en-US'

    review_errors = (_load_inline_review().ReviewError,) if manuscript else ()
    try:
        result = check_file(filepath, language, manuscript=manuscript)
    except review_errors as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except (OSError, UnicodeError) as exc:
        print(f'{filepath}:1: {exc}', file=sys.stderr)
        sys.exit(2)
    generate_report(filepath, result)

if __name__ == '__main__':
    main()
