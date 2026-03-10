# DevOps Plugin

**Version**: 1.0.0

DevOps patterns and infrastructure skills for containerized development and database operations.

## Installation

```bash
/plugin install devops@asha-marketplace
```

## Skills

### docker-patterns

Best practices for Docker and Docker Compose development.

**Triggers**: Docker setup, container optimization, multi-stage builds

**Coverage**:

- Multi-stage Dockerfile patterns
- Docker Compose structure (base, override, production)
- Volume strategies (named, bind, anonymous, tmpfs)
- Network configuration and service discovery
- Security hardening (non-root, read-only, capability dropping)
- Health checks
- Build optimization and layer caching

### database-migrations

Safe database migration patterns for zero-downtime deployments.

**Triggers**: Schema changes, column additions/removals, index creation

**Coverage**:

- Expand-contract pattern for zero-downtime changes
- PostgreSQL-specific patterns (concurrent indexes, batch updates)
- ORM workflows (Prisma, Django, golang-migrate)
- Safety checklist for production migrations
- Dangerous operations and safe alternatives
- Rollback strategies (forward-fix vs reverse)

## Usage

Skills activate automatically based on context. When working on Docker configuration or database migrations, the relevant patterns are available for reference.

```
# Example prompts that trigger these skills:
"Set up Docker for this Node.js app"
"Add a new column to the users table safely"
"Create a multi-stage Dockerfile"
```

## License

MIT
