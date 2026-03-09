---
name: api-design
description: REST API design patterns and conventions for production systems
triggers:
  - Creating API endpoints
  - Designing REST resources
  - API versioning decisions
  - Response format questions
---

# API Design Patterns

Production-ready REST API conventions for consistency and developer experience.

## Resource Design

| Principle | Example |
|-----------|---------|
| Plural nouns | `/api/v1/users`, `/api/v1/orders` |
| Kebab-case | `/api/v1/team-members` |
| No verbs | `/api/v1/users` not `/api/v1/getUsers` |
| Nested resources | `/api/v1/users/:id/orders` |

## HTTP Methods

| Method | Purpose | Idempotent |
|--------|---------|------------|
| GET | Retrieve resource(s) | Yes |
| POST | Create resource | No |
| PUT | Replace resource | Yes |
| PATCH | Partial update | Yes |
| DELETE | Remove resource | Yes |

## Status Codes

### Success

| Code | When |
|------|------|
| 200 OK | GET, PUT, PATCH success |
| 201 Created | POST success (include Location header) |
| 204 No Content | DELETE success |

### Client Errors

| Code | When |
|------|------|
| 400 Bad Request | Malformed syntax |
| 401 Unauthorized | Missing/invalid auth |
| 403 Forbidden | Valid auth, insufficient permissions |
| 404 Not Found | Resource doesn't exist |
| 409 Conflict | State conflict (duplicate, version mismatch) |
| 422 Unprocessable Entity | Semantic validation failure |
| 429 Too Many Requests | Rate limit exceeded |

### Server Errors

| Code | When |
|------|------|
| 500 Internal Server Error | Unexpected server failure |
| 502 Bad Gateway | Upstream service failure |
| 503 Service Unavailable | Temporarily overloaded |

## Response Envelopes

### Success Response

```json
{
  "data": { ... },
  "meta": {
    "page": 1,
    "perPage": 20,
    "total": 150
  }
}
```

### Error Response

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request data",
    "details": [
      { "field": "email", "message": "Invalid email format" }
    ]
  }
}
```

## Pagination

### Offset-Based (Simple)

```
GET /api/v1/users?page=2&per_page=20
```

Good for: Small datasets, dashboards, random access.

### Cursor-Based (Scalable)

```
GET /api/v1/users?cursor=eyJpZCI6MTAwfQ&limit=20
```

Good for: Large datasets, infinite scroll, real-time data.

## Filtering & Sorting

```
GET /api/v1/products?price[gte]=10&price[lte]=100
GET /api/v1/products?category=electronics,books
GET /api/v1/products?sort=-created_at,name
GET /api/v1/orders?status=pending&user.role=admin
```

| Pattern | Meaning |
|---------|---------|
| `[gte]`, `[lte]` | Comparison operators |
| Comma-separated | Multiple values (OR) |
| `-` prefix | Descending sort |
| Dot notation | Nested field access |

## Versioning

**URL path versioning** (recommended):

```
/api/v1/users
/api/v2/users
```

Rules:

- Maximum 2 active versions
- 6-month deprecation notice
- Document breaking changes

## Authentication

```
Authorization: Bearer <token>
```

- Tokens in headers, never URLs
- Short-lived access tokens (15-60 min)
- Refresh tokens for renewal
- Scope-based permissions

## Rate Limiting

Headers:

```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 999
X-RateLimit-Reset: 1640000000
```

Tiers:

| Tier | Limit |
|------|-------|
| Anonymous | 60/hour |
| Authenticated | 1000/hour |
| Premium | 10000/hour |

## HATEOAS (Optional)

```json
{
  "data": { "id": 1, "name": "..." },
  "links": {
    "self": "/api/v1/users/1",
    "orders": "/api/v1/users/1/orders"
  }
}
```

## Anti-Patterns

| Avoid | Instead |
|-------|---------|
| `/api/v1/getUsers` | `/api/v1/users` (GET) |
| `/api/v1/user` (singular) | `/api/v1/users` (plural) |
| Verbs in URLs | HTTP methods convey action |
| Leaking internal IDs | Use UUIDs or slugs |
| 200 for errors | Proper status codes |
