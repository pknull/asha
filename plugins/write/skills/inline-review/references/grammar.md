# asha-inline-review/v1

## Syntax

An optional, whole-line `<!-- REVIEW-GRAMMAR v1 -->` declaration may occur once
outside spans, frontmatter and fenced code. Unknown versions are refused.
A live span starts with `<!-- REVIEW C1 START -->` alone on a line. The span
contains at least one complete source line. Immediately after it,
`<!-- REVIEW C1 END` alone on a line opens the note comment. The note ends with
`-->` alone on a line. No leading/trailing whitespace is allowed on markers.
LF and CRLF are supported and existing newline bytes are retained.
Source line numbers advance only at LF (including CRLF); other control or
Unicode separators are content, not additional source-line boundaries.

IDs match `C[0-9]+`, are unique per file, and are never renumbered. Cross-file
references use `path#C1`; moving/renaming a file also requires keeping its sibling
archive and updating external references explicitly. Allocation uses the next
number above live and archived IDs; an explicit ID must be unused and unretired.
`C01` and `C1` are distinct spellings; neither is normalized.

Speaker lines are `LLM: text`, `AUTHOR: text`, or `LLM(agent-name): text`.
Agent names contain ASCII letters, digits, underscores or hyphens. At least one
speaker line is required. Continuation lines start with two spaces and follow
a speaker or another continuation. Supply continuations with their indentation;
commands do not infer speakers. Exactly one `STATUS: open`, `STATUS: resolved`,
or `STATUS: dismissed` line is required. Status may appear anywhere among the
speaker entries; a continuation cannot attach to status. Comment delimiters
(`<!--` or `-->`) in note text are refused, never escaped. A second speaker must
be appended with a separate explicit `reply`, not embedded in `--text`.

```markdown
<!-- REVIEW-GRAMMAR v1 -->
<!-- REVIEW C1 START -->
The lamp stood beside the window.

Rain darkened the sill.
<!-- REVIEW C1 END
LLM: The repeated location draws attention to the sill.
  This is an observation, not a continuity claim.
AUTHOR: Keep the emphasis here.
LLM(line-editor): Understood; the local preference is recorded.
STATUS: dismissed
-->

§

The room grew quiet.
```

Spans are whole-line, non-overlapping and non-nested; nesting is refused rather
than flattened. They cannot cross initial YAML frontmatter or fenced code.
Backtick and tilde fences (at least three characters, up to three leading spaces)
protect example markers from being interpreted as live threads. Legacy HTML
comments are opaque to the review grammar. Malformed live markers, mismatches,
missing ends, duplicates, invalid statuses and unterminated notes produce
file-and-line diagnostics. Annotation conservatively refuses lines touching
legacy comments, existing threads, declarations, frontmatter or fences.
An unterminated final prose line must be given a newline by the author before
annotation; the tool will not add one to prose.

## Projection and maps

Projection validates first, removes initial frontmatter and only HTML comments
classified by the parser outside fenced code (including live review markup and
legacy comments), and never normalizes Markdown. Every byte in a parser-recognized
fenced block survives unchanged, including its info string, marker examples and
closed or unclosed comment openers. A fence opens with up to three spaces and a
run of at least three backticks or tildes; backtick info strings cannot contain
backticks. It closes with up to three spaces, the same character repeated at
least as many times, and only optional spaces or tabs afterward. Frontmatter
cannot open a body fence. A comment opened outside a fence remains opaque until
its terminator, swallowing fence-looking lines exactly as the parser scans them.
Story order, emphasis, wikilinks, section symbols, blank
lines and paragraph boundaries survive. Comment-only lines disappear; inline
comment bytes disappear without adding spaces or reflowing the remaining text.
Blank lines inside a comment disappear with the comment. Invalid source is not
silently cleaned. A projected line maps to its original, one-based source line;
columns are not mapped. `line_map[0]` describes projected line 1.

```markdown
---
type: synthetic
---
The *lamp* stood beside [[the window]]. <!-- legacy note -->

§

The room grew quiet.
```

