---
name: write-revision-pass
description: Run an authority-first manuscript revision pass with read-only act reviews, decision capture, style audit, old-value proof, and optional source-bound candidate comparison. Use when a requested revision spans sections, retires a value, or needs an advisory comparison of authorized alternatives.
---

# Revision Pass

Execute the requested revision without asking clarifying questions. Resolve
ambiguity from the repository's authority hierarchy; record irreducible
judgment calls instead of silently inventing an answer.

## Contract

1. **Read authority before prose.** Identify and read the repository's binding
   project instructions, manuscript plan, canon/state sources, and style or
   voice document before editing any manuscript text. Treat live manuscript
   and authority files as stronger evidence than summaries or review notes.
   Open and read each binding file's contents before the first edit: listing
   paths, copying files, or hashing them in a receipt does not satisfy this
   step. A reviewer's later read cannot satisfy the writer's pre-edit read.
2. **Define the pass.** State the old value being retired, the intended new
   value when one exists, the acts/sections in scope, and the acceptance proof.
   Resolve `ASHA_ROOT` from the wrapper environment or
   `${ASHA_HOME:-$HOME/.asha}/config.json`, then run
   `"$ASHA_ROOT/plugins/session/tools/declare-pass.sh" OLD [NEW]` when that
   tool is available so the end-of-turn hook can recheck the declaration.
3. **Preserve originals, then revise in manuscript order.** Before editing,
   save byte-for-byte copies of the in-scope manuscripts in a new directory
   under the project's `Work/reports/`, for example `<pass>/originals/`. Keep
   earlier copies intact. Use these copies for before/after comparisons and
   preservation proofs. Preserve facts and language outside the stated pass;
   do not broaden into an unrelated rewrite.
4. **Capture, review and bind every act.** Before dispatching reviews, follow
   **Review receipts** below to capture the revised manuscript, saved originals
   used for comparison, and every authority actually used. Read
   `harnesses/capabilities.json`, select the current harness's `subagents`
   surface, and assign exactly one read-only review agent to each act. Agents
   may run in parallel when that surface exists. If no usable subagent surface
   exists, perform the same read-only act reviews sequentially. A reviewer may
   report findings but must not edit manuscript or authority files. Persist the
   combined report, then record and check its source receipt. These operations
   apply to an ordinary revision; candidate comparison is not required.
5. **Do not ask clarifying questions.** Resolve mechanical ambiguity from the
   authority sources. Append every genuine creative or continuity judgment
   call to the manuscript project's `DECISIONS.md`, following its existing
   format, before relying on that decision in prose.
6. **Audit the revision against style.** Re-read the authoritative style or
   voice document, then audit your own changed prose against it. Fix confirmed
   deviations; do not substitute generic preferences for project rules. If a
   fix changes a reviewed input, repeat the affected review under a new capture
   and report; do not attach current hashes to the previous findings.
7. **Prove the old value is gone.** From the repository root, run a scoped
   repository-wide fixed-string search. It excludes `.git`, `.jj`, the notes
   under `Work`, and every `DECISIONS.md` (step 5 records the old value there
   on purpose), and it still searches a manuscript kept under `Work/novel`
   (the novel-state layout), for example:

   ```bash
   paths=(.); [[ -d Work/novel ]] && paths+=(Work/novel)
   grep -rInF --exclude-dir=.git --exclude-dir=.jj --exclude-dir=Work \
     --exclude=DECISIONS.md -- "OLD" "${paths[@]}"
   ```

   Record the exact command and its empty output. A non-empty result means the
   pass is incomplete; revise and rerun rather than explaining the residue
   away.
8. **Commit from the repository root.** Run the repository's required checks,
   inspect the bounded diff, and create the requested commit from the root.
   Never commit from a manuscript subdirectory.

## Output

Report the authority files read, act-review verdicts, entries added to
`DECISIONS.md`, style-audit outcome, verification commands with exit status,
the exact empty old-value grep proof, report/snapshot/receipt paths and the
receipt check result with any uncovered inputs, and the resulting commit
identity when a commit was requested.

## Review receipts

Use the existing source-receipt procedure in
`$ASHA_ROOT/plugins/write/skills/novel-state/SKILL.md` for every new revision
review report. Resolve `ASHA_ROOT` from the wrapper environment or
`${ASHA_HOME:-$HOME/.asha}/config.json` in each consuming tool call. The helper
is `$ASHA_ROOT/plugins/write/skills/novel-state/scripts/artifact_freshness.py`;
it does not require Git or initialized Asha project metadata.

Before analysis, run `capture` with explicit `--project`, each revised act as
`--manuscript`, and every saved original and authority actually used as
`--source`. Original manuscripts used to prove that unrelated bytes were
preserved are review dependencies too. Give reviewers the declared project
paths, using the saved originals for comparisons. Include
authority-bearing manuscript frontmatter through an additional `--source`
declaration for that same file. Use a new snapshot name under the manuscript
project's `Work/reports/`, then give the reviewers the declared scope.

