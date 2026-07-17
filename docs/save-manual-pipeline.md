# Manual Save Pipeline

**Audience**: any session where the save plugin is absent, partial, or unmountable
(fresh machine, moved checkout, broken symlink mount). This is the documented
fallback that `save-preflight-env.sh` points at when its plugin-verification
phase fails (exit code 3).

The automated pipeline (synthesis, guardrails, engine-backed gates) is always
preferred. Use this document only when it is unavailable, and record in the
commit message that the save was manual.

---

## 0. Confirm you are actually in the fallback case

```bash
"$ASHA_ROOT/plugins/session/tools/save-preflight-env.sh" --report
```

- Exit `2` — environment problem, not a missing plugin. Fix `ASHA_ROOT`
  (`~/.asha/config.json` → `asha_root`) or run `./install.sh`; do not fall back.
- Exit `3` — plugin genuinely missing/partial. Continue below.
- Exit `0`/`1` — the plugin is present; use the normal `/session:save` flow.

## 1. Write the handoff by hand

Edit `Memory/activeContext.md` directly:

- **Lead section** must be `## What Was Accomplished (YYYY-MM-DD — topic)` with
  the session stamp as its first body line:

  ```markdown
  ## What Was Accomplished (2026-07-17 — <topic>)
  <!-- wwa-session: <your session id> -->

  Concrete narrative: file paths touched, decisions made, blockers hit.
  ```

- **Next Steps** must be actionable cold-start items — file paths, commands,
  blocked decisions. Never `Review and plan next session`.
- Update frontmatter `lastUpdated` to the current UTC time
  (`YYYY-MM-DD HH:MM UTC`). Never a future time.

## 2. Verify against disk — disk is ground truth

Every claim in the notes must survive contact with the filesystem. The notes
are flagged and corrected, never the reverse.

```bash
# Every path the notes reference must exist:
grep -oE '`[A-Za-z0-9_./~-]+/[A-Za-z0-9_./-]+`' Memory/activeContext.md \
  | tr -d '`' | while read -r p; do [ -e "$p" ] || echo "CONTRADICTION: $p missing on disk"; done

# Claims of "committed/pushed/clean" must match git:
git status --short
git log --oneline -3
```

Remove or correct any contradicted claim before proceeding.

## 3. Continuity gate checklist (manual equivalent of the engine gates)

| Gate | Manual check | Blocks commit? |
|---|---|---|
| memory_substrate | `Memory/` and `Memory/events/` exist (`mkdir -p` them) | yes |
| session_integrity | The notes describe THIS session, not a stale/foreign one | yes |
| ac_clobber | No `Created N file(s)` / `Modified N file(s)` / `No significant changes recorded` stub lines | yes |
| ac_wwa_provenance | Lead WWA carries `<!-- wwa-session: … -->` for this session | yes |
| ac_handoff | Next Steps is actionable, not the generic stub | no (fix anyway) |
| disk_truth | Step 2 found zero contradictions | no (fix anyway) |

Do not commit past a "yes" row that fails.

## 4. Commit and push

```bash
git add Memory/
ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1 git commit -m "Session save (manual): <summary>"
git push || echo "UNPUSHED — record this in next session's notes"
```

The override env is required because the `save-commit-gate` PreToolUse hook
(when the plugin IS mounted) refuses Memory commits without the gates-ok
marker; in the true fallback case (plugin absent) no hook fires and plain
`git commit` works. Using the override while the plugin is healthy defeats the
gate — don't.

## 5. Aftermath

On the next healthy session, run `/session:save` so the engine-backed pipeline
re-synthesizes and the manual entry is absorbed into the normal history.
