# Claude Code Panel Plugin

**Version**: 3.0.2
**Description**: Panel system for structured multi-perspective analysis with dynamic agent assignment

Convenes 8 universal panel characters (Asha, The Adversary, The Recruiter, The Architect, The Archivist, The Engineer, The Ethicist, The Curator) for systematic inquiry across 11 phases with automatic expert agent deployment.

---

## What is the Panel System?

The panel system enables Claude Code to conduct structured expert analysis by:

1. **Convening specialized characters** with distinct analytical perspectives (challenge, synthesis, evidence, implementation, ethics, craft)
2. **Dynamically assigning expert agents** as tools to characters based on problem complexity (The Recruiter orchestrates 239-agent ecosystem)
3. **Executing 11-phase protocol** from Goal Clarification → Workforce Assignment → Cross-Examination → Research Gate → Synthesis → Decision
4. **Producing structured Decision Reports** with evidence citations, confidence scoring, and dissent notation

**Use cases**: System design, security review, narrative craft evaluation, strategic decisions, implementation feasibility analysis, ethical assessment, technical architecture

---

## Installation

### Prerequisites

This plugin assumes your Claude Code project uses:

1. **AGENTS.md Framework** (required):
   - Authority Hierarchy (DAG) defining coordinator/agent relationships
   - Mode Selection (Symbolic Dominance vs Technical Precision)
   - Agent Deployment Framework for task→agent matching

2. **Memory Bank Architecture** (recommended):
   - `Memory/activeContext.md` for session continuity
   - `Memory/projectbrief.md` for project scope
   - Decision logging integration

3. **Session Watching** (optional):
   - `Work/sessions/` directory for panel transcript archival
   - Enables crash recovery and systematic documentation

4. **agent-fabricator** (recommended):
   - The Recruiter deploys `agent-fabricator` when capability gaps detected (no agent scores >4)
   - If unavailable, panels limited to existing 239-agent library

### Install from GitHub

```bash
# Install plugin
/plugin install pknull/claude-code-panel

# Verify installation
/panel
```

### Manual Installation

```bash
# Clone or download this repository
git clone https://github.com/pknull/claude-code-panel.git

# Add to Claude Code plugins directory
cp -r claude-code-panel ~/.claude/plugins/

# Or configure as custom marketplace in settings.json:
{
  "extraKnownMarketplaces": [
    {
      "name": "custom",
      "url": "https://github.com/pknull"
    }
  ]
}
```

---

## Quick Start

### Basic Panel (Manual Composition)

```bash
/panel

# You'll be prompted for:
# - Topic (one-line problem statement)
# - Goals (2-4 objectives)
# - Which characters to include (defaults: Asha, Adversary, Recruiter, Architect)
```

### Profile-Based Panel

```bash
# Use pre-configured panel profile
/panel-use system_design --topic "Review microservices API" --mode out_of_world

# Available profiles (see docs/profiles.md):
# - world_consistency (creative)
# - narrative_craft (creative)
# - character_evaluation (creative)
# - system_design (software)
# - security_review (software)
# - code_quality (software)
```

### Roster-Based Panel

```bash
# Use pre-configured character roster
/panel-use --roster architecture --topic "GraphQL schema design"

# Available rosters (see docs/rosters.md):
# - core (creative): 7 characters
# - architecture (software): 5 characters
# - security (software): 5 characters
# - minimal (utility): 3 characters
```

### Universal Composition (All 8 Characters with Abstention)

```bash
/panel

# When prompted, set:
composition: universal

# All 8 characters invited
# Phase 1: Characters self-select or abstain with rationale
# Expected: 5-6 active participants (20-30% abstention)
# Token cost: 35-50k (vs 30-40k manual)
```

---

## Panel Characters

| Character | Analytical Question | Domain |
|-----------|-------------------|---------|
| **Asha** | "What is the PROCESS?" | Moderator + Archive Guardian (always present) |
| **The Adversary** | "Is this WRONG/broken/contradictory?" | Challenge & Quality Gate |
| **The Recruiter** | "Who has CAPABILITY?" | Workforce Intelligence (agent assignment) |
| **The Architect** | "What are the OPTIONS?" | Structure & Synthesis |
| **The Archivist** | "What does EVIDENCE say?" | Research & Sources |
| **The Engineer** | "Can we BUILD this?" | Implementation Excellence |
| **The Ethicist** | "Who is AFFECTED?" | Values & Cultural Integrity |
| **The Curator** | "Does this SERVE its purpose?" | Aesthetic Fitness (Creative Specialist) |

