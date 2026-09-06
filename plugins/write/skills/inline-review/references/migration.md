# Explicit migration only

Do not migrate real manuscripts as a side effect of review or export. Request
an explicit source/range and approval for migration; retain a backup under the
project's normal author-controlled process. No bulk migration command is provided.

1. Run `check`; repair malformed markup manually with author approval.
2. Read the legacy comment and nearby prose. Do not infer an unknown speaker or
   invent an AUTHOR reply. Leave unattributed material untouched until clarified.
3. Select whole prose lines, avoiding code, frontmatter and existing spans.
4. Use `annotate` with explicitly supplied speaker/text. A human may then remove
   the redundant legacy comment in a separately approved edit; the tool never
   deletes it automatically. Compare the clean views and inspect the source diff.
5. Preserve IDs after author edits. Never flatten nested threads or renumber
   across files. Keep the sibling archive on moves and backups.

Synthetic legacy source:

```markdown
The latch clicked.
<!-- A legacy observation with unknown authorship. -->
```

Synthetic source after authorship and migration are explicitly authorized:

```markdown
<!-- REVIEW C1 START -->
The latch clicked.
<!-- REVIEW C1 END
LLM: The short sentence makes the latch prominent.
STATUS: open
-->
```

This pair illustrates a manual, approved conversion, not an automatic deletion
promise. A local preference in an author reply does not rewrite project policy.
Clean export uses `--manuscript` without migrating or saving over the source.
