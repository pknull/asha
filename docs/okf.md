# OKF in Asha

Asha stores knowledge as Open Knowledge Format (OKF) bundles: directories of
markdown concepts with YAML frontmatter, readable by people, agents, Obsidian
and the spec's own tooling. This document is the mechanism reference for the
`okf` plugin and for the learnings store, the first bundle. The decision record
is in project Memory; the plugin README owns the tooling inventory and the
vendored pin.

## Bundle contract (OKF v0.2)

- A bundle is a directory tree. Its root is wherever a reader points; there is
  no `okf/` directory convention. A root `index.md` may declare `okf_version`
  and nothing else in its frontmatter. Asha declares it once per bundle root;
  a bundle nested in a larger one declares none.
- Every non-reserved `.md` file is a concept with parseable frontmatter and a
  non-empty `type`. Recommended: `title`, `description`. Optional families:
  `sources`, `generated {by, at}`, `verified [{by, at}]`, `status`
  (draft, stable, deprecated), `stale_after`. Unknown keys are preserved.
- `index.md` (any directory) and `log.md` are reserved and carry no frontmatter
  (except the root index above). Links are standard markdown links to `.md`.
- Actors: `human:<id>` for people, `<producer>/<version>` for agents and tools,
  `process:<id>` for automation. A `human:` verifier is the only path to the
  human-reviewed trust tier, so `verified` is written only by a verb a person
  runs; Asha never derives it.
- Conformance has three errors (frontmatter, `type`, reserved-file shape).
  Everything else is a producer lint. Asha runs strict: lints fail the gate.

## The okf plugin

`plugins/okf/tools/` holds the pinned upstream validator and visualizer (header
and digests in the plugin README), the gate `okf_validate.py` (exclude list,
expected file count, three Asha lints, exit 2 never a pass), the indexer
`okf_index.py` (generated indexes; `--check` for drift), the log writer
`okf_log.py` (newest first), and `okf_viz.py` (inline JSON made script-safe).
The `okf-bundle` skill teaches index-first reading. Indexes and logs are
generated, never hand-written.

## The learnings bundle

`~/.asha/learnings/` becomes an OKF bundle when `migrate_learnings_okf.py`
writes `.okf-0.2.json`. Until then the writer emits the legacy shape, so
SessionStart maintenance can never migrate the store a record at a time. The
marker is written only by the migration, which the Keeper runs once after
rehearsing on a copy (`--root COPY`); it copies the resolved directory to
`<name>.pre-okf-v02-YYYYMMDD` beside it, refuses a pending journal, a second
run, an existing backup or a store whose state directories are symlinks (a
copied backup would alias the live files), renders and checks every record
before writing the backup or the marker so a bad record changes nothing, keeps
every filename, and ends with the SessionStart render compared byte for byte.
Rollback is `mv` and is printed on success and on any failure after the
marker. The live store is a symlink into the dotfiles git tree: roll back with
that `mv` (or remove the marker too), never with `git restore` alone, which
would leave the marker and so the new writer shape behind.

### Record mapping

Frontmatter stays JSON (a valid YAML mapping) so the SessionStart render path
needs no PyYAML. A record containing a code point PyYAML or UTF-8 refuses
(DEL, C1 controls, U+FFFE/FFFF, a lone surrogate) is refused, not escaped; the
legacy writer before the marker is byte-for-byte unchanged.

| Field | Source | Notes |
|---|---|---|
| `type` | `learning` | unchanged |
| `id`, `trigger`, `action` | unchanged | `render_active` reads only these |
| `title` | `id` | recommended field |
| `description` | `trigger` | index entries carry the trigger; open the record for the action |
| `status` | `state` | candidate→draft, active→stable, retired→deprecated; `_parse` maps back |
| `generated.by` | `asha/<session plugin version>` | never `asha/unknown` |
| `generated.at` | date trigger/action last changed | migration uses `created` |
| `verified[]` | `verify --id [--by human:<id>]` | only `human:` actors; migration writes none |
| `stale_after` | `updated + 90 days` | candidates only, recomputed on every save, so corroboration bumps it |
| `state`, `created`, `updated`, `retirement_reason`, `applicability`, `evidence` | unchanged | private keys; `evidence` drives activation, dedup and Control rule order |

### Lifecycle verbs

`propose`, `corroborate`, `contradict`, `activate-if-eligible`, `retire` are
unchanged in meaning. `maintain` (the SessionStart entry, `expire` is an alias)
replays a pending journal and writes the stale candidate list to `.stale.json`;
it retires nothing. `extend --id` gives a stale candidate another 90 days;
`verify --id` records a person's confirmation. Every save appends a
newest-first `log.md` entry naming the verb and resulting state.

### Gate

Layered strict, decided 2026-10-31:

