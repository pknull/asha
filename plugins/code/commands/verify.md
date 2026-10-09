---
name: code-verify
description: "Run code verification (types, lint, tests, security)"
argument-hint: "[--quick | --full] [--file PATH]"
allowed-tools: ["Bash", "Read"]
---

# Verify Command

Run unified code verification across your project. Automatically detects project type and runs appropriate checks.

## Usage

```bash
/code:verify                    # Standard verification (types, lint, tests)
/code:verify --quick            # Fast checks only (types, format)
/code:verify --full             # Full suite (+ security scans)
/code:verify --file src/app.ts  # Check single file (quick mode)
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
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"
[[ -n "$ASHA_ROOT" ]] || { echo "ERROR: asha_root unresolved — run ./install.sh or launch via the asha wrapper" >&2; exit 1; }
python3 "$ASHA_ROOT/plugins/code/tools/verify.py" $ARGUMENTS
```

If arguments are empty, default to standard level:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"
[[ -n "$ASHA_ROOT" ]] || { echo "ERROR: asha_root unresolved — run ./install.sh or launch via the asha wrapper" >&2; exit 1; }
python3 "$ASHA_ROOT/plugins/code/tools/verify.py" --level standard --verbose
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

The engine reads no configuration file. It selects checkers from project files
(`tsconfig.json`, `pyproject.toml`, `go.mod`, `pom.xml`, `Cargo.toml` and
similar) and the tools found on `PATH`. When that selection does not fit the
project, infer the narrow checks from repository-native manifests and
instructions (`AGENTS.md`, package/build files, CI configuration), run those
instead, and report that inference.

## Tips

- Run `/code:verify --quick` frequently during development
- Run `/code:verify` before committing
- Run `/code:verify --full` before opening PR
- Post-edit hook runs quick checks automatically