**Note**: Characters are persistent analytical perspectives with consistent voices. Agents are technical tools The Recruiter assigns dynamically based on panel topic.

---

## Protocol Overview (11 Phases)

### Phase 0: Goal Clarification
Asha analyzes topic for ambiguity, requests clarification if needed

### Phase 1: Participation Declaration (Universal Mode Only)
Characters assess relevance, declare participation or abstain
**Format**: `ABSTENTION: [1-sentence rationale]`
**Re-entry**: Permitted at Phase 7 (Research Gate)

### Phase 2: Workforce Assignment
**The Recruiter orchestrates all agent deployment**:
1. Reads character Capability Requirements
2. Analyzes panel topic context (research-heavy? implementation? creative?)
3. Searches 239-agent library systematically
4. Scores agents 0-10 for each character's capability needs
5. Assigns optimal agents (0-N per character based on complexity)
6. Deploys `agent-fabricator` if gaps detected (no agent scores >4)

**Output**: Workforce allocation table with scores and rationale

### Phase 3: Framing
Asha introduces panel, references Recruiter's workforce assignments

### Phase 4: Infrastructure Check
Compares proposals against existing infrastructure (avoid duplication)

### Phase 5: Initial Positions
Characters deploy assigned agents, present 5-bullet briefs with evidence

### Phase 6: Cross-Examination
The Adversary challenges assumptions, characters may update briefs

### Phase 7: Research Gate
If evidence gaps detected, authorize additional research via agents

### Phase 8: Reflection Round
Characters revise positions based on Cross-Examination and Research Gate

### Phase 9: Synthesis
The Architect enumerates options with explicit tradeoffs

### Phase 10: Decision
Asha applies decision rule, records dissent, lists Next Steps

---

## Dynamic Agent Assignment

**Traditional Approach (v2.0 - deprecated)**:
- Static Tier 1 defaults assigned to all characters
- Tier 2 specialized agents added manually

**Dynamic Approach (v3.0+)**:
- **The Recruiter assigns ALL agents** based on topic analysis
- **No static defaults** - assignments vary by panel topic
- **0-10 scoring system** evaluates 239-agent library systematically
- **0-N agents per character** (simple topics: agentless, complex: 2-3+ agents)
- **Gap detection** triggers `agent-fabricator` for new agent creation

**Example**:

| Panel Topic | Character | Assigned Agents (Score) | Rationale |
|-------------|-----------|------------------------|-----------|
| **Simple**: Prose quality audit | The Curator | prose-analysis (10/10) | Prose optimization perfect match |
| **Simple**: Prose quality audit | The Architect | agentless | Direct synthesis capability sufficient |
| **Complex**: Multi-modal narrative | The Curator | prose-analysis (10/10)<br>narrator (9/10)<br>intimacy-designer (10/10) | Comprehensive craft coverage |
| **Research**: Historical accuracy | The Archivist | research-assistant (10/10) | External research required |
| **Gap Detected**: Novel capability | [Any Character] | agent-fabricator → new agent | No existing agent scores >4 |

---

## Composition Modes

### Manual Composition (Default)

**When to Use**:
- Single-domain topics (technical, creative, implementation)
- Clear expertise requirements
- Efficiency priority

**Configuration**:
```yaml
composition: "manual"  # default
characters:
  - Asha              # Always present
  - The Adversary     # Default (challenge)
  - The Recruiter     # Default (workforce)
  - The Architect     # Default (synthesis)
  # Add domain-specific:
  - The Curator       # Creative craft
  - The Ethicist      # Value-heavy decisions
```

**Token Cost**: 30-40k tokens

---

### Universal Invitation (All 8 with Abstention)

**When to Use**:
- Multi-domain topics
- Strategic decisions with unclear composition needs
- High-stakes decisions justifying comprehensive evaluation

**Configuration**:
```yaml
composition: "universal"
# No characters array - all 8 invited automatically
```

**Process**:
1. All 8 characters invited
2. **Phase 1**: Self-selection or abstention with rationale
3. Phase 2+: Proceeds with active participants (≥3 required)

**Expected Participation**: 5-6 active (20-30% abstention typical)
**Token Cost**: 35-50k tokens (5-15k overhead vs manual)

---

## Modes

### inworld (default)
Panel convenes at **Redacted1194** (liminal space, white ring at cosmic center)

**RP Semantics**:
- **Parentheses ()**: Actions/Thoughts → `(Asha gestures toward The Architect)`
- **Brackets []**: Meta-Instructions → `[Research Gate: run Perplexity on X]`
- **Plain text**: Dialogue → Character voices in direct speech

