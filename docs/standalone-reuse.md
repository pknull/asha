# Standalone reuse

A small, opt-in set of Asha components can be used without Asha: no dispatcher,
persona, Control, Memory v2 or installer. This guide classifies components,
exports the reusable ones with immutable provenance, and validates an export
from any directory without Asha's runtime. It adds no workflow: Asha installs
and launches exactly as before, and selective install
(`./install.sh --only code,...`) is unchanged.

The machine-readable authority is
[`lib/standalone-components.json`](../lib/standalone-components.json); the
matrix below mirrors it: `tests/python/test_standalone_export.py` fails when a
component, its class, a support file, a licence file or a declared dependency
differs between the two. Components not listed are unclassified: treat them as
Asha-coupled until evaluated.

## Classes

| Class | Meaning |
|---|---|
| standalone-safe | Works from an isolated, non-Asha directory with only the listed dependencies. Exportable. |
| adapter-required | A reusable procedure whose current form needs a client adapter: frontmatter translation, path resolution or Asha's renderer. Exportable as source; it does not work unchanged. |
| asha-runtime-required | Depends on the Asha dispatcher, Control, Memory v2, the broker registry, Asha configuration or the persona. Not exportable. |

## Component matrix

| Component | Class | Support files (exact paths) | Dependencies | Client behaviour | Licence and attribution |
|---|---|---|---|---|---|
| `github-cli-skill` | standalone-safe | `plugins/code/skills/github-cli/SKILL.md`, `plugins/code/skills/github-cli/references/setup.md` | `gh`; network to the GitHub host | Agent Skills directory, mounted as `code-github-cli` because a skill's name must equal its directory name. Asha does this on Claude Code, Codex, Copilot CLI and OpenCode (symlinked native skill) and in the Copilot build; other clients and placements are untested | MIT, `plugins/code/LICENSE` |
| `verify-tool` | standalone-safe | `plugins/code/tools/verify.py` | `python3` (standard library); uses `tsc`, `ruff`, `pytest`, `cargo` and other toolchains when present | Command-line tool; no client integration | MIT, `plugins/code/LICENSE` |
| `find-skills-inspector` | standalone-safe | `plugins/asha/skills/find-skills/tools/find_skills.py`, `plugins/asha/skills/find-skills/tools/find_skills_cli.py`, `plugins/asha/skills/find-skills/tools/find_skills_common.py`, `plugins/asha/skills/find-skills/tools/find_skills_inspect.py`, `plugins/asha/skills/find-skills/tools/find_skills_store.py` | `python3`; PyYAML for `inspect`; network to skills.sh (`search`) and GitHub (`inspect`) | Command-line tool: `search`, `inspect` and `status --asha-home STORE` | MIT, `plugins/asha/LICENSE` |
| `debugger-guidance` | adapter-required | `plugins/code/agents/debugger.md`, `plugins/code/modules/orchestration.md` | none | Claude Code agent frontmatter (`tools`, `memory: user`); Asha renders the other three harnesses. Names `tdd`, `reviewer` and a user-level debugging rule it does not include | MIT, `plugins/code/LICENSE`; adapted from obra/superpowers systematic-debugging (MIT License, Copyright (c) 2025 Jesse Vincent) |
| `code-verify-command` | adapter-required | `plugins/code/commands/verify.md` (pulls in `verify-tool`) | `verify-tool` | Claude command frontmatter; resolves `ASHA_ROOT` from the environment or `~/.asha/config.json`. Set `ASHA_ROOT` to the export root; other clients need Asha's rendering | MIT, `plugins/code/LICENSE` |
| `capability-broker` | asha-runtime-required | not exported | Asha dispatcher and broker registry | `asha process route`, `asha capabilities match` and `asha capabilities plan` | MIT |
| `find-skills-workflow` | asha-runtime-required | not exported | Asha skill store and installer | Imports into `$ASHA_HOME/skills`; the installer mounts imports | MIT |
| `codebase-historian` | asha-runtime-required | not exported | Memory v2 and `~/.asha/learnings` | Rendered agent inside Asha | MIT |
| `issue-loop` | asha-runtime-required | not exported | Dual opt-in in `~/.asha/config.json`, Asha policy guard, Workflow tool | Claude Code with Asha | MIT |
| `control-memory-persona` | asha-runtime-required | not exported | The Asha runtime itself | Dispatcher, Control, Memory v2, identity merge | MIT |

