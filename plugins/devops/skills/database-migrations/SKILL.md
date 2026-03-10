---
name: database-migrations
description: Safe database migration patterns for zero-downtime deployments
triggers:
  - Schema changes
  - Database migrations
  - Column additions/removals
  - Index creation
---

# Database Migration Patterns

Safe, reversible schema changes for production systems.

## Core Principles

1. **Every change is a migration** — No manual ALTER statements
2. **Forward-only in production** — Rollbacks use new migrations
3. **Separate schema from data** — DDL and DML in different migrations
4. **Test against production volume** — Performance matters
5. **Immutable after deployment** — Never edit deployed migrations

## Safety Checklist

- [ ] UP migration tested
- [ ] DOWN migration tested (or marked irreversible)
- [ ] No exclusive table locks on large tables
- [ ] New columns are nullable OR have defaults
- [ ] Indexes created concurrently
- [ ] Data backfill separate from schema change
- [ ] Tested against production-size dataset
- [ ] Rollback procedure documented

## Zero-Downtime Strategy

### Expand-Contract Pattern

**Phase 1: Expand** (add without breaking)

```sql
-- Add nullable column
ALTER TABLE users ADD COLUMN new_email TEXT;
```

- Deploy app writing to both columns
- Old code ignores new column

**Phase 2: Migrate** (move data)

```sql
-- Backfill in batches
UPDATE users SET new_email = email
WHERE new_email IS NULL
LIMIT 1000;
```

- Deploy app reading from new column

**Phase 3: Contract** (remove old)

```sql
-- After all apps updated
ALTER TABLE users DROP COLUMN email;
ALTER TABLE users RENAME COLUMN new_email TO email;
```

### Timeline Example

| Day | Action |
|-----|--------|
| 1 | Add new column, deploy dual-write |
| 2 | Backfill data |
| 3 | Deploy read from new column |
| 7 | Drop old column |

## PostgreSQL Patterns

### Adding Columns

```sql
-- SAFE: Nullable column (instant)
ALTER TABLE users ADD COLUMN avatar_url TEXT;

-- SAFE: With default (instant in PG 11+)
ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT true;

-- DANGEROUS: NOT NULL without default (rewrites table)
-- Don't do this on large tables
ALTER TABLE users ADD COLUMN required_field TEXT NOT NULL;
```

### Creating Indexes

```sql
-- DANGEROUS: Blocks writes
CREATE INDEX idx_users_email ON users(email);

-- SAFE: Allows concurrent writes
CREATE INDEX CONCURRENTLY idx_users_email ON users(email);
```

**Note:** CONCURRENTLY can't run in a transaction. Use separate migration.

### Renaming Columns (Expand-Contract)

```sql
-- Migration 1: Add new column
ALTER TABLE users ADD COLUMN full_name TEXT;

-- Migration 2: Backfill (run separately)
UPDATE users SET full_name = name WHERE full_name IS NULL;

-- Migration 3: After app updated
ALTER TABLE users DROP COLUMN name;
```

### Batch Updates

```sql
-- Process in batches to avoid long locks
DO $$
DECLARE
  batch_size INT := 1000;
  rows_updated INT;
BEGIN
  LOOP
    UPDATE users
    SET status = 'active'
    WHERE id IN (
      SELECT id FROM users
      WHERE status IS NULL
      LIMIT batch_size
      FOR UPDATE SKIP LOCKED
    );

    GET DIAGNOSTICS rows_updated = ROW_COUNT;
    EXIT WHEN rows_updated = 0;

    COMMIT;
    PERFORM pg_sleep(0.1);  -- Brief pause
  END LOOP;
END $$;
```

## ORM Workflows

### Prisma

```bash
# Generate migration from schema changes
npx prisma migrate dev --name add_user_avatar

# Apply in production
npx prisma migrate deploy
```

Custom SQL for unsupported operations:

```sql
-- prisma/migrations/xxx_concurrent_index/migration.sql
-- Note: Run manually, Prisma can't do CONCURRENTLY
CREATE INDEX CONCURRENTLY idx_users_email ON users(email);
```

### Django

```bash
# Generate migration
python manage.py makemigrations

# Apply
python manage.py migrate
```

Custom operations:

```python
from django.db import migrations

class Migration(migrations.Migration):
    operations = [
        migrations.RunSQL(
            sql="CREATE INDEX CONCURRENTLY idx_email ON users(email);",
            reverse_sql="DROP INDEX idx_email;",
        ),
    ]
```

### golang-migrate

```bash
# Create migration pair
migrate create -ext sql -dir migrations -seq add_user_avatar

# Apply
migrate -database "postgres://..." -path migrations up
```

```sql
-- migrations/000001_add_user_avatar.up.sql
ALTER TABLE users ADD COLUMN avatar_url TEXT;

-- migrations/000001_add_user_avatar.down.sql
ALTER TABLE users DROP COLUMN avatar_url;
```

## Dangerous Operations

| Operation | Risk | Safe Alternative |
|-----------|------|------------------|
| `NOT NULL` without default | Table rewrite | Add nullable, backfill, then alter |
| `CREATE INDEX` | Write lock | `CREATE INDEX CONCURRENTLY` |
| `DROP COLUMN` | Instant but irreversible | Verify no references first |
| `RENAME TABLE` | Breaks queries | Create new, migrate, drop old |
| `ALTER TYPE` | May rewrite | Add new column, migrate |

## Rollback Strategies

### Forward-Fix (Preferred)

```sql
-- Original migration broke something
-- Don't rollback, deploy fix instead

-- Migration N+1: Fix the issue
ALTER TABLE users ALTER COLUMN status SET DEFAULT 'pending';
UPDATE users SET status = 'pending' WHERE status IS NULL;
```

### Reverse Migration

```sql
-- migrations/xxx_add_feature.down.sql
DROP INDEX IF EXISTS idx_users_email;
ALTER TABLE users DROP COLUMN IF EXISTS avatar_url;
```

## Anti-Patterns

| Pattern | Problem | Solution |
|---------|---------|----------|
| Manual SQL in production | No audit trail | Always use migrations |
| Editing deployed migrations | Environment drift | Create new migration |
| `NOT NULL` on existing column | Table lock | Nullable + backfill + alter |
| Non-concurrent indexes | Write blocking | `CONCURRENTLY` |
| Schema + data in one migration | Hard to rollback | Separate migrations |
| No down migration | Can't rollback | Always include or mark irreversible |
