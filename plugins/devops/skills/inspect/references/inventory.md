# Private inventory contract

This is a new DevOps convention, not a claim that a file already exists. Keep
one authoritative inventory and link runbooks to it. Never put personal host
records or credential values in the portable Asha plugin.

Discovery order: explicit user path, `ASHA_DEVOPS_INVENTORY`, project
`memory-local/infrastructure/inventory.json`, then
`~/.asha/devops/inventory.json`. Resolve explicitly and pass `--inventory` to the
helper; its standalone default is the environment path or the user-private path.
Do not load another project's inventory just because its file exists.

Example (fictional, not an authorized target):

```json
{
  "version": 1,
  "verified_at": "2026-01-01T00:00:00Z",
  "hosts": [{
    "id": "example-server",
    "kind": "host",
    "owned": true,
    "expected_hostname": "example-server",
    "ssh": {"target": "example-server"},
    "availability": "always-on",
    "role": "application server",
    "runbook": "hosts/example-server.md"
  }]
}
```

Required: version 1; 1..64 host records with unique IDs, explicit boolean
`owned`, and `kind` host/alias/template/controller-record. Only owned `host`
records can connect. A host without `ssh` remains in coverage as unavailable.
For SSH, require exact `expected_hostname` and `ssh.target` (existing alias or
user@address); optionally `ssh.identity_file`, an absolute path reference, never
key contents. Use configured aliases for IPv6/ports/jump hosts. Do not generate
or change SSH config during an inspection. Existing SSH config remains the
connection authority; inventory points to it rather than copying every option.

Optional descriptive fields: verification time/source, interfaces and their
identity evidence, alias_of, VM/physical relationship, role, availability,
workload runbook, metrics URL/identity/sample freshness, backup job/destination/
expected cadence/last evidenced success/restore test. The helper never executes
commands from these fields. Record unknown ownership as `owned: false` pending
confirmation, not as permission to probe. Label time-limited observations.

Keep authentication in the existing Asha secret-export mechanism or SSH agent;
store variable names/key paths only here. Do not search harness memory for
credentials or duplicate keys in inventory. Private documents themselves may
contain sensitive topology; confirm ignores and access mode before writing.