Approval gates travel with the files: they are instructions in the exported
text and in `STANDALONE.md`, not enforcement. No client is assumed to enforce
them, and enforcement differs between clients (see
[harness-enforcement.md](harness-enforcement.md)). In particular:

- `github-cli-skill` writes only on an explicit request; merge, approval,
  ready-for-review and release creation need separate authorization; installing
  `gh`, logging in and persistent configuration need explicit approval. Hosts
  come only from the user (`github.com` unless they name an Enterprise host),
  every authentication check is pinned to that host, reads that return bodies,
  comments, reviews or assets apply `--jq` budgets, and pushes are limited to the
  pull request's own branch, never forced.
- `verify-tool` runs the target project's own toolchain and tests at every
  level: use it only on code you trust. `--list`, and a root with no recognised
  project files, execute nothing from the project. It reads no configuration
  file, so pass `--root` explicitly.
- `find-skills-inspector` fetches candidate bytes and never executes them. Its
  `dry-run` and `import` subcommands remain Asha-coupled: they check an Asha
  checkout for name collisions and write the store the installer mounts.

## Export with immutable provenance

```bash
asha standalone list [--json]
asha standalone export verify-tool github-cli-skill --revision v1.2.3 --out /path/to/new-dir \
  [--source /path/to/asha-git-checkout] [--json]
```

`--revision` is required. The tool resolves it to a full commit id and reads
every file from Git objects at that commit with `git ls-tree` and `git cat-file`,
so uncommitted edits and untracked files never reach the export, and the
working tree is never read. (The Copilot build in
[distribution-copilot.md](distribution-copilot.md) copies tracked worktree
bytes instead and marks uncommitted changes in its README; it is a different,
installer-adjacent path.) The export refuses:

- an `asha-runtime-required` or unknown component;
- a revision it cannot resolve, a revision that begins with `-`, or one that
  predates `lib/standalone-components.json`;
- a symbolic link, submodule or directory where a component lists a file, and
  any path with a `.git` component or a control character;
- an output directory that already holds anything, or whose parent does not
  exist;
- a source that is not a Git checkout. A jj workspace without a colocated
  `.git` is not one: pass `--source` pointing at the colocated checkout;
- a partial (promisor) clone, whose missing objects Git would fetch from the
  network.

Files keep their repository-relative paths, so links such as
`references/setup.md` still resolve. Git records only whether a file is
executable, not full permissions; the export writes 0755 or 0644 to match.
Components a requested component needs (`requires_components`) are added and
marked `"requested": false`. Licence files always travel with their component.

The export adds two files:

- `PROVENANCE.json` (`asha.standalone-export.v1`): commit, requested revision,
  manifest blob, each component's classification, dependencies, approvals,
  limitations, licence and attribution, and every file's path, mode, Git blob
  id, size and SHA-256, plus a digest over the whole file list.
- `STANDALONE.md`: the same facts for a human reader.

Nothing is installed, no client configuration is touched, no persona is added
and no Memory is created.

## Evaluate without Asha's runtime

