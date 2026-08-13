# Process and capability brokerage

The remaining broker surfaces are opt-in and advisory:

```text
asha process route <task> [--json] [--harness claude|codex|copilot|opencode]
asha capabilities match <task> [--json] [--harness claude|codex|copilot|opencode]
```

`process route` selects a registry-backed workflow with prerequisites, risk,
approvals, verification, and an inline fallback. `capabilities match` resolves
that workflow's capability identifiers against the harness capability registry.
Neither executes the selected process, spawns an agent, writes memory, or
publishes work.

The former operational context-brief catalogue and memory steward/curator
agents were removed in Memory v2. Canonical workspace knowledge lookup remains
owned by the workspace knowledge tools rather than this broker.
