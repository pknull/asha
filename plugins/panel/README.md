# Claude Code Panel Plugin

**Version**: 5.0.0
**Plugin Name**: `panel-system`
**Command**: `/panel`
**Description**: Dynamic multi-perspective analysis with 3 core roles + recruited specialists

Just type `/panel <your topic>` and the system automatically:

- The Analyst analyzes topic and recruits 2-5 specialist agents from available library
- Assigns specialists with evocative session-specific names
- The Challenger argues against proposals and demands proof of necessity
- The Moderator moderates and produces comprehensive Decision Report
- Executes structured 11-phase decision protocol

---

## Quick Start

```bash
/panel How do we pimp fish
/panel Should we implement GraphQL or REST for the new API
/panel Evaluate Chapter 9's horror-erotica effectiveness without camp
```

**That's it.** No flags, no YAML, no manual configuration.

### Interview Mode

```bash
/panel --interview Build a task management API
/panel --interview "Create a CLI tool for file organization"
```

Interview mode runs Socratic Q&A to crystallize vague ideas into validated specifications:

1. **The Questioner** asks clarifying questions via AskUserQuestion
2. **The Examiner** validates problem framing (essence, root cause, assumptions)
3. **The Codifier** generates `seed.yaml` specification

Output: `Work/panels/<id>/seed.yaml`

---

## What is the Panel System?

The panel system enables automatic expert analysis by:

1. **Topic analysis** (Phase -1) - The Analyst determines needed expertise and recruits 2-5 specialists from available agent library with session-specific names
2. **Structured inquiry** (Phases 1-8) - Framing, cross-examination, research gate, reflection, synthesis, decision
3. **Decision Report** - Evidence citations, confidence scoring, dissent notation, actionable next steps

**Use cases**: System design, security review, narrative craft, strategic decisions, feasibility analysis, ethical assessment, architecture

---

## Panel Architecture

### Core Roles (Always Present)

**The Moderator** (Moderator/Facilitator)

- Manages 11-phase protocol execution
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

### Dynamic Panelists (Recruited Per Topic)

The Analyst assigns agents from `.claude/agents/*.md` with **evocative session-specific names** based on topic context.

**Examples by Topic Type**:

**Creative Writing Panel** (Callum Chapter 9 evaluation):

- `prose-analysis` → **"The Editor"** (craft assessment)
- `intimacy-arbiter` → **"The Architect of Dread"** (genre mechanics)
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

**Session-Specific Naming**: Same agent becomes different "character" depending on context:

- `prose-analysis` → "The Editor" (creative), "The Code Reviewer" (technical), "The Stylist" (marketing)
- `research-assistant` → "The Archivist" (historical), "The Evidence Gatherer" (legal), "The Data Scout" (analytics)

---

## 11-Phase Protocol

**Phase -1: Topic Analysis & Workforce Recruitment** (The Analyst)

- Analyze topic domain and determine required expertise (2-5 domains typical)
- Score available agent library (0-10) for capability match
- Assign specialists with session-specific names
- Deploy fabricator if gaps detected
- Set decision rule (consensus default, unanimous for security)

**Phase 0: Goal Clarification** (The Moderator)

- Request clarification if topic ambiguous
- Skip if well-specified

**Phase 1: Framing** (The Moderator)

- State topic, goals, decision rule
- Introduce panel composition (core roles + recruited specialists)

**Phase 2: Infrastructure Check** (The Moderator)

- Compare against existing assets to avoid duplication

**Phase 3: Initial Positions** (All Panelists)

- Specialists deploy assigned agents to gather evidence
- The Challenger takes opposition stance: "DON'T do this because..." and demands proof
- Present 5-bullet briefs with citations

**Phase 4: Cross-Examination** (The Challenger-led)

- Challenge assumptions, find contradictions
- The Analyst may assign additional agents if gaps revealed

**Phase 5: Research Gate** (The Moderator)

- Authorize additional research if evidence gaps exist
- Enforce confidence scoring (≥0.8 high confidence)

**Phase 6: Reflection Round** (All Panelists)

- Revise positions based on evidence and challenges

**Phase 7: Synthesis** (Recruited Architect or The Moderator)

- Structure viable options with tradeoffs

**Phase 8: Decision** (The Moderator)

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

3. **fabricator** (recommended):
   - Deployed when capability gaps detected (no agent scores >4)
   - If unavailable, panels limited to existing agent library

### Install from Marketplace

```bash
# Install the panel plugin (all plugins: ./install.sh with no --only)
./install.sh --only panel

# Restart Claude Code to load plugin
```

### Verify Installation

