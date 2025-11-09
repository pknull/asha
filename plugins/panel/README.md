# Claude Code Panel Plugin

**Version**: 4.0.0
**Description**: Simplified automatic multi-perspective analysis system

Just type `/panel <your topic>` and the system automatically:
- Selects relevant characters from 8 universal analytical perspectives
- Assigns optimal expert agents via Recruiter scoring (239-agent library)
- Executes structured 11-phase decision protocol
- Produces comprehensive Decision Report with evidence and next steps

---

## Quick Start

```bash
/panel How do we pimp fish
/panel Should we implement GraphQL or REST for the new API
/panel Evaluate Chapter 9's horror effectiveness without camp
```

**That's it.** No flags, no YAML, no manual configuration.

---

## What is the Panel System?

The panel system enables automatic expert analysis by:

1. **Analyzing your topic** (Phase -1) - Detects domain (technical, creative, research-heavy, security-critical) and selects 4-8 relevant characters
2. **Dynamic character participation** (Phase 1) - Invited characters declare participation or abstain if domain doesn't apply
3. **Automatic agent assignment** (Phase 2) - The Recruiter scores 239-agent library (0-10) and assigns optimal tools to characters
4. **Structured inquiry** (Phases 3-10) - Cross-examination, research gate, reflection, synthesis, decision
5. **Decision Report** - Evidence citations, confidence scoring, dissent notation, actionable next steps

**Use cases**: System design, security review, narrative craft, strategic decisions, feasibility analysis, ethical assessment, architecture

---

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

Not all characters participate in every panel—Phase -1 selects relevant characters based on topic analysis, and Phase 1 allows abstention if domain doesn't apply.

---

## 11-Phase Protocol

**Phase -1: Topic Analysis & Character Selection** (Asha)
- Analyze topic domain and select 4-8 relevant characters
- Set decision rule (consensus default, unanimous for security)
- Infer primary goals from context

**Phase 0: Goal Clarification** (Asha)
- Request clarification if topic ambiguous
- Skip if well-specified

**Phase 1: Participation Declaration** (Invited Characters)
- Declare PARTICIPATE or ABSTAIN with rationale
- Minimum 3 active participants required

**Phase 2: Workforce Assignment** (The Recruiter)
- Score 239-agent library (0-10) for each character's capability needs
- Assign optimal agents (0-N per character)
- Deploy agent-fabricator if gaps detected

**Phase 3: Framing** (Asha)
- State topic, goals, decision rule, character selection rationale

**Phase 4: Infrastructure Check** (Asha)
- Compare against existing assets to avoid duplication

**Phase 5: Initial Positions** (Active Characters)
- Deploy assigned agents to gather evidence
- Present 5-bullet brief: Position, Evidence, Risks, Unknowns, Recommendation

**Phase 6: Cross-Examination** (The Adversary-led)
- Challenge assumptions, find contradictions

**Phase 7: Research Gate** (Asha)
- Authorize additional research if evidence gaps exist
- Enforce confidence scoring

**Phase 8: Reflection Round** (Active Characters)
- Revise positions based on evidence and challenges

**Phase 9: Synthesis** (The Architect)
- Structure viable options with tradeoffs

**Phase 10: Decision** (Asha)
- Apply decision rule, record dissent, list next steps

---

## Installation

### Prerequisites

This plugin assumes your Claude Code project uses:

1. **AGENTS.md Framework** (required):
   - Authority Hierarchy (DAG) defining coordinator/agent relationships
   - Agent Deployment Framework for task→agent matching

2. **Memory Bank Architecture** (recommended):
   - `Memory/activeContext.md` for session continuity
   - Decision logging integration

3. **agent-fabricator** (recommended):
   - Deployed when capability gaps detected (no agent scores >4)
   - If unavailable, panels limited to existing agent library

### Install from Marketplace

```bash
# Add marketplace
/plugin marketplace add pknull/asha-marketplace

# Install panel plugin
/plugin install panel@asha-marketplace

# Restart Claude Code to load plugin
```

### Verify Installation

```bash
/plugin list
# Should show: ✔ panel · Installed

/panel
# Should display panel help and usage examples
```

---

## Usage Examples

### Technical Topics

```bash
/panel Should we use PostgreSQL or MongoDB for the time-series data
/panel Review authentication system for OWASP Top 10 vulnerabilities
/panel Assess microservices architecture for performance bottlenecks
```

**Auto-selected characters**: Asha, Adversary, Recruiter, Architect, Engineer (technical focus)
**Decision rule**: Consensus (unanimous for security topics)

### Creative Topics

```bash
/panel Evaluate manuscript Chapter 9 prose quality and AI contamination
/panel Does the magic system maintain internal consistency across chapters
/panel Character arc analysis for protagonist in Act 2
```

**Auto-selected characters**: Asha, Adversary, Recruiter, Curator, Architect (craft focus)
**Decision rule**: Consensus

