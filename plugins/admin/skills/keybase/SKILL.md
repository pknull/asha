---
name: admin-keybase
description: Keybase encrypted git remotes (keybase://) compared with GitHub and gh. Use when a repo's remote is keybase://, when listing, creating, deleting or garbage-collecting Keybase repos, when a push prints a Keybase gc tip or a gc fails, or when deciding where a private repo should live.
metadata:
  triggers: "keybase:// remote | keybase git list/create/delete/gc | 'consider running keybase git gc' tip | gc error 'Getting object ... failed: object not found' | Keybase vs GitHub for a private repo | push to keybase fails or hangs"
---

# Keybase git

Keybase hosts end-to-end encrypted git repos inside KBFS. Plain `git` does the
work through the `git-remote-keybase` helper; `keybase git` only manages the
repos themselves. Run `keybase git list` for the current repos; never assume a
list.

## Keybase vs GitHub

| | GitHub (`gh`) | Keybase (`keybase git`) |
|---|---|---|
| Remote URL | `git@github.com:OWNER/REPO.git` | `keybase://private/USER/REPO`, `keybase://team/TEAM/REPO` |
| Transport | SSH/HTTPS to a server | local Keybase service + KBFS; needs the service running and logged in |
| Visibility | public or private; the server can read it | always encrypted; only the user or team members can read it |
| Issues, PRs, CI, releases, web UI, API | yes | none: it is a bare remote |
| List repos | `gh repo list` | `keybase git list` |
| Create | `gh repo create` | `keybase git create REPO` (`--team=T`) |
| Delete | `gh repo delete` (asks to confirm) | `keybase git delete REPO`: **immediate and irreversible** |
| Rename | `gh repo rename` | not supported: create new, push, swap the remote, delete the old one |
| Maintenance | server-side | `keybase git gc REPO` (`--force` only skips the "is gc needed" check) |
| LFS | built in | `keybase git lfs-config` in the checkout |
| Archive | download | `keybase fs archive start` with `--git` |

Use `gh` for anything that needs collaboration features. Keybase is for
private, encrypted storage and mirroring.

## Reads (run freely)

```bash
keybase status | grep -E 'Logged in|status'   # service and KBFS up?
keybase git list
git remote -v                                  # which remotes are keybase://
git ls-remote origin                           # verify a push landed
```

Read-only inspection of repo storage: `/keybase/private/USER/.kbfs_git/REPO/`
holds packs, refs, a `.gc` marker (time of the last successful gc) and a
`.gc_in_progress` lock. Logs: `~/.cache/keybase/keybase.service.log` and
`keybase.kbfs.log`. Never write under `.kbfs_git`.

## Writes (ask first)

`create`, `delete`, `gc`, `settings` and every push are outward-facing; ask
before each. `delete` has no undo. Before deleting, verify a fresh clone of the
replacement (`git fsck --full`, matching commit count) and keep a
`git bundle create FILE --all` backup outside the repo.

To mirror to two hosts, give one remote several push URLs:

```bash
git remote set-url --add --push origin git@github.com:OWNER/REPO.git
git remote set-url --add --push origin keybase://private/USER/REPO
```

Once any push URL is set, the fetch URL is no longer pushed to, so list every
target explicitly.

## Gotchas

- **Service down.** Pushes and fetches fail or hang when the Keybase service or
  KBFS is not running. Check `keybase status`; on Linux, `run_keybase` starts
  it. This is the usual cause of a failed push, not the repo.
- **gc tip on every push.** The push prints a "consider running gc" tip when
  the last successful gc is over 7 days old and the repo has over 50 packs or
  loose refs. Each push adds a pack. If gc keeps failing, the marker never
  refreshes and the tip repeats.
- **Submodules break gc.** Keybase's go-git object walker recurses into
  submodule gitlinks (mode 160000) as if they were trees. It then asks for an
  object the repo never holds and aborts with
  `Getting object <sha> failed: object not found`. Seen on client 6.5.1; the
  walker is unchanged on Keybase master. A gitlink in a ref's current tree is
  enough to fail; gitlinks only in older history did not stop gc. Check with
  `git ls-tree -r HEAD | grep '^160000'`. Fix it by removing the submodule
  (`git rm` it and drop `.gitmodules`), and avoid submodules in Keybase repos.
  Re-pushing and `--force` do not help. The missing sha belongs to the
  submodule's upstream, so `git fsck` on the superproject passes.
- **Stale `.gc_in_progress`.** A failed gc leaves this lock behind; Keybase
  expires it on its own. Leave it.
- **No rename.** Moving a repo means create, push all refs, verify, swap the
  remote, then delete the old one after a soak period.
