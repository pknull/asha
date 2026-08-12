# Evidence-backed memory and capability brokerage

Issue: [#27](https://github.com/pknull/asha/issues/27)

Brokerage is opt-in. Existing sessions do not invoke it automatically.

```text
asha context brief <task> [--json] [--budget-bytes N] [--timeout-ms N]
asha process route <task> [--json] [--harness claude|codex|copilot|opencode]
asha capabilities match <task> [--json] [--harness claude|codex|copilot|opencode]
```

All three commands run deterministic inline protocols. The specialist agents
are optional harness wrappers around those protocols; they are never required
for correctness and never spawn another broker.

## Context protocol

`context brief` reads catalogue metadata only:

1. `ASHA_MEMORY_DIR/MEMORY.md`, when explicitly configured;
2. `<active-repository>/Memory/MEMORY.md`;
3. the detected workspace operational `Memory/MEMORY.md`; and
4. `ASHA_LEARNINGS_DIR/index.md`, or `~/.asha/learnings/index.md`.

It does not recursively scan memory, load raw transcripts, or read indexed
document bodies. Catalogue links must remain within their source root.
Descriptions resembling credentials or private keys are omitted. The response
labels project operational, workspace operational, and evaluated-local sources
with authority, scope, catalogue provenance, match inference, and learning
confidence where available.

`--budget-bytes` bounds total catalogue bytes read. `--timeout-ms` bounds work;
zero forces an immediate typed timeout result. The output always states budget
and timeout exhaustion and returns `no_relevant_context: true` rather than
broadening the search. `source_signature` is deterministic over the catalogues
read and lets a caller reuse an unchanged briefing without rescanning document
bodies. The command does not modify any memory plane.

## Routing and matching

`process route` selects one registered process template and reports:

- prerequisites and risk;
- expected verification;
- explicit approval requirements;
- selected capability identifiers;
- resolved harness status and limitations; and
- an inline fallback.

`capabilities match` resolves those identifiers against the broker registry and
then against `harnesses/capabilities.json`. It reports
`native | rendered | partial | unsupported` exactly as recorded by the harness
contract. Selection is advisory. Neither command starts loops, creates
worktrees, executes tools, changes files, publishes, commits, pushes, merges,
deletes, or runs destructive commands.

## Registry and overrides

The shipped, versioned registry is:

```text
plugins/session/broker/capabilities.json
plugins/session/broker/capabilities.schema.json
```

It references `harnesses/capabilities.json` schema v3 by capability identifier;
it does not copy harness support claims. Each entry owns task patterns,
categories, prerequisites, configuration names, risk/approval metadata, output
contract, permissions, fallback, owner, and version.

Overrides merge by existing identifier, in this order:

1. `~/.asha/broker-capabilities.override.json`
2. the nearest `.asha/broker-capabilities.override.json`
3. `ASHA_BROKER_OVERRIDE`
4. repeated `--override PATH`

Example tightening override:

```json
{
  "schema_version": 1,
  "capabilities": [{
    "id": "memory-steward",
    "enabled": false,
    "risk": "high",
    "approval": ["explicit-review"],
    "prerequisites": ["approved-context-scope"],
    "required_config": ["PRIVATE_MEMORY_ENABLED"]
  }]
}
```

Overrides cannot add identifiers, enable a disabled entry, remove a
prerequisite/configuration/approval, lower risk, alter permissions or output
contracts, add commands/actions, or modify harness support. An attempted
`unsupported` → `partial/rendered/native` promotion therefore fails with a
typed `permission_widening` error rather than changing the result.

## Agent surfaces

The session plugin ships four source agents:

| Role | Claude | Codex | Copilot | Boundary |
|---|---|---|---|---|
| memory-steward | native agent | rendered TOML | rendered `.agent.md` | read-only context |
| memory-curator | native agent | rendered TOML | rendered `.agent.md` | proposals only |
| process-router | native agent | rendered TOML | rendered `.agent.md` | advisory only |
| capability-broker | native agent | rendered TOML | rendered `.agent.md` | registry claims only |

The exact status is resolved at runtime through capability references; this
table describes the current schema-v3 registry, not an independent promise.
Partial hooks remain partial enforcement and are irrelevant to broker
correctness.

The curator's output is `asha.memory-curation-proposal.v1`. It may prepare a
review candidate in its response, but it has no write tools and must report
`publication_performed: false`. Canonical/shared publication still requires an
explicit user decision and the configured Git review workflow.

## Telemetry

Telemetry is disabled by default so context brokerage remains read-only.
Set ASHA_BROKER_TELEMETRY=1 to opt in. Best-effort telemetry is then written to
`$ASHA_HOME/state/broker-events.jsonl` with mode `0600`. It records only event
type, harness, result count, status, version, and timestamp—never task text,
paths, matched content, credentials, or output. Any value other than explicit
1, and the existing silence marker, disable it.
