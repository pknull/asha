# Panel System Architecture

The Panel is a multi-agent consensus framework for structured decision-making, quality analysis, and workforce planning. This document explains how the system components interoperate.

---

## Simplified Architecture (2025-11-08)

**Core Concept**: Small set of persistent character identities (ur-personalities) that deploy agents as research/execution tools via two-tier protocol.

```
Asha (Moderator)     ← Session coordinator + Panel facilitator
   ↓
Universal Characters ← 6 persistent analytical perspectives
   ↓
Two-Tier Protocol    ← Characters deploy agents as tools
   ↓
239 Agent Library    ← Specialized technical capabilities
```

---

## Composition Modes (v3.0)

**Two Operational Patterns for Panel Formation**:

### Manual Composition (Default)

**When to Use**:
- Single-domain topics with clear expertise requirements
- Routine decisions where character selection is obvious
- Efficiency priority (30-40k tokens)

**How It Works**:
User or Asha selects 3-5 characters based on topic needs.

**Examples**:
- GraphQL API review → Adversary, Architect, Engineer, Ethicist (4 characters, technical focus)
- Prose quality audit → Adversary, Curator, Architect (3 characters, craft focus)

### Universal Invitation (All 8 Characters with Abstention)

**When to Use**:
- Multi-domain topics requiring comprehensive coverage
- Strategic decisions with unclear composition needs
- High-stakes decisions justifying thorough evaluation

**How It Works**:
1. All 8 characters invited automatically
2. **Phase 1**: Characters declare participation or abstain with rationale
3. Phase 2+ proceeds with active participants only (≥3 required)

**Abstention Protocol**:
- Format: `ABSTENTION: [1-sentence domain mismatch rationale]`
- Timing: Phase 1 (before Workforce Assignment)
- Re-entry permitted during Phase 7 (Research Gate) if relevance emerges
- Expected participation: 5-6 active characters (20-30% abstention typical)

**Token Overhead**: 35-50k tokens (5-15k overhead vs manual)

**Proof-of-Concept**: Panel composition architecture meta-panel (2025-11-08) demonstrated universal pattern successfully with 7/8 participation (The Curator abstained, process architecture outside craft domain).

### Selection Guidance

| Factor | Manual | Universal |
|--------|--------|-----------|
| **Topic** | Single-domain, clear | Multi-domain, ambiguous |
| **Priority** | Efficiency, speed | Comprehensive coverage |
| **Token Budget** | 30-40k | 35-50k |
| **Use Case** | Routine panels | Strategic decisions |

**PanelSpec Parameter**:
```yaml
composition: "manual"   # default - specify characters list
composition: "universal" # all 8 invited, Phase 1 abstention
```

---

## Universal Characters

**Total Panel Members**: Asha + 7 characters (8 total)

**Asha** (Archive Guardian + Moderator):
- **Role**: Session coordination, process facilitation, decision rule application
- **Always Present**: Asha runs panel sessions, enforces constraints, formalizes consensus
- **Character File**: `Panel/characters/Asha.md`

**The Adversary** (Challenge & Quality Gate):
- **Role**: Stress-testing, assumption interrogation, failure mode identification
- **Question**: "Is this WRONG/broken/contradictory?"
- **Character File**: `Panel/characters/The Adversary.md`

**The Recruiter** (Workforce Intelligence):
- **Role**: Agent deployment optimization, capability mapping, gap analysis with ROI formula
- **Character File**: `Panel/characters/The Recruiter.md`

**The Architect** (Structure & Synthesis):
- **Role**: Problem decomposition, option enumeration, synthesis phase leadership
- **Character File**: `Panel/characters/The Architect.md`

**The Archivist** (Evidence & Sources):
- **Role**: Research execution, source validation, Perplexity queries, citation management
- **Character File**: `Panel/characters/The Archivist.md`

**The Engineer** (Implementation Excellence):
- **Role**: "How do we BUILD this?" - execution quality, technical debt, maintainability
- **Character File**: `Panel/characters/The Engineer.md`

