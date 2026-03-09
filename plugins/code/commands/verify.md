---
description: "Run code verification (types, lint, tests, security)"
argument-hint: "[--quick | --full] [--file PATH]"
allowed-tools: ["Bash", "Read"]
---

# Verify Command

Run unified code verification across your project. Automatically detects project type and runs appropriate checks.

## Usage

```bash
/verify                    # Standard verification (types, lint, tests)
/verify --quick            # Fast checks only (types, format)
/verify --full             # Full suite (+ security scans)
/verify --file src/app.ts  # Check single file (quick mode)
```

## Verification Levels

| Level | Checks | Speed | Use When |
|-------|--------|-------|----------|
| `quick` | Types, format | <10s | Post-edit, quick sanity |
| `standard` | Types, lint, tests | 30-60s | Before commit |
| `full` | + security, audit | 2-5min | Before PR, release |

## Supported Languages

| Language | Type Check | Lint | Test | Security |
|----------|-----------|------|------|----------|
| TypeScript | tsc | biome/eslint | npm test | npm audit |
| Python | mypy | ruff | pytest | bandit, safety |
| Go | go build | go vet, staticcheck | go test | gosec |
| Java | mvn compile | - | mvn test | mvn verify |
| Rust | cargo check | clippy | cargo test | cargo audit |

## Execution

Run the verification engine:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/verify.py" $ARGUMENTS
```

If arguments are empty, default to standard level:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/verify.py" --level standard --verbose
```

## Output

```
Verification: PASS (12.3s)
Project: typescript | Level: standard

  ✓ tsc (2.1s)
  ✓ biome-check (0.8s)
  ✓ test (9.2s)

Summary: 3/3 checks passed
```

On failure:

```
Verification: FAIL (8.4s)
Project: typescript | Level: standard

  ✓ tsc (2.1s)
  ✗ biome-check (0.3s)
      src/auth.ts:42 - Unexpected console.log
  ✓ test (5.8s)

Summary: 2/3 checks passed
```

## Configuration

Create `.claude/verify.yaml` to customize (optional):

```yaml
level: standard
checkers:
  typescript:
    typecheck: true
    lint: true
  python:
    enabled: false
thresholds:
  coverage: 80
```

## Tips

- Run `/verify --quick` frequently during development
- Run `/verify` before committing
- Run `/verify --full` before opening PR
- Post-edit hook runs quick checks automatically
