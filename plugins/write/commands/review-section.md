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

The skill looks for review configuration in this order:

1. **Project config**: `Vault/Books/[Project]/work/review-config.md`
2. **Fallback**: the default configuration documented below

### Config Format

```yaml
---
project: "Example Novel"
agents:
  - agent: prose-analysis
    modes: [voice, continuity, coherence, docs]
    voice_guide: "Vault/Books/Example_Novel/work/prose_voice.md"
    documentation: "Vault/Books/Example_Novel/work/Example_Novel_Complete_Documentation.md"
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
/review-section Vault/Books/Example_Novel/Example_Novel.md:Ch03
```

Voice/craft review only:

```
/review-section Vault/Books/Example_Novel/Example_Novel.md:Ch03 --voice
```

Facts-only review (continuity + docs):

```
/review-section Vault/Books/Example_Novel/Example_Novel.md:Ch03 --continuity --docs
```

Full review (adds project-configured specialist reviews):

```
/review-section Vault/Books/Example_Novel/Example_Novel.md:Ch03 --full
```

The skill:

1. Extracts project path from section path
2. Looks for `work/review-config.md` in that project
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
│                    /review-section                       │
│                           │                              │
│                    Read config from                      │
│              Vault/Books/[Project]/work/                 │
│                    review-config.md                      │
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

1. Create `Vault/Books/[YourProject]/work/review-config.md`
2. Define which agents to run and their configurations
3. Specify documentation paths for doc-verification
4. Set report output path

See `Vault/Books/Example_Novel/work/review-config.md` for a complete example.

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
