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

## Fail-open completion nudge

The nudge comes from a handler under `plugins/session`; harness adapters only
select the verified delivery seam. It never publishes Memory or blocks a tool.

| Nudge | Claude Code | OpenAI Codex | GitHub Copilot CLI | OpenCode |
|---|---|---|---|---|
| Declared verification pass | `Stop` runs `verify-pass-complete.sh`; remaining fixed-string hits return one `{"decision":"block","reason":...}` retry and `stop_hook_active` suppresses the loop | Same Stop JSON, subject to Codex's per-command hash-bound hook trust | No Stop claim: next `userPromptSubmitted` runs the handler and returns top-level `additionalContext` | Root `session.idle` appends handler stdout to pending context for the next system transform; known child-session idles are ignored |

The declared-pass handler excludes `.git`, `.jj`, and `Work` from its
fixed-string search, including binary/NUL-containing files, names remaining
files, and after an empty proof clears the marker only
when its `old` value, re-read under the lock, still equals the value that was
proved. Internal search or lock errors fail open. The marker lock uses
`flock(1)`, which `lib/portable.sh` does not yet cover: without it the
declare tool refuses with a Linux-only error and the handler is a `{}` no-op.

Hooks can run outside the harness sandbox, so no handler executes a file inside
the project tree, which a sandboxed agent can write. The project-local
`.asha/style-audit` nudge and the project `.asha/.venv` interpreter preference
were removed for that reason; Python handlers use the system `python3`.

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
| `turn-stopped` return channel | The Stop hook can return one fail-open `block` wake decision per new coordinator journal cursor; a hub session's pending graceful-close request rides the same channel once per request (`docs/session-hub.md`). | not claimed; requires a live probe. Hub close requests for Codex are therefore queued-only. |
| `session-ended` | Wired from `SessionEnd` | Codex has no equivalent event. |

Copilot and OpenCode provide process liveness only to Control tasks; Asha
claims no semantic task events for either harness. OpenCode hub sessions report
session start, tool start/end and idle through the generated plugin, and
Copilot hub sessions report session start, prompt, tool completion and session
end through its recovery hook file (`harnesses/capabilities.json`; neither is
live-proven).

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

**Recorded Codex ask finding (documentation checked 2026-09-02; no behavior
change):** PreToolUse `ask` is "parsed but not supported yet. Codex marks the hook run as failed, reports the error, and continues the tool call";
"PermissionRequest accepts only allow|deny." Asha therefore retains its
existing conservative Codex policy mapping from `ask` to an exit-2 denial
instead of emitting an inert ask response.

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
Headless Codex result staging remains supported when its sandbox cannot reach
the tmux socket or host PID ancestry: a digest-bound launch token proves the
staging reservation, while the pane proof remains primary wherever tmux is
reachable.

