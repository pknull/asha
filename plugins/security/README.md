# Security Plugin

**Version**: 1.0.0

Security review patterns, OWASP guidelines, and defensive coding practices for web applications.

## How to use it

This plugin contains one skill and no slash command or agent. It activates from
security-sensitive work, or you can name it explicitly on any harness:

```text
Use security-review to examine this authentication change.
Apply security-review while implementing this upload endpoint.
Run the pre-deployment security checklist against the current diff.
```

Use `/code:review` when you need a multi-lens review of a local diff. Use
`security-review` when the task needs the security checklist throughout design
or implementation. They can be combined; they are not duplicates.

## Installation

```bash
./install.sh --only security --target claude
./install.sh --only security --target codex
./install.sh --only security --target copilot
```

## Skills

### security-review

Security checklist and defensive patterns for web applications.

**Triggers**: Authentication implementation, handling user input, API endpoint creation, payment/sensitive features, working with secrets

**Coverage**:

- **Secrets Management**: Environment variables, no hardcoding, gitignore patterns
- **Input Validation**: Schema validation (Zod, Joi), whitelist approach, sanitization
- **SQL Injection**: Parameterized queries, ORM usage
- **Authentication**: Password hashing, secure cookies, session management, MFA
- **Authorization**: RBAC/ABAC, ownership verification, default deny
- **XSS Prevention**: Output encoding, CSP headers, DOMPurify
- **CSRF Protection**: Tokens, SameSite cookies, origin validation
- **Rate Limiting**: IP-based, user-based, exponential backoff
- **Error Handling**: Generic user messages, detailed server logs
- **Security Headers**: HSTS, CSP, X-Frame-Options, etc.
- **Dependencies**: Audit, lock files, automated updates

## OWASP Top 10 Coverage

| Risk | Prevention Pattern |
|------|-------------------|
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

## Usage

The skill activates automatically when working on security-sensitive code. Use the pre-deployment checklist before shipping.

```
# Example prompts that trigger this skill:
"Review this authentication code"
"Is this API endpoint secure?"
"Check for vulnerabilities"
```

## License

MIT
