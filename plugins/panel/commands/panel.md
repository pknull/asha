---
description: "Convene multi-perspective expert panel for analysis and decision-making"
argument-hint: "Topic or question to analyze"
allowed-tools: ["Task", "Read", "Write", "Edit", "Grep", "Glob"]
---

# Panel - Expert Multi-Perspective Analysis

Convene a panel with 3 core roles + dynamically recruited specialists who analyze your topic from distinct perspectives and produce a structured decision through an 11-phase protocol.

## Usage

```bash
/panel How do we pimp fish
/panel Should we implement GraphQL or REST for the new API
/panel Evaluate Chapter 9's horror effectiveness
```

**That's it.** The panel handles everything automatically:
- The Recruiter analyzes topic and recruits 2-5 specialist agents from available library
- Assigns specialists with evocative session-specific names
- Infers goals from topic context
- Applies consensus decision rule (unanimous for security topics)
- The Adversary argues against proposals and demands proof of necessity
- Asha moderates and compiles the decision report

## Core Roles (Always Present)

**Asha** (Moderator/Facilitator)
- Manages 11-phase protocol execution
- Ensures procedural integrity and timebox enforcement
- Synthesizes final decision report
- **Question**: "What is the PROCESS?"

**The Recruiter** (Workforce Intelligence)
- Analyzes topic to determine needed expertise
- Scores available agent library (0-10) for capability match
- Recruits 2-5 specialist agents with session-specific names
- Deploys `agent-fabricator` if capability gaps detected
- **Question**: "Who has CAPABILITY?"

**The Adversary** (Opposition & Quality Gate)
- **Default stance: OPPOSE** - argues against proposals and defends status quo
- Demands evidence before changing working systems: "Show me user complaints, failure data, metrics"
- Forces proponents to prove necessity: "The current system works. Prove it doesn't."
- Prevents premature action and consensus formed without data
- **Question**: "Why should we do this at all?"

## Dynamic Panelists (Recruited Per Topic)

The Recruiter assigns agents from `.claude/agents/*.md` with **evocative session-specific names** based on topic context.

**Examples by Topic Type**:

**Creative Writing Panel** (Redacted229 Chapter 9 evaluation):
- `prose-analysis` → **"The Editor"** (craft assessment)
- `intimacy-designer` → **"The Architect of Dread"** (genre mechanics)
- `narrative-architect` → **"The Structuralist"** (story coherence)
- `character-developer` → **"The Psychologist"** (character authenticity)

**Technical Architecture Panel** (GraphQL vs REST):
- `research-assistant` → **"The Evidence Gatherer"** (source validation)
- `architect` → **"The Systems Designer"** (architecture patterns)
- `ml-engineer` → **"The Model Capability Analyst"** (performance analysis)

**Culinary Innovation Panel** (How do we pimp fish):
- `research-assistant` → **"The Culinary Historian"** (technique research)
- `trend-analyst` → **"The Flavor Prophet"** (emerging patterns)
- `creative-director` → **"The Presentation Architect"** (plating design)

**Session-Specific Naming Convention**:
- **Agent role** describes what it does (e.g., `prose-analysis`)
- **Session name** describes who it becomes for this panel (e.g., "The Editor")
- Names should be evocative, contextual, and domain-appropriate

## 11-Phase Protocol

**Phase -1: Topic Analysis & Workforce Recruitment** (The Recruiter)
- Analyze topic domain (technical, creative, research-heavy, security-critical)
- Determine required expertise areas (2-5 domains typical)
- Search agent library systematically (`.claude/agents/*.md`)
- Score agents 0-10 for topic capability match:
  * 10: Perfect specialist match
  * 7-9: Strong capabilities alignment
  * 4-6: Partial match, can handle with coordination
  * 1-3: Poor match, inefficient
  * 0: No coverage, gap identified
- Assign specialists with session-specific names (e.g., `prose-analysis` → "The Editor")
- Deploy `agent-fabricator` if gaps detected (no agent scores >4)
- Set decision rule (consensus default, unanimous for security)
- Infer primary goals from topic context

**Phase 0: Goal Clarification** (Asha)
- Request clarification if topic is ambiguous or underspecified
- Formalize refined topic statement
- Skip if topic is already well-specified

**Phase 1: Framing** (Asha)
- State topic, inferred goals, constraints, decision rule
- Introduce panel composition:
  * Core roles (Asha, Recruiter, Adversary)
  * Recruited specialists with session names
- Explain recruitment rationale (why these specialists for this topic)
- Establish complete panel composition before Initial Positions

**Phase 2: Infrastructure Check** (Asha)
- Compare proposals against existing assets to avoid duplication:
  * Memory files (workflowProtocols.md, activeContext.md)
  * Commands (/panel, /save, /notes, /validate-vault)
  * Agents (research-assistant, narrator, etc.)
- Output "Existing Infrastructure Comparison"
- Redirect to enhancement if duplicative

