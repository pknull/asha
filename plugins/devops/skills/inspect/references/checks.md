# Targeted inspection checks

Use existing tools and documented paths. Apply a per-command timeout (typically
8–15 seconds), record exit status, and cap records/bytes before exporting output.
No installs, full-disk tests, filesystem crawls, SMART self-tests, stress tests,
repair commands or persistent agents during inspection. `sudo -n` only for an
explicitly authorized read; denied access remains permission-limited.

## Linux and Raspberry Pi

The bundled baseline checks OS/kernel, uptime/reboots, CPU/load sample,
RAM/swap/pressure, space/inodes/local mounts, block layout, failed system units,
NTP properties, and Pi temperature/throttling/core voltage. Missing utilities
are unavailable, not healthy. Nonzero exit is a gap, not necessarily a fault.
Mounts are inventory reads; do not walk network shares. Swap occupancy alone is
not current memory pressure. Compare temperatures to actual model thresholds.
Pi core voltage is not a measurement of its external 5V power supply.

Journal baseline is warning-or-higher, past 24 hours, last 1000 records, 1 MiB
cap, effective-user visibility only. Zero matches cannot establish no system
errors. For full evidence, query the relevant unit or current-boot kernel with
an authorized reader, preserve query exit status, cap locally, and return
category counts/timestamps rather than raw messages. Record truncation and
visibility; inspect narrowly redacted relevant excerpts only when needed.

## Storage

Resolve actual devices from mounts and block layout first. Prefer existing
SMART/NVMe/ZFS/mdraid telemetry. Examples for documented devices: `smartctl -n
standby -H -A <device>` (do not wake idle disks), `nvme smart-log <device>`,
`zpool status`, `/proc/mdstat`. Record unavailable tools/permissions. No tests,
scrubs or resyncs started. A SMART pass is not a guarantee; examine counters,
trends and historical context. SD-card wear may be unobservable. Judge scrub
freshness against the documented schedule, not a guessed interval.

## Workloads and backups

Read the relevant runbook before checking. Query selected system/user units and
named containers with narrow state fields (status, health, restart count,
OOMKilled), never full Docker inspect, compose config, environments or tokens.
Stopped containers can be intentional; confirm expected state. Check a bounded
read-only application endpoint using normal TLS, without triggering jobs or
printing response secrets. Distinguish LAN/hairpin checks from external access.

Check documented backup result metadata, exit status and last-success timestamp
against cadence. Inspect only designated backup directories/catalogs. A timer,
archive filename, Git ref mtime, snapshot or configured job alone proves neither
successful current coverage nor recoverability. Distinguish same-host snapshot,
independent copy, offsite copy and restore-test evidence. Do not restore or
transfer backup data during an inspection.

## Desktops and metrics

Use existing Prometheus/node_exporter/windows_exporter or an authorized native
management channel. Prometheus scrape success is reachability, not application
health. Record target identity, sample timestamp and stale/absent data. Controller
client telemetry cannot supply host CPU/RAM/disk health.

Windows: an RDP port response is not authenticated resource access. Use the
already deployed exporter or locally executed, bounded PowerShell/CIM reads of
OS, CPU, memory, disks and selected service/event records. Do not enable WinRM,
SSH or exporters, adjust firewall, or prevent sleep to obtain a report. macOS
needs its native probes; the Linux helper must not be treated as full coverage.

For a monitoring proposal, separate continuous metrics from on-demand privileged
reads. Account for always-on versus sleeping desktops, shared switch/upstream
dependencies, per-host expected workloads, backup freshness, alert ownership and
retention. Reuse deployed collectors before proposing another stack. Obtain
separate approval before implementing the proposal.
