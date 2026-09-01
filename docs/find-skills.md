# Third-party Agent Skill discovery and import

`find-skills` fills one narrow gap: discover a reusable Agent Skill, inspect its
pinned upstream bytes, and copy a Keeper-approved candidate into Asha's
user-owned skill store. It is not a registry, package manager, publishing
system, update service, or execution sandbox.

## Authority and storage

The canonical store is `${ASHA_HOME:-~/.asha}/skills/`:

```text
~/.asha/skills/
├── imported.lock.json       # Asha provenance and hashes
├── .mounts/                 # installer-derived portable name adapters
└── candidate-name/          # byte-for-byte upstream skill directory
    ├── SKILL.md
    └── ... support files
```

The store deliberately sits outside the repository. A repository checkout or
`git clean` therefore cannot remove imported user content, and third-party
files are never redistributed as part of Asha. The lockfile is the only place
Asha adds provenance. Imported files receive no Asha header, comment, or
frontmatter mutation.

`asha install` remains the only harness adapter. It reads lock-recorded skill
directories from the canonical store and mounts them as `imported-<name>` on
all supported harnesses. Because Agent Skills require the frontmatter name to
match that mount directory, the installer derives a hidden
`.mounts/imported-<name>` adapter whose only semantic change is the name. The
canonical imported bytes remain untouched. The importer contains no harness
paths. Uninstall and retired-link reconciliation recognize both repository
sources and the imported store, while preserving foreign links.

The `imported-` prefix leaves 55 characters for the upstream name under the
Agent Skills 64-character name cap. The installer deterministically accepts
names through that limit and refuses longer derived mount names before writing
an adapter or harness link; it never emits an over-limit mounted skill.

## Trust boundaries

### Fetched and untrusted

- `https://www.skills.sh/api/search?q=<query>` supplies discovery metadata
  (`id`, `skillId`, `name`, `installs`, and `source`). It does not supply skill
  content or approval.
- Public GitHub API responses resolve the named upstream repository to a
  40-hex commit and enumerate its tree.
- Raw repository bytes are fetched only at that immutable commit. Inspection
  covers `SKILL.md` and all blobs beneath its directory. Root licence evidence
  is hashed separately in the lockfile.

Search popularity, repository ownership, licence metadata, documentation, and
the absence of a scanner finding are not proof that code is safe.

### Validated and reported

Inspection validates the portable Agent Skills frontmatter contract, directory
name, paths, response bounds, immutable revision, and complete (non-truncated)
tree. Evidence reports:

- declared compatibility/dependencies and dependency manifests;
- declared tools and permissions;
- frontmatter and repository licence evidence;
- every fetched file's size and SHA-256 plus a deterministic tree digest;
- network, shell-out, package-installation, credential, and path-escape text;
- executable support files, symlinks, submodules, and Git LFS pointers.

Unknown frontmatter keys fail import by name. `allowed-tools` also fails import:
although present in the open format, it requests a permission guarantee Asha
cannot preserve consistently across Claude, Codex, Copilot, and OpenCode.
Symlinks, submodules, and LFS pointers fail because safe copying would not
faithfully reproduce their upstream filesystem semantics.

Scanner findings other than format blockers are evidence for the Keeper, not a
claim that a candidate is malicious or safe.

The Skills.sh JSON search transport is Python 3 standard library only. Both
pinned-revision inspection and the installer's mount-name adaptation depend on
PyYAML for standards-compliant `SKILL.md` frontmatter parsing. If PyYAML is
unavailable, inspection fails before an import can be proposed or written, and
the installer refuses the imported mount with a dependency-and-remedy message;
search remains available.

### Never done

- No `npx`, Node runtime, package manager, upstream installer, or fetched script
  is invoked.
- No fetched file is executed during search, inspection, dry run, or import.
- No import occurs without `--approve`; replacing recorded or locally drifted
  content additionally requires `--replace`.
- No telemetry, publication, background refresh, or automatic update exists.
- No harness link is created by the skill tool.

## Review and maintenance

Use `inspect`, then a pinned `dry-run`, and present both to the Keeper before
running `import --approve`. The import invocation prints its proposal before
writing. A first import atomically regenerates `imported.lock.json`; later
imports retain previous entries in lock history. Replacement moves prior user
content into `.find-skills-backups/` rather than deleting it.

Run `find_skills.py status` or `asha doctor` to recompute local hashes. Drift is
reported but never repaired automatically. Re-inspect upstream content at a new
commit and obtain fresh approval for any update.
