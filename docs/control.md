# Asha Control: Rooms, state and retired initiative evidence

The default session launcher and dashboard are described in
[Project sessions](session-hub.md), and structured sessions and their
on-demand owners in [Managed sessions](managed-sessions.md). This guide covers the retained Room
surface, Control's doctor, the cockpit, Control's state layout, and the
read-only evidence of the retired initiative engine and `asha task` substrate.

## Command surface

```text
asha room open NAME --project PROJECT --harness H --prompt TEXT [--json]
asha room list [--json]
asha room attach NAME|UUID [--json]
asha room close NAME|UUID [--yes] [--json]

asha control                 open the session dashboard
asha control doctor [--json]
asha control projects [--root DIR]... [--depth N] [--match TEXT] [--json]
asha control tmux
asha control session ...     see session-hub.md and managed-sessions.md
asha control supervisor ...  retired 2026-10-07; prints how to remove its unit

asha initiative list [--json]
asha initiative show <id|slug> [--json]
asha initiative export
```

Exit codes: `0` success (and, for `control doctor`, all required checks
matched); `1` when doctor checks complete with `ok:false`, or on an internal
error; `2` usage or refusal; and `130` interrupted.

`asha task` and `asha trigger` were retired on 2026-10-05, and `asha migrate`
on 2026-10-07; each refuses by name with exit code `2` and never falls
through to a harness launch.

## Rooms: dynamic project sessions

A Room is a detached tmux session running the full Asha persona and operational
layer directly in one initialized Memory v2 project's canonical directory. It
creates no Git or jj workspace and has no plan, seal, integration, TTL,
archive, reopen, or `/session:save` lifecycle. Detaching leaves it running;
only an explicit confirmed close ends its exact-owned tmux session.

An exact existing `PROJECT` path is validated directly, including outside the
configured roots. Friendly names, directory names, and `project_id` selectors
resolve case-insensitively through the configured multi-root project index and
must select exactly one project. Rooms do not require Git or jj.
`H` is one installed `claude`, `codex`, `copilot`, or `opencode`; their prompt
forms are positional for Claude/Codex, `--interactive PROMPT` for Copilot, and
`--prompt PROMPT` for OpenCode. The `ASHA_CLAUDE_CMD`, `ASHA_CODEX_CMD`,
`ASHA_COPILOT_CMD`, and `ASHA_OPENCODE_CMD` executable overrides are honored by
both CLI and dashboard preflight and launch. `open` is detached and returns
immediately with the UUID, tmux identity, and exact attach command.

Room records are `rooms` rows in the Control database, created on the first
Room of a fresh home, and retain only a digest of the opening prompt. Attach and close revalidate the recorded
immutable tmux session/pane IDs and UUID/project markers in the same tmux
server action; a readable tmux name is never ownership evidence. Missing or
ended owned Rooms remain safely closable, while a foreign collision is reported
as `mismatch` and never killed. `close --json`
and other non-interactive closes require `--yes`; the dashboard requires exact
lowercase `yes` every time.

Multiple Rooms may share a project checkout. The CLI and dashboard mark every
live member `shared working tree`; concurrent edits are therefore immediately
visible and need human coordination. A worker whose assignment needs isolation
creates its own worktree.

## Doctor

`asha control doctor [--json]` runs Control's probes and prints one line per
probe, or the `asha.control-doctor.v1` JSON payload: `python`,
`configuration`, `supervisor-service`, `tmux`, `harness`, `gh`,
`rooms-registry`, `managed-sessions`, `hooks` and `tui`. The `gh` and `supervisor-service` probes are informational and never
fail the check; `supervisor-service` now only reports a leftover unit of the
retired supervisor and how to remove it. Hook checks cover only the installed Claude and Codex
configurations. The retired task substrate's probes (`jj`, `repository`,
`default-context`, `transactions`, `registry-backend`, `prunable`,
`stale-workspaces` and `harness-events`) left with it, and `asha task doctor`
is now `asha control doctor`. The `migration` probe left with `asha migrate`.

## Retired initiatives and tasks

The legacy initiative engine, the `asha task` substrate (isolated jj
workspaces and tmux task runs) and the one-shot SQLite staging, activation and
rollback machinery were retired on 2026-10-05 (Keeper ruling, subtraction panel
K1). Nothing migrates, deletes, re-imports or resumes their records. New work
uses the chair, workers and Rooms.

Their evidence stays where it was:

- every record in the Control database's `records` table (the
  `initiatives` and `initiative.*` domains, `tasks`, `creation-journals`,
  `prunes`, `repository-inits`, `authorities`, `registry-activation-ledger`
  and the rest), left in place;
- the frozen file trees under `${ASHA_HOME:-~/.asha}/state/control/`
  (`initiatives/`, `tasks/`, `transactions/`, `prunes/`, `repository-inits/`,
  `authorities/`) and its `logs/`;
- seal commit IDs, which stay reachable in their source repositories.

The read-only reader opens the database with a read-only connection and
writes nothing:

- `asha initiative list [--json]` lists every initiative with its slug, state
  and update time (`asha.initiative-evidence-list.v1`).
