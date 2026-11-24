---
name: devops-unifi
description: "Read UniFi Network controller inventory, topology and health without changing network state. Use when discovering UDM clients, switches, access points, host availability or controller telemetry. Requires UNIFI_BASE_URL and UNIFI_API_KEY for the bundled legacy API reader."
---

# UniFi read-only operations

Controller records establish network presence, not machine ownership or shell
access. Before querying a controller, read the private infrastructure
inventory/runbook. Restrict every request to the authorized controller and site;
do not scan subnets or probe unknown clients. No adoptions, reconnects,
provisioning, port toggles, firmware,
firewall, reservations or policy changes.

## Access

Use the Asha wrapper's environment:

- `UNIFI_BASE_URL`: documented legacy site base, for example
  `https://controller.example/proxy/network/api/s/default`.
- `UNIFI_API_KEY`: existing authorized local controller key.
- `UNIFI_CA_CERT`: optional approved CA/certificate bundle path for TLS trust.

For a service-backed request, first run this local presence check, even when
network access is forbidden. Unset or empty means missing. If local commands
are forbidden or unavailable, report presence as unverified.

```sh
unifi_url_status=${UNIFI_BASE_URL:+present}
unifi_key_status=${UNIFI_API_KEY:+present}
printf 'UNIFI_BASE_URL: %s\n' "${unifi_url_status:-missing}"
printf 'UNIFI_API_KEY: %s\n' "${unifi_key_status:-missing}"
```

Report the observed names/statuses. If either is missing, halt the operation and
tell the user to provision it through the existing Asha wrapper and relaunch via
`asha <harness>`. If network access is forbidden, stop after the check even when
configured and state that no controller was reached. Presence does not verify
API access or site health.

**Blocked reply:** Include the observed variable names/statuses, the setup and
relaunch guidance above for every missing variable, and whether a controller was
reached in the final response. These requirements are cumulative when both
configuration and network access are unavailable. Setup guidance describes a
future authorized session; it authorizes neither credential recovery nor a
service request. Apply the current task's permissions to any action.

Never read or source secrets files, print values, put tokens in argv,
search Claude auto-memory, or ask for credentials in chat. Prefer a read-only-scoped
key where supported; a write-capable key does not authorize writes. Do not
create/rotate keys or migrate secret storage without direction.

Verify TLS normally. No insecure mode is provided. A self-signed certificate
requires independently verified trust configured by the Keeper, not silent
acceptance or copying a prior inspection's one-time TLS exception.

## Query

Resolve `scripts/unifi_read.py` relative to this loaded skill. On a POSIX
operator host with Python 3, run one of:

```text
python3 <skill-dir>/scripts/unifi_read.py clients
python3 <skill-dir>/scripts/unifi_read.py devices
python3 <skill-dir>/scripts/unifi_read.py health
python3 <skill-dir>/scripts/unifi_read.py remembered
```

The helper permits only four GET endpoints, rejects redirects and environment
proxies, uses verified HTTPS, limits a response to 2 MiB, times out sockets at
8 seconds and the CLI at 30 seconds. It emits allowlisted inventory/telemetry
fields, never full controller payloads. Missing expected fields remain unknown.
Use native approval for network access; the helper is not a permissions bypass.

This is explicitly a **legacy Network API adapter**, supported by previously
verified site runbooks, not an implementation of all UniFi APIs. Read
[API families and interpretation](references/api.md) before choosing a family.
Never silently switch endpoints/authentication when an API or permission fails.
Stop after a failed call, report it, and investigate scope/version/trust rather
than retrying login or mutating settings.

## Interpret

Keep active client interfaces, remembered clients, adopted network devices and
physical machines separate. Multiple addresses/MACs can belong to one machine;
a controller name, IP reservation or stale last_seen is not identity proof.
Require host-side identity before attributing host resources. Return the
observation timestamp, API family/site, per-query success/limits and evidence
for deduplication. Report returned coverage, not "all systems", without a
completeness and ownership check. Query each authorized site separately.

Store only in authorized ignored private reports. Treat even filtered fields as
sensitive topology. Delegate machine baselines to `devops-inspect`. Proposals
for changes belong to `devops-change`, never this reader.
