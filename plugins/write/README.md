# Write Plugin

**Version**: 1.6.0

Fiction drafting, editorial review, manuscript state, style measurement, and export workflows.

## Agents

| Agent | Role |
|---|---|
| `claim-verifier` | Structurally read-only verification of consistency-report claims against the manuscript (confirmed/denied matrix) |
| `continuity-reviewer` | Manuscript continuity and pre-writing state checks |
| `developmental-editor` | Structure, pacing, arc, and theme review |
| `intimacy-arbiter` | Review-only boundary and heat-level arbitration |
| `line-editor` | Sentence-level craft and mechanical polish |
| `novel-state-updater` | Extract accepted section state into continuity records |
| `outline-architect` | Chapter structures, beats, and outlines |
| `prose-analysis` | Configurable voice, character, continuity, coherence, and documentation review |
| `prose-writer` | Draft generation from approved beats and voice sources |
| `voice-analyst` | Interpret exemplar analyses and build voice guidance |

## Commands

| Command | Purpose |
|---|---|
| `/write:init-novel` | Initialize the novel-state directory structure |
| `/write:review-section` | Run project-configured editorial reviews upon a section |

## Skills

| Skill | Purpose |
|---|---|
| `book-export` | Export manuscripts to PDF and ePub |
| `languagetool` | Grammar and style checking through a local LanguageTool server |
| `novel-state` | Define and initialize bible, state, timeline, and story storage |
| `style-analyzer` | Measure sentence, dialogue, vocabulary, repetition, and configured prose patterns |

## Recipes

| Recipe | Purpose |
|---|---|
| `chapter-creation.yaml` | Outline, draft, developmental review, voice pass, and line edit |
| `character-development.yaml` | Character development and voice testing |
| `manuscript-revision.yaml` | Manuscript revision workflow |
| `verify-consistency-report.yaml` | Verify a consistency report's rewrite-triggering claims via parallel read-only claim-verifiers before revising |

## Quality boundary

The plugin reports descriptive style measurements and editorial findings. It does not infer authorship from prose, and length-derived scores are not quality gates.

## Installation

```bash
./install.sh --only write
```

## License

MIT
