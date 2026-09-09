# Managed-session implementation and rollout audit

Audited against [the original plan](2026-09-07--managed-agent-sessions.md).
Implementation, native acceptance, live SQLite cutover and final verification
are complete. This audit accompanies the authorized Git landing.

## Storage and migration

| Requirement | Implementation and inspected evidence |
| --- | --- |
| One local SQLite operational store; no Redis service | `database.py`, `registry_backend.py`, and the typed SQLite stores. Live activation `8ddce800-cbb7-46d5-837e-47f4f9a81148` selected SQLite on 2026-09-09. Source files are retained and fenced. |
| WAL, FULL synchronization, foreign keys, private files, bounded contention | `test_control_database.py` checks settings, ownership, sidecars, rollback, contention, and relationship validity. No weaker durability fallback. |
| Stable identities, canonical bytes, generations, cursors, digests and seals | Registry staging/activation tests preserve original payload bytes and verify their relationships. Live stage retained 123 initiatives, 236 tasks, 236 creation journals, 2 Rooms and 1090 artifacts. |
| Atomic transitions and unique delivery keys | `session_store.py` reserves turns and input together; `managed_launch.py` commits initiative, first event, session, opening message and launch receipt together. Lost-response and interrupted-write tests prove reuse or rollback. |
| Versioned schemas, indexed queries, FTS5 and doctor | Database/schema tests cover upgrades, interrupted publication, index definitions and Unicode search/rebuild. Current-work tests inspect query plans and exclude historical output from status reads. |
| Backup/restore includes committed WAL state | `test_live_wal_backup_preserves_committed_data_and_refuses_overwrite`, `test_restore_committed_wal_state_pauses_dispatch_and_refuses_existing_state`, and process-death restore tests. Output bodies, retention gaps and acknowledgement cursors survive backup together. |
| Storage exhaustion leaves no partial custody or success | Real SQLite `max_page_count` tests raise FULL during message retention and turn output: no partial message/event or cursor advancement survives, and custody can resume after capacity returns. |
| Quiescent import, explicit activation, stale-writer refusal | Stage/activation tests cover source changes, inode/mode binding, partial freeze, lost commit response, rollback and cached/raw SQL writers. Live migration succeeded with scheduling stopped. |
| Malformed records reported rather than silently omitted | Live preflight checked 10754 initiative records and reported one misnamed duplicate ingestion record. Its original bytes and path/digest manifest were archived privately; the identical canonical record and historical evidence remain. No missing historical content was invented. |
| Safe rollback | Pre-write rollback tests restore authority; new records or artifacts refuse rollback. Legacy transport handoff keeps SQLite and refuses live owners/providers or uncertain submissions. |

Large artifacts and inode-bound ownership evidence retain their verified files;
their references and operational records use the selected SQLite stores. Human
exports and retained migration files are not alternate writable authorities.

## Runtime, delivery and actions

| Requirement | Implementation and inspected evidence |
| --- | --- |
| Independently owned harness connection | `sessions.py` and `session_harness.py` keep one owner per session. The real-process restart test kills/replaces the supervisor while preserving owner/provider identity and the pending question. |
| Stable native/session/turn IDs and generation fencing | Managed coordinator, IPC and Codex actor tests cover stale owners, foreign roots, native call scope, process ancestry and replacement generations. Environment labels alone cannot grant authority. |
| Queued, submitted, consumed and resolved evidence stay distinct | Session message/turn rows, exact answers, typed native requests and retained receipts. Both advertised adapters explicitly report no provider consumption receipt; submission is never relabelled consumption. Legacy message acknowledgements keep their original meaning. |
| Duplicate delivery and answers do not repeat work | Queue claims, answer replay, native request/call receipts and coordinator dispatch reservation tests. Ambiguous native submission is retained for explicit recovery instead of automatic replay. |
| Concurrent event draining and bounded output | Protocol/transport tests exercise structured streams, native requests and bounded actor work. Output tests retain immutable audit envelopes, bounded display bodies and explicit retention gaps. Missing terminal events do not become success. |
| Quota, failure, cancellation and retry accounting | `test_control_provider_recovery.py` checks structured quota windows, reset conditions, mixed failure causes, exact recovery digests, explicit overrides and no automatic retry. Turns and attempts count separately from polling. |
| Exact-seal review budget amendment | File and SQLite review-budget tests cover request/signature binding, one extra attempt, stale or spent grants, interrupted consumption, terminal-event repair, and unchanged deadline/storage gates. |
| Clarification, plan authority and native permissions stay separate | Native request, action-resolution, IPC and CLI tests cover exact digests, stale/conflicting answers and operator-only decisions. Clarification cannot approve a plan or mint a persistent tool grant. |
| Pause, drain and stop survive closing Control | Durable runtime policy and supervisor tests cover admission, live turns, cancellation and explicit recovery. Live supervisor is stopped and admission remains stopped. |

Historical legacy coordinator records can outlive their original conversation.
Migration checks their authority state as well as process identity: retired legacy
generations cannot act even when their old terminal now hosts the operator.
Managed owners still require quiescence in every coordinator state. Staging and
activation share that predicate; the full state matrix and byte-preservation
checks pass. No operator or conversational Room process was killed for migration.