**Diegetic Tool Boundary**: Bracketed meta authorizes tool calls, maintains scene integrity

### out_of_world (fallback)
Protocol-first; no scene transcript. Explicitly request if needed.

---

## Files Included

```
claude-code-panel/
├── .claude-plugin/
│   └── plugin.json              # Plugin metadata
├── commands/
│   ├── panel.md                 # /panel slash command
│   └── panel-use.md             # /panel-use profile invocation
├── agents/
│   └── recruiter.md             # The Recruiter (workforce assignment)
├── docs/
│   ├── characters/              # 8 character files
│   │   ├── Asha.md
│   │   ├── The Adversary.md
│   │   ├── The Recruiter.md
│   │   ├── The Architect.md
│   │   ├── The Archivist.md
│   │   ├── The Engineer.md
│   │   ├── The Ethicist.md
│   │   └── The Curator.md
│   ├── rosters.md               # Pre-configured character compositions
│   └── profiles.md              # Pre-configured panel specifications
└── README.md                    # This file
```

---

## Prerequisites Documentation

### AGENTS.md Framework Required

Your project needs an `AGENTS.md` file with these sections:

```markdown
## Authority Hierarchy (DAG)

Level 0 (User) → Absolute authority
Level 1 (Coordinator) → Session coordinator
Level 2+ (Agents) → Specialized agents

## Agent Deployment Framework

Deployment criteria, parallel vs sequential modes, conflict resolution
```