After the reviews, write the report inside that same project and run `record
--project ... --snapshot ... --artifact ... --output ...`, followed by
`check --project ... --receipt ...`. Paths other than `--project` are normalized
project-relative paths. Keep old reports and receipts; if inputs change or a
review needs another source, take a new complete capture and rerun affected
analysis. Do not silently omit dependencies. If an input must remain outside
the project and current permissions prevent preserving a project-local copy,
an explicit report destination is outside the project, or the helper cannot run,
honor that constraint and name the uncovered path and coverage limit in both
the saved report and final response. A successful `check` verifies only declared
inputs; it cannot prove that the declaration is complete or bind an unsupported
external artifact. Do not claim complete review applicability while relying on
uncovered inputs. The old-value
declaration helper failing does not make this independent receipt helper
unavailable.

## Optional candidate comparison

Use this bounded protocol when the author requests comparing alternatives or
when an authorized optional improvement merits checking against the original.
Do not generate alternatives or expand a revision's scope merely to use it.
The helper prepares a packet and summarizes supplied rankings; it makes no
model calls, edits no prose, and never promotes a candidate.

1. Write the concrete objective, author constraints, voice criteria and success
   check to a new local objective file. Identify every voice/canon/configuration
   source actually needed. Keep candidate files separate from the manuscript.
2. Decide eligibility before asking for preferences. In `optional` mode, the
   unchanged original is eligible. In `required` mode, it is an ineligible
   reference; compare at least two alternatives already checked for compliance
   with the requested change. The caller declares compliance: the helper
   cannot establish it. A required change with only one compliant revision
   proceeds through normal review without this comparison.
3. Resolve `ASHA_ROOT` as in the Contract and set `PROJECT_ROOT` explicitly.
   Prepare a new packet directory; paths are project-relative:

   ```bash
   python3 "$ASHA_ROOT/plugins/write/skills/revision-pass/scripts/revision_compare.py" prepare \
     --project "$PROJECT_ROOT" --objective Work/revision/objective.md \
     --original Work/revision/original.md --candidate Work/revision/alternative.md \
     --source Work/novel/bible/voice.md --mode optional --output Work/revision/comparison1
   ```

   Repeat `--source` for actual authority files. For required changes, use
   `--mode required` and at least two `--candidate` arguments. The packet binds
   raw hashes of the objective, authority and candidate sources, and hashes its
   presentation. Review prose uses the strict inline-review clean projection;
   raw source hashes conservatively invalidate even editorial-only changes.
4. Give each read-only reviewer only `comparison.md`, which contains the
   objective, declared authority and shuffled candidate labels. `packet.json`
   retains the mapping for the operator; this is anonymized presentation,
   **not enforced blindness**. Do not claim independent judges or hide a known
   conflict just because labels are anonymous. Retain the returned packet ID.
5. Collect complete rankings in a local JSON file, replacing the packet ID and
   labels below with those actually returned. Use a distinct reviewer identity
   per supplied judgment:

   ```json
   {"packet_id": "ID_FROM_PREPARE", "rankings": [{"reviewer": "reviewer-1", "order": ["C2", "C1"]}]}
   ```

   ```bash
   python3 "$ASHA_ROOT/plugins/write/skills/revision-pass/scripts/revision_compare.py" summarize \
     --project "$PROJECT_ROOT" --packet Work/revision/comparison1/packet.json \
     --judgments Work/revision/rankings.json
   ```

   Summarization rechecks declared sources and presentation. Any changed task
   or source invalidates that comparison. Missing sources, no judgments,
   duplicate reviewers, or invalid/duplicate/incomplete rankings stay unknown;
   they never count as an incumbent win. Each complete ranking awards one fewer
   point per lower position, with zero for last. A score tie has no preference.
6. Present the preference, eligibility assumptions and reviewer reasoning to
   the author. `author_decision: null` remains null even when a candidate wins.
   Only the author's choice authorizes adoption within the revision's scope.
   Keep packets and previous reports as history; use a new packet if inputs
   change. The helper's `check --project ... --packet ...` command is read-only.

Neither comparison nor receipt helpers replace claim verification, canon
authority, or the author's creative decision.

## Inline manuscript threads

For requested source annotations, follow `write-inline-review`
(`asha-inline-review/v1`; `skills/inline-review/SKILL.md` in the write plugin).
Read existing replies and archived dismissals before raising a concern again;
preserve prose and speakers, and leave closure to the author. Existing read-only
review roles stay read-only: propose annotations for the authorized writer.
Local replies do not amend canon or global voice rules. Project voice and
review-config authority override the writing module's generic structural bans;
separate taste observations from source-backed conflicts. Use opt-in clean
scanner/export projections; detector scores are not authorship proof and do not
authorize detector-evasion loops.
