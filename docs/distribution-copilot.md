# Copilot-Native Plugin Distribution

How to package asha's plugins for GitHub Copilot CLI and distribute them to a
team (issue #3). Personal installs keep the symlink-mount (`./install.sh
--target copilot`); this path is **additive**, for org-scale rollout via
Copilot's native plugin system.

Enforcement/capability verdicts live in
[harness-enforcement.md](harness-enforcement.md); this doc is mechanism.

## Build

```bash
asha build copilot                          # default basekit -> dist/copilot/
asha build copilot --only code,devops      # curated subset
asha build copilot --only test --version 0.0.1   # probe canary
```

Default basekit: `code devops security prompt`. Personal namespaces
(`admin asha image panel schedule session test write`) require explicit
`--only`; `output-styles` is refused (Claude-only). Versions parse from each
`plugins/<ns>/README.md` `**Version**:` line; `--version` overrides all.

Output (`dist/copilot/`, gitignored):

```
marketplace.json          # Copilot marketplace index (owner + plugins[].source)
README.md                 # provenance: source SHA, build date, versions, install matrix
settings-snippet.json     # {"enabledPlugins": {"asha-<ns>@asha": true, ...}}
plugins/asha-<ns>/
├── plugin.json           # {name, version, description}
├── skills/<s>/SKILL.md   # real skills verbatim; commands/*.md converted
├── agents/<a>.agent.md   # frontmatter reduced to {name, description}
└── modules/ recipes/ ... # content dirs verbatim; hooks NEVER packaged
```

Hooks are never packaged: the source `hooks/hooks.json` files are
Claude-schema, and plugin-delivered hooks don't fire anyway
(github/copilot-cli#2540). Guardrails remain a user-scope install
(`./install.sh --target copilot`).

## Publish

The dist tree is itself a Copilot marketplace. Publish it to your
distribution remote (for org rollout: an internal GitHub Enterprise repo —
not the asha source repo):

```bash
asha build copilot
cd dist/copilot
git init && git add -A && git commit -m "asha copilot plugins <source-sha>"
git tag v1.0.0
git remote add origin <ghe-org>/<asha-copilot-dist>
git push -u origin main --tags
```

Suggested automation (not shipped): a build-on-tag workflow in the source
repo that regenerates the dist and pushes to the mirror. Keep the mirror's
default branch as "current release" — see Pinning.

## Consume (teammate experience)

```bash
# One-time, per user — register the marketplace and install plugins:
copilot plugin marketplace add <ghe-org>/<asha-copilot-dist>
copilot plugin install asha-code@asha
copilot plugin install asha-devops@asha

# Or per repo, declaratively: merge settings-snippet.json into the repo's
# .github/copilot/settings.json (scaffolded by `asha init-repo`).
```

Plugins are copied to `~/.copilot/installed-plugins/<marketplace>/<plugin>/`
and their skills load under plain `copilot` — no asha wrapper involved. The
Asha persona is NOT part of the distribution (wrapper-only by design).

## Pinning

There is **no `plugin@marketplace@version` syntax** (verified 1.0.65: the
second `@` segment parses as a marketplace name). Pinning is repo discipline
on the distribution mirror:

- Tag every publish (`vX.Y.Z`); the default branch IS the current release.
- Teams that need to hold back consume from a fork/branch of the mirror.
- Per-plugin versions live in `plugin.json`/`marketplace.json` and surface in
  `copilot plugin list`.

## Verification status (plant-and-probe, Copilot CLI 1.0.65, 2026-07-01)

| Assumption | Status | Evidence |
|---|---|---|
| plugin.json minimal fields `{name,version,description}` | **VERIFIED** | canary install succeeded |
| marketplace.json schema (`owner` + `plugins[].source`) | **VERIFIED** | CLI validator named the fields; Claude-marketplace compatible |
| Local-dir marketplace add | **VERIFIED** | `copilot plugin marketplace add <dir>` works; `file://` URLs rejected |
| Whole-tree copy; extra dirs tolerated | **VERIFIED** | styles/, agents/, README, LICENSE all copied; install clean |
| `triggers:` frontmatter tolerated | **VERIFIED** | skill loaded and fired |
| Skill fires under plain `copilot` | **VERIFIED** | `test:ping` sentinel returned with the personal skill copy hidden |
| `enabledPlugins` object-map form | **VERIFIED** | install writes `{"asha-test@asha": true}` |
| No `@version` pin syntax | **VERIFIED** | parses as marketplace name |
| `.agent.md` FUNCTIONS as agent when plugin-delivered | UNVERIFIED | install reports skills only; rejection ruled out, function not |
| Relative refs (`../../tools/x.py`) resolve at runtime | UNVERIFIED | canary has no tool refs; probe with asha-code |
| `owner/repo:path` + declarative repo-scope auto-install | UNVERIFIED | needs a real remote; probe on the mirror before rollout |

Re-probe the UNVERIFIED rows on the actual distribution remote before team
rollout, and re-check copilot-cli#2540 before ever packaging hooks.
