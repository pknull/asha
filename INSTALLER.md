# Symlink-Mount Installer

Flat, no-registry install model for this marketplace's primitives.
Replaces the three-file plugin registration chain (`marketplace.json`
→ `installed_plugins.json` → `enabledPlugins`).

## Model

1. The marketplace folder (`~/life/marketplace/`) stays where it is — it is
   the single source of truth.
2. `./install.sh` creates symlinks into `~/.claude/{skills,agents,commands,output-styles}/`
   pointing back into `plugins/<ns>/...`.
3. Hook scripts are **not** symlinked. They are registered in
   `~/.claude/settings.json` via absolute source paths, tagged with
   `"source": "marketplace:<ns>"`.
4. `./uninstall.sh` scans those `~/.claude/*` locations for symlinks whose
   target resolves inside this marketplace and removes them, plus strips any
   tagged hook entries from `settings.json`.

No plugin manifests (`.claude-plugin/plugin.json`) are consulted after install.

## Namespaces

`namespaces.json` maps each plugin directory to the name used for slash
commands and scan-path subdirectories. Almost all entries map 1:1 with the
dir name; two exceptions preserved from the legacy plugin names:

| Directory | Namespace |
|---|---|
| `plugins/panel/` | `panel-system` |
| `plugins/schedule/` | `scheduler` |

So `/panel-system:panel` and `/scheduler:schedule` resolve even though the
source dirs are shorter.

## Layout on disk after install

```
~/.claude/
├── skills/
│   ├── <ns>-<skill>/                  # symlink -> plugins/<ns>/skills/<skill>/
│   └── ...
├── agents/<ns>/<agent>.md             # symlink -> plugins/<ns>/agents/<agent>.md
├── commands/<ns>/<cmd>.md             # symlink -> plugins/<ns>/commands/<cmd>.md
├── output-styles/<ns>-<style>.md      # symlink -> plugins/<ns>/styles/<style>.md
└── settings.json
    └── hooks.<Lifecycle>[].hooks[]    # tagged "source": "marketplace:<ns>"
                                       # command = abs path into plugins/<ns>/hooks/
```

## Commands

```
./install.sh [--dry-run] [--only ns1,ns2,...] [--force] [--verbose]
./uninstall.sh [--dry-run] [--verbose]
./deprecate-marketplace.sh [--dry-run]       # one-shot legacy cleanup
```

`install.sh` is idempotent: re-running skips links already correct, refuses
mismatched ones (require `--force`). `uninstall.sh` is idempotent: re-running
is a no-op.

## Test plugin

`plugins/test/` ships one of every primitive emitting a unique sentinel
string. Use it as a canary:

```
./install.sh --only test
# restart Claude Code
/test:ping                  # expect TEST-PING-CMD-OK
```

See `plugins/test/README.md` for the full smoke-test matrix.

## Backups

Every mutating operation backs up `~/.claude/settings.json` (and
`installed_plugins.json` for `deprecate-marketplace.sh`) with a timestamped
`.bak-<YYYYMMDD-HHMMSS>` suffix before editing.

## Known edges

### Dotfiles-backed `agents/` and `hooks/`

`~/.claude/agents` and `~/.claude/hooks` may themselves be symlinks into a
separately-tracked dotfiles repo. The installer writes per-plugin
subdirectories there (`~/.claude/agents/<ns>/`, `~/.claude/hooks/<ns>/`).
Those subdirs show as untracked in the dotfiles repo. Either:

- add `claude/.claude/agents/*/` and `claude/.claude/hooks/*/` to the
  dotfiles `.gitignore`, or
- break the dotfiles symlink and let `~/.claude/agents` be a real directory
  with per-file symlinks into dotfiles for the user's curated list.

### Output styles are plugin-local

The `/output-styles:style` command reads from its *own plugin's* `styles/`
directory only (`${CLAUDE_PLUGIN_ROOT}/styles/`). It does NOT scan
`~/.claude/output-styles/` at the user level. Consequence: a plugin *other*
than `output-styles` cannot contribute styles via the symlink-mount model —
the symlink is created but no scanner ever sees it.

The installer still creates the symlinks (they're harmless), but expect
every style under `~/.claude/output-styles/` except the ones inside the
`output-styles` plugin to be invisible. Cross-plugin output styles require
either: teaching the `/style` command to also scan `~/.claude/output-styles/`,
or moving all styles into the `output-styles` plugin's `styles/` dir.
