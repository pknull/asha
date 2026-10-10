# DevOps Plugin

**Version**: 0.1.0

Portable infrastructure procedures for Claude, Codex, Copilot and OpenCode.
One canonical source per skill; the existing Asha installer mounts them as
`devops-inspect`, `devops-unifi` and `devops-change`. These are skills, not
colon-named slash commands. No harness-specific tool grants are bundled.

| Skill | Responsibility |
|---|---|
| inspect | Owned inventory, strict SSH identity gate, bounded Linux baselines, evidence gaps and targeted follow-up |
| unifi | Read-only controller inventory/health with a filtered legacy API helper |
| change | Explicitly authorized changes with preflight, recovery and verification |

Ask to inspect the owned systems, discover UniFi clients, or plan a named
infrastructure change. The matching skill supplies its workflow. Scripts are
resolved relative to the loaded skill, not a Claude/Codex home path.

## Configuration boundaries

Host facts belong in a private inventory/runbook, not a per-server skill or this
repository. Inspection resolves an explicit inventory, `ASHA_DEVOPS_INVENTORY`,
the project's `memory-local/infrastructure/inventory.json`, then
`~/.asha/devops/inventory.json`. See the inspect skill's inventory contract.
Existing SSH configuration owns connection details and keys.

UniFi uses `UNIFI_BASE_URL`, `UNIFI_API_KEY` and optional `UNIFI_CA_CERT` exported
by the existing Asha wrapper. Never place real values here. Provisioning these
values or certificate trust is a separate operator action; legacy harness
memory is not a credential backend. The reader intentionally has no insecure
TLS option and no mutation mode. It does not implement the newer Integration
or cloud Site Manager APIs.

The Python helpers run on a POSIX operator host; fleet SSH targets require
Linux and Python 3. No third-party Python dependencies. Existing exporter or
native management evidence is used separately for Windows/macOS, not represented
as Linux baseline coverage. No SSH agent forwarding or credential transfer.

## Installation and verification

Use the ordinary installer when deployment is authorized:

```text
asha install claude --only devops
asha install codex --only devops
```

New mounts become discoverable when the harness refreshes its skill catalog
(normally a new session). Source creation alone does not install or refresh
running sessions. Preserve native approvals; installation does not authorize
remote changes or automatically provision credentials/inventory.

Offline checks:

```text
python3 -m unittest discover -s tests/python -p test_devops.py
./tests/validate-plugins.sh
./tests/validate-versions.sh
```

Test mount parity in isolated harness homes before deployment. The helper tests
mock remote/API transport; they do not prove a live controller's compatibility,
credentials, trust or host access. Report those separately.
