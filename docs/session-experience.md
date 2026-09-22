# Session experience and reviewed learning

Experience policy defaults to `off` unless the user config supplies a default or
the project has an explicit override. No configuration is enabled by installation.
Worker guidance selects compatible active rules automatically; experience capture
and save review run only when policy enables them.
The native review release gate is also off: Claude's packet-only adapter has
fixture coverage; its paid native tool-refusal probe remains outstanding. Codex,
Copilot and OpenCode automatic reviewers are unsupported. A policy change cannot
bypass this gate. No installer enables policy or starts a session.

## User defaults and project policy

The optional user config `~/.asha/config.json` (or `ASHA_CONFIG`) accepts
`"session_experience": {"default_mode": "off"|"capture"|"review"}`. Missing or
invalid values resolve to off; `asha doctor` reports invalid values. Resolution is
project override, then user default, then builtin off. Reads return `source` as
`project`, `default`, or `builtin`, plus mode and revision; they do not migrate state.

```bash
asha control session experience policy --project PROJECT --json
asha control session experience policy --read-only --project PROJECT --json
asha control session experience policy --project PROJECT --mode capture --json
asha control session experience policy --project PROJECT --mode capture --revision INSPECTED_REVISION --json
asha control session experience policy --project PROJECT --mode review --revision INSPECTED_REVISION --json
asha control session experience policy --project PROJECT --clear --json
```

Without `--revision`, read-and-set is one write transaction. With it, supply the
opaque revision just inspected for compare-and-set. `--clear` deletes the override
and follows the user default; a retained counter prevents old revision reuse.
Default revisions fingerprint that counter and mode without writing during reads;
returning external config to the same mode in the same counter epoch has the same
fingerprint. Policy mutations remain chair/Keeper-only. Policy lives in private
Control SQLite under the stable project ID. `capture` requests an assessment at
completion and graceful close; `review` additionally selects bounded review utilities. Changing
policy never replays history. Disabling review cancels only owned learning-review
utilities. `Work/markers/silence` suppresses new learning content, review and
adoption; existing data and ordinary close remain available.

## Reports and close

Enabled terminal worker briefs describe the assessment contract. A finished report
without an assessment returns a bounded `experience_request` with a controller-issued
key and exact follow-up command. Attach using that key; the original task result
is preserved and identical retries return the same receipt. A new assignment gets
a new request. If the process exits first, capture stays missing with reason
`exited-before-capture`; no report is inferred. Structured/review utilities do not
receive these completion prompts.

```bash
asha control session report --state finished --text 'Result' --experience-file /absolute/report.json --key UUID
asha control session handoff --request REQUEST --attempt N --outcome no-durable-update --detail 'No binding change' --experience-file /absolute/report.json
asha control session handoff --request REQUEST --attempt N --outcome no-durable-update --detail 'Same findings' --experience-ref REPORT_UUID
asha control session handoff --request REQUEST --attempt N --outcome no-durable-update --detail 'Correction' --experience-file /absolute/correction.json --supersedes REPORT_UUID --key NEW_UUID
```

`--experience-ref` and `--experience-file` are alternatives. Both report and
handoff accept `--supersedes` plus a new `--key` for corrections. The close request
remains a separate identity. Original bytes and source-session lineage survive.
A Memory-only handoff retry reuses the current request's latest captured receipt.
A closed actor cannot correct its report or impersonate a replacement generation.

Report JSON is UTF-8, at most 16 KiB, with duplicate keys and unknown fields
refused. The contract is `asha.session-experience.v1`:

- `assessment`: `observations`, `none-observed`, or `insufficient-evidence`.
- `outcome`: `succeeded`, `partial`, `failed`, or `unknown` (agent attestation).
- `summary`: bounded text. `observations`: at most three. `evidence`: at most four.
- Each observation has `key`, `kind`, `observed`, `evidence_ids`, `uncertainty`;
  optional `explanation` (hypothesis), `lesson` (`trigger`, `action`), and
  `applicability` (`harnesses`, `task_kind`, `limitations`, optional `project_ids`
  and exact `versions`). Kinds are `correction`, `failure-recovery`,
  `verification-conflict`, `context-gap`, `orchestration-failure`,
  `unexpected-improvement`.
  The key `@report-assessment` is reserved for controller-declared review scope.