**The Ethicist** (Values & Cultural Integrity):
- **Role**: Stakeholder advocacy, value conflicts, **cultural appropriation detection** (PRIMARY for REDACTED99)
- **Character File**: `Panel/characters/The Ethicist.md`
- **Special Protocol**: Mandatory Tier 1 cultural scan for REDACTED99 creative panels

**The Curator** (Aesthetic Fitness - Creative Specialist):
- **Role**: "Does this SERVE ITS PURPOSE?" - narrative necessity, craft standards, style guide enforcement
- **Question**: "Is this purposeful/necessary/earned?"
- **Character File**: `Panel/characters/The Curator.md`
- **Domain**: Creative writing panels (core, worldbuilding, prose rosters)
- **Distinction**: Complements The Adversary—Adversary finds flaws, Curator evaluates purpose

---

## Two-Tier Agent Deployment Protocol

**Key Innovation**: Characters maintain consistent personality/voice while flexing tools based on problem domain.

### Tier 1: Default Agents (Automatic Deployment)

Characters document default agents for routine needs in their character files. Deploy instantly without Recruiter intervention.

**Examples**:
- **The Adversary** → `adversary` agent (standard challenge)
- **The Architect** → `sequential-thinker` (decomposition), `architect` (system design)
- **The Archivist** → `research-assistant` (evidence gathering)
- **The Ethicist** → `research-assistant` (cultural scan, MANDATORY for REDACTED99 creative) + `narrator` (general ethics)

### Tier 2: Recruiter Search (Specialized Needs)

When character needs specialized capability beyond Tier 1 defaults:

1. Character articulates specific requirement
2. The Recruiter searches agent ecosystem (grep `.claude/agents/`)
3. The Recruiter scores candidates 0-10 based on capability match
4. The Recruiter proposes optimal agent(s)
5. Character approves deployment

**Example**: The Adversary needs specialized security assessment → requests Recruiter search → Recruiter proposes `penetration-tester` (offensive) vs `zero-trust-strategist` (defensive) with 0-10 scores → Adversary selects optimal match.

---

## Character Consistency Across Domains

**Same personalities, different tools**:

### Example 1: GraphQL API Security Review

**Phase 2: Workforce Assignment** (The Recruiter analyzes character Capability Requirements + panel topic):
- **The Adversary**: Agentless (challenges API design directly) + security-auditor (7/10) for security validation
- **The Architect**: sequential-thinker (9/10) + graphql-architect (10/10) → problem decomposition + GraphQL expertise
- **The Engineer**: typescript-pro (9/10) for implementation feasibility in TypeScript context

### Example 2: Novel Chapter Evaluation

**Phase 2: Workforce Assignment** (The Recruiter analyzes character Capability Requirements + panel topic):
- **The Adversary**: Agentless (direct critical analysis—no agent needed for challenge perspective)
- **The Curator**: prose-analysis (10/10) + narrator (9/10) → prose quality + narrative coherence
- **The Architect**: sequential-thinker (8/10) → structures evaluation findings into synthesis
- **The Ethicist**: research-assistant (10/10) cultural scan + narrator (9/10) general ethics

**Phase 5: Initial Positions** (characters deploy assigned agents to gather information, synthesize into briefs)

**Complementary Roles**: The Adversary asks "Is this WRONG?" while The Curator asks "Does this SERVE the story?" Both essential, different questions.

**Voice stays constant. Only technical tools change based on panel topic.**

---

## System Components

### `rosters.md` - Character Collections

**Purpose**: Recommended character combinations for common problem domains.

**Structure**:
```markdown
### architecture (ROSTER NAME)
Purpose: System design and architectural decisions
Characters:
- Asha (moderator)          ← Always present
- The Adversary             ← Universal (always recommended)
- The Recruiter             ← Universal (always recommended)
- The Architect             ← Domain: structure & synthesis
- The Engineer              ← Domain: implementation
```

**Key Categories**:
- **Creative Writing** (3 rosters): core, worldbuilding, prose
- **Software Engineering** (10+ rosters): architecture, security, infrastructure, performance, quality, etc.
- **Utility** (3 rosters): minimal, planning, comprehensive

