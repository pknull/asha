---
name: devops-inspect
description: "Resolve an owned-host inventory and collect bounded, read-only fleet health evidence over SSH and existing metrics. Use when checking servers, desktops, Raspberry Pi health, machine resources, reachability, backup freshness or monitoring coverage."
---

# Fleet inspection

Keep machine facts in private inventory/runbooks, not per-server skills. This
skill collects snapshots, not continuous monitoring. It never authorizes repairs.

## Resolve scope and access

1. Read the project's instructions and Memory through its existing reader.
   Resolve a user-specified inventory, `ASHA_DEVOPS_INVENTORY`, the project's
   `memory-local/infrastructure/inventory.json`, then
   `~/.asha/devops/inventory.json`. Use the first explicitly applicable source;
   do not silently merge inventories. Read [the inventory contract](references/inventory.md).
2. If none exists, inspect named infrastructure documents and configured SSH
   aliases, not broad filesystem scans or historical conversations. Ask one
   ownership question only if it blocks identifying intended systems; continue
   safe checks on known hosts. Use `devops-unifi` for controller discovery when
   authorized. A controller client record is not ownership or shell access.
3. Separate physical/virtual machines, interfaces, aliases, templates and stale
   controller records. Do not count multiple IPs as multiple machines. Verify
   remote hostname before attributing resource results. Local hostname and
   controller labels are separate evidence, not interchangeable identities.
4. Inspect only documented owned targets within the request. Review selected SSH
   config, including ProxyCommand/ProxyJump and Match exec, before connecting;
   these are executable local configuration. Preserve existing keys and native
   approval gates. Never enroll/change host keys during inspection.

## Collect

Resolve `scripts/fleet.py` beside this loaded skill, not through a hardcoded
Claude or Codex directory. Python 3 and OpenSSH are required on the operator's
POSIX host; the remote Linux host needs Python 3. The helper creates no remote
files, elevates nothing, disables forwarding and multiplexing, and verifies
hostname inside the same SSH session before running its fixed probes.

```text
python3 <skill-dir>/scripts/fleet.py --inventory <private-inventory> --list
python3 <skill-dir>/scripts/fleet.py --inventory <private-inventory> --host <id>
python3 <skill-dir>/scripts/fleet.py --inventory <private-inventory>
```

`--list` never connects. A collection is sequential, bounded to 210 seconds per
selected host (at most 64 records), with per-command time/output limits. Select
individual hosts when a shorter total budget is needed. Unknown selections fail
before connecting. No automatic retry or arbitrary commands from inventory.
Status `ok` means the baseline protocol succeeded, NOT every check passed.
Inspect every check status; missing commands, permissions, caps and timeouts
are coverage gaps. A failed SSH connection does not prove a machine is down.

Read [targeted checks](references/checks.md) for storage health, journal scope,
workloads, backups, Windows/macOS and existing monitoring. The helper explicitly
leaves host-specific storage/workload/backup checks unverified. Complete those
from the runbook when authorized and available, or report the exact gap.

## Report

Write only to an authorized private path after verifying it is ignored and not
tracked; directory 0700, reports 0600. Preserve older snapshots. Do not commit
private host details, credentials, environments, raw API bodies or sensitive
logs. If no private output location is authorized, summarize in the conversation.

Include collection start/end UTC, inventory source and last verification,
coverage per machine and check, observed evidence, impact, and proposed follow-up.
Distinguish inaccessible, permission-limited, unavailable, stale and unverified.
A configured backup is not a successful backup. Metrics can be stale; record
sample time and last successful scrape. A sleeping desktop is expected absence
only when its policy says so; absence alone never proves sleep.

Any remediation, agent/exporter installation, monitoring configuration, wake,
reboot, migration or firewall change is a separate authorized change using
`devops-change`. Do not turn a health check into a deployment.