Heading removal is opt-in: `project(text, file, headings=False)` or
`project FILE --no-headings` drops ATX heading lines outside initial frontmatter
and fenced code, after strict validation and comment removal. An ATX heading's
comment-free text starts with zero to three spaces, one to six `#` characters,
then a space, tab, or end of line. A heading with a trailing inline comment is
still dropped. Fenced examples remain byte-for-byte, including their comments;
setext underlines (`===` or `---`) are not detected and remain. Frontmatter is
removed as usual and cannot open a fence in the body. Surrounding blank lines
survive unchanged: dropping headings, like comment-only lines, never joins
paragraphs across their blank-line boundaries. Dropped lines have no map entry;
every remaining line maps to its original source line. The default
`headings=True` preserves the existing projection bytes and JSON shape
`{text, line_map, file}`. Only `project` accepts `--no-headings`; other subcommands
refuse it with exit 2. Validation and exit codes are unchanged.

## Python API and CLI

Load `scripts/inline_review.py` (standard library only, Python 3.10+, POSIX).
Text APIs accept a UTF-8-decoded string and an optional diagnostic filename:

- `parse(text, file='<text>') -> Document`: lines, threads, diagnostics;
  `Document.strict()` raises `ReviewError` on any diagnostic.
- `check(text, file='<text>') -> list[Diagnostic]`.
- `project(text, file='<text>', *, headings=True) -> Projection`: `.text`, `.line_map`, `.file`,
  and `.source_line(projected_line)`; raises on diagnostics.
- `read_source(path)`: reads UTF-8 without translating newline bytes.

Mutating APIs accept paths and return a revalidated `Document`:

- `annotate(path, start_line, end_line, *, speaker, text, id=None)`.
- `reply(path, id, *, speaker, text)` appends, preserving prior entries/status.
- `set_status(path, id, status)` changes only the addressed STATUS line.
- `archive(path, id)` explicitly retires the thread without deleting its prose.

CLI: `check FILE`, `list FILE`, `show FILE ID`, `project FILE`,
`annotate FILE FIRST LAST --speaker SPEAKER --text TEXT [--id ID]`,
`reply FILE ID --speaker SPEAKER --text TEXT`, `status FILE ID STATUS`,
`archive FILE ID`. All accept `--json`. Check returns diagnostics, project returns
text/map/file, and other commands return live thread records (show filters by ID).
Exit codes: 0 clean/success, 1 diagnostics/refused edit, 2 usage or I/O/UTF-8 error.
`project` writes only stdout, never the manuscript. Exceptions in Python retain
diagnostics; the CLI prints them with file and line.

## Mutation safety and archive recovery

Every mutation strictly parses the entire source, validates any sibling archive,
edits only the target markup, re-parses, compares every non-review byte, and
checks byte-for-byte projection equality before atomic replacement. Existing
speaker lines are immutable; `reply` appends supplied text and `status` changes
only status. No operation auto-closes or rewrites a span. Symlink mutation targets
are refused. Mode bits are preserved. Directory-inode locks serialize cooperating
commands; snapshot checks refuse detected external edits. Arbitrary editors do
not honor these locks: avoid concurrent author saves during a mutation (the last
snapshot check is not an OS-level compare-and-swap).

`FILE.review-archive.json` is the sibling archive. Each record retains file name,
ID, source start line, exact span excerpt, complete marked thread, source SHA-256,
UTC archive time, and explicit-operation provenance. Keep it with the manuscript:
removing it destroys ID-retirement history. All mutations refuse corrupt archives.

There is no portable two-file atomic rename. Archive commits its durable record
first, then atomically replaces the manuscript. Interruption between these steps
leaves a duplicate, never a lost thread. Re-running explicit archive completes
removal only if the saved thread is identical; a changed thread with that retired
ID is refused for manual recovery. Do not discard either copy. Nothing archives
automatically, including resolved or dismissed threads.