**Default Members**: Asha (moderator), The Adversary, The Recruiter appear in most rosters.

**New Pattern**: Rosters list *characters*, not abstract seats. Characters deploy agents via two-tier protocol.

---

### `profiles.md` - Preconfigured Sessions

**Purpose**: Ready-to-use panel configurations with predefined topic templates, goals, constraints, and decision rules.

**Structure**:
```markdown
### system_design (PROFILE NAME)
Roster: architecture              ← References roster from rosters.md
Topic: System architecture and design decisions
Goals:
- Assess architectural soundness
- Identify integration risks
- Validate design patterns
Mode: out_of_world               ← Technical precision mode
Constraints:
- Timebox: 25 minutes
- Must reach decisive recommendation (APPROVE/REVISE/REJECT)
Decision Rule: consensus          ← How panel reaches decision
```

**Invocation**:
```bash
/panel-use system_design --topic "Assess panel_runner.py architecture"
```

**Customization**:
```bash
# Add extra goals
/panel-use system_design --topic "..." --goals "+disaster recovery validation"

# Add extra agents
/panel-use system_design --topic "..." --add "compliance-auditor"

# Change mode
/panel-use system_design --topic "..." --mode inworld
```

---

## How the Pieces Interoperate

### Workflow: From User Request → Panel Execution

**Step 1: User Invokes Profile**
```bash
/panel-use system_design --topic "Review microservices API design for Python backend"
```

**Step 2: Profile Loads Roster**
- `system_design` profile references `architecture` roster
- Roster defines characters: Asha (moderator), The Adversary, The Recruiter, The Architect, The Engineer, The Ethicist

**Step 3: Characters Deploy Agents (Two-Tier Protocol)**
- **Context Analysis**: "microservices", "API", "Python backend"
- **Tier 1 Defaults** (automatic):
  - Asha → moderator agent (session facilitation)
  - The Adversary → adversary agent (critical evaluation)
  - The Recruiter → recruiter agent (workforce optimization)
  - The Architect → sequential-thinker agent (problem decomposition)
  - The Engineer → depends on context
  - The Ethicist → research-assistant + narrator agents (ethics analysis)

- **Tier 2 Specialized** (Recruiter search with 0-10 scoring):
  - The Architect needs services expertise → Recruiter deploys `microservices-architect`
  - The Engineer needs Python expertise → Recruiter deploys `python-developer`
  - The Ethicist needs security stakeholder analysis → Recruiter may deploy `security-auditor`

**Step 4: Panel Session Executes**
- Characters receive topic, goals, constraints from profile
- Each character delivers 5-bullet brief using deployed agents as research/execution tools
- Asha facilitates cross-examination and consensus
- The Recruiter provides workforce analysis (agent deployment recommendations)
- The Adversary challenges assumptions and surfaces risks
- Final decision reached per decision rule (consensus/unanimous/majority)

---

## Architecture Diagram

```
USER REQUEST
     ↓
/panel-use system_design --topic "Review Python microservices API"
     ↓
┌─────────────────────────────────────────────────────────────┐
│ PROFILE: system_design (profiles.md)                        │
│   - Topic template: System architecture decisions           │
│   - Goals: Assess soundness, identify risks, validate       │
│   - Constraints: 25 min timebox, APPROVE/REVISE/REJECT      │
│   - Decision Rule: consensus                                 │
│   - References: ROSTER = "architecture"                      │
└─────────────────────────────────────────────────────────────┘
     ↓
┌─────────────────────────────────────────────────────────────┐
│ ROSTER: architecture (rosters.md)                           │
│   Characters: Asha (moderator), The Adversary,              │
│               The Recruiter, The Architect, The Engineer,    │
│               The Ethicist                                   │
└─────────────────────────────────────────────────────────────┘
     ↓
┌─────────────────────────────────────────────────────────────┐
│ TWO-TIER AGENT DEPLOYMENT                                   │
│                                                              │
│   Tier 1 (Automatic - Character Defaults):                  │
│     Asha → moderator                                         │
│     The Adversary → adversary                                │
│     The Recruiter → recruiter                                │
│     The Architect → sequential-thinker                       │
│     The Ethicist → research-assistant + narrator             │
│                                                              │
│   Tier 2 (Specialized - Recruiter Search):                  │
│     Context: "Python" + "microservices" + "API"              │
│     The Architect requests services expert                   │
│       → Recruiter scores: microservices-architect (9/10)     │
│     The Engineer requests Python expert                      │
│       → Recruiter scores: python-developer (10/10)           │
└─────────────────────────────────────────────────────────────┘
     ↓
┌─────────────────────────────────────────────────────────────┐
│ PANEL SESSION EXECUTION                                     │
│   - Characters use agents as tools for research/execution   │
│   - Same character voice, different technical capabilities  │
│   - Asha facilitates 8-phase protocol                       │
│   - Consensus/decision formalized per decision rule          │
└─────────────────────────────────────────────────────────────┘
```

