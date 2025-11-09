# Expert Panel Consensus Session

Create a structured expert panel where universal characters deploy specialized agents as tools to evaluate ideas and produce clear decisions. Default mode is inworld at Redacted1194, with a fixed Decision Report. An out_of_world fallback is available if explicitly requested.

## Modes

- **inworld (default)**: Panel convenes at **Redacted1194**—the white ring at cosmic center, liminal space beneath all planes where minds and essences roam. Uses /rp semantics:
  - **Parentheses () = Actions/Thoughts**: `(Asha gestures toward The Architect)` → narrative actions in liminal space
  - **Brackets [] = Meta-Instructions**: `[Research Gate: run Perplexity on X]`, `[Switch to technical mode]` → functional directives outside narrative
  - **Plain text = Dialogue**: Character voices in established personalities → direct speech at the Threshold
- **out_of_world (fallback)**: Protocol-first; no scene transcript. Explicitly request this mode if needed.

## Character-Based Panel Architecture

**Core Concept**: 8 universal characters with persistent analytical perspectives deploy agents as tools via two-tier protocol.

**Universal Characters** (Panel/characters/):
- **Asha** (Archive Guardian + Moderator) → "What is the PROCESS?"
- **The Adversary** (Challenge & Quality Gate) → "Is this WRONG/broken/contradictory?"
- **The Recruiter** (Workforce Intelligence) → "Who has CAPABILITY?"
- **The Architect** (Structure & Synthesis) → "What are the OPTIONS?"
- **The Archivist** (Evidence & Sources) → "What does EVIDENCE say?"
- **The Engineer** (Implementation Excellence) → "Can we BUILD this?"
- **The Ethicist** (Values & Cultural Integrity) → "Who is AFFECTED?"
- **The Curator** (Aesthetic Fitness - Creative Specialist) → "Does this SERVE its purpose?"

**Dynamic Agent Assignment (Phase 1)**:
- **The Recruiter orchestrates all agent deployment**: No static defaults—assignments vary by panel topic
- **Process**:
  1. Read character Capability Requirements from character files
  2. Analyze panel topic context (research-heavy? implementation? creative?)
  3. Search 239-agent library systematically
  4. Score agents 0-10 for each character's capability needs
  5. Assign optimal agents (0-N per character based on topic complexity)
  6. Deploy `agent-fabricator` if gaps detected (no agent scores >4)
- **Examples**:
  - Simple panel: The Architect operates agentless (direct analytical perspective)
  - Research panel: The Archivist receives `research-assistant` (10/10 score for external research)
  - Complex panel: The Curator receives `prose-analysis` + `narrator` + `intimacy-designer` (multi-agent coverage)

**Same character voice, different technical tools based on problem context.**

## PanelSpec (include in your invocation; defaults shown)

```yaml
topic: "<one-line problem>"
goals:
  - "<goal 1>"
  - "<goal 2>"
decision_rule: "consensus"   # consensus|majority|weighted|unanimous
mode: "inworld"              # default
venue: "Redacted1194"
composition: "manual"        # manual (default) | universal (all 8 characters with abstention)
characters:                  # Used when composition: manual
  - Asha                     # Always present (moderator)
  - The Adversary            # Default (challenge)
  - The Recruiter            # Default (workforce)
  - The Architect            # Default (synthesis)
  # Add domain-specific characters as needed:
  # - The Archivist          # For research-heavy topics
  # - The Engineer           # For implementation decisions
  # - The Ethicist           # For value-heavy decisions
  # - The Curator            # For creative craft evaluation
constraints: ["confidential","timebox: 20 minutes"]
output_format: "fixed Decision Report headings"
```

## No-Argument Help (auto)

If `/panel` is invoked without any additional input:

- Display quick help (what panel does and how to supply input)
- List available Profiles from `Panel/profiles.md` (profile names + descriptions)
- List available Rosters from `Panel/rosters.md` (roster names + character compositions)
- Show two examples:
  - Profile-based: `/panel-use system_design --topic "<one-line>" --mode out_of_world`
  - Roster-based: `/panel-use --roster core --topic "<one-line>"`

