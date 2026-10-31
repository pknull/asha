# OKF

**Version**: 0.1.0

Asha's layer for the Open Knowledge Format (OKF): pinned upstream tooling, a
dependency-free indexer and log writer, a gate that refuses a validator pass it
cannot account for, and the `okf-bundle` skill that teaches index-first reading.

## When to use it

Use this plugin for any directory of markdown concepts with YAML frontmatter
that Asha reads or writes: the learnings store first, `~/.asha` next, canon
vaults and repository documents later. A bundle is any directory of concepts; the
outermost bundle's root `index.md` declares `okf_version` once and nested
roots declare none (the learnings store declares none because `~/.asha` will).
There is no `okf/` convention, and every tool here takes the bundle root as an
argument.

## Inventory

| Path | Role |
|---|---|
| `skills/bundle/SKILL.md` | `okf-bundle`: validate, reindex, log, query, visualize |
| `tools/okf_validate.py` | Gate over the vendored validator: exclude list, expected file count, three Asha lints, exit 2 never a pass |
| `tools/okf_index.py` | Generates every directory's `index.md` from concept descriptions and owns every `index.md` it finds (exclude hand-maintained ones); `--check --title T` reports drift |
| `tools/okf_log.py` | Appends newest-first, date-grouped `log.md` entries; exports the ordering lint |
| `tools/okf_viz.py` | Runs the vendored visualizer with `</` escaped in its inline JSON |
| `tools/okf_io.py` | Shared atomic write, exclude matching and dependency-free frontmatter scalars |
| `tools/okf_common.py`, `tools/validate.py`, `tools/visualize.py` | Vendored upstream, unchanged below the header |

The learnings writer (`plugins/session/tools/learnings_manager.py`) imports the
indexer and log writer from this plugin by checkout path; the two plugins ship
together.

## Vendored upstream

Source: https://github.com/sniperunder123/okf-knowledge (MIT), an unofficial
community toolkit for the spec published at
https://github.com/GoogleCloudPlatform/open-knowledge-format.

Pinned commit: `cbfb00bc42be73c0756b243029c783ad31406f60` (2026-09-17,
"Migrate to OKF v0.2"). Each vendored file carries the upstream sha256 of its
body in its header, asserted by `tests/python/test_okf_tools.py`:

| File | upstream sha256 |
|---|---|
| `okf_common.py` | `1400b7a89af9404c424a32a45cc1abaf5b38743ade9e60482a9288af7fea6858` |
| `validate.py` | `1f6ded8a9b44806bbda6bcefe1108d2181c1bb6dbdbd46d42d0ba478f3c9a5fc` |
| `visualize.py` | `6c27b624b8d7828184fff674ba41d4cc442353479b7e711431d06df8a6138db5` |

Re-vendor procedure: fetch the three raw files at the chosen full commit sha,
compute their sha256, read the diff against the current vendored bodies, grep
for network, subprocess, `eval`/`exec`, `pickle` and `yaml.load` (none are
expected), update the headers and this table, and run the OKF tests. Never edit
a vendored body; put Asha behaviour in the wrapper modules. The upstream test
suite is pytest-style and is not vendored; this repository's unittest cases
cover the wrappers and the pinned behaviour the learnings store relies on
(stale_after is format-checked only; `status: draft` is accepted).

## Conformance and lints

The upstream validator enforces only the spec's three errors (parseable
frontmatter, non-empty `type`, reserved-file shape). Strict mode adds its
producer lints (title, description, link and orphan checks, the v0.2 field
families). `okf_validate.py` adds reachability from the root index, log order
and malformed `verified` entries, and refuses a pass whose file count differs
from the admitted set or is zero.

## Verification

```bash
python3 -m unittest tests.python.test_okf_tools -v
./tests/validate-plugins.sh
```
