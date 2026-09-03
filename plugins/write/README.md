# Write Plugin

**Version**: 1.10.0

Fiction-state initialization, drafting workflows, editorial review, style
measurement, continuity verification, and book export.

## Choose the right surface

| Need | Use |
|---|---|
| Start structured state for a novel | `/write:init-novel` |
| Review a finished scene or section | `/write:review-section` |
| Run an authority-first multi-act revision | `write-revision-pass` skill |
| Measure an exemplar or draft | `style-analyzer` skill |
| Check grammar and mechanical style | `languagetool` skill |
| Export a manuscript | `book-export` skill |
| Request one editorial perspective | Name the relevant agent directly |
| Run a full drafting or revision chain | Use the matching recipe as the orchestration plan |

The plugin does not treat every writing task as “generate prose.” Structure,
drafting, continuity, developmental editing, line editing, and state updates
remain separate charges so one pass does not quietly overwrite another.

## Invocation by harness

| Harness | Invocation |
|---|---|
| Claude Code | Use `/write:init-novel` and `/write:review-section`; agents and skills may also be named directly |
| OpenAI Codex | Ask for the operation or name the rendered `write-init-novel` / `write-review-section` skill |
| GitHub Copilot CLI | Ask for the operation or name the rendered `write-init-novel` / `write-review-section` skill |

Examples that work as task requests on every harness:

```text
Use write-init-novel in this repository.
Use write-review-section on Work/novel/story/chapter-03.md with --full.
Run prose-analysis in voice and continuity modes, but do not rewrite anything.
Use book-export to create a beta-reader PDF.
```

## First-time novel setup

```text
/write:init-novel
```

This creates:

```text
Work/novel/
├── bible/                   voice, rules, characters, world
├── state/                   chapter snapshots and current-state link
├── timeline/                master timeline and structured events
└── story/                   synopsis, outline, and manuscript material
```

After initialization:

1. Put the authoritative style rules in `Work/novel/bible/voice.md`.
2. Put immutable story constraints in `Work/novel/bible/rules.md`.
3. Record characters and world facts in the corresponding bible directories.
4. Keep the synopsis and chapter structure under `Work/novel/story/`.
5. Snapshot accepted chapter state under `Work/novel/state/`.

The initializer supplies a default layout. Existing projects may retain their
own manuscript layout; `/write:review-section` discovers its configuration from
the target section's ancestor chain rather than assuming `Work/novel/`.

## Commands

### `/write:init-novel [project-path]`

Initialize the standard bible/state/timeline structure in the current directory
or an explicit project path.

```text
/write:init-novel
/write:init-novel /path/to/novel
```

Use this once per novel project. It does not draft a synopsis, voice guide, or
chapter merely because the files now exist.

### `/write:review-section SECTION [--full]`

Run the project-configured editorial suite on a completed section.

```text
/write:review-section Work/novel/story/chapter-03.md
/write:review-section Work/novel/story/chapter-03.md --voice
/write:review-section Work/novel/story/chapter-03.md --continuity --docs
/write:review-section Work/novel/story/chapter-03.md --full
```

Configuration is resolved by walking upward from the supplied section and
selecting the nearest `Work/review-config.md` or `work/review-config.md`.
Without one, the command defaults to `prose-analysis` in voice and continuity
modes and writes reports under `Work/reports/`.

A minimal configuration:

```yaml
---
project: "Example Novel"
agents:
  - agent: prose-analysis
    modes: [voice, continuity, coherence, docs]
    voice_guide: "Work/novel/bible/voice.md"
    documentation:
      - "Work/novel/bible/rules.md"
      - "Work/novel/timeline/master.md"
full_review_adds:
  - agent: developmental-editor
report_path: "Work/reports/example-novel/"
---
```

`--full` adds configured specialist reviews. Review output is a report, not
permission to rewrite the manuscript. Claims that would force a continuity
rewrite should pass through the `verify-consistency-report.yaml` recipe first.

## Agents

### Creation and structure

| Agent | Role | Use directly when |
|---|---|---|
| `outline-architect` | Beat sheets, chapter structure, and transformation arcs | You need structure before prose |
| `prose-writer` | Draft prose from an approved outline and voice source | The structure and authorship boundary are already explicit |
| `voice-analyst` | Convert exemplar measurements into a unified voice guide | Building or revising the voice bible |

### Review

| Agent | Role | Use directly when |
|---|---|---|
| `continuity-reviewer` | Timeline, space, knowledge, objects, character traits, and world rules | Checking continuity or gating a new scene |
| `developmental-editor` | Structure, pacing, arcs, and theme | The manuscript needs forest-level diagnosis |
| `line-editor` | Sentence craft, rhythm, diction, and mechanics | Structure is settled and the prose needs a line pass |
| `prose-analysis` | Configurable voice, character, continuity, coherence, and document verification | You need selected review modes in one report |
| `intimacy-arbiter` | Review-only boundary and heat-level arbitration | An intimate scene may have drifted from project intent |
| `claim-verifier` | Read-only verification of report claims against manuscript text | A reported contradiction would trigger rewriting |

### State maintenance

| Agent | Role | Use directly when |
|---|---|---|
| `novel-state-updater` | Extract accepted section state into situation, character, knowledge, and inventory records | A section has passed validation and its consequences should become state |

Agents do not infer authority. Voice guides, project rules, manuscript text,
and canonical state files remain the evidence hierarchy supplied by the
project.

## Skills

| Skill | Purpose | Typical request |
|---|---|---|
| `book-export` | Produce PDF or ePub using manuscript, beta-reader, or publication profiles | `Export this manuscript as a beta-reader PDF.` |
| `languagetool` | Query the local LanguageTool service for grammar and style findings | `Run LanguageTool on this chapter.` |
| `novel-state` | Create and explain the standard story-state layout | `Set up novel state in this repository.` |
| `style-analyzer` | Measure sentence, dialogue, vocabulary, repetition, and configured prose patterns | `Analyze these exemplars and generate voice metrics.` |
| `write-revision-pass` | Revise act-by-act with read-only reviews, decision capture, style audit, and an empty old-value proof | `Run a revision pass replacing the old rule throughout the manuscript.` |

Directory and multi-file `/write:review-section` targets fan out one review
agent per section by default, then synthesize the returned section reports.
Harnesses without a subagent surface execute the same charges sequentially.

Skills activate from matching task language. Name one explicitly when several
could plausibly apply.

## Recipes

Recipes under `recipes/` are orchestration plans rather than independent slash
commands.

| Recipe | Sequence |
|---|---|
| `chapter-creation.yaml` | continuity gate → outline approval → draft → developmental and line review |
| `character-development.yaml` | concept → backstory → continuity check → voice test |
| `manuscript-revision.yaml` | continuity audit → developmental diagnosis → approved restructuring and revision |
| `verify-consistency-report.yaml` | partition report claims → parallel read-only verification → confirmed/denied matrix |

The author remains the approval boundary at every manuscript-changing
checkpoint. Reports can recommend; they do not silently become canon.

## Quality boundary

The plugin reports descriptive style measurements and editorial findings. It
does not infer authorship from prose, and length-derived scores are not quality
gates. A clean grammar report is not a developmental verdict; a continuity
report is not a line edit. Different instruments, different cuts.

## Installation

```bash
./install.sh --only write --target claude
./install.sh --only write --target codex
./install.sh --only write --target copilot
```

Re-run installation after changing command or agent sources because Codex and
Copilot receive generated artifacts for those forms.

## License

MIT