Operator steps to generate lists:
1) Read `Panel/profiles.md` and extract profile names with purposes
2) Read `Panel/rosters.md` and list roster names with character compositions

Tip: For precision mode, either set `mode: out_of_world` in the spec or add `[Switch to technical mode]`.

### Profiles Integration

- To avoid manual YAML copy, use `/panel-use <profile> --topic "..."` to load a named profile from `Panel/profiles.md` and run `/panel` with the merged spec.
- Profiles define: roster (character composition), goals, constraints, decision_rule, mode
- Example: `/panel-use system_design --topic "Review microservices API"`

### Roster Integration

- Define character collections once in `Panel/rosters.md`
- Rosters specify which characters participate for different problem domains
- Use `/panel-use --roster <name> --topic "..."` to invoke roster directly
- Example rosters:
  - **core** (creative): Asha, Adversary, Recruiter, Curator, Architect, Engineer, Ethicist
  - **architecture** (software): Asha, Adversary, Recruiter, Architect, Engineer, Ethicist
  - **security** (software): Asha, Adversary, Recruiter, Architect, Ethicist
  - **minimal** (utility): Asha, Adversary, Recruiter

## Protocol (11-Phase Enhanced Process)

**Enhanced based on CSIRO agent design pattern research (2025)**

**0) Goal Clarification** (Asha - Passive Goal Creator pattern):
   - Analyze user topic for ambiguity, underspecification, or unclear constraints
   - Request clarification if topic lacks sufficient context for decision-making
   - Formalize refined topic statement before proceeding to Participation Declaration
   - Skip if topic is already well-specified

**1) Participation Declaration** (All Characters - Universal Composition Mode Only):
   - **Trigger**: Only executes when `composition: universal` in PanelSpec
   - **Purpose**: Characters evaluate topic relevance and declare participation intent or abstain
   - **Process**:
     1. Asha presents topic and goals from Phase 0
     2. Each character assesses whether their analytical domain applies to topic
     3. Characters declare: PARTICIPATE (brief rationale) or ABSTAIN (brief rationale)
     4. Asha confirms ≥3 active participants (minimum threshold)
   - **Abstention Format**: `ABSTENTION: [1-sentence rationale explaining domain mismatch]`
   - **Re-entry Permitted**: Abstaining characters may re-join during Phase 7 (Research Gate) if evidence reveals relevance
   - **Output**: List of active participants for Workforce Assignment
   - **Skip Condition**: When `composition: manual`, Phase 1 is skipped entirely

**2) Workforce Assignment** (The Recruiter - Dynamic Agent Orchestration):
   - **Purpose**: Assign optimal agents to panel characters based on topic requirements
   - **Read Capability Requirements**: Review each character's documented capability needs from character files
   - **Analyze Topic Context**: Determine if panel is research-heavy, implementation-focused, creative, technical, etc.
   - **Search Agent Library**: Systematic grep across 239-agent ecosystem (`.claude/agents/*.md`)
   - **Score Agent Matches**: For each character's capability needs, score available agents 0-10:
     * 10: Perfect specialist, capability in core competency
     * 7-9: Strong match, documented capabilities align
     * 4-6: Partial match, can handle with coordination
     * 1-3: Poor match, inefficient
     * 0: No coverage, gap identified
   - **Assign Agents**: Allocate optimal agents to characters (0-N agents per character based on topic complexity)
   - **Gap Detection**: If any capability scores <4 (no adequate agent exists) → deploy `agent-fabricator` to create new agent
   - **Document Assignments**: Output workforce allocation table for transparency:
     ```
     Character          | Assigned Agents (Score)           | Rationale
     ------------------ | --------------------------------- | ---------
     The Archivist      | research-assistant (10/10)        | External research capability perfect match
     The Architect      | sequential-thinker (9/10)         | Complex decomposition needed
     The Curator        | prose-analysis (10/10)            | Prose quality assessment
     ...                | ...                               | ...
     ```
   - **Simple Topics**: Many characters may operate agentless (direct analytical perspective)
   - **Complex Topics**: Characters may receive 2-3+ agents for comprehensive coverage