```bash
/plugin list
# Should show: ✔ panel-system · Installed

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

**Recruited specialists**: Systems Designer, Security Auditor, Performance Engineer (topic-specific)
**Decision rule**: Consensus (unanimous for security topics)

### Creative Topics

```bash
/panel Evaluate manuscript Chapter 9 prose quality and AI contamination
/panel Does the magic system maintain internal consistency across chapters
/panel Character arc analysis for protagonist in Act 2
```

**Recruited specialists**: The Editor, The Worldbuilder, The Character Psychologist (craft focus)
**Decision rule**: Consensus

### Research-Heavy Topics

```bash
/panel What are best practices for podcast transcription with speaker diarization
/panel Historical accuracy of 1895 Laramie setting in current draft
```

**Recruited specialists**: The Evidence Gatherer, The Technical Analyst, The Historical Consultant (research focus)

### Ethical/Values Topics

```bash
/panel Does our AI moderation system disproportionately affect marginalized users
/panel Privacy implications of storing user voice recordings
```

**Recruited specialists**: The Ethicist, The Privacy Analyst, The Bias Detector (ethics focus)
**Decision rule**: Consensus with ethics weight

---

## Character Profiles

Core roles have documented profiles in `plugins/panel/docs/characters/`:

- **The Moderator.md** - Moderator/Facilitator
- **The Analyst.md** - Workforce Intelligence
- **The Challenger.md** - Opposition & Quality Gate

Recruited specialists are documented in `.claude/agents/*.md` (agent count varies by host project).

---

## Decision Report Output

Every panel produces a structured report:

- Topic (with Phase 0 clarifications if applicable)
- Inferred Goals
- Decision Rule
- Panel Composition (core roles + recruited specialists with session names)
- Recruitment Rationale
- Infrastructure Comparison
- Expert Briefs (Initial Positions with evidence)
- Cross-Examination Findings
- Research Findings (if Research Gate activated)
- Confidence Summary
- Reflection Round Summary
- Synthesis (options/tradeoffs)
- Decision
- Next Steps (owners, deliverables, due dates)

Reports saved to: `Work/meetings/YYYY-MM-DD--panel--<slug>.md`

---

## Dynamic Agent Recruitment

**How it works**:

1. **The Analyst analyzes topic** (Phase -1) - Determines if research-heavy, implementation-focused, creative, technical, etc.
2. **Scores available agent library** - 0-10 match for topic expertise needs
3. **Assigns specialists with session names** - 2-5 agents typical, given evocative contextual names
4. **Deploys fabricator** - If gaps detected (no agent scores >4)

**Examples**:

- Creative panel: `prose-analysis` becomes "The Editor", `intimacy-arbiter` becomes "The Architect of Dread"
- Technical panel: `architect` becomes "The Systems Designer", `ml-engineer` becomes "The Model Capability Analyst"
- Culinary panel: `research-assistant` becomes "The Culinary Historian", `trend-analyst` becomes "The Flavor Prophet"

Same agent, different contextual identity based on topic domain.

---

## Troubleshooting

### Panel command not found

**Solution**: Restart Claude Code after installation. Plugins load on startup.

### "Panel character file not found"

**Issue**: Missing core character profiles in `plugins/panel/docs/characters/`
**Solution**: Ensure 3 core files exist (The Moderator.md, The Challenger.md, The Analyst.md)

### Agent assignment failures

**Issue**: The Analyst cannot find agents in `.claude/agents/`
**Solution**: Verify agent library exists in host project (`.claude/agents/*.md`)

### fabricator missing from install

**Issue**: Capability gaps cannot be filled with new agents
**Solution**: Panel proceeds with existing agents only. Recommendation: reinstall asha (`./install.sh --only panel`) — the fabricator ships with the panel plugin.

---

## Version History

### 4.1.2 (2025-11-17)

- **BREAKING**: Renamed plugin from `"panel"` to `"panel-system"` to resolve namespace conflict
- **Fix**: `/panel` command now works as short form (no longer requires `/panel:panel`)
- **Root cause**: Plugin name matching command name created ambiguous namespace
- **Installation**: Use `./install.sh`
- **Migration**: Uninstall old `panel` plugin, reinstall as `panel-system`

### 4.1.1 (2025-11-17) - DEPRECATED

- Attempted fix using directory-based command discovery (did not resolve issue)
- Superseded by v4.1.2 plugin rename approach

### 4.1.0 (2025-11-08)

- **Dynamic recruitment architecture**: 3 core roles + recruited specialists with session-specific names
- **Removed static characters**: Deleted 5 universal character profiles (Architect, Archivist, Curator, Engineer, Ethicist)
- **Retained core roles**: The Moderator (facilitation), The Analyst (workforce), The Challenger (opposition)
- **Session-specific naming**: Agents given evocative contextual names per topic
- **Simplified protocol**: Removed Phase 1 (Participation Declaration - no abstentions needed)
- **Quality restoration**: Returns panel depth and domain expertise from v3.0 architecture

### 4.0.0 (2025-11-08)

- **Simplified invocation**: `/panel <topic>` only (removed flags, YAML, profiles, rosters)
- **8 universal characters**: Static analytical perspectives with automatic selection
- **Removed modes**: No more inworld/outworld distinction
- **Removed `/panel-use`**: Single command interface
- **Quality regression**: Generic analysis without deep domain expertise (fixed in v4.1)

### 3.0.2 (2025-11-08)

- Added character validation script
- Fixed plugin.json validation errors
- Restructured marketplace with plugins/ subdirectory

### 3.0.0 (Initial Release)

- 11-phase protocol with manual composition
- Profile/roster system
- Inworld/outworld modes
- Dynamic agent assignment via The Analyst

---

## License

MIT License - See LICENSE file for details

## Support

- **Issues**: https://github.com/pknull/asha/issues
- **Documentation**: `plugins/panel/docs/PANEL_PROTOCOL.md`
- **Character Files**: `plugins/panel/docs/characters/`

---

## Pattern Implementation

Based on CSIRO Agent Design Patterns (Liu et al. 2025):

- **Passive Goal Creator** (Phase 0): Clarifies ambiguous topics
- **Role-Based Cooperation**: Core roles with hierarchical workflow
- **Debate-Based Cooperation**: Cross-Examination enables argument exchange
- **Self-Reflection**: Reflection Round allows position revision
- **Cross-Reflection**: Specialists review each other's arguments
- **Human Reflection**: Decision Report enables user contestability

**Reference**: Liu et al. (2025). "Agent design pattern catalogue: A collection of architectural patterns for foundation model based agents." *The Journal of Systems and Software* 220, 112278.
