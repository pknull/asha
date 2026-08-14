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
| Claude Stop hook | `hooks/stop.sh` | creates `/tmp/asha-marketplace-test-hook-fired` |

The sentinel text retains the historical `marketplace` label for test
compatibility. Installation itself now uses direct symlink mounts and generated
harness artifacts; there is no plugin-marketplace registration flow.

## Install and verify

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
test -f /tmp/asha-marketplace-test-hook-fired && echo TEST-HOOK-OK
```

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
rm -f /tmp/asha-marketplace-test-hook-fired
```

Substitute the installed target as needed.
