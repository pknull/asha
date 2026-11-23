---
name: admin-keybase
description: Keybase encrypted git remotes (keybase://) compared with GitHub and gh. Use when a repo's remote is keybase://, when listing, creating, deleting or garbage-collecting Keybase repos, when a push prints a Keybase gc tip or a gc fails, or when deciding where a private repo should live.
metadata:
  triggers: "keybase:// remote | keybase git list/create/delete/gc | 'consider running keybase git gc' tip | gc error 'Getting object ... failed: object not found' | gc that never returns | git-remote-keybase error 'is not empty and can't be removed' | Keybase vs GitHub for a private repo | push to keybase fails or hangs"
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
`keybase.kbfs.log`. Never write under `.kbfs_git`, with the one exception
under "Deleted repos linger".

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
  walker is unchanged on Keybase master. A gitlink anywhere in reachable
  history is enough to fail, not only one in a ref's current tree: one repo
  failed on 2026-10-27 and 2026-11-23 with its submodule gone from the tree
  since 2026-10-02. Check both: `git ls-tree -r HEAD | grep '^160000'` for the
  tree, `git log --all --raw | grep -m1 160000` for history. Removing the
  submodule (`git rm` it and drop `.gitmodules`) keeps new commits clean but
  does not repair gc. Re-pushing and `--force` do not help. The missing sha
  belongs to the submodule's upstream, so `git fsck` on the superproject
  passes. Avoid submodules in Keybase repos. Two ways out, both the user's
  call:
  - Rewrite history to drop the gitlinks. Every commit hash changes and every
    clone must be re-cloned. Worked on one repo on 2026-11-23: gc no longer
    failed on the missing object. The first run stalled (see "gc stalls on
    upload"); a second run that day finished in five minutes and left one
    28 MB pack where there had been 1.3 GB.
  - Delete and recreate the repo under the same name, then push. This gives
    the single pack gc would have given and keeps the remote URL; gc still
    fails afterwards. `keybase git delete` asks for a typed `YES` on a
    terminal: with no tty it fails with `open /dev/tty`, and through a pty the
    answer must end in a carriage return, not a newline. Read "Deleted repos
    linger" first: on 2026-11-23 this route broke every repo of the user
    until the leftover was moved away.
- **Deleted repos linger, and their cleanup can break every repo.**
  `keybase git delete` moves the repo to
  `.kbfs_git/.kbfs_deleted_repos/NAME<id>-<unix-nanoseconds>`. Hours later,
  every git operation in that namespace first tries to remove it. On
  2026-11-23, eight hours after a repo was deleted, that removal failed
  with `Directory <two-word-name> is not empty and can't be removed` (the name
  is Keybase's log obfuscation), and from then every `ls-remote`, fetch and
  clone of every private repo of that user failed with the same error, after
  listing refs correctly. It failed the same way on each try. In
  `keybase.git.log`, look for `Cleaning deleted repo` and
  `Done checking deleted repos`. Why the removal fails is not established.
  What fixed it: with the user's word, move the leftover directory out of
  `.kbfs_deleted_repos` into the user's private folder with a plain `mv`
  through the mount (`/keybase/private/USER/NAME-deleted-repo-DATE`). It is a
  rename inside one folder tree, so nothing is lost; every repo worked again
  at once. This is the one exception to "never write under `.kbfs_git`".
  Deleting the moved copy afterwards is a separate decision.
- **gc stalls on upload.** gc does its work in a temporary Keybase instance
  (`~/.local/share/keybase/kbfsgit<digits>/`) and then uploads from that
  instance's journal. On 2026-11-23 the upload looped on
  `Flushed 0 block entries and 0 MD entries ... but got error EOF` in
  `keybase.kbfs.log`, hundreds of times a second (about 20 MB of log a
  minute, rotating older logs away), for seven hours. A gc that has not
  returned after ten minutes: check that log. Stopping the `keybase git gc`
  client ended the loop and removed the temporary directory; nothing had been
  uploaded, so the repo was as before, with the `.gc_in_progress` lock left
  behind. Cause not established. A rerun half an hour later, after the
  leftover deleted repo had been moved away, finished normally and cleared
  the lock; whether the two are connected is not established. To rerun
  safely, watch the log and stop the client if the loop returns.
- **`--force-with-lease` is refused.** git sends the lease to the helper as
  `option cas` and then an ordinary `push src:dst` with no `+`; the Keybase
  helper ignores the option, sees a non-fast-forward and answers
  `! [remote rejected] ... (some refs were not updated)` after uploading the
  objects. Seen on client 6.5.1 on 2026-11-23. A rewritten history needs a
  plain `git push --force`; check `git ls-remote origin` by hand first, since
  nothing else guards against a concurrent push.
- **Stale `.gc_in_progress`.** A failed gc leaves this lock behind; Keybase
  expires it on its own. Leave it.
- **No rename.** Moving a repo means create, push all refs, verify, swap the
  remote, then delete the old one after a soak period.
