---
name: docker-patterns
description: Docker and Docker Compose best practices for containerized development
triggers:
  - Docker setup
  - Docker Compose configuration
  - Container optimization
  - Multi-stage builds
---

# Docker Development Patterns

Best practices for containerized development and production.

## Multi-Stage Dockerfile

```dockerfile
# Stage 1: Dependencies
FROM node:22-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci

# Stage 2: Development
FROM deps AS dev
COPY . .
CMD ["npm", "run", "dev"]

# Stage 3: Build
FROM deps AS build
COPY . .
RUN npm run build

# Stage 4: Production
FROM node:22-alpine AS prod
WORKDIR /app

# Security: non-root user
RUN addgroup -g 1001 -S app && \
    adduser -S -u 1001 -G app app

COPY --from=build --chown=app:app /app/dist ./dist
COPY --from=deps --chown=app:app /app/node_modules ./node_modules

USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s \
  CMD wget -q --spider http://localhost:3000/health || exit 1

CMD ["node", "dist/index.js"]
```

## Docker Compose Structure

### Base Configuration (docker-compose.yml)

```yaml
services:
  app:
    build:
      context: .
      target: dev
    volumes:
      - ./src:/app/src
      - /app/node_modules  # Anonymous volume
    environment:
      - NODE_ENV=development
    depends_on:
      db:
        condition: service_healthy

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: app
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER}"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  postgres_data:
```

### Development Override (docker-compose.override.yml)

```yaml
# Auto-loaded with docker compose up
services:
  app:
    ports:
      - "3000:3000"
      - "9229:9229"  # Debug port
    environment:
      - DEBUG=app:*
    command: npm run dev

  mailpit:
    image: axllent/mailpit
    ports:
      - "8025:8025"
      - "1025:1025"
```

### Production (docker-compose.prod.yml)

```yaml
services:
  app:
    build:
      target: prod
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '1'
          memory: 512M
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

## Volume Strategies

| Type | Use Case |
|------|----------|
| Named volumes | Persistent data (databases) |
| Bind mounts | Source code (development) |
| Anonymous volumes | Protect container paths from bind mounts |
| tmpfs | Ephemeral data (sessions, cache) |

```yaml
volumes:
  # Named volume for persistence
  postgres_data:

services:
  app:
    volumes:
      # Bind mount for hot reload
      - ./src:/app/src
      # Anonymous volume protects node_modules
      - /app/node_modules
      # tmpfs for temp files
      - type: tmpfs
        target: /app/tmp
```

## Networking

### Service Discovery

```yaml
services:
  api:
    # Other services reach this as: http://api:3000
    networks:
      - backend

  db:
    networks:
      - backend

  nginx:
    networks:
      - backend
      - frontend
    ports:
      - "80:80"  # Only nginx exposed

networks:
  backend:
    internal: true  # No external access
  frontend:
```

### Host Binding

```yaml
services:
  db:
    ports:
      # Bind to localhost only (not 0.0.0.0)
      - "127.0.0.1:5432:5432"
```

## Security Hardening

### Non-Root User

```dockerfile
RUN addgroup -g 1001 -S app && \
    adduser -S -u 1001 -G app app
USER app
```

### Read-Only Filesystem

```yaml
services:
  app:
    read_only: true
    tmpfs:
      - /tmp
      - /app/cache
```

### Capability Dropping

```yaml
services:
  app:
    cap_drop:
      - ALL
    cap_add:
      - NET_BIND_SERVICE  # If needed for ports < 1024
```

### Specific Image Tags

```yaml
# BAD
image: node:latest

# GOOD
image: node:22.12-alpine3.20
```

## .dockerignore

```
node_modules
.git
.env*
*.md
tests/
coverage/
.vscode/
*.log
dist/
```

## Health Checks

```dockerfile
# HTTP check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:3000/health || exit 1

# TCP check (for databases)
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
  CMD pg_isready -U postgres || exit 1
```

## Common Commands

```bash
# Development
docker compose up              # Start with override
docker compose logs -f app     # Stream logs
docker compose exec app sh     # Shell into container

# Debugging
docker compose ps              # Status
docker compose exec app nslookup db  # DNS check
docker stats                   # Resource usage

# Cleanup
docker compose down            # Stop containers
docker compose down -v         # Stop + remove volumes
docker system prune -a         # Full cleanup
```

## Build Optimization

### Layer Caching

```dockerfile
# Dependencies change less often - cache this layer
COPY package*.json ./
RUN npm ci

# Source changes often - separate layer
COPY . .
RUN npm run build
```

### Build Arguments

```dockerfile
ARG NODE_ENV=production
ENV NODE_ENV=$NODE_ENV

RUN if [ "$NODE_ENV" = "development" ]; then \
      npm install; \
    else \
      npm ci --only=production; \
    fi
```

## Anti-Patterns

| Avoid | Instead |
|-------|---------|
| `:latest` tag | Specific version tags |
| Root user | Dedicated app user |
| Single mega-container | Service per container |
| Secrets in image | Runtime env vars or secrets |
| Data in containers | Named volumes |
| `docker compose` in production | Kubernetes, ECS, etc. |