**3) Framing** (Asha):
    - State Topic, Goals, Constraints, Decision Rule
    - Introduce each character and explain why they were selected for this specific topic
    - Reference Recruiter's workforce assignments from Phase 2
    - Establish complete panel composition rationale before proceeding to Initial Positions

**4) Infrastructure Check** (Asha - Infrastructure Validation Gate):
    - Compare proposals against current assets to avoid duplication:
      - Memory: `workflowProtocols.md` (Infrastructure Validation Protocol), `activeContext.md`
      - Commands: /panel, /save, /notes, /validate-vault
      - Agents: research-assistant, narrator, etc.
    - Output "Existing Infrastructure Comparison" noting duplicates vs genuinely new capabilities
    - If duplicative, redirect to enhancing existing infrastructure instead of creating new

**5) Initial Positions** (all characters): 5 bullets each → Position, Evidence, Risks, Unknowns, Recommendation.
   - Each character deploys their assigned agents (from Phase 2) to gather information
   - Characters with multiple agents coordinate outputs (e.g., research-assistant findings + domain specialist analysis)
   - Characters operating agentless deliver direct analytical perspective
   - Synthesize agent outputs into structured 5-bullet brief with citations
   - Present findings to panel with evidence from agent research

**6) Cross-Examination** (The Adversary-led): Challenge assumptions; characters may update briefs.
   - The Adversary asks "Is this WRONG?" - finds contradictions, flaws, failure modes
   - Other characters respond from their analytical perspectives
   - Characters may request additional agent deployment via Recruiter if challenges reveal evidence gaps

**7) Research Gate** (Asha): If evidence gaps block decisions, authorize additional research
    - Direct The Archivist to run targeted queries using assigned research agents
    - Recruiter may assign additional specialized agents if initial assignments insufficient
    - Enforce Confidence Scoring: report Relevance, Completeness, Confidence Score
    - Thresholds: <0.6 Insufficient (no synthesis); 0.6–0.79 Preliminary (synthesize with caveats); ≥0.8 High confidence

**8) Reflection Round** (all characters - Self-Reflection + Cross-Reflection patterns):
   - Characters review Cross-Examination arguments and Research Gate findings
   - Revise Initial Positions if persuaded by evidence or challenges
   - Submit updated briefs acknowledging what changed and why
   - Asha identifies convergence or remaining disagreements

**9) Synthesis** (The Architect): Analyze updated briefs and structure viable options with explicit tradeoffs; articulate decision pathways and their implications. The Architect synthesizes; Asha facilitates.
   - The Architect asks "What are the OPTIONS?" - enumerates paths with tradeoffs
   - May request additional agent deployment via Recruiter for complex synthesis requiring specialized tools

**10) Decision** (Asha): Apply decision_rule; record dissent and rationale; list Next Steps.

## Diegetic Tool Boundary (inworld)

- **Bracketed meta authorizes tool calls**: `[Research Gate: run Perplexity on "X"]` → The Archivist executes, returns with sources, resumes diegetic voice
- **Maintain scene integrity**: Keep meta concise and strictly functional; all character dialogue occurs at the Threshold
- **Tool execution**: Bracketed instructions break fourth wall briefly; return to liminal space immediately after execution

## Decision Report (fixed output)