### Research-Heavy Topics

```bash
/panel What are best practices for podcast transcription with speaker diarization
/panel Historical accuracy of 1895 Laramie setting in current draft
```

**Auto-selected characters**: Asha, Adversary, Recruiter, Archivist, Architect (research focus)
**Archivist receives**: research-assistant agent (10/10 score for external research)

### Ethical/Values Topics

```bash
/panel Does our AI moderation system disproportionately affect marginalized users
/panel Privacy implications of storing user voice recordings
```

**Auto-selected characters**: Asha, Adversary, Recruiter, Ethicist, Architect (ethics focus)
**Decision rule**: Consensus with Ethicist weight

---

## Character Profiles

Each character has a documented profile in `plugins/panel/docs/characters/`:
- Nature and essence
- Analytical approach and key questions
- Capability requirements (used by Recruiter for agent scoring)
- Role in panel sessions
- Voice examples

Character files used by Phase 2 (Workforce Assignment) to match agents to capability requirements.

---

## Decision Report Output

Every panel produces a structured report:

- Topic (with Phase 0 clarifications if applicable)
- Inferred Goals
- Decision Rule
- Character Selection Rationale
- Participation Declarations (active + abstentions)
- Workforce Assignments (Recruiter's agent allocation table)
- Infrastructure Comparison
- Character Briefs (Initial Positions with evidence)
- Cross-Examination Findings
- Research Findings (if Research Gate activated)
- Confidence Summary
- Reflection Round Summary
- Synthesis (options/tradeoffs)
- Decision
- Next Steps (owners, deliverables, due dates)

Reports saved to: `Work/meetings/YYYY-MM-DD--panel--<slug>.md`

---

## Dynamic Agent Assignment

**How it works**:

1. **Recruiter analyzes topic** (Phase 2) - Determines if research-heavy, implementation-focused, creative, technical, etc.
2. **Reads character capability requirements** - From character profile files
3. **Scores 239-agent library** - 0-10 match for each character's needs
4. **Assigns optimal agents** - 0-N per character based on complexity
5. **Deploys agent-fabricator** - If gaps detected (no agent scores >4)

**Examples**:
- Simple topic: The Architect operates agentless (direct analytical perspective)
- Research topic: The Archivist receives `research-assistant` (10/10 score)
- Complex topic: The Curator receives `prose-analysis` + `narrator` + `intimacy-designer`

Same character, different tools based on problem context.

---

## Troubleshooting

### Panel command not found

**Solution**: Restart Claude Code after installation. Plugins load on startup.

### "Panel character file not found"

**Issue**: Missing character profiles in `plugins/panel/docs/characters/`
**Solution**: Ensure all 8 character files exist (Asha.md, The Adversary.md, etc.) and _TEMPLATE.md

### Agent assignment failures

**Issue**: Recruiter cannot find agents in `.claude/agents/`
**Solution**: Verify 239-agent library exists or reduce dependency on specialized agents

### agent-fabricator not available

**Issue**: Capability gaps cannot be filled with new agents
**Solution**: Panel proceeds with existing agents only. Recommendation: Install agent-fabricator in host project.

---

## Version History

### 4.0.0 (2025-11-08)
- **Simplified invocation**: `/panel <topic>` only (removed flags, YAML, profiles, rosters)
- **Automatic character selection**: Phase -1 analyzes topic and selects relevant characters
- **Removed modes**: No more inworld/outworld distinction
- **Removed `/panel-use`**: Single command interface
- **Updated protocol**: Phase -1 (Topic Analysis) now first step
- **Version bump**: Major breaking change (v3 → v4)

### 3.0.2 (2025-11-08)
- Added character validation script
- Fixed plugin.json validation errors
- Restructured marketplace with plugins/ subdirectory

### 3.0.0 (Initial Release)
- 11-phase protocol with manual composition
- Profile/roster system
- Inworld/outworld modes
- Dynamic agent assignment via Recruiter

---

## License

MIT License - See LICENSE file for details

## Support

- **Issues**: https://github.com/pknull/asha-marketplace/issues
- **Documentation**: `plugins/panel/docs/PANEL_PROTOCOL.md`
- **Character Files**: `plugins/panel/docs/characters/`

---

## Pattern Implementation

Based on CSIRO Agent Design Patterns (Liu et al. 2025):
- **Passive Goal Creator** (Phase 0): Clarifies ambiguous topics
- **Role-Based Cooperation**: Fixed character assignments with hierarchical workflow
- **Debate-Based Cooperation**: Cross-Examination enables argument exchange
- **Self-Reflection**: Reflection Round allows position revision
- **Cross-Reflection**: Characters review each other's arguments
- **Human Reflection**: Decision Report enables user contestability

**Reference**: Liu et al. (2025). "Agent design pattern catalogue: A collection of architectural patterns for foundation model based agents." *The Journal of Systems and Software* 220, 112278.
