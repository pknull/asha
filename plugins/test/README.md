# Test Plugin — Installer Canary

This plugin exists for Asha installer development. It verifies that commands,
skills, agents, and a Claude Stop hook resolve from the installed topology. It
is not an application testing framework.

## Surfaces

| Surface | Source | Expected sentinel |
|---|---|---|
| Claude command | `commands/ping.md` (`/test:ping`) | `TEST-PING-CMD-OK …` |
| Skill | `skills/ping/SKILL.md` | `TEST-PING-OK …` |
| Agent | `agents/echo.md` | `TEST-ECHO-OK …` |
| Claude Stop hook | `hooks/stop.sh` | appends a timestamp to the canary marker |

The sentinel text retains the historical `marketplace` label for test
compatibility. Installation itself now uses direct symlink mounts and generated
harness artifacts; there is no plugin-marketplace registration flow.

## Install and verify

The canary is excluded from default installs. Use `--only test` to install
just this plugin, or `--with-canary` to include it with the normal plugin set.

### Claude Code

```bash
./install.sh --only test --target claude
```

Restart Claude Code, then:

```text
/test:ping
Run the test-ping skill.
Spawn the test-echo agent.
```

End the session and inspect the hook marker:

```bash
./install.sh --only test --target claude
export ASHA_CANARY_MARKER="$(mktemp)"
# launch/restart Claude Code from this environment, end a session, then:
marker="$ASHA_CANARY_MARKER"
test -s "$marker" && echo TEST-HOOK-OK
```

`ASHA_CANARY_MARKER` is the recommended explicit marker path. Without it, the
hook uses `$XDG_RUNTIME_DIR/asha-canary-hook-fired` when that directory exists
and is owned by the caller. Otherwise it creates a unique
`${TMPDIR:-/tmp}/asha-canary-hook.XXXXXX` marker. Marker failures are
fail-open: the hook still prints `{}` and exits successfully.

### Codex or Copilot

```bash
./install.sh --only test --target codex
./install.sh --only test --target copilot
```

Commands are rendered as skills and agents are rendered to each harness's
native format. Request `test-ping` and `test-echo` by name. The Claude-specific
Stop-hook marker is not the parity test for those harnesses; use `asha doctor`
and the repository installer tests for their hook contracts.

## Preferred verification

The canary answers “did this primitive resolve?” It does not prove the whole
installation is healthy. Run:

```bash
asha doctor
./tests/run-tests.sh
./bin/asha-drift-check.sh --target codex   # after Codex install changes
```

## Cleanup

```bash
./uninstall.sh --target claude    # removes the complete Asha install for that target
rm -f "$marker"                  # removes the explicit marker from the example
```

Substitute the installed target as needed.