## Harnesses and operator interface

| Requirement | Implementation and inspected evidence |
| --- | --- |
| Claude first and default; native resume preserves policy/context | Tested Claude 2.1.266 structured print transport. A real four-turn coordinator cycle used one conversation, one clarification and answer, implementation, independent review, verification and final report. |
| Codex app-server protocol and exact native permissions | Tested Codex 0.153.4 start/resume and native dynamic actor tools. Contract fixtures and native acceptance cover exact command/file allow/deny, clarification, bounded calls and the complete workflow. |
| Unsupported capabilities remain explicit | Copilot/OpenCode managed adapters and mid-turn steering are unavailable; existing Room/worker paths remain supported. No terminal-keystroke simulation substitutes for a missing managed adapter. |
| One shared current-work/action projection | `session_activity.py`, `orchestration/current_actions.py`, CLI pages, Control G/M pickers and chair startup use the same typed records. Tests cover counts, partial observations, paged questions and exact request resolution. Capacity waits are named in both row reasons and the shared summary, without mutating state. |
| Startup summary without a model call | Live startup reported 1 Room, 0 live tasks, 26 retained active initiatives, 0 decisions and 0 managed sessions, with missing address evidence explicitly unknown. Preserved initiatives include 21 paused, 3 planning and 2 draft; they were not automatically resumed. |
| Project-bound default launch and retryable form | Control n and `coordinator launch` use one atomic managed intake. The Project/Harness/Assignment form retains input and launch ID through errors/resizes. Missing harnesses fail before new custody; committed retries remain inspectable. |
| Readable inspection and terminal resize | Managed Enter inspection and paged coordinator listing work without tmux. Resizing does not mutate state. Live Control output, including menus, stayed within 42/80 columns. |
| Installer, doctor and canonical instructions | All four harness installs succeeded; separate Codex/OpenCode and combined drift checks pass. The reviewed Codex hook migration removed 14 proven installer-owned inline groups, preserved all other parsed config values, and retained a private backup. |
| Legacy Rooms and transport rollback | Real isolated tmux Room open/attach/detach/close passed on SQLite. Same-initiative handoff requires a stopped, settled managed predecessor. Explicit `--transport tmux` remains available. |

The interactive chair uses deterministic startup and explicit read surfaces.
Automatic delivery into an idle interactive chair and a full PTY emulator were
not promised capabilities; managed coordinator delivery is independent of that UI.

## Acceptance observations

| Scenario | Result |
| --- | --- |
| Claude complete workflow | 319.875 seconds; 4 coordinator turns; 3 completed stage dispatches; 8 exact native permission decisions; accepted review and passed verification; 0 coordinator terminal relays. |
| Codex complete workflow | 232.716 seconds; 4 coordinator turns; 12 native actor calls; 3 completed stage dispatches; accepted review and passed verification; 0 coordinator terminal relays. |
| Restart | SIGKILL/replacement of the actual supervisor preserved owner, provider and question; repeated answer produced one effective follow-up. |
| Load | 250 historical sessions plus 2 active owners; 4 eligible turns dispatched in 0.254–0.497 seconds with a 1-second tick. All completed; 0 queued messages or pending requests remained. Provider time was measured separately. |
| Live migrated queries | All 123 initiative heads and 34 graphs read within the 2-second observation budget; 89 archived heads, 0 unavailable records, 0 attention items. |

Native workflows used disposable projects and reached ready-for-integration;
they did not integrate those fixture changes. Reports and reproducible acceptance
scripts are retained locally under `Work/code-orchestrate/managed-sessions/`
(stages 26, 29, 30, 31 and 32). No new initiative was created for the rollout itself.

## Final verification and handoff

- `./tests/run-tests.sh` passed all 24 suite groups with zero failures and zero
  skipped groups. This includes all 2,983 Python tests (1,441.288 seconds) and the
  shell orchestration entry point's 633 tests (535.669 seconds).
- The final run used unchanged source: all 132 staged file hashes matched the
  recorded start snapshot. Earlier stale-fixture and mixed-import failures were
  rerun successfully; the final full run is independently green.
- Separate Codex and OpenCode drift checks, and `--target all`, passed against
  the installed harness surfaces. All four harness installations succeeded.
- Claude's final source integration review returned SHIP. Confirmed capacity
  visibility and first-launch initialization-lock concerns have direct failing
  regressions followed by passing fixes. The listing contract is versioned and
  assignment length fits the acceptance field's limit.
- Live runtime status confirms admission stopped, no live supervisor, no held
  supervisor lock, inactive/disabled supervisor service, and an empty complete
  managed-session page. Owned test and native acceptance processes have exited.
  The user's conversational Room is preserved.

Final logs, source hashes and review dispositions are retained locally in
`Work/code-orchestrate/managed-sessions/33-final-verification/`; the full-suite
log is `/tmp/asha-managed-final-suite.log`. Git history records the feature
landing and its intended predecessor commits. No automatic scheduling is enabled
as part of handoff.