---

## Key Design Principles

### 1. Character-Agent Separation

- **Characters**: Persistent analytical perspectives with consistent voice/personality
- **Agents**: Technical tools deployed by characters based on problem domain

**Why**: Same character maintains consistent voice while flexing technical capabilities. The Architect's analytical approach stays constant whether using `cloud-architect` or `microservices-architect` agents.

### 2. Two-Tier Agent Deployment

**Problem**: Different contexts require different expertise, but systematic deployment prevents tool sprawl.

**Solution**: Two-tier protocol balances efficiency with specialization.

**Tier 1** (Automatic): Characters have documented default agents for routine needs
- The Architect → `sequential-thinker` (decomposition)
- The Adversary → `adversary` (challenge)
- The Archivist → `research-assistant` (evidence)

**Tier 2** (Specialized): When Tier 1 insufficient, character requests Recruiter search
- The Recruiter searches 239-agent library
- Scores candidates 0-10 based on capability match
- Character approves deployment

**Example**:
- "Fix React performance issues" → The Engineer requests frontend expert → Recruiter scores `react-specialist` (10/10)
- "Fix Vue performance issues" → The Engineer requests frontend expert → Recruiter scores `vue-expert` (10/10)
- Same character, different tools, appropriate expertise

### 3. Composition Over Duplication

**Rosters** compose character collections for domains.
**Profiles** compose rosters + goals + constraints for workflows.

**Result**: 7 universal characters × 16 rosters × 12 profiles = comprehensive coverage without character file bloat.

### 4. Three Default Members

**Every panel includes**:
- **Asha (moderator)** - Session facilitation, process enforcement, decision formalization
- **The Adversary** - Quality gatekeeper, challenges inadequate reasoning
- **The Recruiter** - Workforce optimizer, identifies optimal agent deployment

**Rationale**: These three functions are required for effective panel operation regardless of domain.

---

## Common Workflows

### Workflow 1: Use Existing Profile

**Simplest approach** - leverage preconfigured session:

```bash
# Security assessment
/panel-use security_assessment --topic "Auth system vulnerability scan"

# Frontend review
/panel-use frontend_review --topic "React component architecture"

# Database schema design
/panel-use database_schema --topic "Multi-tenant data isolation strategy"
```

### Workflow 2: Customize Existing Profile

**Add goals or agents**:

```bash
# Add extra goal
/panel-use system_design --topic "..." --goals "+disaster recovery validation"

# Add specialist agent
/panel-use security_assessment --topic "..." --add "zero-trust-strategist"

# Change decision rule
/panel-use api_review --topic "..." --decision-rule unanimous
```

### Workflow 3: Direct Roster Invocation

**Use roster without profile constraints**:

```bash
# Use architecture roster with custom topic/goals
/panel topic "Evaluate monolith → microservices migration" \
  roster architecture \
  goals "Assess migration risks" "Estimate effort" \
  timebox 30
```

### Workflow 4: Ad-Hoc Panel

**Fully custom agent selection**:

```bash
/panel topic "Custom analysis requiring specific expertise" \
  experts moderator adversary recruiter custom-specialist-1 custom-specialist-2 \
  decision_rule consensus \
  timebox 25
```

