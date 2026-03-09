#!/usr/bin/env python3
"""
Initialize novel state directory structure.

Creates the bible/state/timeline/story directory hierarchy
with template files for novel writing workflows.

Usage:
    python init_novel_state.py [project_root]

Arguments:
    project_root    Project directory (default: current directory)

Examples:
    python init_novel_state.py                    # Initialize in current directory
    python init_novel_state.py /path/to/project   # Initialize in specific directory
    python init_novel_state.py ~/Code/my-novel    # Initialize in home-relative path
"""

import json
import sys
from pathlib import Path


VOICE_TEMPLATE = '''# Voice Guide

## Author DNA

- Primary influences: (list authors/works that define the voice)
- Sentence rhythm: (describe cadence - staccato, flowing, varied)
- POV approach: (close third, first person, omniscient)

## Prohibited Patterns

- [ ] Metronomic sentence length (all sentences ~same length)
- [ ] Hedging accumulation ("seemed to", "appeared to", "felt like")
- [ ] Generic sensory language ("felt a sense of", "a feeling of")
- [ ] Adverb stacking ("quickly quietly carefully")
- [ ] (add project-specific prohibited patterns)

## Required Patterns

- [ ] Varied sentence length (range 5-30 words)
- [ ] Character-specific voice markers
- [ ] Sensory grounding in setting
- [ ] (add project-specific required patterns)

## Grep Patterns

Automated checks for prohibited patterns:

```bash
# Hedging accumulation
grep -E "(seemed to|appeared to|felt like|as if)" *.md

# Adverb stacking
grep -E "\\w+ly\\s+\\w+ly" *.md

# Generic sensory
grep -E "(felt a sense|a feeling of|somehow)" *.md
```

## Example Passages

(Add 3-5 clean exemplar passages demonstrating target voice)

### Example 1: Action scene

(paste example)

### Example 2: Dialogue

(paste example)

### Example 3: Interiority

(paste example)
'''

RULES_TEMPLATE = '''# Story Rules

Immutable constraints. Violations require human approval with documented rationale.

## World Constraints

- (list physical/magical laws that cannot be broken)
- (list technology/capability limits)
- (list social/political constants)

## Character Constraints

- (list what specific characters cannot do)
- (list knowledge boundaries for characters)
- (list relationship constraints)

## Narrative Constraints

- No deus ex machina
- Chekhov's gun: every introduced element must pay off
- (add project-specific narrative rules)
'''

SYNOPSIS_TEMPLATE = '''# Synopsis

## Logline

(One sentence summary)

## Summary

(2-3 paragraph overview of the complete story)

## Theme

(Core thematic concern)

## Tone

(Emotional register and atmosphere)
'''

OUTLINE_TEMPLATE = '''# Outline

## Act 1: Setup

### Chapter 1: (title)
- Scene 1:
- Scene 2:

### Chapter 2: (title)
- Scene 1:
- Scene 2:

## Act 2: Confrontation

### Chapter 3: (title)
...

## Act 3: Resolution

### Chapter N: (title)
...
'''

TIMELINE_TEMPLATE = '''# Timeline

## Story Timeline

| Date | Event | Chapter | Characters |
|------|-------|---------|------------|
| (date) | (event) | Ch1 | (who) |

## Background Timeline

Events before story begins:

| Date | Event | Relevance |
|------|-------|-----------|
| (date) | (event) | (why it matters) |
'''


def init_novel_state(project_root: str = '.') -> None:
    """Create novel state directory structure with template files."""
    root = Path(project_root).expanduser().resolve()
    novel_root = root / 'Work' / 'novel'

    print(f"Initializing novel state in: {novel_root}")
    print()

    # Create directories
    directories = [
        novel_root / 'bible' / 'characters',
        novel_root / 'bible' / 'world',
        novel_root / 'state',
        novel_root / 'timeline',
        novel_root / 'story',
    ]

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        print(f"  Created: {directory.relative_to(root)}")

    print()

    # Create template files
    templates = {
        novel_root / 'bible' / 'voice.md': VOICE_TEMPLATE,
        novel_root / 'bible' / 'rules.md': RULES_TEMPLATE,
        novel_root / 'story' / 'synopsis.md': SYNOPSIS_TEMPLATE,
        novel_root / 'story' / 'outline.md': OUTLINE_TEMPLATE,
        novel_root / 'timeline' / 'master.md': TIMELINE_TEMPLATE,
    }

    for path, content in templates.items():
        if not path.exists():
            path.write_text(content.strip() + '\n')
            print(f"  Created: {path.relative_to(root)}")
        else:
            print(f"  Exists:  {path.relative_to(root)} (skipped)")

    # Create events.json
    events_path = novel_root / 'timeline' / 'events.json'
    if not events_path.exists():
        events_data = {
            "events": [],
            "timeline_start": None,
            "timeline_end": None
        }
        events_path.write_text(json.dumps(events_data, indent=2) + '\n')
        print(f"  Created: {events_path.relative_to(root)}")
    else:
        print(f"  Exists:  {events_path.relative_to(root)} (skipped)")

    # Create .gitkeep files for empty directories
    gitkeep_dirs = [
        novel_root / 'bible' / 'characters',
        novel_root / 'bible' / 'world',
        novel_root / 'state',
    ]

    for directory in gitkeep_dirs:
        gitkeep = directory / '.gitkeep'
        if not gitkeep.exists() and not any(directory.iterdir()):
            gitkeep.touch()
            print(f"  Created: {gitkeep.relative_to(root)}")

    print()
    print("Novel state initialized successfully.")
    print()
    print("Next steps:")
    print("  1. Edit Work/novel/bible/voice.md with your style guide")
    print("  2. Edit Work/novel/bible/rules.md with story constraints")
    print("  3. Edit Work/novel/story/synopsis.md with your story summary")
    print("  4. Edit Work/novel/story/outline.md with chapter structure")
    print("  5. Create character files in Work/novel/bible/characters/")
    print()
    print("To start a chapter:")
    print("  mkdir -p Work/novel/state/ch01")
    print("  ln -sfn ch01 Work/novel/state/current")


def main() -> None:
    """Entry point."""
    if len(sys.argv) > 1:
        if sys.argv[1] in ('-h', '--help'):
            print(__doc__)
            sys.exit(0)
        project_root = sys.argv[1]
    else:
        project_root = '.'

    try:
        init_novel_state(project_root)
    except PermissionError as e:
        print(f"Error: Permission denied - {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
