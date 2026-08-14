---
name: panel-system-panel
description: "Run or manage a resumable multi-perspective analysis"
argument-hint: "[--quick|--think|--interview|--list|--show ID|--resume ID|--abandon ID] [topic]"
allowed-tools: ["Task", "Read", "Write", "Edit", "Grep", "Glob", "Bash"]
---

# Panel Command

Use a panel when a problem has competing frames, hidden assumptions, or a
decision that benefits from independent expertise and explicit opposition.
Routine edits and factual lookups do not need one.

## Interfaces

```text
/panel-system:panel "Should this API use REST or GraphQL?"     # full
/panel-system:panel --quick "Pressure-test this cache rule"    # deliberate only
/panel-system:panel --think "Decompose a multi-repo release"   # decomposition only
/panel-system:panel --interview "Specify a task CLI"           # requirements only

/panel-system:panel --list
/panel-system:panel --list --status=active
/panel-system:panel --show <id>
/panel-system:panel --resume <id>
/panel-system:panel --abandon <id>
```

Modes are mutually exclusive. The default is `full`. Management operations do
not accept a new topic. `--status` accepts `active`, `completed`, or
`abandoned` and is valid only with `--list`.

## Role execution

Use the shipped agents for bounded roles:

- `thinker`: dependency-aware decomposition;
- `questioner`: one-question-at-a-time requirements gathering;
- `examiner`: essence, root cause, prerequisites, and assumptions;
- `codifier`: write the interview seed from validated requirements;
- `recruiter`: match the topic to installed specialist agents;
- `fabricator`: define a missing agent only after recruiter justification.

The Moderator controls process and remains neutral. The Challenger begins from
opposition, tests assumptions and second-order effects, and labels unsupported
claims. Their concise role contracts live under `docs/characters/`.

Spawn agents when the harness supports subagents. Otherwise execute the same
role inline. Record `spawned` or `inline` for each role in state. A fabricated
agent stays in the panel workspace and is never installed automatically.

## Persistence contract

Every run uses one directory:

```text
Work/panels/<YYYY-MM-DD--slug>/
├── state.json
├── decision.md       # written when the run completes
└── seed.yaml         # interview mode only
```

`state.json` is the sole resumable record. It contains the topic, mode, status,
timestamps, current stage, completed stages, role execution modes, inputs, and
all accumulated stage results needed by the next stage. Update it after each
stage with a temporary file in the same directory followed by an atomic rename;
the command's Bash allowance exists for this boundary. Validate the temporary
JSON before replacement. Never require another file to resume.

`decision.md` is the final human-readable artifact:

- full/quick: decision, evidence, dissent, confidence, and next steps;
- think: the completed decomposition and unresolved decision points;
- interview: the examination verdict and a summary of the generated seed.

Interview mode additionally writes `seed.yaml`. The `codifier` agent owns its
canonical schema; do not invent another shape in the command or final decision.

### Required state fields

Keep the state compact but sufficient:

- identity: `schema_version` (currently `2`), `id`, `topic`, `mode`, `status`,
  `created`, `updated`;
- cursor: `current_stage`, `completed_stages`;
- execution: role-to-mode mapping and recruited specialist names;
- working data: decomposition, clarifications, goals, evidence, positions,
  challenges, synthesis, verdict, and decision as they become available;
- interview data: question/answer pairs, examination verdict, and whether the
  seed was generated.

Use `null`, empty arrays, or absent optional keys for results not reached. Do
not duplicate the same content under phase-number and semantic-name keys.

## Mode protocols

### Full

1. **Initialize**: create the panel directory and active state.
2. **Decompose**: `thinker` produces numbered steps, dependencies, and
   HIGH/MEDIUM/LOW clarity. Store the result in state.
3. **Clarify if needed**: for MEDIUM/LOW steps, `questioner` gathers missing
   requirements and `examiner` tests the revised problem frame. Store answers
   and verdict. On `REVISE`, question the named gaps and re-examine. On
   `REFRAME`, stop for user confirmation. Continue only upon `SOUND`.
4. **Recruit and frame**: `recruiter` selects 2-5 installed specialists,
   records fit evidence, identifies genuine gaps, and chooses consensus as the
   default decision rule (unanimous for security-critical decisions).
