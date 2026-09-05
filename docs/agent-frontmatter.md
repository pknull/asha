# Agent frontmatter schema

Agent sources under `plugins/*/agents/*.md` begin with one YAML frontmatter
mapping. This document is the source schema; `tests/validate-plugins.sh`
enforces it for every agent in the repository.

## Fields

| Field | Presence | Contract |
|---|---|---|
| `name` | required | Non-empty string matching `^[a-z0-9]+(-[a-z0-9]+)*$`. Colons are forbidden. |
| `description` | required | Non-empty string describing the role and its deployment boundary. |
| `tools` | required | `[]` or a comma-separated list of names from `harnesses/capabilities.json` at `agent_frontmatter.tool_vocabulary`. A YAML sequence containing those names is also accepted. |
| `model` | optional | One of `haiku`, `sonnet`, or `opus`. |
| `memory` | optional | Harness-specific memory metadata. |
| `trigger` | optional | Dispatch trigger metadata. |
| `dispatch_priority` | optional | Dispatch ordering metadata. |
| `ownership` | optional | Structured path-ownership metadata. |

No other top-level frontmatter keys are accepted. Optional metadata is kept in
the source schema even when a target adapter deliberately omits it.

Example:

```yaml
---
name: claim-verifier
description: Independently checks review claims against primary text.
tools: Read, Grep, Glob
model: sonnet
---
```

An agent that needs no tools must declare the empty list explicitly:

```yaml
tools: []
```

## Tool vocabulary and enforcement

`harnesses/capabilities.json` is the machine-readable authority for both the
tool vocabulary and whether a target enforces the `tools` allowlist. The
current contract is:

| Harness | `tools` enforcement |
|---|---|
| Claude Code | Enforced by native agent frontmatter |
| OpenAI Codex | Not enforced; the renderer omits this Claude vocabulary |
| GitHub Copilot CLI | Not enforced; the renderer omits it |
| OpenCode | Not enforced; the renderer omits it |

Agent instructions may still impose a read-only role on every harness, but
must not present that role as a harness-enforced capability boundary. Any prose
that describes allowlist enforcement must say that enforcement is **Claude
Code only** and that the same boundary is advisory elsewhere.

## Rendered names

Adapters namespace generated agent paths to avoid collisions. Canonical names
are already colon-free, while the generated-artifact adapters also translate a
legacy colon to `-` before constructing a filename. This compatibility
sanitization does not make colon-bearing source frontmatter valid.
