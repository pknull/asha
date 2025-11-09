# Panel Profiles

Preconfigured panel sessions for common evaluation scenarios. Profiles reference rosters from `rosters.md` and can be invoked via `/panel-use <profile>`.

---

## Software Engineering Profiles

### system_design
**Roster**: architecture
**Topic**: System architecture and design decisions
**Goals**:
- Assess architectural soundness and maintainability
- Identify integration risks with existing infrastructure
- Validate design patterns and structural approach
- Review error handling and edge cases
**Mode**: out_of_world
**Constraints**:
- Timebox: 25 minutes
- Must reach decisive recommendation (APPROVE/REVISE/REJECT)
**Decision Rule**: consensus

### api_review
**Roster**: integration
**Topic**: API contract and integration design review
**Goals**:
- Validate RESTful/GraphQL design patterns
- Assess backward compatibility and versioning strategy
- Review authentication and authorization approaches
- Identify performance bottlenecks
**Mode**: out_of_world
**Constraints**:
- Timebox: 30 minutes
- OpenAPI/GraphQL schema required
**Decision Rule**: consensus

### security_assessment
**Roster**: security
**Topic**: Security vulnerability and threat assessment
**Goals**:
- Identify OWASP Top 10 vulnerabilities
- Review input validation and sanitization
- Assess authentication and session management
- Evaluate secrets management approach
- Check dependency security
**Mode**: out_of_world
**Constraints**:
- Timebox: 35 minutes
- Security-first mindset mandatory
**Decision Rule**: unanimous (security cannot have dissent)

### infrastructure_design
**Roster**: infrastructure
**Topic**: DevOps and deployment architecture review
**Goals**:
- Validate CI/CD pipeline design
- Assess container orchestration strategy
- Review monitoring and observability approach
- Evaluate disaster recovery and backup procedures
**Mode**: out_of_world
**Constraints**:
- Timebox: 30 minutes
- Cloud provider agnostic recommendations
**Decision Rule**: consensus

### code_review_major
**Roster**: quality
**Topic**: Major refactor or feature code review
**Goals**:
- Assess code maintainability and readability
- Validate test coverage and quality
- Review error handling patterns
- Identify technical debt introduction
**Mode**: out_of_world
**Constraints**:
- Timebox: 25 minutes
- Diff or file list required
**Decision Rule**: consensus

### performance_optimization
**Roster**: performance
**Topic**: Performance bottleneck analysis and optimization strategy
**Goals**:
- Identify critical performance issues
- Propose optimization approaches with benchmarks
- Assess scalability implications
- Review caching and database query patterns
**Mode**: out_of_world
**Constraints**:
- Timebox: 30 minutes
- Baseline metrics required
**Decision Rule**: consensus

### ml_system_design
**Roster**: intelligence
**Topic**: Machine learning system architecture review
**Goals**:
- Validate ML pipeline design
- Assess model training and deployment strategy
- Review feature engineering approach
- Evaluate monitoring and retraining procedures
**Mode**: out_of_world
**Constraints**:
- Timebox: 35 minutes
- Focus on production readiness
**Decision Rule**: consensus

### database_schema
**Roster**: data
**Topic**: Database schema design and migration strategy
**Goals**:
- Validate normalization and indexing strategy
- Assess query performance implications
- Review migration rollback procedures
- Evaluate data integrity constraints
**Mode**: out_of_world
**Constraints**:
- Timebox: 25 minutes
- Schema diagram required
**Decision Rule**: consensus

### frontend_review
**Roster**: frontend
**Topic**: Frontend implementation and UX review
**Goals**:
- Assess component architecture and reusability
- Validate accessibility compliance
- Review performance and bundle size
- Evaluate state management approach
**Mode**: out_of_world
**Constraints**:
- Timebox: 30 minutes
- Component structure required
**Decision Rule**: consensus

### backend_review
**Roster**: backend
**Topic**: Backend service architecture review
**Goals**:
- Assess service boundaries and responsibilities
- Validate API design and integration patterns
- Review error handling and resilience
- Evaluate security and authentication
**Mode**: out_of_world
**Constraints**:
- Timebox: 30 minutes
- Service diagram required
**Decision Rule**: consensus

---

## Creative Writing Profiles (Original)

### manuscript_review
**Roster**: core
**Topic**: Alpha manuscript section evaluation
**Goals**:
- Assess narrative coherence and pacing
- Evaluate character development and voice consistency
- Identify structural weaknesses
- Provide remediation estimates
**Mode**: out_of_world
**Constraints**:
- Timebox: 30 minutes
- Scoring on 0-10 scale required
**Decision Rule**: consensus

### world_consistency
**Roster**: worldbuilding
**Topic**: World consistency and lore validation
**Goals**:
- Identify contradictions in established canon
- Assess cultural and historical plausibility
- Review magical system coherence
- Validate setting authenticity
**Mode**: out_of_world
**Constraints**:
- Timebox: 25 minutes
- Reference Vault canon
**Decision Rule**: consensus

### prose_quality
**Roster**: prose
**Topic**: Prose quality and AI contamination assessment
**Goals**:
- Detect AI generation patterns
- Assess stylistic consistency
- Identify weak writing vs AI artifacts
- Provide remediation recommendations
**Mode**: out_of_world
**Constraints**:
- Timebox: 20 minutes
- Use 3+ markers threshold
**Decision Rule**: consensus

---

## Utility Profiles

### implementation_planning
**Roster**: planning
**Topic**: Pre-implementation workforce strategy and task decomposition
**Goals**:
- Decompose problem into atomic tasks
- Map tasks to existing agent capabilities (0-10 scoring)
- Identify capability gaps with ROI analysis
- Recommend optimal agent deployment strategy
**Mode**: out_of_world
**Constraints**:
- Timebox: 25 minutes
- Must provide coverage metrics and gap analysis
**Decision Rule**: consensus

### quick_sanity
**Roster**: minimal
**Topic**: Fast sanity check for straightforward decisions
**Goals**:
- Identify obvious blockers or red flags
- Provide go/no-go recommendation
**Mode**: out_of_world
**Constraints**:
- Timebox: 10 minutes
- Binary decision required
**Decision Rule**: consensus

### comprehensive_audit
**Roster**: comprehensive
**Topic**: Complete system audit across all domains
**Goals**:
- Security, performance, code quality, architecture review
- Identify all risks and technical debt
- Prioritize remediation by severity
**Mode**: out_of_world
**Constraints**:
- Timebox: 45 minutes
- Comprehensive documentation required
**Decision Rule**: consensus

---

## Usage Examples

```bash
# Pre-implementation workforce planning
/panel-use implementation_planning --topic "Build podcast transcription system with speaker diarization"

# System architecture review
/panel-use system_design --topic "Assess panel_runner.py Phase 1 architecture"

# API design review
/panel-use api_review --topic "BookStack MCP integration API contract" --goals "+rate limiting strategy"

# Security audit
/panel-use security_assessment --topic "Authentication system vulnerability scan"

# Frontend implementation review
/panel-use frontend_review --topic "React component architecture assessment"

# Backend service review
/panel-use backend_review --topic "Microservices integration patterns"

# Add extra expertise to any profile
/panel-use infrastructure_design --topic "Multi-region deployment strategy" --goals "+disaster recovery validation"
```

## Notes

- Profiles provide sensible defaults; use `--goals`, `--add`, `--mode` to customize
- Software profiles assume `out_of_world` mode (technical precision)
- Creative profiles may use `inworld` mode for narrative perspective
- All timeboxes are guidelines; extend if complexity justifies
