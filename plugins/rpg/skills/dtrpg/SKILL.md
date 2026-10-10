---
name: rpg-dtrpg
description: Audit a DriveThruRPG library against the user's hand-curated RPG folder, fetch missing or updated files through the drpg client, file them into the right folders, and find free backer copies emailed by publishers that were never claimed. Use when the user asks to sync, audit or update DriveThruRPG purchases, find missing RPG PDFs, or claim DriveThruRPG comp copies. Requires DRPG_TOKEN from the Asha secrets wrapper.
metadata:
  triggers: "Sync or audit DriveThruRPG purchases | Find RPG PDFs missing from the local library | Download updated DriveThruRPG files | Claim free DriveThruRPG copies from backer emails | Any DTRPG library maintenance"
---

# DriveThruRPG library maintenance

`scripts/dtrpg.py` is the execution path. It reuses the third-party
[`drpg`](https://github.com/glujan/drpg) client for authentication and its
prepare-and-poll download flow, then adds what drpg lacks: auditing against a
library organised by game rather than by publisher, placement into that
library, and the comp-copy claim check.

Do **not** run the `drpg` command itself against the library root. It writes a
`Publisher/Product/` tree and would duplicate every purchase beside the curated
folders.

## Setup

- `uv tool install drpg` provides the client. Run the script with that tool's
  interpreter so `drpg` and `httpx` import:
  `PY=~/.local/share/uv/tools/drpg/bin/python`
  `SCRIPT=~/.claude/skills/rpg-dtrpg/scripts/dtrpg.py`
- `DRPG_TOKEN` is a DriveThruRPG Library App key (account settings → Library
  App Keys), exported from `~/.asha/secrets.env` by `asha <harness>`. If it is
  unset, stop and say so. Never ask for the key in chat and never read the
  secrets file. In a background job, source the bootstrap in a subshell:
  `( set +x; source ~/Code/asha/bin/asha-env-bootstrap.sh >/dev/null 2>&1; "$PY" "$SCRIPT" ... )`.
- The library root comes from `DTRPG_LIBRARY_ROOT`, set in
  `~/.asha/secrets.env` beside the token, or from `--root`. Audit, fetch and
  place stop when neither is set. Staging defaults to
  `~/Downloads/dtrpg-staging` and must stay outside the root.
- Personal state lives in `~/.asha/dtrpg/` (override with `DTRPG_STATE_DIR`),
  never in this repo:
  - `placements.tsv`: `product_id<TAB>folder relative to root<TAB>note`
  - `ignore.tsv`: `product_id<TAB>filename glob<TAB>reason` for deliberate
    exclusions (other languages, alternate formats, zips already extracted)

Every command prints one JSON object; errors are JSON with a non-zero exit.

## Audit and fetch

```bash
"$PY" "$SCRIPT" library > /tmp/dtrpg-library.json
"$PY" "$SCRIPT" audit --library-json /tmp/dtrpg-library.json > /tmp/dtrpg-audit.json
"$PY" "$SCRIPT" fetch --from-audit /tmp/dtrpg-audit.json --status missing,stale --dry-run
timeout 3600 "$PY" "$SCRIPT" fetch --from-audit /tmp/dtrpg-audit.json --status missing,stale > /tmp/dtrpg-fetch.json
"$PY" "$SCRIPT" place --from-fetch /tmp/dtrpg-fetch.json --library-json /tmp/dtrpg-library.json
"$PY" "$SCRIPT" place --from-fetch /tmp/dtrpg-fetch.json --library-json /tmp/dtrpg-library.json --apply
```

Audit statuses: `ignored`, `present`, `stale` (a local copy exists but the
publisher updated the product after the last download), `present_by_size` (a
renamed copy, a hint only), `missing`.

Procedure:

1. Run `audit`. Present `missing` and `stale` grouped by product with sizes.
   Explain `present_by_size` hits rather than fetching them.
2. Downloads need the user's go. A request to sync or update the library is
   that go for the listed items; otherwise list them and ask once.
3. `fetch` downloads one item at a time with retry. drpg polls a "Preparing"
   file with no time limit, so keep the `timeout` wrapper (or run it in the
   background) and treat a timeout as one stuck item, not a dead library.
   A `stale` row whose reason says "never downloaded from DTRPG" usually
   matches the local copy already (it came from a pledge manager); compare
   before fetching. Then run `place` without `--apply` and review the plan.
4. A product with no `placements.tsv` row is a placement decision. Follow the
   existing folder pattern (by game or system, e.g. `CallOfCthulhu/Books`,
   `YearZero/<game>`, `ShadowRun/Core Resources/Missions`), add the row, and
   log the choice in the report. Ask only when no folder plausibly fits.
5. Apply. `place` never overwrites. For a `stale` file, compare it with the
   older local copy (`pdfinfo` pages and dates, `md5sum`) and send the
   superseded copy to the desktop trash with `gio trash`. Never `rm`.
6. Zips are placed as-is. Extract where the user keeps extracted content:
   test with `unzip -tq`, skip `__MACOSX` and `.DS_Store`, compare against
   existing files by md5 before adding, then trash the zip. Add an `ignore.tsv`
   row for a zip whose contents now live extracted, so the next audit does not
   report it as missing.
7. Re-run `audit` and report the remaining `missing` and `stale` counts.

## Unclaimed free copies

Publishers deliver backer PDFs as DriveThruRPG comp-copy emails. A copy enters
the library only after its link is clicked and a $0 checkout completes, so
unclaimed copies never appear in `audit`.

1. Find the emails: `from:no-reply-comp-copies@drivethrurpg.com`. Prefer the
   `gws` CLI; the Gmail connector also works.
2. Save each message's raw content to its own file in a fresh temp directory.
   Use `gws gmail users messages get --params '{"userId":"me","id":"ID","format":"raw"}'`
   and write its `raw` field, or the connector's `RAW` format. Raw content
   matters: the links are quoted-printable, and decoded text corrupts codes
   such as `discount=91f7…`.
3. Run:
   `"$PY" "$SCRIPT" claims --eml-dir DIR --library-json /tmp/dtrpg-library.json`
4. Give the user each unclaimed `claim_url`. The legacy
   `browse.php?discount=` links redirect but add nothing to the cart; the
   script emits the working `/en/browse?discountId=` form. Adding to the cart
   is fine to automate in the browser, but the user completes the $0 checkout
   themselves.
5. After checkout, re-run `library` and `audit`; the new products show as
   `missing`.

## Gotchas

- DriveThruRPG stamps each download for the buyer, so local and library sizes
  differ for many PDFs without being different versions. `stale` comes from the
  server's last-modified versus last-downloaded dates, not from size.
- Prepared links point at `watermark.drivethrurpg.com`, whose load balancer
  answers non-browser user-agents with `503 "Blocked for not following
  robot.txt rules, Bad bot"`. The script sends drpg's browser-like headers; a
  503 with that body means a client sent the wrong headers, not an outage.
- The website's per-file Download buttons fetch a short-lived storage link with
  no retry and can return 503 while the stamped copy is being prepared. The
  script polls prepare and retries instead. The website's multi-select bundle
  ("Download N items") opens a popup the browser may block; the user's own
  window handles it.
- Any website download attempt, even a failed one, updates the product's
  last-downloaded date and can hide a real update from `stale`. When in doubt,
  compare against the local copy directly.
- drpg uses an undocumented API and may break without notice. If `library`
  fails on schema or authentication, check for a newer drpg release
  (`uv tool upgrade drpg`) before debugging further.