- Evidence is `{id, kind:"agent-attestation", text}` or an explicit
  `{id, kind:"project-file", path:PROJECT_RELATIVE_PATH, sha256:EXPECTED_DIGEST}`.
  Source bytes are safely copied and hashed together: 16 KiB per file, 32 KiB total.
  Oversized sources refuse; v1 does not discover logs, follow URLs or capture
  arbitrary scratch paths. Excerpts must instead be explicitly submitted as
  bounded attestations, with their omissions described.
- Optional `guidance_feedback` contains at most three `{id, version, use,
  evidence_ids}` entries; `use` is `applied`, `not-applied`, `not-applicable`, or
  `unknown`. Optional `target_failure` is `observed`, `not-observed`, or `unknown`.
  Feedback must refer to a version actually supplied to that session generation.

Report files must be owned regular files with no hard links or symlink components.
Project captures stay in the verified project. Known secret patterns cause content
omission with a bounded generic reason; this is best-effort filtering, not a promise
that arbitrary private content is safe. Private excerpts stay in Control.

Capture (`captured`, `none-observed`, `insufficient-evidence`, `missing`, `invalid`,
`disabled`), review, task outcome and Memory publication are independent. Invalid
optional capture does not invalidate a valid Memory acknowledgement. Capture occurs
before Memory CAS and survives a later publication conflict. Review never delays
close; terminal close works without a supervisor. Claude uses its existing Stop
return channel; Codex, Copilot and OpenCode use the existing queued message seam.
No terminal is typed into or scraped. An observed idle Codex session with a captured
native ID can continue its same conversation for close; unknown activity, missing
native IDs, unsupported harnesses or resume failures require attachment.

