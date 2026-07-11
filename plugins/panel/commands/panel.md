---
name: panel-system-panel
description: "Convene multi-perspective expert panel for analysis and decision-making"
argument-hint: "Topic or question to analyze"
allowed-tools: ["Task", "Read", "Write", "Edit", "Grep", "Glob"]
---

# Panel - Expert Multi-Perspective Analysis

Convene a panel that decomposes complex problems, clarifies ambiguities, then deliberates with full context. The default workflow runs the complete pipeline:

```
Decompose → Clarify → Deliberate
(structure)  (gaps)    (decision)
```

## Usage

```bash
# Full pipeline (default) - decompose, clarify unclear steps, then deliberate
/panel "Build a microservices auth system"
/panel "Should we implement GraphQL or REST for the new API"

# With options
/panel --format=github "Topic here"     # Output as GitHub PR comment
/panel --format=json "Topic here"       # Output as structured JSON
/panel --context=docs/RFC.md "Topic"    # Inject reference material
```

### Quick Mode (Skip Decomposition)

```bash
/panel --quick "Should we use tabs or spaces"
/panel --quick "Evaluate Chapter 9's effectiveness"
```

For simple, well-defined topics that don't need decomposition. Jumps straight to deliberation (Phases -1 through 8).

### Think Mode (Decomposition Only)

```bash
/panel --think "Break down this architecture"
/panel --think "What are all the pieces of building a CLI tool"
```

Just runs The Thinker for problem decomposition. No interview, no deliberation. Output: `Work/thinking/<id>/summary.md`

### Interview Mode (Requirements Only)

```bash
/panel --interview "Build a task management CLI"
/panel --interview "Create a REST API for user management"
```

Just runs Socratic Q&A workflow for requirements crystallization. No decomposition, no deliberation. Output: `Work/panels/<id>/seed.yaml`

### Panel Management

```bash
/panel --list                    # List all panels (active and completed)
/panel --list --status=active    # List only active/interrupted panels
/panel --list --status=completed # List only completed panels
/panel --list --status=abandoned # List abandoned panels
/panel --show <id>               # Display panel summary by ID
/panel --resume <id>             # Resume interrupted panel from last phase
/panel --abandon <id>            # Mark panel as abandoned
```

### Flags Reference

| Flag | Purpose |
|------|---------|
| `--quick` | Skip decomposition, go straight to deliberation |
| `--think` | Decomposition only (no interview, no deliberation) |
| `--interview` | Requirements Q&A only (no decomposition, no deliberation) |
| `--format=<type>` | Output format (`markdown` default, `github`, `json`) |
| `--context=<file>` | Pre-load reference material into panel context |
| `--list` | List panels in index (combine with `--status` to filter) |
| `--status=<type>` | Filter for `--list` (`active`, `completed`, `abandoned`) |
| `--show <id>` | Display summary of specific panel |
| `--resume <id>` | Continue panel from last completed phase |
| `--abandon <id>` | Mark panel as abandoned (cannot resume) |

**The panel handles everything automatically:**

- The Analyst analyzes topic and recruits 2-5 specialist agents from available library
- Assigns specialists with evocative session-specific names
- Infers goals from topic context
- Applies consensus decision rule (unanimous for security topics)
- The Challenger argues against proposals and demands proof of necessity
- The Moderator moderates and compiles the decision report

## Core Roles (Always Present)

**The Thinker** (Sequential Problem Decomposition)

- Breaks complex problems into numbered, dependency-aware steps
- Assesses clarity of each step (HIGH/MEDIUM/LOW)
- Supports revision and branching for alternative approaches
- Maintains audit trail in `Work/thinking/<id>/`
- **Question**: "What are the PIECES?"

**The Moderator** (Moderator/Facilitator)

- Manages full pipeline and phase protocol execution
- Ensures procedural integrity and timebox enforcement
- Synthesizes final decision report
- **Question**: "What is the PROCESS?"

**The Analyst** (Workforce Intelligence)