**Template**: See [AGENTS.md example](https://github.com/pknull/claude-code-panel/blob/main/docs/AGENTS.md.template)

### Memory Bank Integration (Recommended)

Panel integrates with Memory Bank if available:

- **Decision Logging**: Panel decisions appended to `Memory/activeContext.md`
- **Session Continuity**: References project context from Memory files
- **Optional**: Panel works standalone without Memory integration

### agent-fabricator Integration (Recommended)

The Recruiter deploys `agent-fabricator` when:
- No agent scores >4 for required capability
- Panel topic requires novel expertise not in 239-agent library

**If unavailable**: Panel proceeds with existing agents only (reduced capability coverage)

---

## Usage Examples

### Example 1: GraphQL Security Review

```bash
/panel-use security --topic "Evaluate GraphQL API authentication middleware"

# Automatic roster: Asha, Adversary, Recruiter, Architect, Ethicist
# Phase 2 (Workforce Assignment):
#   - The Adversary: security-auditor (10/10)
#   - The Architect: api-designer (9/10)
#   - The Ethicist: zero-trust-strategist (8/10)
# Decision Report includes security threat model + mitigation recommendations
```

### Example 2: Narrative Craft Evaluation

```bash
/panel-use narrative_craft --topic "Assess Chapter 9 horror effectiveness"

# Automatic roster: Asha, Adversary, Curator, Architect, Ethicist
# Phase 2 (Workforce Assignment):
#   - The Curator: prose-analysis (10/10), narrator (9/10), intimacy-designer (10/10)
#   - The Adversary: prose-analysis (8/10)
# Decision Report includes craft mechanics analysis + revision recommendations
```

### Example 3: System Architecture Decision

```bash
/panel

Topic: "Choose between microservices vs monolith for payment processing"
Goals:
  - Evaluate scalability tradeoffs
  - Assess operational complexity
  - Consider team capability constraints
Composition: manual
Characters:
  - Asha
  - The Adversary
  - The Recruiter
  - The Architect
  - The Engineer

# Phase 2 (Workforce Assignment):
#   - The Architect: cloud-architect (10/10), microservices-architect (9/10)
#   - The Engineer: backend-developer (9/10), devops-engineer (8/10)
#   - The Adversary: code-skeptic (10/10)
# Decision Report includes architecture comparison matrix with explicit tradeoffs
```

---

## Agent Library Reference

The Recruiter has access to **239 specialized agents** across categories:

**Creative Writing & TTRPG** (14):
- narrator, character-developer, world-builder, narrative-architect, scene-composer, writer, intimacy-designer, prose-analysis, editor, roleplay-gm, research-assistant, external-llm, task-manager, sequential-thinker

**Software Engineering** (223):
- Architecture: architect, cloud-architect, microservices-architect, graphql-architect, api-designer, bff-engineer
- Development: full-stack-developer, frontend-developer, backend-developer, python-developer, javascript-pro, typescript-pro, rust-engineer, golang-pro, java-architect, csharp-developer
- Infrastructure: devops-architect, platform-engineer, site-readiness-engineer, kubernetes-specialist, terraform-engineer, database-administrator
- Security: security-auditor, cybersecurity-expert, penetration-tester, zero-trust-strategist
- Quality: code-reviewer, qa-expert, test-automator, tdd, code-skeptic
- Specialized: ml-engineer, mlops-engineer, ai-engineer, data-scientist, performance-engineer
- [See full inventory](docs/agent-inventory.md)

**Panel Infrastructure** (1):
- recruiter (bundled with plugin)

---

## Decision Report Format

Every panel produces a structured Decision Report with:

1. **Topic** (refined from Phase 0 if applicable)
2. **Goals** (2-4 objectives)
3. **Decision Rule** (consensus | majority | weighted | unanimous)
4. **Composition Mode** (manual | universal)
5. **Panel Composition Rationale** (which characters, why selected)
6. **Participation Declarations** (Phase 1 - if universal: active + abstentions)
7. **Workforce Assignments** (Phase 2 - Recruiter's agent allocation table)
8. **Existing Infrastructure Comparison** (Phase 4 - avoid duplication)
9. **Character Briefs** (Phase 5 - Initial Positions with agent evidence)
10. **Cross-Examination Findings** (Phase 6 - challenges, contradictions)
11. **Research Findings** (Phase 7 - sources if Research Gate activated)
12. **Confidence Summary** (Relevance, Completeness, Confidence Score, Threshold)
13. **Reflection Round Summary** (Phase 8 - revised positions, convergence)
14. **Synthesis** (Phase 9 - options/tradeoffs from The Architect)
15. **Decision** (Phase 10 - final recommendation with dissent notation)
16. **Next Steps** (owner, deliverable, due)

Reports saved to `Work/meetings/YYYY-MM-DD--panel--<slug>.md` (if session watching enabled)

---

## Version History

### v3.0.2 (2025-11-08)
- Added universal composition mode with Phase 1 abstention protocol
- Clean 11-phase numbering (0-10, no fractional phases)
- Proof-of-concept: Panel composition meta-panel demonstrated 7/8 participation

### v3.0.1 (2025-11-07)
- Replaced static Tier 1/Tier 2 with dynamic Recruiter-orchestrated assignment
- 0-10 scoring system for 239-agent library
- Gap detection triggers agent-fabricator deployment

### v3.0.0 (2025-11-06)
- Migrated from 3-layer (Characters → Tier 1 → Tier 2) to 2-layer (Characters → Agents)
- Characters as persistent analytical perspectives, agents as dynamic tools
- The Recruiter character introduced for workforce intelligence

### v2.0 (2024)
- Static 3-layer architecture (deprecated)

---

## Troubleshooting

### "Command /panel not found"

**Solution**: Verify plugin installation
```bash
/plugin list
# Should show: claude-code-panel

# If not installed:
/plugin install pknull/claude-code-panel
```

### "agent-fabricator not found" during Phase 2

**Impact**: The Recruiter cannot create new agents for capability gaps
**Solution**:
1. Install agent-fabricator agent in host project (`.claude/agents/agent-fabricator.md`)
2. Or accept reduced capability coverage (panels limited to existing 239 agents)

### "AGENTS.md framework not found"

**Impact**: Panel cannot execute (missing operational framework)
**Solution**: Create `AGENTS.md` with Authority Hierarchy and Agent Deployment protocols
**Template**: See [AGENTS.md.template](docs/AGENTS.md.template)

### "Memory integration failed"

**Impact**: Decision logging disabled, no session continuity
**Solution**:
1. Panel works standalone (decisions logged to stdout only)
2. Or create Memory Bank structure (`Memory/activeContext.md`, `Memory/projectbrief.md`)

### Panel token usage exceeds budget

**Solution**:
1. Use **manual composition** instead of universal (30-40k vs 35-50k)
2. Reduce character count (minimal roster: 3 characters)
3. Set explicit token limits in Claude Code settings

---

## Contributing

Issues and PRs welcome at: https://github.com/pknull/claude-code-panel

---

## License

MIT License - See LICENSE file

---

## References

**CSIRO Agent Design Patterns**:
Liu et al. (2025). "Agent design pattern catalogue: A collection of architectural patterns for foundation model based agents." *The Journal of Systems and Software* 220, 112278.

**Anthropic Claude Code**:
https://docs.claude.com/claude-code

**Panel System Documentation**:
Comprehensive protocol details in `docs/` directory