- `asha initiative show <id|slug> [--json]` prints one initiative, the count of
  its child records by domain and each seal's node, outcome and `jj_commit_id`
  (`asha.initiative-evidence-show.v1`). An ambiguous slug is refused in favour
  of the id.
- `asha initiative export` writes every row of the `records` table, whatever
  its domain, as one JSON object per line on stdout: `record_id`, `domain`,
  `scope`, `record_key`, `payload` (the stored text, byte for byte), `digest`
  (its SHA-256), `revision`, `state` and `updated_at`. One read transaction
  gives one consistent snapshot while live sessions keep writing; the count
  goes to stderr. A home without a Control database lists and exports nothing.

Every other `asha initiative` verb is refused. Standing authority records
remain as evidence but nothing reads them; a structured session still bound
to an initiative is stopped by its owner rather than served. The
`control.workspace_trust` and `control.workspace_root` configuration keys
still parse so existing configuration loads; nothing but the legacy-layout gate
reads `workspace_root`.

## Cockpit

`asha cockpit [DIR] [--session NAME] [--check|--no-check] [--dry-run]` opens
one tmux window: the left pane runs `asha claude` at `DIR` (default: the
current directory), the chair; the right pane runs `asha control`, the session
dashboard. `DIR` is the projects root the chair resolves projects against
through `asha control projects`. Inside tmux the window is added to the
current session; outside tmux a detached session named `asha-cockpit-<dir>` is
created once and attached. Before opening, a preflight runs `asha doctor
claude` and counts the Asha projects under `DIR`, refusing with the
remediation when the Claude install is not healthy (`--check` runs only the
preflight; `--no-check` skips it; `--dry-run` prints the tmux plan without it).

## tmux

`asha control tmux` prints an optional format snippet and does not edit the
user's tmux configuration. Popups are bound to the client attached to the
caller's own session and never fall back to another tmux client.

### Socket reaping

Some tmux commands start a server when none is running (`list-commands` does
on tmux 3.4); with no sessions it exits at once but leaves its socket file
behind. 1,584 such files from short-lived helpers and test servers accumulated
in `/tmp/tmux-1000` between mid-August and 2 September 2026.

The doctor's capability probe (`list-commands display-popup`) therefore runs on
a private `-S` socket inside a temporary directory that is removed with it, and
never touches the operator's default socket. Tests that start their own
`-L asha-*` servers reap them through the test fixture
`tests/python/socket_reaper.py`: it kills the server, proves it dead before
unlinking (a refused or indeterminate connect counts as live), signals the
socket's same-user holders when `kill-server` cannot reconnect, refuses every
name outside `asha-…`, and stays armed and retryable after a failed close.

No sweep utility exists. A sweep keyed on the `asha-` prefix alone would be
unsafe, because namespace isolation can make a live server look unreachable.

## State locations: one asha root

Everything durable lives under a single root — `$ASHA_HOME`, default
`~/.asha` — with only the ephemeral runtime dir outside it:

```text
${ASHA_HOME:-~/.asha}/config.json
${ASHA_HOME:-~/.asha}/state/control/control.sqlite3    (sessions, Rooms, retired records)
${ASHA_HOME:-~/.asha}/state/control/rooms/              (frozen pre-SQLite Room files, unread)
${ASHA_HOME:-~/.asha}/state/control/initiatives/ ...    (frozen retired evidence)
${ASHA_HOME:-~/.asha}/workspaces/                       (retired task workspaces)
${ASHA_HOME:-~/.asha}/cache/                            (rendered persona files)
${XDG_RUNTIME_DIR:-/tmp/user-$UID}/asha-control/
```

`XDG_STATE_HOME` and `XDG_DATA_HOME` are no longer consumed; setting them is
ignored. `ASHA_HOME` is the one override for the root, exported once by
`bin/asha` so hooks and harnesses agree; `ASHA_CONFIG` still overrides the
config file specifically. The local user and their processes are trusted
(threat model, 2026-10-05), so Control never refuses its own trees for their
modes, owners, link counts or symlinks: a symlinked `$ASHA_HOME` (state on
another disk) and a config file reached through symlinks are followed, and a
group-writable `$ASHA_HOME` works. Control still creates its own directories
0700 and its files 0600.

If the `/tmp/user-$UID` runtime fallback already exists but has a symlink or
non-directory component, Control refuses it and directs the operator to set
`XDG_RUNTIME_DIR` to an existing private directory.

## Chair startup observation

After a successful no-argument seat entry, `bin/asha` passes a generated
current-activity observation to the chair as the harness's native initial
prompt (positional for Claude and Codex, `--interactive` for Copilot,
`--prompt` for OpenCode). It carries only fixed headings, a UTC timestamp, and
the dashboard's observed Room and worker counts and summary, labelled as lower
bounds when the snapshot is partial; it never includes retained labels,
commands, message bodies, or criteria. When the observation is unavailable the
prompt says so. Explicit arguments keep their exact argv and caller cwd, and
`--yes`, a failed seat entry, persona-off launches, Rooms and workers receive
no startup prompt.