**Phase 3: Initial Positions** (All Panelists)
- Each specialist (via recruited agent) gathers information and analyzes from their domain
- The Adversary takes opposition stance: "DON'T do this because..." and demands proof
- Synthesize into 5-bullet brief: Position, Evidence, Risks, Unknowns, Recommendation
- Present findings with citations

**Phase 4: Cross-Examination** (The Adversary-led)
- The Adversary challenges assumptions, finds contradictions and failure modes
- Specialists respond from their domain perspectives
- Recruiter may assign additional agents if challenges reveal capability gaps

**Phase 5: Research Gate** (Asha)
- If evidence gaps block decisions, authorize additional research
- Direct specialists to run targeted queries using assigned agents
- Recruiter may assign additional specialized agents if insufficient
- Enforce Confidence Scoring: Relevance, Completeness, Confidence Score
- Thresholds: <0.6 Insufficient | 0.6–0.79 Preliminary | ≥0.8 High confidence

**Phase 6: Reflection Round** (All Panelists)
- Review Cross-Examination arguments and Research Gate findings
- Revise Initial Positions if persuaded by evidence or challenges
- Submit updated briefs acknowledging what changed and why
- Asha identifies convergence or remaining disagreements

**Phase 7: Synthesis** (Recruited Architect or Asha)
- Analyze updated briefs and structure viable options with tradeoffs
- Articulate decision pathways and implications
- If complex synthesis needed, Recruiter may assign architecture specialist

**Phase 8: Decision** (Asha)
- Apply decision rule (consensus/unanimous based on topic)
- Record dissent and rationale if present
- List Next Steps with owners, deliverables, due dates

## Decision Report (Fixed Output)

Every panel produces a structured decision report:

- **Topic** (including Phase 0 clarifications if applicable)
- **Inferred Goals** (derived from topic analysis)
- **Decision Rule** (consensus or unanimous)
- **Panel Composition**:
  * Core Roles (Asha, Recruiter, Adversary)
  * Recruited Specialists (agent → session name mapping with scores)
  * Recruitment Rationale (why these specialists for this topic)
- **Existing Infrastructure Comparison** (Phase 2 findings)
- **Expert Briefs** (Phase 3 Initial Positions with agent-gathered evidence)
- **Cross-Examination Findings** (Phase 4 challenges and responses)
- **Research Findings** (Phase 5 sources, if Research Gate activated)
- **Confidence Summary** (Relevance, Completeness, Score, Threshold)
- **Reflection Round Summary** (Phase 6 revised positions, convergence)
- **Synthesis** (Phase 7 options/tradeoffs)
- **Decision** (Phase 8 final determination)
- **Next Steps** (actionable items with ownership)

## Dynamic Agent Recruitment Architecture

**Core Roles vs Recruited Specialists**:
- **Core Roles** = Persistent panel infrastructure (Asha, Recruiter, Adversary)
- **Recruited Specialists** = Topic-specific experts from agent library with session names

**Recruitment Flow**:
1. **Phase -1**: Recruiter analyzes topic → determines expertise needs → scores agents → assigns with session names
2. **Phase 3**: Specialists deploy assigned agents for research and analysis
3. **Phase 4-5**: Recruiter may assign additional agents if gaps detected
4. **Phase 7**: Recruiter may assign architecture specialist for complex synthesis

**Session-Specific Naming**:
- Same agent becomes different "character" depending on context
- `prose-analysis` → "The Editor" (creative), "The Code Reviewer" (technical), "The Stylist" (marketing)
- `research-assistant` → "The Archivist" (historical), "The Evidence Gatherer" (legal), "The Data Scout" (analytics)
- Names should reflect domain context and analytical role

**Gap Detection & Agent Creation**:
If no agent scores >4 for required capability → Recruiter deploys `agent-fabricator` to create new specialized agent during Phase -1.

## Character Files

Core roles have documented profiles in `plugins/panel/docs/characters/`:
- **Asha.md** - Moderator/Facilitator
- **The Recruiter.md** - Workforce Intelligence
- **The Adversary.md** - Opposition & Quality Gate

Recruited specialists are documented in `.claude/agents/*.md` (agent count varies by host project).

## Logging

Panel transcripts are automatically saved to:
```
Work/meetings/YYYY-MM-DD--panel--<slug>.md
```

Suggested frontmatter:
```yaml
---
date: YYYY-MM-DD
topic: "<one-line topic>"
mode: "inworld|outworld"
decision_rule: "consensus|unanimous"
experts: ["moderator", "adversary", "recruited-agent-1", "recruited-agent-2", ...]
---
```

## Notes

- **Dynamic recruitment**: No static panelists—Recruiter assigns 2-5 specialists per topic
- **Session-specific names**: Agents given evocative contextual names for panel depth
- **Evidence standards**: Use markers where appropriate: [Inference], [Speculation], [Unverified]
- **Optional phases**: Skip Phase 0 if topic well-specified, skip Phase 6 for simple decisions
- **Tool segregation**: Memory/Tools via filesystem; Vault via Obsidian tools; BookStack via MCP
- **Core role consistency**: Asha, Recruiter, Adversary always present; specialists vary by topic

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