Explicit saves inside verified terminal hub sessions gain controller-derived
`hub_session_id` and `hub_generation`. Control retains the exact publication receipt.
A Room generation that saved after its latest assignment is not asked for another
close assessment: capture is disabled with reason `explicit-save-published`.
Further assignments or a new generation invalidate this omission. Successful explicit saves also produce a separate verified completion receipt for
issue #92. Close consumes it only after rechecking the current turn, project
identity and Memory digests at an observed idle boundary. A failed or superseded
completion receipt does not erase evidence that publication succeeded. See
[completion and closure](session-hub.md#completion-before-close) for the support
matrix and attachment guidance.

For structured utilities only, launch can explicitly select
`--result-contract asha.session-result.v1`. Its retained result is then
`{contract:"asha.session-result.v1", result:"TEXT", experience:REPORT_OBJECT}`;
`experience` is optional. Ordinary unselected free-form results are unchanged.
Selected envelope text is buffered up to 32 KiB at the native event seam before
retention. Codex deltas supply its otherwise summary-less completion. Failed or
truncated native results leave an invalid capture receipt and omit optional text.

## Review custody and limits

Selection uses reported observation kinds, explicit improvements, or a stable
controller-ID/policy-revision hash for a deterministic 10% routine sample.
`none-observed` is eligible; missing capture never authorizes transcript extraction.
Selection reason and disabled/deferred/unsupported results are retained.

The existing supervisor owns review dispatch. A single SQLite transaction reserves
one utility ID and daily admission before its opening message. Limits are one
concurrent automatic review globally, five reservations per project per UTC day,
one execution turn, and 300 seconds including permission waits and queue delay.
Existing runtime admission can further defer work. Lost replies return the same
utility identity; uncertain submission never earns an automatic second turn.
Live providers keep their concurrency slot until cleanup is proven. There is no
native token cap claim; unavailable usage remains unknown.

The Claude adapter removes built-in tools, MCP, skills and hooks at native CLI
seams; the owner also refuses every tool/permission frame. It uses a private
utility directory, no Room/coordinator tool authority, and a controller-owned
`experience-review` purpose derived from immutable utility linkage. Review utilities
cannot receive follow-ups, resume, capture experience or recursively schedule work.
The native release gate remains closed until an approved refusal probe confirms
these settings in the installed backend. CLI configuration evidence and protocol
fixtures are not live enforcement evidence. Relevant primary documentation:
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and
[hook restrictions](https://code.claude.com/docs/en/hooks).

Packets delimit bounded frozen task/evidence data as untrusted and bind report,
evidence and packet digests. Input is at most 64 KiB; output at most 16 KiB.
Malformed, incomplete or secret-bearing output fails review without preserving
rejected content. Each observation receives `supported`, `unsupported`,
`insufficient-evidence`, or `no-action`, cited evidence and contradictions,
inference/uncertainty/scope, remedy destination, check, expected benefit and risks.
Native execution success does not validate findings or task success.

For a report with no observations, the frozen packet declares one
`@report-assessment` review subject. It requires one finding, including an explicit
`no-action` or `insufficient-evidence` when appropriate. A supported assessment
must cite retained evidence. This lets routine sampling identify a missed issue
in the submitted summary/evidence while keeping the worker's original report
unchanged. It does not reconstruct unreported history. Pending findings and
adopted provenance label this subject `reviewer-report-assessment`; they do not
attribute the finding to the worker. Its initial applicability is restricted to
the source project, harness and known version, with broader scope unverified.
Original session/report lineage, explicit-save limits and replay rules still apply.
Historical reviews with empty findings remain historical; reads add no findings.

## Inspection and explicit save

```bash
asha control session experience list --project PROJECT --limit 50 --offset 0 --json
asha control session experience show REPORT_UUID --json
asha control session experience packet REPORT_UUID --json
asha control session experience pending --project PROJECT --limit 50 --offset 0 --json
asha control session experience guidance --project PROJECT --limit 50 --offset 0 --json
asha control session experience stats --project PROJECT --json
asha control session experience unreviewed --project PROJECT --limit 50 --offset 0 --json
```

Pages include total/completeness/next offset. Reads launch nothing. The report view
retains reviews; the pending view includes findings and prior dispositions so the
saving agent can read original evidence before acting. Bounded inspected backfill
uses `experience review --project PROJECT --report REPORT_UUID` (repeat at most
20 times) and requires review policy. It refuses replay of an earlier native
attempt. `packet` supplies the exact frozen input and packet digest for inspection.
For an explicit chair advisory review, `--result-file FILE` records one
frozen result without running a utility or claiming native enforcement.
Manual review requires capture or review policy; policy off and silence suppress it.

After explicit publication, the saving chair or verified terminal Room reviews up
to five selected unreviewed reports, oldest first, through `experience packet ID`.
Record the existing reviewer-result contract with `experience review --project
PROJECT --report ID --result-file FILE --publication-file RECEIPT`. The receipt
binds a retry-safe five-report budget; excess remains for the next save. These
results are labelled `advisory-save-review` in stats, pending findings and adopted
provenance; they do not open the native gate. Native review custody takes precedence.
The unreviewed read supplies `selection_reason` and `skip_reason`; a report from
the saving session's own lineage is skipped before reading its packet. Record it
with `review --report ID --skip-own-lineage --project PROJECT --publication-file
RECEIPT`. Resumes and retained native identity remain the same source lineage.

Rooms may review/dispose only in their own project; disposal requires an exact
controller-retained explicit-save publication from their current generation.
Workers, structured utilities, managed actors/coordinators and worker ancestry
remain refused, and own-lineage findings cannot be adopted. A Room uses its verified
hub identity for the save candidate limit. Chair advisory review without a save
receipt retains its existing `operator-advisory` label.

The save procedure checks silence and performs one `policy --read-only` gate.
Off skips subsequent experience reads and mutations. Installed Codex rules allow
the read-only policy form, pending/show and unreviewed/packet; mutation flags are
incompatible with the read-only policy form. Review, dispose and report keep native
approval. No live rules or policy are changed by this source implementation.

At an explicit save, first read coherent Memory digests before drafting. Ordinary
`memory_v2.py publish` and scope-none `save_none.py publish` require
`--expected-active` and `--expected-decisions`. A stale baseline refuses; reread,
merge and retry. Publication receipts are produced from validated bytes under the
lock. A subsequent publication may supersede those bytes without invalidating the
earlier successful transaction. Initialization and reviewed migration retain their
separate creation/preimage contracts. Scope-none and close have no Git seam.

After successful explicit publication, `experience dispose --project PROJECT
--decision-file FILE --publication-file FILE` records one deliberate disposition.
The decision object has `review_id`, `observation_key`, `finding_digest`, `save_key`,
`disposition`, `reason`; adoption additionally supplies `rule_id`, and either
`trigger`/`action` for `propose` or the inspected `rule_version` for `corroborate`.
Dispositions are `propose`, `corroborate`, `project-decision`,
`code-test-followup`, `reject`, or `defer`. Other destinations do not create issues,
edit code, or silently publish a project decision. A binding project decision
belongs in the saving agent's reviewed Memory draft.

The command uses the resolved explicit-save native session identity and successful
publication receipt for the same project plane. Close supplies no new publication
authority. An intent receipt precedes the locked manager mutation; retry reconciles
exact origin and semantics before completing it. Learning failure is reported and
remains nonfatal to an already successful Memory publication. New candidates are
limited to three for the saving session/project, regardless of worker count.
The existing three-source-sessions/two-projects activation heuristic remains;
resumes, subagents, reviewers and repeated saves do not add source sessions.
Contradictions fence earlier positives; active semantics cannot be overwritten.
Legacy evidence retains legacy provenance. Cross-project diversity is not proof
of causal independence or effectiveness.

## Selected guidance and measurement

Launch, resume and send accept repeated `--learning ID` or `--learning ID@DIGEST`.
Workers with no `--learning` automatically select active compatible rules. Order is
project scope, harness scope, distinct source-session evidence count descending,
then rule ID; unscoped rules are compatible. An explicit `--learning` disables
automatic selection; `--no-learning` supplies none. Rooms retain their existing
context behavior. Only active compatible rules are eligible: at most three rules and 3 KiB.
Candidate/retired/stale/incompatible selections are excluded with reasons. Unknown
runtime versions are flagged rather than inferred; exact-version applicability
remains unknown when the native version is unknown. Plain workers receive no
blanket persona, Memory, or learning bundle.

Manifests retain `selection: automatic|explicit|none`, supplied version/digest, exclusions, runtime unknowns,
generation and delivery state. Queued messages are not supplied until their native
read/acknowledgement seam establishes delivery. Supply is not use or improvement.
Terminal `session messages` retains up to eight bounded emitted-body receipts and
returns a `delivery_digest` when retained. Acknowledge that body with
`session ack-message MESSAGE_ID --delivery-digest DIGEST`. An acknowledgement
without a digest preserves ordinary message handling and leaves guidance supply
unknown. Unknown/evicted digests refuse; re-read before retrying. Silence suppresses
new receipt content and omits that optional digest. Structured native input
acknowledgements bind their frozen rendered assignment directly. Recovery keeps
prior exposure history and excludes retired/stale rules in undelivered input.
Report envelopes freeze the latest Control assignment with its coverage limits;
unobserved native conversational followups remain unknown. Supplied-version
references are deduplicated and bounded to 64 with explicit completeness/counts.
Stats show capture and explicit completion denominators separately, review states
and actionable yield by destination, routine-sample findings, origin-deduplicated
adoption, selected/supplied guidance, feedback attestations, recurrence unknowns,
review reservations, observed provider launches, unknown launches, elapsed time,
unknown tokens/dollars and stored payload volume. Feedback is the latest submitted
attestation per session generation/rule version; repeated reports do not multiply
use or recurrence. Assignment-level use remains separately unknown even when a
generation has feedback. The report-creation cohort and independent close/request
and exposure-time denominators are included in the response.
Filters include project, report policy revision, time, harness and known model.
Assignment-level comparability and verified improvement remain unknown in v1;
absence of feedback is not success. Capture-only baseline collection and paid
native probes require the Keeper's later rollout decision.