- Topic (including clarifications from Phase 0 if applicable)
- Goals
- Decision Rule
- Composition Mode (manual or universal)
- Panel Composition Rationale (which characters, why selected for this topic)
- Participation Declarations (Phase 1 - if universal mode: active participants + abstentions with rationales)
- Workforce Assignments (Phase 2 - Recruiter's agent allocation table with scores and rationale)
- Existing Infrastructure Comparison (Phase 4)
- Character Briefs (by character - Phase 5 Initial Positions with agent-gathered evidence)
- Cross-Examination Findings (Phase 6)
- Research Findings (Phase 7 - sources, if Research Gate activated)
- Confidence Summary: Relevance, Completeness, Confidence Score, Threshold
- Reflection Round Summary (Phase 8 - revised positions, convergence points)
- Synthesis (Phase 9 - options/tradeoffs from The Architect)
- Decision (Phase 10)
- Next Steps (owner, deliverable, due)

## Composition Mode Selection

**Two Operational Patterns**:

### Manual Composition (Default)

**When to Use**:
- Single-domain topics with clear expertise requirements (technical, craft-specific, implementation)
- Routine decisions where character selection is obvious
- Efficiency priority (minimize token overhead)
- Examples: GraphQL API review, prose quality audit, security assessment

**How It Works**:
```yaml
composition: "manual"  # default
characters:
  - Asha
  - The Adversary
  - The Recruiter
  - The Architect
  # Add domain-specific characters:
  - The Curator          # For creative craft
  - The Ethicist         # For value-heavy decisions
```

**Default Core**: Asha (always), The Adversary, The Recruiter, The Architect (4 characters)

**Add Specialized Characters**:
- **The Archivist**: Research-heavy topics, external evidence, source validation
- **The Engineer**: Implementation feasibility, technical constraints, build quality
- **The Ethicist**: Value-heavy decisions, stakeholder advocacy, cultural integrity
- **The Curator**: Creative craft, narrative purpose, style guide enforcement

**Token Estimate**: 30-40k tokens for typical 4-5 character panel

---

### Universal Invitation (All 8 Characters with Abstention)

**When to Use**:
- Multi-domain topics requiring comprehensive perspective coverage
- Strategic decisions with unclear composition needs
- Ambiguous problems where emergent insights valuable
- High-stakes decisions justifying thorough evaluation
- Examples: Panel system architecture, complex worldbuilding, cross-domain policy

**How It Works**:
```yaml
composition: "universal"
# No characters array needed - all 8 invited automatically
```

**Process**:
1. All 8 characters invited (Asha, Adversary, Recruiter, Architect, Archivist, Engineer, Ethicist, Curator)
2. **Phase 1**: Characters declare participation or abstain with rationale
3. Phase 2+ proceeds with active participants only (≥3 required)

**Abstention Protocol**:
- **Format**: `ABSTENTION: [1-sentence domain mismatch rationale]`
- **Example**: "ABSTENTION: Process architecture outside craft evaluation domain" (The Curator)
- **Timing**: Phase 1 (before Workforce Assignment to avoid agent assignment waste)
- **Re-entry**: Permitted during Phase 7 (Research Gate) if evidence reveals relevance
- **Transparency**: Abstention notices included in Decision Report

**Expected Participation**: 5-6 active characters (20-30% abstention rate typical per research)

**Token Estimate**: 35-50k tokens (5-15k overhead vs manual for comprehensive coverage)

---

### Selection Guidance

| Factor | Manual Composition | Universal Invitation |
|--------|-------------------|---------------------|
| **Topic Clarity** | Clear domain focus | Multi-domain or ambiguous |
| **Expertise Needs** | 3-5 characters obvious | Uncertain which perspectives needed |
| **Priority** | Efficiency, speed | Comprehensive coverage, thoroughness |
| **Complexity** | Single-domain | Cross-domain, strategic |
| **Token Budget** | 30-40k baseline | 35-50k with overhead |
| **Use Case** | Routine panels | Strategic decisions |

**Examples**:
- **Manual**: GraphQL security review → Adversary, Architect, Engineer, Ethicist (4 characters, technical focus)
- **Universal**: Panel composition architecture → All 8 invited, 1 abstained (strategic meta-decision)

**Character Independence**: Each character operates from their unique analytical perspective without referencing other panel members. They only interact through the panel protocol phases.

## Pattern Implementation Notes

**CSIRO Agent Design Patterns Applied**:
- **Passive Goal Creator** (Phase 0): Clarifies ambiguous user topics before panel begins
- **Role-Based Cooperation**: Fixed character assignments with hierarchical workflow
- **Debate-Based Cooperation**: Cross-Examination phase enables argument exchange
- **Self-Reflection**: Reflection Round allows characters to revise their positions
- **Cross-Reflection**: Characters review each other's arguments during Reflection Round
- **Human Reflection**: Decision Report enables user contestability and feedback

**Reference**: Liu et al. (2025). "Agent design pattern catalogue: A collection of architectural patterns for foundation model based agents." *The Journal of Systems and Software* 220, 112278.

## World Consistency & Character Portrayal (inworld mode)

**REDACTED99 Universe Standards** (when applicable to topic):
- 12 Spheres of Magic system
- Cosmic horror tone (Gla'aki, Redacted234, Obelisks)
- Ætheric science and consciousness research
- Redacted107 institutional framework

**Canon Sources**:
- Character sheets: `Vault/World/Characters/`
- World documents: `Vault/World/`
- TTRPG session notes: `Vault/TTRPG/Notes/`
- Published fiction: `Vault/Books/`
- Memory Bank: `Memory/`

**Panel Character Standards** (Universal Characters at The Threshold):
1. **Read character file** (`Panel/characters/The [Name].md`)
2. **Maintain consistent voice** from documented character essence
3. **Deploy agents as tools** via two-tier protocol (Tier 1 defaults + Tier 2 Recruiter search)
4. **No cross-references** - characters work independently from their unique perspectives
5. **Respect analytical boundaries** - each character asks their specific question

**Period Authenticity** (for REDACTED99-related topics):
- 1895 Laramie, Wyoming language patterns
- Victorian/Western vocabulary
- No anachronisms in character speech
- Period-appropriate social dynamics

## Notes

- **Characters vs Agents**: Characters are persistent analytical perspectives with consistent voices. Agents are technical tools The Recruiter assigns dynamically based on panel topic. Same character (The Architect) may receive different agents (simple topic: agentless, complex topic: `sequential-thinker` + domain specialists) depending on problem context.
- **Dynamic Agent Assignment** (Phase 2): The Recruiter reads character Capability Requirements, analyzes panel topic, performs 0-10 scoring across 239-agent library, assigns optimal agents to characters (0-N per character). No static defaults—assignments vary by topic.
- **Agent Deployment Flow**: Phase 2 (Recruiter assigns) → Phase 5 (characters use assigned agents for research) → Phase 6-7 (Recruiter may assign additional agents if gaps detected) → Phase 9 (Architect may request specialized agents for synthesis).
- **Gap Detection & Agent Creation**: If no agent scores >4 for required capability → Recruiter deploys `agent-fabricator` to create new specialized agent during Phase 2.
- **Contributions**: Keep concise and decision-focused. Use evidence labels where appropriate: [Inference], [Speculation], [Unverified].
- **Tool segregation**: Memory/Tools via filesystem; Vault via Obsidian tools; BookStack via MCP.
- **Optional Phases**: Skip Phase 0 (Goal Clarification) if topic is well-specified; skip Phase 1 (Participation Declaration) when composition: manual; skip Phase 8 (Reflection Round) for simple/uncontentious decisions.
- **Narrative integrity**: In inworld mode, maintain liminal space atmosphere while executing structured protocol.

## Logging (default)
- Save panel transcript + Decision Report to `Work/meetings/YYYY-MM-DD--panel--<slug>.md` using filesystem tools.
- Suggested frontmatter for meeting files:
  ```
  ---
  date: YYYY-MM-DD
  topic: "<one-line topic>"
  mode: "inworld|out_of_world"
  decision_rule: "consensus|majority|weighted|unanimous"
  characters: ["Asha","The Adversary","The Recruiter","The Architect"]
  ---
  ```
