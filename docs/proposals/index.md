# proposals

## Concepts

- [Overnight issue-to-merge loop](./2026-08-04--issue-to-merge-loop.md): Overnight issue-to-merge loop: triage, one worker per safe issue in a worktree, cold review, draft PRs only; delivered in v2.5.0
- [Workspace-aware multi-repository memory](./2026-08-06--workspace-memory.md): Workspace-aware multi-repository memory: planes, visibility and publication across repositories; delivered via PR #28 and the v1 issues
- [Workspace read-side context injection (v2)](./2026-08-08--workspace-read-side.md): Workspace read-side context injection (v2): what a session reads from sibling repositories; delivered in PR #51
- [Asha Control](./2026-08-14--asha-control.md): Asha Control: the session supervisor, state store and TUI designed after Workmux, ccsessionctl and FleetView; six increments delivered
- [Asha Orchestration](./2026-08-15--asha-orchestration.md): Asha Orchestration: initiatives, coordinators, plans, gates and staged results on top of Control; the engine was retired on 2026-10-05
- [Donmai runtime study for Asha](./2026-09-07--donmai-runtime-study.md): Source study of the Donmai runtime against Asha's managed sessions; a comparison, not an adoption proposal
- [Managed agent sessions and reliable delivery](./2026-09-07--managed-agent-sessions.md): Implementation plan for managed agent sessions and reliable delivery, drawn from the Donmai study and the reference implementations
- [Managed-session implementation and rollout audit](./2026-09-07--managed-sessions-audit.md): Audit of the managed-session implementation and rollout against its plan: storage, migration, native acceptance and the live cutover
- [Session experience capture and reviewed learning](./2026-09-12--session-experience-learning.md): Session experience capture and reviewed learning; retired 2026-10-07 (subtraction N2) and kept as a record
- [Session experience: automatic defaults and a working loop](./2026-09-13--session-experience-defaults.md): Automatic defaults and a working loop for session experience capture; retired 2026-10-07 with its parent proposal
