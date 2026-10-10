---
name: devops-change
description: "Plan and execute explicitly authorized infrastructure changes with identity gates, preflight, recovery and live verification. Use when changing server workloads, host configuration, exporters, monitoring or network services; not for read-only health inspections."
---

# Controlled infrastructure changes

This skill is a procedure, not standing authorization or a remote execution
engine. A request to inspect, propose monitoring or create a skill does not
permit installing agents, changing firewall rules or restarting services.

1. **Establish authority and identity.** Read the private inventory and target
   runbook, plus repository instructions for any local files. Confirm the exact
   target by remote hostname using strict existing SSH host-key verification.
   Resolve ambiguity before any mutation. Retain native permission gates and
   existing trust; never disable them to make automation work.
2. **Reproduce or establish baseline.** Use bounded read-only probes from
   `devops-inspect`. State evidence, plausible alternatives and intended success
   checks. Identify dependents, active users and required downtime. Do not infer
   a fault from a stopped container or absent sleeping desktop.
3. **State the authorized change.** Name affected files/services/machines,
   side effects, maintenance window, success criterion and rollback limits.
   Distinguish advice from an instruction to execute. Obtain explicit approval
   for destructive, irreversible or newly discovered out-of-scope effects.
4. **Prepare recovery before mutation.** Use the existing project change
   mechanism; take only authorized, scope-limited backups that preserve modes
   and ownership. Confirm the backup/restore path actually works. Same-host
   copies do not protect against host loss. If rollback cannot restore data
   after migration, say so before proceeding. No blind rollback over new writes.
5. **Gate every mutation.** Check identity, expected current state, tools,
   permissions and recovery prerequisites first. Abort if a gate fails. Use
   explicit paths, never glob deletion or speculative cleanup. Multi-step SSH
   operations use an explicitly selected interpreter; do not depend on the
   login shell. Keep credentials out of argv, logs, environments printed to
   stdout, raw compose output and general-purpose inspection dumps.
6. **Execute the bounded change once.** Use noninteractive authorized elevation
   only; never password embedding. Stop on the first unexpected outcome. Do not
   retry a mutation unless actual state and idempotence have been established.
   Do not install permanent monitoring as a side effect of diagnostics.
7. **Verify actual effect.** Read resulting configuration and live service
   state, then check its intended function from the relevant client location.
   An exit code, running container or LAN endpoint alone does not prove end-to-end
   health. Check boot ordering and persistence where applicable; do not reboot
   solely for verification without approval. Mark deferred checks explicitly.
8. **Report and preserve.** Record what changed, before/after evidence, actual
   verification, rollback availability and outstanding risks in authorized
   private reporting. Follow the project's Memory publication workflow only
   when authorized; exclude credentials and private topology from shared state.
   Never commit, push or close a Room merely because a change succeeded.

Reuse the local host runbook and its established gated-change template when
available. Keep host-specific paths, topology and special cases there rather
than copying them into this portable skill. Do not replace a working template
with a new generic automation framework for a single change.
