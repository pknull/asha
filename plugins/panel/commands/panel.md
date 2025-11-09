# Panel - Expert Multi-Perspective Analysis

Convene a panel of 8 universal characters who analyze your topic from distinct perspectives, deploy specialized agents as tools, and produce a structured decision through an 11-phase protocol.

## Usage

```bash
/panel How do we pimp fish
/panel Should we implement GraphQL or REST for the new API
/panel Evaluate Chapter 9's horror effectiveness
```

**That's it.** The panel handles everything automatically:
- Selects which characters participate based on topic analysis
- Assigns optimal agents to characters via Recruiter scoring (239-agent library)
- Infers goals from topic context
- Applies consensus decision rule (unanimous for security topics)

## Universal Characters

Each character brings a unique analytical lens:

- **Asha** (Moderator) → "What is the PROCESS?"
- **The Adversary** (Challenge & Quality Gate) → "Is this WRONG/broken/contradictory?"
- **The Recruiter** (Workforce Intelligence) → "Who has CAPABILITY?"
- **The Architect** (Structure & Synthesis) → "What are the OPTIONS?"
- **The Archivist** (Evidence & Sources) → "What does EVIDENCE say?"
- **The Engineer** (Implementation Excellence) → "Can we BUILD this?"
- **The Ethicist** (Values & Cultural Integrity) → "Who is AFFECTED?"
- **The Curator** (Aesthetic Fitness) → "Does this SERVE its purpose?"

Not all characters participate in every panel—they evaluate relevance and abstain if their domain doesn't apply.

## 11-Phase Protocol

**Phase -1: Topic Analysis & Character Selection** (Asha)
- Analyze topic domain (technical, creative, research-heavy, security-critical, etc.)
- Invite 4-8 relevant characters based on detected needs
- Set decision rule (consensus default, unanimous for security)
- Infer primary goals from topic context

**Phase 0: Goal Clarification** (Asha)
- Request clarification if topic is ambiguous or underspecified
- Formalize refined topic statement
- Skip if topic is already well-specified

**Phase 1: Participation Declaration** (All Invited Characters)
- Each character evaluates topic relevance to their analytical domain
- Declare PARTICIPATE (brief rationale) or ABSTAIN (brief rationale)
- Minimum 3 active participants required
- Abstaining characters may re-join during Research Gate if evidence reveals relevance

**Phase 2: Workforce Assignment** (The Recruiter)
- Read Capability Requirements from each active character's profile
- Analyze topic context (research-heavy? implementation? creative? technical?)
- Search 239-agent library systematically (`.claude/agents/*.md`)
- Score agents 0-10 for each character's capability needs:
  * 10: Perfect specialist match
  * 7-9: Strong capabilities alignment
  * 4-6: Partial match, can handle with coordination
  * 1-3: Poor match, inefficient
  * 0: No coverage, gap identified
- Assign optimal agents (0-N per character based on complexity)
- Deploy `agent-fabricator` if gaps detected (no agent scores >4)
- Output workforce allocation table with scores and rationale

**Phase 3: Framing** (Asha)
- State topic, inferred goals, constraints, decision rule
- Introduce active characters and explain selection rationale
- Reference Recruiter's workforce assignments
- Establish complete panel composition before Initial Positions

**Phase 4: Infrastructure Check** (Asha)
- Compare proposals against existing assets to avoid duplication:
  * Memory files (workflowProtocols.md, activeContext.md)
  * Commands (/panel, /save, /notes, /validate-vault)
  * Agents (research-assistant, narrator, etc.)
- Output "Existing Infrastructure Comparison"
- Redirect to enhancement if duplicative

**Phase 5: Initial Positions** (All Active Characters)
- Each character deploys assigned agents to gather information
- Characters with multiple agents coordinate outputs
- Characters operating agentless deliver direct analytical perspective
- Synthesize into 5-bullet brief: Position, Evidence, Risks, Unknowns, Recommendation
- Present findings with citations

**Phase 6: Cross-Examination** (The Adversary-led)
- The Adversary challenges assumptions, finds contradictions and failure modes
- Other characters respond from their analytical perspectives
- Characters may request additional agent deployment if challenges reveal gaps

**Phase 7: Research Gate** (Asha)
- If evidence gaps block decisions, authorize additional research
- Direct The Archivist to run targeted queries using assigned agents
- Recruiter may assign additional specialized agents if insufficient
- Enforce Confidence Scoring: Relevance, Completeness, Confidence Score
- Thresholds: <0.6 Insufficient | 0.6–0.79 Preliminary | ≥0.8 High confidence