- Analyzes topic to determine needed expertise
- Scores available agent library (0-10) for capability match
- Recruits 2-5 specialist agents with session-specific names
- Deploys `fabricator` if capability gaps detected
- **Question**: "Who has CAPABILITY?"

**The Challenger** (Opposition & Quality Gate)

- **Default stance: OPPOSE** - argues against proposals and defends status quo
- Demands evidence before changing working systems: "Show me user complaints, failure data, metrics"
- Forces proponents to prove necessity: "The current system works. Prove it doesn't."
- Prevents premature action and consensus formed without data
- **Question**: "Why should we do this at all?"

## Dynamic Panelists (Recruited Per Topic)

The Analyst assigns agents from `.claude/agents/*.md` with **evocative session-specific names** based on topic context.

**Examples by Topic Type**:

**Creative Writing Panel** (Callum Chapter 9 evaluation):

- `prose-analysis` → **"The Editor"** (craft assessment)
- `intimacy-arbiter` → **"The Architect of Dread"** (genre mechanics)
- `narrative-architect` → **"The Structuralist"** (story coherence)
- `character-developer` → **"The Psychologist"** (character authenticity)

**Technical Architecture Panel** (GraphQL vs REST):

- `research-assistant` → **"The Evidence Gatherer"** (source validation)
- `reviewer` → **"The Systems Designer"** (architecture patterns)
- `ml-engineer` → **"The Model Capability Analyst"** (performance analysis)

**Culinary Innovation Panel** (How do we pimp fish):

- `research-assistant` → **"The Culinary Historian"** (technique research)
- `trend-analyst` → **"The Flavor Prophet"** (emerging patterns)
- `creative-director` → **"The Presentation Architect"** (plating design)

**Session-Specific Naming Convention**:

- **Agent role** describes what it does (e.g., `prose-analysis`)
- **Session name** describes who it becomes for this panel (e.g., "The Editor")
- Names should be evocative, contextual, and domain-appropriate

## Role Execution Model (Harness-Aware)

Panel roles ship as real agents (`plugins/panel/agents/`) and execute in one of two modes:

- **Spawned** (preferred when the harness supports subagents, e.g. Claude Code's Agent tool): deploy `thinker`, `questioner`, `examiner`, `codifier`, `recruiter`, or `fabricator` as subagents for their phases. Recruited specialists likewise spawn from the agent library.
- **Inline** (fallback on harnesses without subagent spawning, or when a spawn fails): the session performs the role directly, following the same agent file's protocol and the character docs in `plugins/panel/docs/characters/`. Phase outputs and persistence obligations are identical in both modes.

State which mode each role ran in within the phase files (one word: spawned/inline).

## Full Pipeline Protocol (Default)

The default `/panel` command runs 3 stages:

1. **Decomposition** (Phase -2): The Thinker breaks problem into steps
2. **Clarification** (Phase -1.5): Interview for LOW/MEDIUM clarity steps
3. **Deliberation** (Phases -1 through 8): Full panel analysis

### Stage 1: Decomposition (Phase -2)

**The Thinker** (Sequential Problem Decomposition)

- Generate thinking session ID: `YYYY-MM-DD--<slug>`
- Create `Work/thinking/<id>/` directory
- Break problem into numbered steps with:
  - Dependencies between steps
  - Clarity ratings: HIGH (actionable) / MEDIUM (needs detail) / LOW (needs investigation)
  - Revision support (supersede, don't delete)
  - Branching for alternative approaches
- Output: `Work/thinking/<id>/summary.md`, `thoughts.jsonl`, `state.json`
- **Write phase file**: `phase--2-decomposition.md`

**Decomposition Decision Point**:

- If ALL steps are HIGH clarity → proceed to Phase -1 (skip interview)
- If ANY steps are LOW or MEDIUM clarity → proceed to Phase -1.5 (interview)

### Stage 2: Clarification (Phase -1.5)

**Conditional**: Only runs if decomposition found unclear steps.

For each LOW/MEDIUM clarity step:

1. **The Questioner** asks clarifying questions via AskUserQuestion
2. **The Examiner** validates the step's problem framing
3. Update decomposition with clarified information

- **Write phase file**: `phase--1.5-clarification.md`
- Update `Work/thinking/<id>/summary.md` with clarifications

**Clarification Exit Criteria**:

- All steps now HIGH clarity, OR
- User indicates "done" / sufficient clarity achieved

### Stage 3: Deliberation (Phases -1 through 8)

Standard panel protocol with decomposition context injected.

---

## Deliberation Protocol (11 Phases)

**Phase -1: Topic Analysis & Workforce Recruitment** (The Analyst)

- **Initialize persistence**:
  - Generate panel ID: `YYYY-MM-DD--<slug>` (slug from topic, lowercase, hyphens)
  - Create directory: `Work/panels/<id>/`
  - Initialize state.json with status `active`
  - Add entry to `Work/panels/index.json`
- Analyze topic domain (technical, creative, research-heavy, security-critical)
- Determine required expertise areas (2-5 domains typical)
- Search agent library systematically (`.claude/agents/*.md`)
- Score agents 0-10 for topic capability match:
  - 10: Perfect specialist match
  - 7-9: Strong capabilities alignment
  - 4-6: Partial match, can handle with coordination
  - 1-3: Poor match, inefficient
  - 0: No coverage, gap identified
- Assign specialists with session-specific names (e.g., `prose-analysis` → "The Editor")
- Deploy `fabricator` if gaps detected (no agent scores >4)
- Set decision rule (consensus default, unanimous for security)
- Infer primary goals from topic context
- **Write phase file**: `phase-00-recruitment.md`
- **Update state.json**: Record panel composition, goals, decision rule

**Phase 0: Goal Clarification** (The Moderator)

- Request clarification if topic is ambiguous or underspecified
- Formalize refined topic statement
- Skip if topic is already well-specified

**Phase 1: Framing** (The Moderator)

- State topic, inferred goals, constraints, decision rule
- Introduce panel composition:
  - Core roles (The Moderator, Analyst, Challenger)
  - Recruited specialists with session names
- Explain recruitment rationale (why these specialists for this topic)
- Establish complete panel composition before Initial Positions

**Phase 2: Infrastructure Check** (The Moderator)

- Compare proposals against existing assets to avoid duplication:
  - Memory files (workflowProtocols.md, activeContext.md)
  - Commands (/panel, /save, /notes, /validate-vault)
  - Agents (research-assistant, narrator, etc.)
- Output "Existing Infrastructure Comparison"
- Redirect to enhancement if duplicative

**Phase 3: Initial Positions** (All Panelists)

- Each specialist (via recruited agent) gathers information and analyzes from their domain
- The Challenger takes opposition stance: "DON'T do this because..." and demands proof
- Synthesize into 5-bullet brief: Position, Evidence, Risks, Unknowns, Recommendation
- Present findings with citations

**Phase 4: Cross-Examination** (The Challenger-led)

- The Challenger challenges assumptions, finds contradictions and failure modes
- Specialists respond from their domain perspectives
- Analyst may assign additional agents if challenges reveal capability gaps

**Phase 5: Research Gate** (The Moderator)

- If evidence gaps block decisions, authorize additional research
- Direct specialists to run targeted queries using assigned agents
- Analyst may assign additional specialized agents if insufficient
- Enforce Confidence Scoring: Relevance, Completeness, Confidence Score
- Thresholds: <0.6 Insufficient | 0.6–0.79 Preliminary | ≥0.8 High confidence

**Phase 6: Reflection Round** (All Panelists)

- Review Cross-Examination arguments and Research Gate findings
- Revise Initial Positions if persuaded by evidence or challenges
- Submit updated briefs acknowledging what changed and why
- The Moderator identifies convergence or remaining disagreements

**Phase 7: Synthesis** (Recruited Architect or The Moderator)

- Analyze updated briefs and structure viable options with tradeoffs
- Articulate decision pathways and implications
- If complex synthesis needed, Analyst may assign architecture specialist

**Phase 8: Decision** (The Moderator)

- Apply decision rule (consensus/unanimous based on topic)
- Calculate consensus percentage: (aligned panelists / total panelists) × 100
- Record dissent with percentage weight and rationale
- Threshold interpretation:
  - **100%**: Unanimous agreement
  - **80-99%**: Strong consensus (proceed with noted concerns)
  - **60-79%**: Moderate consensus (address dissent before proceeding)
  - **<60%**: Weak consensus (requires additional deliberation or escalation)
- List Next Steps with owners, deliverables, due dates
- **Write phase file**: `phase-08-decision.md`
- **Finalize persistence**:
  - Write `transcript.md` (full panel transcript)
  - Update state.json: status `completed`, final decision, consensus
  - Update index.json with decision summary

**Per-Phase Persistence** (all phases):
After completing each phase, update `state.json`:

- Increment `current_phase`
- Add phase number to `completed_phases`
- Update `updated` timestamp
- Store phase-specific data (positions, findings, etc.)

## Interview Mode Protocol

When `--interview` is specified, the panel runs a different workflow focused on requirements crystallization rather than multi-perspective deliberation.

### Interview Mode Roles

**The Questioner** (Requirements Gathering)

- Asks clarifying questions via AskUserQuestion
- Targets biggest sources of ambiguity
- Builds progressively from broad to specific
- Never proposes solutions, only gathers requirements

**The Examiner** (Problem Validation)

- Applies four fundamental questions:
  - **Essence**: "What IS this, really?"
  - **Root Cause**: "Is this root cause or symptom?"
  - **Prerequisites**: "What must exist first?"
  - **Hidden Assumptions**: "What are we assuming?"
- Produces SOUND/REVISE/REFRAME verdict

**The Codifier** (Specification Generation)

- Extracts structured requirements from Q&A
- Generates seed.yaml specification
- Maps to goal, constraints, acceptance criteria, ontology schema

### Interview Mode Phases

| Phase | Role | Description |
|-------|------|-------------|
| 0 | Setup | Create panel, initialize persistence, set interview=true |
| 1-N | The Questioner | Q&A loop via AskUserQuestion until "done" or clarity achieved |
| N+1 | The Examiner | Validate problem framing, produce verdict |
| N+2 | The Codifier | Generate seed.yaml from transcript (if SOUND verdict) |
| Final | Output | Write seed.yaml to `Work/panels/<id>/seed.yaml` |

### Interview Mode state.json

```json
{
  "id": "2026-03-06--task-api",
  "topic": "Build a task management API",
  "interview": true,
  "status": "active",
  "current_phase": "questioning",
  "qa_transcript": [
    {"question": "What domain?", "answer": "Task management"},
    {"question": "What operations?", "answer": "CRUD + filtering"}
  ],
  "examination_verdict": null,
  "seed_generated": false
}
```

### Interview Mode Output

Instead of a decision report, interview mode produces:

**Primary output:** `Work/panels/<id>/seed.yaml`

```yaml
goal: |
  Build a REST API for task management with tag-based organization

constraints:
  - Python 3.11+
  - SQLite for persistence

acceptance_criteria:
  - Tasks can be created via POST /tasks
  - Tasks can be filtered by tag

ontology_schema:
  name: "TaskManagement"
  entities:
    - name: "Task"
      fields:
        - name: "id"
          type: "string"
        - name: "title"
          type: "string"

metadata:
  version: "1.0"
  created: "2026-03-06T17:30:00+10:00"
  interview_id: "2026-03-06--task-api"
  examination_verdict: "SOUND"
```

### Resume Protocol (Interview Mode)

When resuming an interview:

1. Load state.json with interview=true
2. Restore qa_transcript context
3. If current_phase=questioning: continue Q&A
4. If current_phase=examination: re-run examination
5. If examination_verdict=REVISE: return to questioning with specific issues

## Decision Report (Fixed Output)

Every panel produces a structured decision report:

- **Topic** (including Phase 0 clarifications if applicable)
- **Context Materials** (if `--context` used: file summaries and key points)
- **Inferred Goals** (derived from topic analysis)
- **Decision Rule** (consensus or unanimous)
- **Panel Composition**:
  - Core Roles (The Moderator, Analyst, Challenger)
  - Recruited Specialists (agent → session name mapping with scores)
  - Recruitment Rationale (why these specialists for this topic)
- **Existing Infrastructure Comparison** (Phase 2 findings)
- **Expert Briefs** (Phase 3 Initial Positions with agent-gathered evidence)
- **Cross-Examination Findings** (Phase 4 challenges and responses)
- **Research Findings** (Phase 5 sources, if Research Gate activated)
- **Confidence Summary** (Relevance, Completeness, Score, Threshold)
- **Reflection Round Summary** (Phase 6 revised positions, convergence)
- **Synthesis** (Phase 7 options/tradeoffs)
- **Decision** (Phase 8 final determination)
- **Consensus** (percentage, threshold level, dissent summary)
- **Next Steps** (actionable items with ownership)

## Output Formats

### Markdown (Default)

Standard decision report as documented above. Suitable for Memory files, documentation, and general use.

### GitHub PR Comment (`--format=github`)

Condensed format optimized for GitHub pull request comments:

```markdown
## 🎯 Panel Decision: [Topic]

**Consensus**: 85% (Strong) | **Decision Rule**: Consensus

### Summary
[2-3 sentence executive summary]

### Recommendation
[Primary recommendation with rationale]

<details>
<summary>📊 Panel Composition</summary>

- **Core**: The Moderator (Moderator), Analyst, Challenger
- **Specialists**: [Agent] → "Session Name" (score)
</details>

<details>
<summary>⚖️ Key Trade-offs</summary>

[Synthesis bullet points]
</details>

<details>
<summary>🚫 Dissent (15%)</summary>

**The Challenger**: [Dissent rationale]
</details>

### Next Steps
- [ ] [Action item with owner]
```

### JSON (`--format=json`)

Structured data for programmatic consumption:

```json
{
  "topic": "string",
  "goals": ["string"],
  "decision_rule": "consensus|unanimous",
  "panel": {
    "core": ["The Moderator", "Analyst", "Challenger"],
    "specialists": [{"agent": "string", "session_name": "string", "score": 0-10}]
  },
  "consensus": {
    "percentage": 85,
    "threshold": "strong|moderate|weak|unanimous",
    "aligned": 4,
    "total": 5
  },
  "decision": "string",
  "dissent": [{"role": "string", "rationale": "string", "weight": 15}],
  "next_steps": [{"action": "string", "owner": "string", "deliverable": "string"}],
  "confidence": {"relevance": 0.0-1.0, "completeness": 0.0-1.0, "score": 0.0-1.0}
}
```

## Context Injection

The `--context` flag pre-loads reference material before panel deliberation:

```bash
/panel --context=docs/RFC-001.md "Should we adopt this RFC?"
/panel --context=Memory/techEnvironment.md "Evaluate caching strategy"
```

**Behavior**:

1. Read specified file(s) before Phase -1
2. Include content summary in Phase 1 Framing
3. Make content available to all panelists during deliberation
4. Reference in Decision Report under "Context Materials"

**Multiple contexts**:

```bash
/panel --context=spec.md --context=constraints.md "Evaluate feasibility"
```

**URL context** (if WebFetch available):

```bash
/panel --context=https://example.com/api-docs "Design integration approach"
```

## Dynamic Agent Recruitment Architecture

**Core Roles vs Recruited Specialists**:

- **Core Roles** = Persistent panel infrastructure (The Moderator, Analyst, Challenger)
- **Recruited Specialists** = Topic-specific experts from agent library with session names

**Recruitment Flow**:

1. **Phase -1**: Analyst analyzes topic → determines expertise needs → scores agents → assigns with session names
2. **Phase 3**: Specialists deploy assigned agents for research and analysis
3. **Phase 4-5**: Analyst may assign additional agents if gaps detected
4. **Phase 7**: Analyst may assign architecture specialist for complex synthesis

**Session-Specific Naming**:

- Same agent becomes different "character" depending on context
- `prose-analysis` → "The Editor" (creative), "The Code Reviewer" (technical), "The Stylist" (marketing)
- `research-assistant` → "The Archivist" (historical), "The Evidence Gatherer" (legal), "The Data Scout" (analytics)
- Names should reflect domain context and analytical role

**Gap Detection & Agent Creation**:
If no agent scores >4 for required capability → Analyst deploys `fabricator` to create new specialized agent during Phase -1.

## Character Files

Core roles have documented profiles in `plugins/panel/docs/characters/`:

- **The Thinker.md** - Sequential Problem Decomposition
- **The Moderator.md** - Moderator/Facilitator
- **The Analyst.md** - Workforce Intelligence
- **The Challenger.md** - Opposition & Quality Gate

Recruited specialists are documented in `.claude/agents/*.md` (agent count varies by host project).

## Persistence Architecture

Panels are saved to `Work/panels/` with full state for resumption and audit.

### Directory Structure

```
Work/
├── thinking/                            # Decomposition sessions
│   └── YYYY-MM-DD--<slug>/
│       ├── state.json                   # Thinking state
│       ├── thoughts.jsonl               # Append-only audit log
│       └── summary.md                   # Human-readable decomposition
│
└── panels/
    ├── index.json                       # Panel discovery index
    └── YYYY-MM-DD--<slug>/              # Per-panel directory
        ├── state.json                   # Resumable state
        ├── phase--2-decomposition.md    # Phase -2 output (full pipeline)
        ├── phase--1.5-clarification.md  # Phase -1.5 output (if unclear steps)
        ├── phase-00-recruitment.md      # Phase -1 output
        ├── phase-01-framing.md          # Phase 1 output
        ├── phase-02-infrastructure.md   # Phase 2 output
        ├── phase-03-positions.md        # Phase 3 output
        ├── phase-04-cross-examination.md # Phase 4 output
        ├── phase-05-research.md         # Phase 5 output (if activated)
        ├── phase-06-reflection.md       # Phase 6 output
        ├── phase-07-synthesis.md        # Phase 7 output
        ├── phase-08-decision.md         # Phase 8 output (final report)
        └── transcript.md                # Full panel transcript
```

### index.json Schema

```json
{
  "panels": [
    {
      "id": "2026-01-29--graphql-vs-rest",
      "topic": "Should we implement GraphQL or REST for the new API",
      "status": "completed|active|abandoned",
      "created": "2026-01-29T10:30:00+10:00",
      "updated": "2026-01-29T11:45:00+10:00",
      "current_phase": 8,
      "decision": "REST with GraphQL gateway for specific use cases",
      "consensus": 85
    }
  ]
}
```

### state.json Schema

```json
{
  "id": "2026-01-29--graphql-vs-rest",
  "topic": "Should we implement GraphQL or REST for the new API",
  "status": "active|completed|abandoned",
  "mode": "full|quick|think|interview",
  "created": "2026-01-29T10:30:00+10:00",
  "updated": "2026-01-29T11:45:00+10:00",
  "current_phase": 4,
  "completed_phases": [-2, -1.5, 0, 1, 2, 3],
  "decision_rule": "consensus",
  "context_files": ["docs/RFC.md"],
  "decomposition": {
    "thinking_id": "2026-01-29--graphql-vs-rest",
    "total_steps": 6,
    "unclear_steps": [2, 5],
    "clarified": true
  },
  "panel": {
    "core": ["The Moderator", "Analyst", "Challenger", "The Thinker"],
    "specialists": [
      {"agent": "reviewer", "session_name": "The Systems Designer", "score": 9},
      {"agent": "research-assistant", "session_name": "The Evidence Gatherer", "score": 8}
    ]
  },
  "goals": ["Determine optimal API strategy", "Consider team expertise"],
  "positions": {
    "The Systems Designer": {"position": "...", "recommendation": "..."},
    "The Challenger": {"position": "...", "recommendation": "..."}
  }
}
```

### Phase File Format

Each `phase-NN-*.md` contains:

```markdown
---
panel_id: "2026-01-29--graphql-vs-rest"
phase: 3
phase_name: "Initial Positions"
started: "2026-01-29T10:45:00+10:00"
completed: "2026-01-29T11:00:00+10:00"
---

# Phase 3: Initial Positions

[Phase content here]
```

### Resume Protocol

When `--resume <id>` is invoked:

1. Load `Work/panels/<id>/state.json`
2. Verify status is `active` (not `completed` or `abandoned`)
3. Read completed phase files to restore context
4. Continue from `current_phase + 1`
5. Update state.json after each phase completion

### Abandon Protocol

When `--abandon <id>` is invoked:

1. Load state.json
2. Set `status: "abandoned"`
3. Update index.json
4. Panel cannot be resumed after abandonment

## Management Command Protocols

### --list Protocol

1. Read `Work/panels/index.json` (create if missing)
2. Filter by `--status` if provided
3. Display table:

   ```
   ID                          | Status    | Phase | Topic                    | Updated
   ----------------------------|-----------|-------|--------------------------|-------------------
   2026-01-29--graphql-vs-rest | completed | 8     | GraphQL vs REST for API  | 2026-01-29 11:45
   2026-01-28--fish-enhancement| active    | 4     | How do we pimp fish      | 2026-01-28 16:20
   ```

### --show Protocol

1. Load `Work/panels/<id>/state.json`
2. Display summary:
   - Topic, status, decision rule
   - Panel composition with session names
   - Completed phases list
   - Current phase (if active)
   - Decision and consensus (if completed)

### --resume Protocol

1. Validate panel exists and status is `active`
2. Load state.json and all completed phase files
3. Reconstruct panel context:
   - Goals, decision rule, context files
   - Panel composition with session names
   - Positions from completed phases
4. Announce: "Resuming panel '<topic>' from Phase N"
5. Continue protocol from next incomplete phase
6. Update state.json after each phase
7. Update index.json on completion

### --abandon Protocol

1. Validate panel exists and status is `active`
2. Set status to `abandoned` in state.json
3. Update index.json
4. Confirm: "Panel '<topic>' marked abandoned"

## Notes

- **Full pipeline default**: Decompose → Clarify → Deliberate is the standard flow
- **Persistence**: Thinking sessions in `Work/thinking/`, panels in `Work/panels/`
- **Phase files**: Each phase writes to separate file for granular recovery
- **Dynamic recruitment**: No static panelists—Analyst assigns 2-5 specialists per topic
- **Session-specific names**: Agents given evocative contextual names for panel depth
- **Evidence standards**: Use markers where appropriate: [Inference], [Speculation], [Unverified]
- **Conditional phases**: Phase -1.5 only runs if unclear steps exist; Phase 0 skipped if topic well-specified
- **Mode shortcuts**: `--quick` for simple topics, `--think` for decomposition only, `--interview` for Q&A only
- **Core roles**: The Moderator, Analyst, Challenger, The Thinker always present; specialists vary by topic

## Pattern Implementation

Based on CSIRO Agent Design Patterns (Liu et al. 2025):

- **Passive Goal Creator** (Phase 0): Clarifies ambiguous topics
- **Role-Based Cooperation**: Core roles with hierarchical workflow
- **Debate-Based Cooperation**: Cross-Examination phase enables argument exchange
- **Self-Reflection**: Reflection Round allows position revision
- **Cross-Reflection**: Specialists review each other's arguments
- **Human Reflection**: Decision Report enables user contestability

**Reference**: Liu et al. (2025). "Agent design pattern catalogue: A collection of architectural patterns for foundation model based agents." *The Journal of Systems and Software* 220, 112278.

---

**ARGUMENTS**: Free-form topic text (everything after `/panel` is the topic)
