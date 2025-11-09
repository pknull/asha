# Panel Rosters

Panel rosters define character combinations for different evaluation contexts. Characters deploy agents as tools via two-tier protocol. Rosters can be referenced in profiles or invoked directly via `/panel-use`.

---

## Creative Writing

### core
**Purpose**: Core creative team for narrative development
**Characters**:
- Asha (moderator)
- The Adversary (critical challenge, contradiction detection)
- The Recruiter
- The Curator (narrative purpose, aesthetic fitness, craft standards)
- The Architect (story structure, narrative frameworks)
- The Engineer (prose implementation, writing craft)
- The Ethicist (values, representation, cultural integrity)

### worldbuilding
**Purpose**: Worldbuilding and setting development
**Characters**:
- Asha (moderator)
- The Adversary (lore consistency, contradiction detection)
- The Recruiter
- The Curator (atmospheric integrity, world coherence, style compliance)
- The Architect (world structure, cultural systems)
- The Archivist (research, source validation)
- The Ethicist (cultural appropriation scan, living tradition respect)

### prose
**Purpose**: Prose analysis and optimization
**Characters**:
- Asha (moderator)
- The Adversary (voice contradiction, plot hole detection)
- The Recruiter
- The Curator (AI contamination, narrative necessity, style guide enforcement)
- The Engineer (prose craft, style implementation)

---

## Software Engineering

### architecture
**Purpose**: System design and architectural decisions
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (system design, structural vision)
- The Engineer (implementation feasibility)
- The Ethicist (security as stakeholder concern)

### security
**Purpose**: Security review and vulnerability assessment
**Characters**:
- Asha (moderator)
- The Adversary (attack surface analysis)
- The Recruiter
- The Architect (threat modeling, security architecture)
- The Ethicist (stakeholder protection, security ethics)

### infrastructure
**Purpose**: DevOps, deployment, and infrastructure orchestration
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (infrastructure design)
- The Engineer (implementation, reliability)

### performance
**Purpose**: Performance optimization and scalability engineering
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (scalability architecture)
- The Engineer (optimization implementation)

### quality
**Purpose**: Code review, testing strategy, and quality assurance
**Characters**:
- Asha (moderator)
- The Adversary (quality gates, standards enforcement)
- The Recruiter
- The Engineer (code quality, maintainability)

### integration
**Purpose**: API design, service integration, and contract definition
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (integration architecture, contract design)
- The Engineer (implementation patterns)

### data
**Purpose**: Database design, optimization, and data architecture
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (data architecture, schema design)
- The Archivist (data governance, documentation)
- The Engineer (query optimization)

### intelligence
**Purpose**: Machine learning and AI system design
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (ML pipeline design)
- The Archivist (research, dataset curation)
- The Engineer (model implementation)
- The Ethicist (AI ethics, bias assessment)

### frontend
**Purpose**: User interface engineering and experience design
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (component architecture, state management)
- The Engineer (UI implementation, accessibility)
- The Ethicist (user experience ethics, accessibility)

### backend
**Purpose**: Service architecture and backend system design
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (service architecture, data flows)
- The Engineer (implementation, performance)

---

## Utility Rosters

### minimal
**Purpose**: Lightweight evaluation with essential perspectives
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter

### planning
**Purpose**: Pre-implementation workforce strategy and agent deployment
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect (problem decomposition)

### comprehensive
**Purpose**: Maximum coverage across all domains
**Characters**:
- Asha (moderator)
- The Adversary
- The Recruiter
- The Architect
- The Archivist
- The Engineer
- The Ethicist
- The Curator (when creative work involved)

---

## Notes

**Default Members**: All rosters include Asha (moderator), The Adversary, and The Recruiter
- **Asha**: Session facilitation, process enforcement, decision formalization
- **The Adversary**: Critical evaluation, quality gates, assumption challenging
- **The Recruiter**: Agent deployment optimization, capability gap analysis

**Character → Agent Deployment**:
- Characters listed in rosters deploy agents via two-tier protocol:
  - **Tier 1**: Default agents documented in character files (automatic)
  - **Tier 2**: Recruiter search with 0-10 scoring (specialized needs)
- Same characters, different agent tools based on problem context

**Agent Library**: 239 agents in `.claude/agents/` (14 creative, 223 software, 2 shared infrastructure)

**Roster Extension**: Use `--add` flag when invoking `/panel-use` to include additional characters

---

## Coverage Validation

**Software Engineering Rosters**: 10 total
- ✓ **architecture** - System design and structural decisions
- ✓ **security** - Security review and threat assessment
- ✓ **infrastructure** - DevOps and deployment orchestration
- ✓ **performance** - Optimization and scalability
- ✓ **quality** - Code review and testing strategy
- ✓ **integration** - API design and service integration
- ✓ **data** - Database design and data architecture
- ✓ **intelligence** - ML/AI system design
- ✓ **frontend** - UI engineering and experience
- ✓ **backend** - Service architecture and implementation

**Creative Writing Rosters**: 3 total
- ✓ **core** - Narrative development
- ✓ **worldbuilding** - Setting development
- ✓ **prose** - Prose analysis and optimization

**Utility Rosters**: 3 total
- ✓ **minimal** - Lightweight evaluation
- ✓ **planning** - Pre-implementation workforce strategy
- ✓ **comprehensive** - Maximum coverage

**Total**: 16 rosters covering all major technical and creative domains
