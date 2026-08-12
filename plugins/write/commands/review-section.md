---
name: write-review-section
description: "Run periodic review suite on completed section (reads project config)"
argument-hint: "<section-path> [--full]"
allowed-tools: ["Task", "Read", "Grep", "Glob"]
---

# Review Section

Orchestrates review agents for quality assurance after completing a section. Reads project-specific configuration to determine which agents to run.

## Purpose

Catch issues early by running coordinated reviews after completing each section/chapter rather than waiting for full manuscript review.

## Configuration Discovery

Resolve configuration from the supplied section path rather than assuming a
repository layout:

1. Resolve the section argument to an existing manuscript file or directory.
2. Start at that path's directory and walk upward toward the repository root.
3. At each directory, check `Work/review-config.md`, then
   `work/review-config.md`. The nearest match wins. Treat path case as
   significant.
4. Never assume that books live under `Vault/Books`, `Lore/Books`, or any
   other fixed parent directory.
5. If no project config exists before reaching the repository root, use the
   fallback configuration documented below.

Keep discovery scoped to the target's ancestor chain. Do not search the whole
home directory or infer a project by matching its display name elsewhere.

### Config Format

```yaml
---
project: "Example Novel"
agents:
  - agent: prose-analysis
    modes: [voice, continuity, coherence, docs]
    voice_guide: "Work/novel/bible/voice.md"
    documentation:
      - "Work/novel/bible/story_bible.md"
      - "Work/novel/bible/rules.md"
      - "Work/novel/timeline/master.md"
full_review_adds:
  - agent: developmental-editor
report_path: "Work/reports/example-novel/"
---
```

Note: `prose-analysis` is now a single consolidated agent with mode flags:

- `--voice` — Voice enforcement, craft quality, show-don't-tell
- `--continuity` — Spatial tracking, timeline, pronouns
- `--coherence` — Escape hatches, worldbuilding verification
- `--docs` — Documentation verification (anti-hallucination)

### Fallback Default (no project config)

```yaml
agents:
  - agent: prose-analysis
    modes: [voice, continuity]
report_path: "Work/reports/"
```

## Usage

Review a section (all configured modes):

```
/write:review-section path/to/Example_Novel/03_Chapter/01_Scene.md
```

Voice/craft review only:

```
/write:review-section path/to/Example_Novel/03_Chapter/01_Scene.md --voice
```

Facts-only review (continuity + docs):

```
/write:review-section path/to/Example_Novel/03_Chapter/01_Scene.md --continuity --docs
```

Full review (adds project-configured specialist reviews):

```
/write:review-section path/to/Example_Novel/03_Chapter/01_Scene.md --full
```

The skill:

1. Resolves the target from the section path
2. Walks its ancestor chain for the nearest `Work/review-config.md` or
   `work/review-config.md`
3. Falls back to default if not found
4. Runs configured agents in parallel where possible
5. Synthesizes combined report

## Section Identification

Some projects identify sections by custom symbols rather than plain numbers (e.g. alchemical or thematic glyphs).

Otherwise, use line ranges or chapter names:

- `Chapter3:100-250` — Lines 100-250 of Chapter 3
- `Ch05` — Full chapter 5

## Output

Generates combined report at configured `report_path` containing:

- Executive summary with agent verdicts
- Detailed findings from each agent
- Prioritized action items
- Cross-agent synthesis (issues flagged by multiple agents)

## Agent Coordination

```
┌─────────────────────────────────────────────────────────┐
│                    /write:review-section                       │
│                           │                              │
│                    Read config from                      │
│              nearest ancestor project root              │
│              Work/review-config.md                       │
│                           │                              │
│                           ▼                              │
│                   prose-analysis                         │
│            ┌──────────────┼──────────────┐               │
│            ▼              ▼              ▼               │
│        --voice      --continuity    --coherence          │
│        --docs                                            │
│            │              │              │               │
│            └──────────────┼──────────────┘               │
│                           ▼                              │
│              Unified Report + Actions                    │
└─────────────────────────────────────────────────────────┘
```

## Creating a Project Config

To set up review for a new project:

1. Locate the manuscript's project root from the section path
2. Create `Work/review-config.md` at that root (`work/` is also supported)
3. Define which agents to run and their configurations
4. Specify documentation paths for doc-verification
5. Set report output path

`documentation` accepts either one path or a list of file/directory paths.
Resolve relative `voice_guide` and `documentation` paths from the discovered
project root. Resolve `report_path` from the current repository root unless it
is absolute. Read all configured documentation entries; do not silently use
only the first.

## Optional Verification Pass

A consistency report is model output — untrusted until verified. When the
report contains **rewrite-triggering** claims (canon contradictions,
timeline/knowledge/object violations), run the
`recipes/verify-consistency-report.yaml` recipe before acting on it: it fans
read-only `claim-verifier` agents across the claims and returns a
confirmed/denied matrix; only confirmed claims proceed to revision.
Style-level reports have nothing to verify — skip the pass.

## Notes

- `--full` adds any installed agents listed in `full_review_adds`; unresolved names are reported and skipped
- Reports accumulate in configured path for trend analysis
- Run after every 2-3 sections during active drafting
