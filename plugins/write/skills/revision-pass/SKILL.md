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
2. **Define the pass.** State the old value being retired, the intended new
   value when one exists, the acts/sections in scope, and the acceptance proof.
   Resolve `ASHA_ROOT` from the wrapper environment or
   `${ASHA_HOME:-$HOME/.asha}/config.json`, then run
   `"$ASHA_ROOT/plugins/session/tools/declare-pass.sh" OLD [NEW]` when that
   tool is available so the end-of-turn hook can recheck the declaration.
3. **Revise in manuscript order.** Preserve facts and language outside the
   stated pass. Do not broaden into an unrelated rewrite.
4. **Review every act independently.** Read
   `harnesses/capabilities.json`, select the current harness's `subagents`
   surface, and assign exactly one read-only review agent to each act. Agents
   may run in parallel when that surface exists. If no usable subagent surface
   exists, perform the same read-only act reviews sequentially. A reviewer may
   report findings but must not edit manuscript or authority files.
5. **Do not ask clarifying questions.** Resolve mechanical ambiguity from the
   authority sources. Append every genuine creative or continuity judgment
   call to the manuscript project's `DECISIONS.md`, following its existing
   format, before relying on that decision in prose.
6. **Audit the revision against style.** Re-read the authoritative style or
   voice document, then audit your own changed prose against it. Fix confirmed
   deviations; do not substitute generic preferences for project rules.
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
the exact empty old-value grep proof, and the resulting commit identity.

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

For review and extracted-state applicability, use the source-receipt workflow
in `skills/novel-state/SKILL.md`. Neither helper replaces claim verification,
canon authority, or the author's creative decision.

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