5. **Deliberate**: specialists submit Position, Evidence, Risks, Unknowns, and
   Recommendation. The Challenger cross-examines shared assumptions. Perform
   targeted research only when a named evidence gap blocks the decision.
6. **Synthesize and decide**: compare viable options, record material dissent,
   apply the decision rule, and state confidence without false precision.
7. **Complete**: write `decision.md`, set state to `completed`, and return its
   path.

### Quick

Run the Full protocol from **Recruit and frame** onward. Use this only when the
topic and success condition are already clear. The state records that
decomposition and clarification were intentionally skipped.

### Think

Initialize state, run `thinker`, store the numbered decomposition and decision
points, write them to `decision.md`, and mark the run completed. Do not recruit
or deliberate. Branches may be represented inside the decomposition object;
they do not create extra files.

### Interview

1. Initialize interview state.
2. `questioner` asks one short question at a time, targeting the largest
   remaining ambiguity. Persist every answer before asking the next question.
3. `examiner` returns `SOUND`, `REVISE`, or `REFRAME` with reasons.
4. On `REVISE`, resume questioning around the named gaps. On `REFRAME`, stop
   for user confirmation. On `SOUND`, `codifier` writes `seed.yaml` from the
   canonical template.
5. Write `decision.md` with the verdict, settled requirements, open questions,
   and seed path; then mark the run completed.

The Questioner gathers requirements and never proposes solutions. The Codifier
must not invent requirements absent from the saved Q&A.

## Deliberation standards

- Recruit against the current harness's installed agent catalogue; use
  `plugins/*/agents/*.md` only as a source-tree fallback.
- Give each specialist the same topic, goals, constraints, and available
  evidence.
- Evidence must cite a file location, source, or measurement. Mark inference,
  speculation, and unverified claims.
- The Challenger attacks claims, not people, and supplies kill criteria for
  the leading proposal.
- Preserve dissent when it changes risk or implementation order. Do not invent
  a percentage merely to make disagreement look measured.
- Recommendations are analysis, not permission to edit, commit, deploy, send,
  or purchase.

## Final decision format

```markdown
# Panel Decision: <topic>

## Context and Goals
## Panel and Evidence
## Options and Tradeoffs
## Decision
## Dissent and Confidence
## Next Steps
```

Omit inapplicable sections for think and interview modes. Keep evidence close
to the claim it supports.

## Management protocols

### Legacy panel compatibility

Before management, detect the prior schema by `current_phase` or
`completed_phases` without `schema_version: 2`. Legacy records and their phase
artifacts are user data: never delete or overwrite them silently.

- `--list` also discovers old panel state directories and the former
  `Work/thinking/<id>/` decomposition directories, labelling them `legacy`.
- `--show` renders their stored status and artifact paths read-only.
- `--resume` imports an active legacy run before continuing. Copy the original
  state to `state.legacy.json`, read completed artifacts once, condense the
  inputs and findings needed for the next semantic stage into a validated v2
  temporary state, record source paths and SHA-256 digests under
  `legacy_import`, then atomically replace `state.json`. Preserve every old
  artifact. A former thinking-only run imports into a new panel directory in
  `think` mode; refuse ambiguous ID collisions rather than choosing one.
- Completed or abandoned legacy runs remain read-only and need no import.

If any source is malformed or the next stage cannot be derived, stop with the
missing evidence. Never guess prior positions merely to complete migration.

### `--list [--status=...]`

Scan `Work/panels/*/state.json`, filter by status if requested, and display ID,
mode, status, current stage, updated time, and topic. There is no index file.
An absent `Work/panels/` directory means no panels, not an error.

### `--show <id>`

Read that panel's `state.json`. Show topic, mode, status, progress, recruited
specialists, verdict or decision when present, and the paths of final artifacts
that exist. Do not dump internal working data unless requested.

### `--resume <id>`

Load only `state.json`, require `status: active`, and continue from
`current_stage`. Validate that the next stage's inputs are present. Completed or
abandoned panels are immutable through this interface.

### `--abandon <id>`

Require an active panel, set its status to `abandoned`, update the timestamp,
and preserve all accumulated state. Do not create a synthetic decision or
delete artifacts.

If a state file is malformed or lacks the inputs needed to resume, stop and
report the missing fields. Recovery must never guess a prior panel's position.