**Phase 8: Reflection Round** (All Active Characters)
- Review Cross-Examination arguments and Research Gate findings
- Revise Initial Positions if persuaded by evidence or challenges
- Submit updated briefs acknowledging what changed and why
- Asha identifies convergence or remaining disagreements

**Phase 9: Synthesis** (The Architect)
- Analyze updated briefs and structure viable options with tradeoffs
- Articulate decision pathways and implications
- May request additional agent deployment for complex synthesis

**Phase 10: Decision** (Asha)
- Apply decision rule (consensus/unanimous based on topic)
- Record dissent and rationale if present
- List Next Steps with owners, deliverables, due dates

## Decision Report (Fixed Output)

Every panel produces a structured decision report:

- **Topic** (including Phase 0 clarifications if applicable)
- **Inferred Goals** (derived from topic analysis)
- **Decision Rule** (consensus or unanimous)
- **Character Selection Rationale** (why these characters for this topic)
- **Participation Declarations** (active participants + abstentions with rationales)
- **Workforce Assignments** (Recruiter's agent allocation table with scores)
- **Existing Infrastructure Comparison** (Phase 4 findings)
- **Character Briefs** (Phase 5 Initial Positions with agent-gathered evidence)
- **Cross-Examination Findings** (Phase 6 challenges and responses)
- **Research Findings** (Phase 7 sources, if Research Gate activated)
- **Confidence Summary** (Relevance, Completeness, Score, Threshold)
- **Reflection Round Summary** (Phase 8 revised positions, convergence)
- **Synthesis** (Phase 9 options/tradeoffs from The Architect)
- **Decision** (Phase 10 final determination)
- **Next Steps** (actionable items with ownership)

## Dynamic Agent Assignment

**Characters vs Agents**:
- **Characters** = Persistent analytical perspectives with consistent voices
- **Agents** = Technical tools The Recruiter assigns dynamically based on panel topic

Same character may receive different agents depending on problem context:
- Simple topic: The Architect operates agentless (direct analytical perspective)
- Research topic: The Archivist receives `research-assistant` (10/10 score)
- Complex topic: The Curator receives `prose-analysis` + `narrator` + `intimacy-designer`

**Agent Deployment Flow**:
1. Phase 2: Recruiter assigns agents based on capability scoring
2. Phase 5: Characters use assigned agents for research and analysis
3. Phase 6-7: Recruiter may assign additional agents if gaps detected
4. Phase 9: Architect may request specialized agents for synthesis

**Gap Detection & Agent Creation**:
If no agent scores >4 for required capability → Recruiter deploys `agent-fabricator` to create new specialized agent during Phase 2.

## Character Files

Each character has a documented profile in `plugins/panel/docs/characters/`:
- Nature and essence
- Manifestation at The Threshold
- Analytical approach and key questions
- Capability requirements for agent assignment
- Role in panel sessions
- Voice examples

Characters operate independently without cross-referencing other panel members. They only interact through protocol phases.

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
decision_rule: "consensus|unanimous"
characters: ["Asha", "The Adversary", "The Recruiter", "The Architect", ...]
---
```

## Notes

- **Automatic character selection**: Phase -1 analyzes topic and invites relevant characters (4-8 typical)
- **Abstention protocol**: Invited characters declare participation or abstain with rationale
- **Dynamic agent assignment**: No static defaults—assignments vary by topic complexity
- **Evidence standards**: Use markers where appropriate: [Inference], [Speculation], [Unverified]
- **Optional phases**: Skip Phase 0 if topic well-specified, skip Phase 8 for simple decisions
- **Tool segregation**: Memory/Tools via filesystem; Vault via Obsidian tools; BookStack via MCP
- **Character independence**: Each operates from unique perspective, no cross-references

## Pattern Implementation

Based on CSIRO Agent Design Patterns (Liu et al. 2025):
- **Passive Goal Creator** (Phase 0): Clarifies ambiguous topics
- **Role-Based Cooperation**: Fixed character assignments with hierarchical workflow
- **Debate-Based Cooperation**: Cross-Examination phase enables argument exchange
- **Self-Reflection**: Reflection Round allows position revision
- **Cross-Reflection**: Characters review each other's arguments
- **Human Reflection**: Decision Report enables user contestability

**Reference**: Liu et al. (2025). "Agent design pattern catalogue: A collection of architectural patterns for foundation model based agents." *The Journal of Systems and Software* 220, 112278.

---

**ARGUMENTS**: Free-form topic text (everything after `/panel` is the topic)
