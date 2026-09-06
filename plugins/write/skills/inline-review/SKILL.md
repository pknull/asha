---
name: write-inline-review
description: "This skill provides compact, prose-preserving review threads inside Markdown manuscripts when inline annotation, author replies, thread status, or clean scanner/export views are requested."
---

# Inline Manuscript Review

Use `asha-inline-review/v1` for a shared Claude, Codex and Copilot source-editing convention.
Read [the grammar and API](references/grammar.md) before editing markup. Read
[migration guidance](references/migration.md) only when migration is requested.
Use the [synthetic demonstration](examples/README.md) to explain the source view.

## Editorial contract

- Preserve prose exactly. Annotate only on explicit request; do not silently
  migrate legacy comments, rewrite passages, renumber IDs, or close threads.
- Read the entire existing thread before responding. Never invent or alter a
  speaker's words or attribute a reply to the author without supplied author
  text. Do not repeat dismissed suggestions under new IDs; consult the sibling
  archive as well as live threads.
- Keep feedback local and compact. Phrase taste as an observation, distinct
  from source-backed continuity conflicts; cite the authority for conflicts.
- Leave closure to the author. Run `status` or `archive` only on explicit author
  direction. A reply leaves status unchanged, including on dismissed threads.
- Treat local replies as local: they do not amend canon, global voice rules,
  or project review configuration. Project voice and review-config authority
  override generic structural bans in the writing module.
- Treat detector scores as fallible signals, not authorship proof. Do not run
  detector-evasion loops or buy detector calls under this protocol.

## Operations

Run `python3 scripts/inline_review.py check FILE` before working; stop on any
file-and-line diagnostic. Use `list FILE --json` and `show FILE C1` to read
threads. Refer across files as `path#C1`. Keep IDs attached when the author edits
inside a span. Never select nested, overlapping, fenced-code or frontmatter spans.

Use `annotate FILE START_LINE END_LINE --speaker LLM --text 'Observation.'` for
inclusive whole-line spans. Supply every speaker explicitly (`AUTHOR`, `LLM`,
or `LLM(agent-name)`). Use `reply FILE C1 --speaker AUTHOR --text 'Supplied reply.'`
only with actual author text. No command rewrites prose; all mutations validate
before and after, preserve non-review bytes, and replace files atomically.

Run `project FILE --json` for clean text and a one-based source-line map; without
`--json`, projection goes to stdout. Add `--no-headings` to `project` for the
shared ATX-heading-free scanner view; fenced examples, setext underlines, and
surrounding blank lines remain (see grammar for exact syntax). Defaults retain
headings. Never redirect that output over the source.
LanguageTool, style-analyzer, and book-export accept opt-in `--manuscript` (alias
`--clean`); their default behavior is unchanged. Aggregate style metrics provide
per-file maps, not invented per-finding locations. Keep provenance when using
scanner findings in a thread.

This is a source convention, not a GUI. Rendered Markdown hides the discussion.
Skill installation exposes `write-inline-review` through the existing skill
folders; static installation tests do not prove live agent behavior.