Terminal session reports and Memory handoffs take their session from the
environment only (`ASHA_HUB_SESSION_ID` and `ASHA_HUB_GENERATION`), with no
tmux or process-ancestry proof; the generation and lifecycle fences refuse a
stale or closed session. They write Control state, which the workspace-write
sandbox mounts read-only. The installer-owned `rules/asha.rules` therefore pins
`asha` to `$HOME/.local/bin/asha` with `host_executable` and allows exactly
`asha control session report` and `asha control session handoff`, so Codex runs
those two outside the sandbox without a prompt. Neither takes a session
argument, and no allow rule may add one or set those variables, so both act
only for the session the environment names. Codex bypasses the sandbox only
when every segment of a command is allowed, so a save chained with other
commands, or using redirection, `$VAR` or a heredoc, stays sandboxed and fails
on the read-only Control state. Without the pin, a bare-name allow rule also matches a planted `./asha`,
`Work/asha` or `/tmp/x/asha`; with it they no longer match. No allow rule
renders when `HOME` is empty or `/`. Tested qualifier: Codex 0.160.0, terminal
Rooms and workers, by `codex execpolicy check --resolve-host-executables`
against the rendered rules; a live Room probe is pending (#112). Structured
Codex workers were not probed.

The pin covers only `asha`. Allow rules you approve yourself, such as those
Codex appends to `~/.codex/rules/default.rules`, also match by bare name: an
agent that writes `./git` or `Work/curl` into its workspace matches a `git` or
`curl` allow rule and runs it unsandboxed. Pin each program you allow with your
own `host_executable(name = ..., paths = [...])` line. An allowed `asha` command
also runs asha's own code, so do not run a Codex session whose writable
workspace contains the checkout that `~/.local/bin/asha` points to.

#### Installer preservation boundary

Install, update and uninstall **never create, write, replace, remove, back up,
rename or chmod Codex's shared `config.toml`.** Features, MCP, native hook
trust, workspace trust, comments, line endings and trailing bytes remain
native-owned. A concurrent native replacement or in-place save is not
overwritten or restored from a snapshot. There is no TOML publisher, feature
insertion, trust grant or migration. Install reads the file, following a
dotfiles symlink, and refuses only when it is not a regular file, does not
parse as TOML, or gives `features` or `hooks` a non-table value or
`features.hooks` a non-boolean one. Uninstall does not read it. Inline hooks
are not inspected: a leftover pre-JSON `asha:start` block is the user's to
delete (INSTALLER.md).

Asha renders deterministic native `CODEX_HOME/hooks.json`, without
`hooks.state`, and owns it through the generated-artifact ledger like every
other generated file: identical bytes are adopted, a foreign, modified or
symlinked file refuses unless `--force`, and uninstall removes it only while
its bytes match the recorded hash (a modified file stays, with a warning).
Install renders the hooks and checks the config before any staging, mounts,
rules, agents or legacy cleanup, so that refusal changes nothing. Direct
sourced hook calls partial-finalize their own manifest cycle, retaining
unrelated records and caller shell/stage state. Dry-run performs the same
checks without publishing hooks or ownership. The local user and their
processes are trusted (Keeper threat model, 2026-10-05): no ancestor, owner,
link-count or identity check guards these paths, and the ledger's replacement
is not compare-and-swap against a concurrent writer.

The preserved `f491` last-config-rename failure and its red receipt remain
historical evidence; they were not waived or relabeled. The obsolete writer
assertion is replaced by real native replacement/in-place saves at owned JSON
publication/removal, plus full-process syscall evidence of zero installer
config writes. Removing the shared-config writer removes that native-save
loss path, not every possible race against arbitrary artifact writers.

Both installed drift and Control hook probes inspect absent, malformed and
ownership evidence (the installed `hooks.json` must match its ledger row)
against actual expected commands/filters, including verification `Stop` and
recovery `PostToolUse`.
Explicit `features.hooks=false` is disabled. The retained **0.153.4** empty-config
default-true evidence applies only to that release; other absent-flag versions
remain unavailable/unsupported. Neither probe inserts a feature flag.
**Registered, enabled, trusted and executed are different states.** Hash-bound
hook trust stays native and user-controlled; slot counts and a workspace trust
fixture are not hook loading/execution proof. Native acceptance remains the
operator's separate live step.

The parent install/uninstall engines own launcher outcomes. Failed attempted
adapters are distinct from unattempted harnesses requested independently by
`--bin` or `--default`: documented `./install.sh --bin all` still requests all
four shims with the default Claude target, and sourced `install_bin` needs no
adapter-result prerequisite. Failed targets cannot retarget their shims or
default. Shared dispatcher/root/default changes must reuse compatible routing
or refuse nonzero when they would redirect failed or unrequested existing
consumers; successful other adapters are not rolled back. `--default` without
bin installation retains its existing no-default-write behavior. Uninstall
removes only proven-owned shims for successful selected adapters, including
`--target all`, and retains the dispatcher for protected, foreign or unknown
survivors. Outcome state is invocation-local, including repeated sourced calls.
Hidden immediate bin entries count as consumers too. An unknown invocation
that depends on the default remains protected even when all known harnesses
are requested; compatible routing with an unchanged default remains reusable.
The source-only launcher helper is `lib/installer-launchers.sh`; public entry
points and Bash 3.2 compatibility are retained.

### GitHub Copilot CLI

The installer emits:

- `asha-guardrails.json` for translated policy and secret guards
- `asha-recovery.json` for start, prompt, post-tool, and session-end recovery,
  each followed by a hub-session-only `control-event.sh` report
- the declared-pass next-prompt check in `asha-recovery.json`
- the remaining feature-specific hook files required by installed plugins

Legacy lifecycle and nudge hook files are removed during reconciliation.

### OpenCode

`harnesses/opencode.sh` generates `plugins/asha.js`. It calls the shared recovery
handlers directly for start, prompt, post-tool, and dispose, and runs the
declared-pass handler on `session.idle`.
Pending output enters the next system-context transform. Dispose invokes only
the session-end seal path. Commands and agents remain native Markdown under
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

## Optional session experience

[Session experience and reviewed learning](session-experience.md) documents the
dormant project policy, bounded report/close capture, one-turn review custody,
explicit-save dispositions, selected guidance and coverage metrics. Policy defaults
to off; native automatic review remains gated pending separately approved probes.
Ordinary and scope-none Memory publication require both pre-draft snapshot digests;
close remains independent of successful capture or completed review.
