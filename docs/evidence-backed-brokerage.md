# Process and capability brokerage

The remaining broker surfaces are opt-in and advisory:

```text
asha process route <task> [--json] [--harness claude|codex|copilot|opencode]
asha capabilities match <task> [--json] [--harness claude|codex|copilot|opencode]
asha capabilities plan <capability-id> [--json] [--probe] [--harness ...]
```

`process route` selects a registry-backed workflow with prerequisites, risk,
approvals, verification, and an inline fallback. `capabilities match` resolves
that workflow's capability identifiers against the harness capability registry.
Neither executes the selected process, spawns an agent, writes memory, or
publishes work.

## Dependency plans

`capabilities plan` (issue #124) inspects one explicitly named registry id;
there is no task matching, and route and match never call it. It returns
`asha.capability-plan.v1` and exits 0 whenever a plan is produced; readiness is
in `status`.

`plugins/session/broker/capabilities.json` is the only dependency authority.
An entry's `process.capability_ids` are unconditional `requires` edges, and an
optional `dependencies` list adds typed edges:

```json
{"id": "github-cli-setup", "relation": "requires",
 "when": {"type": "command-missing", "command": "gh"},
 "reason": "gh is not on PATH; follow the official setup documentation first."}
```

- `relation` is `requires` or `optional`.
- `when` is data, never a command: `{"type": "always"}` (the default) or
  `{"type": "command-missing", "command": NAME}` with a bare command name.
- Loading the registry fails closed with `unknown_identifier`,
  `conflicting_metadata` (a target listed twice, or repeating
  `process.capability_ids`) or `dependency_cycle`. Conditions are ignored for
  this check, so an edge that is inactive on one machine cannot hide a cycle or
  a dangling id. JSON errors carry the offending edge or cycle in `details`.
- Overrides may not change `dependencies` (`permission_widening`); they still
  disable entries and add prerequisites, configuration names and approvals,
  which the plan reports.

The plan lists every reached node and edge with its `condition_state`
(`active`, `inactive` or `unevaluated`), each node's declared requirement and
activation, a dependencies-first `resolution_order` that omits inactive nodes,
human `prerequisites` (never machine-checked), `required_config` presence,
`missing` items, `unverified` items, approvals, `blockers`,
`conditional_blockers`, `warnings` and the fallback. Edges below an inactive
node read `skipped`. Prerequisite, configuration, approval and `missing`
items (configuration and commands) carry `applicability`, the same
classification as the three blocker lists: `required` (on an all-`requires` path whose
conditions hold), `conditional` (required only if an unevaluated condition
holds) or `optional`. `status` is, in order of precedence: `blocked` when an
active required node is disabled, unsupported on the harness or missing
configuration; `needs-foundation` when a probe found a required command
missing, so the foundation named in `missing[].remedy` must run first, with
its approvals (it precedes the selected capability in `resolution_order`);
`conditionally-blocked` when a blocker holds only behind an unevaluated
condition; otherwise `no-known-blockers`.

Declared harness support comes from `harnesses/capabilities.json` and is
reported apart from `availability`, which stays `unverified`: the plan does
not inspect installations. Configuration is checked for presence only, never
printed, and presence is not authentication or authorization. Without
`--probe`, `command-missing` conditions stay unevaluated. `--probe` looks at
most 16 commands up on `PATH` and never runs them. Conditions are evaluated
root-first, so nothing below an inactive edge is looked up or reported as
unverified. The plan reads only the two
registries and any overrides; it installs nothing, authenticates nothing,
loads no skill, writes no memory or telemetry, and never runs the selected
workflow.

The former operational context-brief catalogue and memory steward/curator
agents were removed in Memory v2.
