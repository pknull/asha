# Memory System v2

Memory v2 separates deliberate semantic publication from bounded mechanical
crash recovery. Nothing infers meaning from host transcripts or hook telemetry.

## Authority order

1. Live system state and verified disk
2. Current explicit publication in `Memory/`
3. Unpublished recovery snapshot
4. Candidate or retired learning evidence

Lower tiers never overwrite higher tiers merely to make notes agree.

## Published semantic memory

Every repository or workspace operational plane has only:

```text
Memory/activeContext.md
Memory/decisions.md
```

`activeContext.md` is no more than 4,096 UTF-8 bytes and has exactly four
level-one headings, in order: Objective, State, Next, Blockers. The latter two
hold at most five items each. `decisions.md` contains only current binding
decisions; resolved/superseded decisions leave the publication rather than
forming a history.

`/session:save` is the sole semantic writer. The live model drafts both files,
checks claims against disk, and calls `memory_v2.py publish`. Both drafts are
validated before publication. A project lock serializes publishers; a private,
ignored recovery journal rolls both files back after a partial replacement and
is replayed before the next validation/publication. The explicit command then
commits and pushes unless `--scope none` or `--no-push` says otherwise.
Every shipped reader acquires the same lock through `memory_v2.py read`, so it
cannot observe a mixed pair during the two sequential file replacements.

No hook, SessionEnd, OpenCode `dispose`, timer, or background process publishes
semantic memory or invokes Git.

One further explicit writer exists for Control project sessions: the graceful
close handoff (`asha control session handoff`). It is not a lifecycle save. The
operator's `session close` requests one final turn; the session's live model
drafts both files from its own context and Control publishes them through the
same validator, lock and journal, with a compare-and-swap on the digests the
model read so a concurrent save is never overwritten. The Stop hook only
carries that request to the model at a turn boundary; it never publishes. The
handoff path has no Git seam and never commits or pushes. See
`docs/session-hub.md`.

At SessionStart, every initialized project reads the pair through
`memory_v2.py startup-context`, which acquires the same publication lock and
labels the result as background state requiring verification. The active
handoff is included in full; decisions are bounded in the injected context and
the exact file path is given when the remainder must be read from disk. This is
orientation, not a lifecycle save.

## Unpublished recovery

Prompt and post-tool callbacks atomically replace:

```text
Work/session-state/<harness>-<session>.json
```

Each file is mode `0600`, no more than 2,048 bytes, project-local, path-safe,
secret-scrubbed, and isolated by session. Touched paths are deduplicated and
capped at ten. SessionStart removes snapshots older than seven days and may
surface the newest one with an explicit unpublished/verify-first label.
SessionEnd only adds its seal timestamp and prunes. `Work/markers/silence`
disables these writes. Silence does not hide an already-clean publication from
SessionStart; if a recovery journal is pending, the read fails closed rather
than repairing state behind the override.

## Learnings

One learning per file lives under:

```text
~/.asha/learnings/candidate/
~/.asha/learnings/active/
~/.asha/learnings/retired/
```

Evidence records date, harness session identity when available, stable project identity
from `.asha/config.json`, kind, and reviewed reason. Explicit save resolves
available native environment seams, with Copilot recovery as a fallback. Duplicate
`(session_id, project_id)` evidence does not count twice. Activation requires
three distinct positive sessions across two projects. This automatic gate is a
user-controlled corroboration heuristic, not a security boundary. Only active learnings are
rendered at SessionStart. SessionStart runs candidate expiry, moving records
older than 90 days to retired; no record
is silently deleted. Contradiction is an explicit transition back to candidate;
retirement is explicit and keeps the record.

## Legacy Memory

The reviewed v1-to-v2 migration (`/session:consolidate`, the learning
manager's `migrate-plan`, `migrate-amend` and `migrate-apply`) was retired on
2026-10-07 after it had run on the one home that runs Asha. Initialization
still refuses a published file that is not valid v2; rewrite it in the v2
format first. Legacy sources, earlier review plans and private backups under
`Work/memory-migration/` stay where they are, and the narrow ignore rule for
that directory remains. `~/.asha/learnings/.migration-v2.json` is an inert
record of the completed migration.

## Separate workspace planes

Canonical workspace `knowledge/` indexes, reviewed promotion infrastructure,
private `memory-local/`, and harness-native memory remain independent. Removing
the operational Memory catalogue does not remove or weaken those systems.

Repository and workspace publications are distinct planes. A workspace-root
session receives the workspace pair once plus metadata. A session in a
declared child repository receives the child pair and the workspace pair. The
SessionStart handler suppresses the workspace publication body only at the
root, preventing accidental duplicate injection without hiding either plane
from a child session.

### Workspace Git visibility

Workspace init and `asha workspace doctor --fix` read the optional top-level
`memory_visibility` in the selected project's `.asha/config.json`. Merge it into
the existing config, preserving the project identity and other settings:

```json
{
  "initialized": true,
  "memory_version": 2,
  "project_id": "existing-project-id",
  "memory_visibility": "private"
}
```

Omitted or `"tracked"` preserves the current managed rules: operational Markdown,
canonical knowledge and workspace metadata remain trackable. `"private"` instead
ignores the entire operational, shared and personal roots (normally `Memory/`,
`knowledge/`, `memory-local/`), all of `Work/`, and `.asha/workspace*.json`. The
private managed block contains no re-inclusion rules. Existing narrow recovery
and migration ignores remain present. Unknown values fail before init or repair
writes; repair also refuses an unreadable or missing project config.

Set this independently in each private component repository; workspace repair
does not read an umbrella's setting or modify child repositories. Leave it absent
or `"tracked"` in the umbrella. Apply an existing workspace's policy with:

```bash
asha workspace doctor --root . --fix
asha workspace doctor --root .
```

Doctor detects stale managed blocks and later negations, and repair reasserts the
selected policy at the end of `.gitignore`. The installation drift check delegates
initialized workspace checks to this same doctor; even with `--fix`, it reports
workspace drift without repairing project files.

This setting controls workspace ignore generation, not Memory publication or save
scope. `/session:init` preserves the config; it does not generate the workspace
block. Git ignores do not untrack already committed files or erase history. Any
index/history cleanup is a separate authorized step. Use a no-Git publication
path for private Memory; this setting does not change Git save behavior.

## Harness seams

- Claude reads `hooks.json` directly and receives context in native
  `SessionStart` output.
- Codex renders the supported shared hooks to native TOML and receives the same
  startup context from the shared handler.
- Copilot installs one `asha-recovery.json`; its SessionStart wrapper carries
  the shared handler output in `additionalContext`.
- OpenCode generates `plugins/asha.js`, calling the same startup/recovery
  handlers through system-prompt transformation and sealing on dispose.

All four use the same coherent publication reader, validator, recovery writer,
and learning manager. The hook transports differ; the authority model does
not.

## Optional session experience

[Session experience and reviewed learning](session-experience.md) documents the
dormant project policy, bounded report/close capture, one-turn review custody,
explicit-save dispositions, selected guidance and coverage metrics. Policy defaults
to off; native automatic review remains gated pending separately approved probes.
Ordinary and scope-none Memory publication require both pre-draft snapshot digests;
close remains independent of successful capture or completed review.