---

## File Organization

```
Panel/
├── README.md                    ← This file
├── profiles.md                  ← Preconfigured sessions (12 profiles)
├── rosters.md                   ← Character collections (16 rosters)
└── characters/                  ← Character profiles (8 active)
    ├── Asha.md                  ← Archive Guardian + Panel Moderator
    ├── The Adversary.md         ← Challenge & Quality Gate
    ├── The Recruiter.md         ← Workforce Intelligence
    ├── The Architect.md         ← Structure & Synthesis
    ├── The Archivist.md         ← Evidence & Sources
    ├── The Engineer.md          ← Implementation Excellence
    ├── The Ethicist.md          ← Values & Cultural Integrity
    ├── The Curator.md           ← Aesthetic Fitness (creative specialist)
    └── archive/                 ← Consolidated character files
        ├── The Moderator.md     (merged into Asha)
        └── The Syncretist.md    (merged into The Ethicist)

.claude/agents/                  ← Technical implementations (239 agents)
├── moderator.md
├── adversary.md
├── recruiter.md
├── architect.md
├── cloud-architect.md
├── microservices-architect.md
├── sequential-thinker.md
├── research-assistant.md
└── ... (231 more agents)
```

---

## Quick Reference

| Component | Purpose | Example |
|-----------|---------|---------|
| **Character** | Persistent analytical perspective with consistent voice | `The Adversary.md` - challenge essence with clinical precision |
| **Roster** | Character collection for domain | `security` roster - Asha, Adversary, Recruiter, Architect, Ethicist |
| **Profile** | Preconfigured session with goals/constraints | `security_assessment` - 35min OWASP review, unanimous decision |
| **Agent** | Technical tool deployed by character | `penetration-tester.md` - ethical hacking specialist |
| **Two-Tier Protocol** | Agent deployment method | Tier 1 defaults + Tier 2 Recruiter search (0-10 scoring) |

---

## Decision Flow

**When should I use which approach?**

```
Is there a preconfigured profile that matches my need?
├─ YES → Use /panel-use <profile>
│         Fast, proven workflow with optimal character roster
│
└─ NO → Do I need a common roster (architecture, security, etc)?
    ├─ YES → Use /panel roster <roster-name> topic "..."
    │         Flexible, domain-appropriate characters
    │
    └─ NO → Build ad-hoc panel with /panel experts [...]
              Maximum flexibility, custom character selection
```

---

## Extension Points

### Adding New Characters

**When**: New recurring analytical perspective not covered by existing 8 characters.

**Caution**: Character addition should be rare. The 8 characters (7 universal + The Curator creative specialist) were designed for comprehensive coverage. Consider whether new capability can be addressed via Tier 2 agent deployment instead.

**Process**:
1. Create `Panel/characters/The [Name].md` using existing character as template
2. Define Nature, Analytical Approach, Role in Panel Sessions
3. Document Tier 1 default agents and Tier 2 deployment triggers
4. Specify relationship to other panel members
5. Add character to relevant rosters in `rosters.md`

### Adding New Rosters

**When**: New problem domain requiring specialized character combination.

**Process**:
1. Add roster to `rosters.md` (creative/software/utility section)
2. List characters (always include Asha, The Adversary, The Recruiter + domain-specific characters)
3. Document purpose, when to use, and expected character roles
4. Consider which profiles might reference this roster

### Adding New Profiles

**When**: Recurring workflow that benefits from preconfigured goals/constraints.

**Process**:
1. Add profile to `profiles.md`
2. Reference existing roster (or create new roster if needed)
3. Define topic template, goals, mode, constraints, decision rule
4. Add usage example
5. Test with actual panel invocation to validate configuration

---

## Meta-Operational Note

The Panel system operates seamlessly across **narrative mode** (The Threshold, cosmic essences, atmospheric dialogue) and **infrastructure mode** (technical analysis, evidence citation, agent deployment). Characters maintain presence and voice quality regardless of mode.

