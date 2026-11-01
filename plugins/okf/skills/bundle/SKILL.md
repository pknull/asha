---
name: okf-bundle
description: Validate, index, log, navigate and visualize an Open Knowledge Format (OKF) bundle, any directory of markdown concepts with YAML frontmatter, through Asha's pinned tooling. Use when a task reads or writes an OKF bundle such as ~/.asha/learnings, when asked to validate or reindex a bundle, when a save must prove a bundle is clean, or when answering a question by walking a bundle's indexes instead of reading every file.
---

# OKF bundle

An OKF bundle is a directory tree of markdown files. Every non-reserved `.md`
file is a concept: YAML frontmatter with a non-empty `type`, then a body. Its
concept ID is its bundle-relative path without `.md`. `index.md` and `log.md`
are reserved: no frontmatter, except that the bundle-root `index.md` may declare
`okf_version` alone. Links are standard markdown links to `.md` paths. The
bundle root is wherever the caller says it is; there is no `okf/` convention.
Spec: OKF v0.2, pinned tooling in `plugins/okf/tools/` (see the plugin README
for the commit and digests).

Tools take the bundle root as their first argument. `TOOLS` below is
`$ASHA_ROOT/plugins/okf/tools`.

## Validate

```bash
python3 "$TOOLS/okf_validate.py" BUNDLE --strict [--exclude 'state/' --exclude '*/SKILL.md']
```

Exit 0 is the only pass. Exit 1: spec errors, lints, or the validator saw a
different number of files than the exclude list admits (`0 file(s)` never
passes). Exit 2: the validator could not run (PyYAML missing, bad path); report
it as "validator unavailable", never as a pass. Strict adds three Asha lints to
the upstream set: every concept reachable from the root `index.md`, `log.md`
dates newest first, and no `verified` entry that is not a mapping.

Exclude harness-owned files (`SKILL.md`, generated agent files) and runtime
state directories, preferably through a `.okfignore` at the bundle root (one
`--exclude` pattern per line) so every tool scopes the bundle the same way;
the gate prints the excluded set with its verdict, because a pass covers the
admitted set only. Directory symlinks are followed; hidden directories are not.

## Reindex

```bash
python3 "$TOOLS/okf_index.py" BUNDLE [--title TEXT] [--okf-version 0.2]   # write
python3 "$TOOLS/okf_index.py" BUNDLE --check                              # drift: exit 1
```

Indexes are generated, never hand-written. Each directory with concepts gets an
`index.md` listing `[title](./file.md): description` and its child indexes,
and every `index.md` already in the bundle is treated as generated and
rewritten, so exclude a directory whose index someone maintains by hand.
Descriptions come from each concept's `description` field; write that field for
a reader deciding whether to open the concept. The root index keeps its heading
and its `okf_version` block across reruns; `--okf-version` declares one when
the root has none, and `--title` renames.

## Log

```bash
python3 "$TOOLS/okf_log.py" BUNDLE --message "**Update** \`concept\` what changed" --actor "asha/<version>"
```

Newest first, grouped by `## YYYY-MM-DD`. Historical entries are records; never
rewrite them. The writer keeps only the title line and the dated groups: a
hand-written preamble between them is dropped on the first append, so put
prose in a concept, not in the log.

## Record a writer's changes

```bash
python3 "$TOOLS/okf_update.py" --plugin rp --verb Canon --message "what changed" --json PATH...
python3 "$TOOLS/okf_update.py" --plugin rp --print-actor     # asha/<version> from plugins/rp/README.md
```

After an agent or command has written concepts, hand it the exact paths it
touched. It finds each file's bundle (the nearest ancestor holding
`.okfconfig`, `.okfignore`, the learnings marker or an `index.md` declaring
`okf_version`), rewrites that bundle's indexes, appends one log entry naming
the concepts, and runs the strict gate; `bundles[].changed` lists the index and
log files to stage beside the writes. It never edits a concept: a malformed one
fails the gate by name, so the writer carries `title`, `description` and `type`
(and `generated` with the actor and `NOW` the caller resolved) itself. Paths in
no bundle, excluded by `.okfignore`, reserved, or `other` (not `.md`, or under
a hidden directory) are reported and skipped; a missing path or a malformed
`--date` refuses the run before anything is written, and an OS error on one
bundle is reported under `errors` with exit 2. `--actor` names the
actor directly (`human:keeper` for the Keeper's own edits).

## Query (index-first)

Start at the root `index.md`. Read descriptions, follow only the links the
question needs, descend one level at a time, and open a concept body only when
its frontmatter says it is relevant. Cite concept paths in the answer. Fall
back to a scoped text search only when the indexes do not lead to an answer,
and say so.

## Visualize

```bash
python3 "$TOOLS/okf_viz.py" BUNDLE [--title TEXT]
```

Writes `viz.html` and `graph.mmd` into the bundle root with inline JSON made
script-safe. Do not run the vendored `visualize.py` directly on a bundle whose
descriptions agents write.

## Vaults with wikilinks

Put `{"links": "obsidian"}` in `.okfconfig` at the vault root BEFORE the first
index run; never rename notes or rewrite `[[links]]` for the gate. Under this
grammar index entries encode spaces as `%20`, which Obsidian reads; under the
standard grammar nothing is encoded. A link into an excluded note is not
broken; an excluded note is not a concept. The gate reports a missing target and a basename
two notes share; fix the second by path-qualifying the link (`[[Magic/Notes]]`).
Waive a field the vault's own taxonomy owns with `"waive": ["status"]`.

## Trust fields

Actors: `human:keeper` for the Keeper's own confirmations, `asha/<plugin
version>` for Asha's writes, `process:<id>` for automation. Write `verified[]`
only for a confirmation a person actually made; never derive it. `generated.at`
is when the content last meaningfully changed. `status` is draft, stable or
deprecated; `stale_after` is an absolute instant, and a stale concept is review
information, not an error.