1. Export at a pinned commit into a new scratch directory (above).
2. Validate before relying on anything in the export, its own `STANDALONE.md`
   included. Name the checkout and the branch, tag or commit in it that you
   trust; `--source` requires `--trusted-ref`, because `HEAD` of another
   checkout may be a branch under review:

   ```bash
   asha standalone validate /path/to/new-dir --source /path/to/asha-checkout \
     --trusted-ref main [--json]
   ```

   Without `--source` this is a self-consistency check only:
   `PROVENANCE.json` vouches for itself. It recomputes every file's size,
   hash and Git executable bit (owner execute, not exact permissions) and
   reports modified, missing, unexpected, unreadable,
   non-regular and symlinked files; a link anywhere on a path is reported,
   never followed, and so is any directory that holds no listed file or cannot
   be listed. Files are hashed in chunks. `PROVENANCE.json` and `STANDALONE.md`
   must be regular files of at most 4 MiB and are never read through a link. A
   record with any field an export does not write, or with a duplicate JSON
   key, is refused (exit 2), in every mode. So is a hollow one: it must carry
   at least one requested component with files, and list exactly its
   components' files and licence files.

   With `--source`, the recorded commit must exist in that checkout and be an
   ancestor of the trusted ref (`source-unverified` or `source-untrusted`
   otherwise): an unreachable or fetched-for-review commit does not count.
   Git replace refs are ignored, so an object's bytes cannot be substituted
   behind its recorded id.
   Every file's mode, Git blob id, size and hash must then match that commit;
   the component records must equal what `export` writes for the requested
   ids (closure, order and flags) from the manifest at that commit; and
   `STANDALONE.md` must re-render byte for byte (`source-mismatch`). A notice
   rendered by an older or newer Asha also reads as a mismatch: re-export to
   refresh it.

   Each declared dependency is checked too: commands by `PATH` lookup (never
   run) and Python modules with `python3 -I -c` and
   `importlib.util.find_spec`. A missing dependency is named with its
   component and kind; the tool never searches for installed copies elsewhere
   or guesses a mount path. Network requirements are listed as declared, not
   probed. Text from the export is printed with control characters escaped.
   Exit status: 0 ok, 1 missing dependencies or failed checks, 2 refused.
3. Read `STANDALONE.md` and the exported files once validation passes.
   Reading executes nothing.
4. Optionally run each tool once:

   ```bash
   asha standalone validate /path/to/new-dir --source /path/to/asha-checkout \
     --trusted-ref main --smoke
   ```

   Run `--smoke` only if you trust the exported Python: it executes it. It runs
   only after integrity and the source check pass (without `--source` it uses
   the validating checkout and its `HEAD`, the code already running, and skips
   smoke when that is not a Git checkout whose `HEAD` reaches the commit). It
   runs the verified blobs read from the source's Git objects, written into a
   private temporary directory, never the export's own files, which could
   change after they were checked. Smoke commands come from the validating
   checkout's manifest, never from the export, and each must run an exported
   file. Each runs under `python3 -I -B` with an environment of only `PATH`,
   `HOME` (an empty directory inside the temporary directory), `TMPDIR` and
   `LANG`: no `ASHA_*`, `XDG_*`, `CODEX_HOME` or `PYTHON*` variables. That
   isolates the working directory, `HOME` and environment only; it is not
   operating-system containment, and the code runs with your user's
   file-system and network permissions. The report says whether that home
   stayed empty, and the directory is removed afterwards.
   Current smoke checks: `verify.py --list`, `verify.py --root EMPTY --json`,
   `find_skills.py --help` and `find_skills.py status --asha-home EMPTY`.
5. Use a component by pointing a client or shell at the exported files. For
   `code-verify-command`, set `ASHA_ROOT` to the export directory. Where a
   client keeps its skills or agents is that client's business; Asha has
   tested only its own installed layout.

An export copied between machines is checked the same way: validate it with
`--source` and `--trusted-ref` against your own clone whose trusted branch or
tag contains the recorded commit.

## What stays in Asha

The broker (`process route`, `capabilities match` and `capabilities plan`),
the `asha-find-skills` import workflow, Control sessions and Rooms, Memory v2,
the issue loop, learnings and the persona are the runtime. They are not part
of the portable subset, and exporting them is refused.