1. Inside the writer, before the transition journal is written, a
   dependency-free per-record check (type, title, description, status, the
   `generated`, `verified` and `stale_after` shapes, no code point YAML or
   UTF-8 refuses) plus a check that the index entry the indexer would write
   parses as exactly one link to the file under the spec's link grammar (an
   id containing `]` is refused). A failure writes nothing, so journal replay
   cannot land an unchecked record. The indexes and `log.md` are derived
   artifacts written after the journal is cleared; a failure there is reported
   on stderr and repaired by the next save, and the strict gate reports the
   stale index as orphan lints in the meantime. Every save re-reads the store
   to rebuild the indexes (about 120 reads on the live store).
2. At the explicit save (`/session:save` step 6) and in
   `tests/validate-plugins.sh` over committed fixture twins, the bundle-wide
   strict run through `okf_validate.py`, which asserts the validated file count
   against the admitted set and treats exit 2 as "validator unavailable".
   Memory publication is independent: the save reports both results.

The upstream validator at the pinned commit checks `stale_after` for ISO 8601
format only and accepts `status: draft`, so a stale candidate is review
information, never a lint. A test pins that so a re-vendor cannot change it
silently.

## The ~/.asha bundle (phase one-b)

`~/.asha` is the second bundle: the root identity and operation files (the hot
`soul.md`, `voice.md` and `keeper.md`, plus `keeper-voice.md`, `operation.md`
and `agent-coordination.md`), the cold corpus under `reference/` and the
learnings store. A file reachable by two paths (`keeper-voice.md` at the root
links to `reference/keeper-voice.md`) is two concepts and is listed twice. Each carries `type`,
`title` and a `description` written for a reader deciding whether to open it;
the generated `index.md` files list them, and the `asha-reference` skill walks
the indexes instead of a hand-maintained table. `okf_version` is declared once,
at `~/.asha/index.md`; the learnings index declares none.

The files live in the dotfiles stow module and reach `~/.asha` through
symlinks, which the walker follows. Runtime trees beside them (`cache/`,
`chair/`, `chat/`, `state/`, `workspaces/`, `session-state/`, backups) are not
concepts: `~/.asha/.okfignore` scopes them out for every tool, and a pass is
reported for the admitted set only (a concept linked solely from an excluded
file lints as an orphan; a link into an excluded file is not broken).

`asha doctor` is this bundle's gate: once the root index exists it runs the
strict validator and the index drift check and fails on either. Regenerate
with `okf_index.py ~/.asha` after editing a described file. Two writers share
the learnings subtree (the learnings manager on every save, the bundle indexer
on request) and agree only because they see the same files: never scope
anything under `learnings/` from the root `.okfignore`.

## Canon vaults (phase two)

Ruled 2026-10-31 on measured facts (about 1,100 of 1,643 Lore notes and some
45 directories carry spaces or punctuation in their names, which the spec's
link grammar cannot address): vaults keep their names and their
`[[wikilinks]]`; Asha owns the link lints. The vault root (`~/Obsidian/AAS/Lore`, the Obsidian vault, not the
repository) gets `.okfconfig` with `{"links": "obsidian"}` (in the phase-two vault change) so `okf_validate`
runs its own strict set over `okf_links`, which resolves wikilinks the way
Obsidian does (basename, partial path, case-insensitive; attachments by a
known extension set skipped; two notes sharing a name reported as ambiguous
unless one is in the linking note's own directory, which is Asha's tie-break,
not a documented Obsidian rule). Links resolve against every note on disk, so
a path into an excluded note is not broken, while only admitted notes are
concepts. Generated indexes percent-encode their targets under this grammar;
declare it in `.okfconfig` before the first index run. The vault's taxonomy (Decision
002) owns `status` with its own ladder, so the bundle waives that lint. The
vault's own quality gate (`Tools/validate_repo.py`) and link checker predate
this and remain; the OKF gate joins them rather than replacing them.

### Writers

The one Asha writer that touches the Lore bundle is the canon-writer agent,
spawned by `/rp:end` after the Keeper ratifies each item. It writes `title`, a
one-line `description`, `type`, `generated {by, at}` (actor and instant the
command resolved: `asha/<rp version>` from the rp README, `date -u`) and
`sources` (`resource` mappings naming session files by beat) on every concept it creates, leaves
`description`, `generated` and `verified` alone on an edit (Decision 004:
descriptions are the Keeper's to revise; ratification confirms a fact, not this
rendering of it), and reports the paths it touched. The command hands those
paths to `okf_update.py`, which finds each file's bundle root by marker,
rewrites the generated indexes, appends one `log.md` entry naming the concepts
with the same actor, and runs the strict gate; the index and log files it
changed are staged beside the promoted files. A failed gate names the concept
and the lint and the session does not close silently on it. Rotation
(`rp_gate.py rotate`) and the summaries write under `Work/rp/`, outside the
bundle, so they maintain nothing; `novel-state-updater` writes a book's state
directory, which the vault's `.okfignore` keeps out of the bundle.

## Later bundles

Harness-owned files (`SKILL.md`, generated agents) stay outside bundles.
