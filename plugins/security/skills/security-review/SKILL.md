---
name: security-review
description: Security checklist and patterns for web applications
triggers:
  - Authentication implementation
  - Handling user input
  - API endpoint creation
  - Payment/sensitive features
  - Working with secrets
---

# Security Review Patterns

Comprehensive security checklist for web applications.

## Pre-Deployment Checklist

### 1. Secrets Management

- [ ] No hardcoded credentials in code
- [ ] API keys in environment variables
- [ ] Secrets not logged or exposed in errors
- [ ] `.env` files in `.gitignore`
- [ ] Different secrets per environment

```typescript
// BAD
const apiKey = 'sk-proj-xxxxx';

// GOOD
const apiKey = process.env.OPENAI_API_KEY;
if (!apiKey) throw new Error('OPENAI_API_KEY not configured');
```

### 2. Input Validation

- [ ] Schema validation on all inputs (Zod, Joi, etc.)
- [ ] Whitelist approach (allow known good)
- [ ] File upload restrictions (type, size, name)
- [ ] Sanitize before storage AND display

```typescript
const userSchema = z.object({
  email: z.string().email(),
  name: z.string().min(1).max(100),
  age: z.number().int().min(0).max(150)
});

const validated = userSchema.parse(input);
```

### 3. SQL Injection

- [ ] Parameterized queries only
- [ ] No string concatenation in queries
- [ ] ORM/query builder preferred

```typescript
// BAD
const query = `SELECT * FROM users WHERE id = ${userId}`;

// GOOD
const query = `SELECT * FROM users WHERE id = $1`;
await db.query(query, [userId]);
```

### 4. Authentication

- [ ] Passwords hashed (bcrypt, argon2)
- [ ] Tokens in httpOnly cookies (not localStorage)
- [ ] Session timeout implemented
- [ ] Account lockout after failed attempts
- [ ] MFA for sensitive operations

```typescript
// Secure cookie settings
res.cookie('session', token, {
  httpOnly: true,
  secure: true,
  sameSite: 'strict',
  maxAge: 3600000
});
```

### 5. Authorization

- [ ] RBAC or ABAC implemented
- [ ] Ownership verified before access
- [ ] Horizontal privilege escalation prevented
- [ ] Default deny policy

```typescript
// Always verify ownership
const resource = await getResource(id);
if (resource.ownerId !== currentUser.id) {
  throw new ForbiddenError();
}
```

### 6. XSS Prevention

- [ ] Output encoding/escaping
- [ ] Content Security Policy headers
- [ ] DOMPurify for user HTML
- [ ] No `dangerouslySetInnerHTML` with user data

```typescript
import DOMPurify from 'dompurify';
const clean = DOMPurify.sanitize(userInput, {
  ALLOWED_TAGS: ['b', 'i', 'em', 'strong', 'a'],
  ALLOWED_ATTR: ['href']
});
```

### 7. CSRF Protection

- [ ] CSRF tokens on state-changing requests
- [ ] SameSite=Strict cookies
- [ ] Origin header validation

### 8. Rate Limiting

- [ ] IP-based rate limits
- [ ] User-based rate limits
- [ ] Aggressive limits on auth endpoints
- [ ] Exponential backoff on failures

```typescript
const limiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 100, // 100 requests per window
  standardHeaders: true
});

// Stricter for auth
const authLimiter = rateLimit({
  windowMs: 60 * 60 * 1000,
  max: 5 // 5 attempts per hour
});
```

### 9. Error Handling

- [ ] Generic messages to users
- [ ] Detailed logs server-side
- [ ] No stack traces in production
- [ ] No sensitive data in errors

```typescript
// BAD
res.status(500).json({ error: err.stack });

// GOOD
log.error('Database error', { error: err, userId });
res.status(500).json({ error: 'An error occurred' });
```

### 10. Headers

- [ ] `Strict-Transport-Security` (HSTS)
- [ ] `Content-Security-Policy`
- [ ] `X-Content-Type-Options: nosniff`
- [ ] `X-Frame-Options: DENY`
- [ ] `Referrer-Policy: strict-origin-when-cross-origin`

### 11. Dependencies

- [ ] `npm audit` / `pip-audit` clean
- [ ] Lock files committed
- [ ] Automated dependency updates
- [ ] No unnecessary packages

## OWASP Top 10 Quick Reference

| Risk | Prevention |
|------|------------|
| Injection | Parameterized queries |
| Broken Auth | Strong sessions, MFA |
| Sensitive Data Exposure | Encryption, minimal data |
| XXE | Disable external entities |
| Broken Access Control | Default deny, verify ownership |
| Misconfiguration | Secure defaults, hardening |
| XSS | Output encoding, CSP |
| Insecure Deserialization | Validate before deserialize |
| Vulnerable Components | Audit, update regularly |
| Insufficient Logging | Audit trails, monitoring |

## Sensitive Operations

Always require re-authentication for:

- Password changes
- Email changes
- Payment methods
- Account deletion
- API key generation
- Permission changes

## Security Anti-Patterns

| Pattern | Risk |
|---------|------|
| `eval()` with user input | Code injection |
| `shell=True` with user input | Command injection |
| Regex from user input | ReDoS |
| `pickle.loads()` untrusted data | Arbitrary code execution |
| JWT in localStorage | XSS token theft |
| Weak random (`Math.random()`) | Predictable tokens |