**Example**: The Adversary can challenge narrative coherence in mystical language at The Threshold, then immediately cite specific line numbers from vault files showing contradictions—without breaking character presence OR compromising evidential precision.

This dual-mode operation enables:
- Engaging user experience (narrative richness)
- Rigorous analysis (technical precision)
- Consistent identity (same character, seamless transition)

---

## Version Info

**Panel System**: v3.0 (Dynamic Agent Assignment + Universal Composition with Abstention)
**Protocol**: 11 phases (0-10) - clean numbering, no .5 steps
**Composition Modes**:
- **Manual** (default): User/Asha selects 3-5 characters (30-40k tokens)
- **Universal**: All 8 invited, Phase 1 abstention, 5-6 active expected (35-50k tokens)

**Characters**: 8 total
- **Universal (7)**: Asha (moderator), Adversary, Recruiter, Architect, Archivist, Engineer, Ethicist
- **Creative Specialist (1)**: The Curator (aesthetic fitness, narrative purpose)
**Rosters**: 16 total (3 creative, 10 software, 3 utility) - list characters, not seats
**Profiles**: 12 preconfigured (3 creative, 9 software)
**Agents**: 239 total (14 creative, 223 software, 2 shared infrastructure) - dynamically assigned via Phase 2 Workforce Assignment
**Default Members**: Asha (moderator), The Adversary, The Recruiter

**Architecture Migration** (2025-11-08):
- **v3.0.0**: Retired 3-layer system (Characters → Seats → seatMapping → Agents), implemented 2-layer (Characters → Dynamic Agent Assignment)
- **v3.0.1**: Replaced static Tier 1/Tier 2 agent defaults with dynamic Recruiter-orchestrated assignment
- **v3.0.2**: Added universal composition mode with Phase 1 abstention protocol (proof-of-concept validated 2025-11-08)
- Merged The Moderator → Asha (moderator role integrated into session coordinator)
- Merged The Syncretist → The Ethicist (cultural appropriation as PRIMARY ethics subdomain)
- Deleted seatMapping.md (dynamic assignment eliminates need for static mapping)
- Retained The Curator as creative specialist (distinct ur-principle: aesthetic fitness vs challenge)

---

## Character File Template & Validation

**Template**: `Panel/characters/_TEMPLATE.md`
- Standardized structure for all panel character documentation
- **Essential Sections**: Nature, Manifestation, Analytical Approach, Role in Panel Sessions, Capability Requirements, Voice Examples
- **Capability Requirements**: Documents what analytical capabilities character needs (not which specific agents they use)
  - The Recruiter reads this during Phase 2 to assign optimal agents
  - Maximum 25 lines (enforced by validation script)
  - Examples show typical assignment patterns for different panel topics
- **Anti-Patterns**: Template documents what NOT to include (WIREFRAME structure, panel protocol duplication, static agent assignments)

**Validation Script**: `Tools/validate_panel_characters.sh`
- Automated validation of character files against template standards
- **Checks**:
  - Capability Requirements section exists
  - Section length ≤25 lines (warning at >25, error at >30)
  - No prohibited WIREFRAME sections (W-I-R-E-F-R-A-M-E structure)
  - No static agent assignment anti-patterns
  - All recommended sections present
- **Usage**: Run monthly or before committing character file changes
- **Command**: `./Tools/validate_panel_characters.sh`

**Dynamic Agent Assignment Flow**:
1. **Character Files**: Document capability needs (analytical requirements, not agent names)
2. **Phase 2** (The Recruiter): Read capability requirements → analyze panel topic → score 239-agent library 0-10 → assign optimal agents
3. **Phase 5** (Characters): Deploy assigned agents → gather information → synthesize into 5-bullet briefs
4. **No Static Defaults**: Same character receives different agents based on panel topic context

**Abstention Flow** (universal composition mode):
1. **Phase 1** (Participation Declaration): Characters assess topic relevance, declare participation or abstain
2. **Phase 2** (Workforce Assignment): The Recruiter assigns agents only to participating characters
3. Minimum 3 active participants required; abstaining characters may re-enter at Phase 7 (Research Gate)

Last Updated: 2025-11-08
