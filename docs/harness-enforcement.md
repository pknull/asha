# Harness Enforcement and Memory Delivery

Asha installs one source corpus into four harness-native surfaces. The shared
contract is portability of behavior, not identical primitives.

## Capability matrix

| Capability | Claude Code | OpenAI Codex | GitHub Copilot CLI | OpenCode |
|---|---|---|---|---|
| Commands | Native slash commands | Rendered skills | Rendered skills | Native commands |
| Agents | Markdown agents | TOML custom agents | `.agent.md` files | Native agents |
| Persona | SessionStart context | Developer-instruction render | Agent instructions | Plugin context |
| Policy guard | Native `PreToolUse` | Native hooks/rules where supported | `preToolUse` adapter | `tool.execute.before` adapter |
| Recovery state | Native prompt/tool/session hooks | Native prompt/tool/session hooks | `asha-recovery.json` hooks | Direct plugin callbacks |
| Semantic Memory publication | Explicit `/session:save` | Explicit session save skill | Explicit session save skill | Explicit session save command |
| Workspace context | SessionStart delivery | SessionStart delivery | SessionStart delivery | Plugin SessionStart delivery |

## Control status event claims

Control writes one bounded current snapshot per managed run. These are status
observations, not enforcement hooks, and only the following native bindings are
claimed:

| Control event | Claude Code | OpenAI Codex |
|---|---|---|
| `session-start` | Wired from `SessionStart` | Wired from `SessionStart` |
| `prompt-submitted` | Wired from `UserPromptSubmit` | Wired from `UserPromptSubmit` |
| `tool-completed` | Wired from `PostToolUse` | Wired from `PostToolUse`; interception is known incomplete for `unified_exec` |
| `permission-requested` | Not claimed. `Notification` is multi-purpose and its payload is unverified. | Wired from `PermissionRequest`; delivery before the operator answers is live-proven on Codex 0.147.0. |
| `turn-stopped` | Wired from `Stop` | Wired from `Stop`; live-proven on Codex 0.147.0. Delivery remains subject to Codex's hash-bound interactive hook trust. |
| `session-ended` | Wired from `SessionEnd` | Codex has no equivalent event. |

Copilot and OpenCode provide process liveness only; Asha claims no semantic
Control events for either harness.

A harness with no wired stop or exit event (Copilot and OpenCode) never
emits a signal that supersedes an in-progress `working`/`needs-input` snapshot.
Reconciliation therefore ages those states to `unknown` once the snapshot is
older than `control.event_staleness_seconds` (default 30 minutes): a live
process with only stale in-progress evidence reads as `unknown`, never as a
false positive. Observed 2026-08-16: a Codex task otherwise reported `working`
for 25+ hours while idle at its prompt before Codex Stop was wired. Claude
wires `Stop` and `SessionEnd`; Codex now wires the live-proven
`PermissionRequest` and `Stop` seams but has no session-end equivalent. The
Codex 0.147.0 permission probe fired before the operator answered a real
network escalation, and the same managed turn later delivered `tool-completed`
and `turn-stopped`. A newly rendered Codex hook command requires its own
interactive trust grant, so installation shape is not itself proof of delivery.

No harness performs automatic semantic publication. Prompt and tool hooks write
only ignored recovery state under `Work/session-state/`. Session end seals that
state and prunes entries older than seven days. An explicit save uses the live
model context to publish `Memory/activeContext.md` and `Memory/decisions.md`, then
validates and performs the requested Git operation.

## Policy boundary

Claude Code has the broadest native hook surface. Codex supports native hooks
and execution rules, including denial for supported simple shell, `apply_patch`,
and MCP calls. This is not a complete enforcement boundary: interception of
every unified shell path and every tool is not available.

Copilot's `preToolUse` adapter translates payloads into the shared policy
engine. Upstream parallel-hook and timeout behavior means it remains a
guardrail, not containment. OpenCode runs the same shared policy through
`tool.execute.before`; unsupported interactive `ask` decisions become denials.

Secret scanning remains a separate pre-tool guard. Memory publication is not a
policy side effect and no save gate runs during ordinary tool use.

## Recovery state

Each active harness session owns one file:

```text
Work/session-state/<harness>-<session>.json
```

The writer uses a same-directory temporary file plus atomic replacement, caps
the serialized document at 2 KiB, keeps at most ten deduplicated paths, scrubs
secret-shaped values, and records the stable project identifier from
`.asha/config.json`. The directory is ignored and is not part of published
Memory.

SessionStart may present the newest unexpired recovery state as explicitly
unpublished continuity. UserPromptSubmit records prompt metadata and performs
direct RP routing. PostToolUse records changed paths. SessionEnd seals and
prunes; it does not summarize, commit, or push.

## Installed hook surfaces

### Claude Code

`plugins/session/hooks/hooks.json` is the source of truth. It registers
SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, and SessionEnd
handlers, plus a Codex-only `PermissionRequest` handler.

### OpenAI Codex

The installer renders source hooks and execution rules into Codex-native
configuration. Custom agents and commands are rendered separately because
Claude command metadata is not portable to Codex.
Control-managed launches trust the workspace root through a per-launch
override; they never modify Codex's persisted trust store.

### GitHub Copilot CLI

The installer emits:

- `asha-guardrails.json` for translated policy and secret guards
- `asha-recovery.json` for start, prompt, post-tool, and session-end recovery
- the remaining feature-specific hook files required by installed plugins

Legacy lifecycle and nudge hook files are removed during reconciliation.

### OpenCode

`harnesses/opencode.sh` generates `plugins/asha.js`. It calls the shared recovery
handlers directly for start, prompt, post-tool, and dispose. Dispose invokes
only the session-end seal path. Commands and agents remain native Markdown under
plural `commands/` and `agents/` directories.

## RP and workspace routing

RP routing is a direct UserPromptSubmit concern, sourced from
`plugins/session/hooks/handlers/rp-routing.md`; it no longer depends upon a
general nudge engine. Workspace context remains a SessionStart concern and is
delivered before optional guidance. Canonical workspace `knowledge/` indexes and
promotion infrastructure are unaffected by removal of the operational Memory
catalogue.

## Verification

The machine-readable support matrix is `harnesses/capabilities.json`. Installer
tests prove rendered artifacts and stale-file reconciliation. Drift checks
compare installed surfaces with their generated source:

```bash
./tests/run-tests.sh
./bin/asha-drift-check.sh --target codex
./bin/asha-drift-check.sh --target opencode
```

Treat `supported`, `partial`, and `unsupported` in the capability matrix as
claims requiring those tests. Documentation does not upgrade a harness
primitive that the host cannot enforce.
