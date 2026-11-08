# Lunch Money API calls

Every call goes to the v2 API, `https://api.lunchmoney.dev/v2`, with
`Authorization: Bearer $LUNCH_MONEY_TOKEN`. Shapes, field names and paging come
from the official v2 OpenAPI spec (`@lunch-money/v2-api-spec` 2.11.1,
`lunch-money-api-v2.yaml`) and the developer guides on pagination, rate
limiting and the v2 overview. No command needs v1.

## Reads

| Command | Calls |
|---|---|
| `me` | `GET /me` |
| `categories` | `GET /categories?format=flattened` |
| `tags` | `GET /tags` |
| `accounts` | `GET /manual_accounts`, `GET /plaid_accounts` |
| `crypto` | `GET /crypto/manual`, `GET /crypto/synced` |
| `transactions` | `GET /transactions` (paged) |
| `transaction` | `GET /transactions/{id}` |
| `attachment-url` | `GET /transactions/attachments/{file_id}` |
| `recurring` | `GET /recurring_items` |
| `budget` | `GET /summary`, `GET /categories` |
| `budget-settings` | `GET /budgets/settings` |
| `balance-history` | `GET /balance_history` or `GET /balance_history/{account_type}/{account_id}` |
| `suggest` | `GET /transactions` (paged), `GET /categories` |
| `subscriptions` | `GET /transactions` (paged), `GET /recurring_items` |
| `missed-bills` | `GET /recurring_items` for the month |
| `duplicates` | `GET /transactions` (paged) |
| `budget-burn` | `GET /summary` (aligned period), `GET /categories` |
| `changed` | `GET /transactions?updated_since=`, `GET /categories`, `GET /tags`, `GET /manual_accounts` |
| `stale-balances` | `GET /manual_accounts`, `GET /crypto/manual` |
| `net-worth` | `GET /me`, `GET /balance_history`, `GET /manual_accounts`, `GET /plaid_accounts`, `GET /crypto/manual`, `GET /crypto/synced` |
| `conventions` | none; reads the local conventions file |

## Writes

The client refuses any other method and path before sending. Every plan
command and every apply first reads `GET /me`: the plan records the budget
`account_id` and user `id`, and apply refuses when the token reaches another.
The drift-check read happens immediately before the write; it is not atomic
with it (see the facts below). Where a plan assigns categories or tags,
apply also reads `GET /categories` and `GET /tags` and refuses one that is
now archived, gone or (for a category) a group.

| Plan action | Drift-check read, just before the write | Write |
|---|---|---|
| `update-transactions` | `GET /transactions` over the rows' dates and over any new dates; `GET /recurring_items?include_suggested=true` when a row links a recurring item | `PUT /transactions` |
| `categorize` | the same as `update-transactions`, plus `GET /categories` (new names absent, groups intact) | `POST /categories` per new category, each re-read with `GET /categories`; then `GET /transactions` again and `PUT /transactions` with the new ids |
| `create-category` | `GET /categories` | `POST /categories` |
| `update-category` | `GET /categories` | `PUT /categories/{id}` |
| `create-tag`, `update-tag` | `GET /tags` | `POST /tags`, `PUT /tags/{id}` |
| `split-transaction` | `GET /transactions/{id}` | `POST /transactions/split/{id}` |
| `unsplit-transaction` | `GET /transactions/{id}` for the parent and its children (each child on its own when it comes without `files`), before saving and again after | `GET /transactions/attachments/{file_id}` per file to save, then `DELETE /transactions/split/{id}`, which deletes every split child |
| `group-transactions` | `GET /transactions` over the members' dates | `POST /transactions/group` |
| `ungroup-transaction` | `GET /transactions/{id}` for the group and its members, before saving and again after | `GET /transactions/attachments/{file_id}` per file to save, then `DELETE /transactions/group/{id}`, which deletes the group transaction |
| `create-transactions` | `GET /transactions` over the dates (refused when the bound is reached with `has_more`), `GET /manual_accounts` | `POST /transactions` with `apply_rules: false`, `skip_balance_update: true` |
| `upsert-budget` | `GET /summary` (occurrences) | `PUT /budgets` |
| `plaid-fetch` | `GET /plaid_accounts` (an all-account fetch also refuses an added account) | `POST /plaid_accounts/fetch` |

The two `DELETE` routes **delete rows**: unsplit deletes every split child
(with its edits, links and attachments) and restores the parent; ungroup
deletes the group transaction (with its attachments) and keeps its members.
They are the only deletes the helper can send. Both plans list each row they
delete in full and save its attachments before the `DELETE` (see the facts
below).

## Facts the helper relies on

- `GET /transactions` filters: `start_date` and `end_date` (both or neither),
  `status`, `category_id` (`0` for uncategorized), `manual_account_id`,
  `plaid_account_id`, `tag_id`, `include_pending`, `updated_since`,
  `created_since`. Pending rows, split parents and group members are excluded
  by default. Paging is by offset: `limit` 1 to 2000, `offset`, and the
  response's `has_more`.
- `GET /transactions/{id}` returns `children` for split parents and groups and
  `files` for attachments.
- `PUT /transactions` updates 1 to 500 rows; `tag_ids` replaces tags; notes
  clear with an empty string. Split and grouped transactions cannot be updated
  through it. The helper sends only `category_id`, `tag_ids`, `status`,
  `notes`, `payee`, `date` and `recurring_id`; never `amount`, `currency`,
  `manual_account_id`, `plaid_account_id`, `external_id` or `custom_metadata`.
- The linked recurring item is `recurring_id` (integer, nullable) in both the
  transaction object and `updateTransactionObject`; `null` unlinks.
  `GET /recurring_items` lists only reviewed items unless
  `include_suggested=true`; each item's `status` is `reviewed` or `suggested`.
  The spec says only reviewed items are applied to matching transactions, yet
  transactions do carry the ids of suggested items, so the helper reads with
  `include_suggested=true` and accepts either.
- `payee` in `updateTransactionObject` has `minLength: 0` and, in 2.11.1, no
  `maxLength`. The spec published `maxLength: 140` through 2.9.1; 2.9.2's
  changelog says only "Increase allowable length for a transaction `payee`".
  The helper caps new payees at 140 characters, the last published limit.
- `date` in `updateTransactionObject` is `format: date` (ISO 8601).
- Split children must sum to the parent's amount; the parent then disappears
  from listings. Groups take 2 to 500 ids, `date` and `payee`.
- `PUT /budgets` upserts by category and period start; the start must be a
  valid period start. `GET /summary` gives budgets per occurrence only for an
  aligned range.
- `POST /plaid_accounts/fetch` answers 202 and runs in the background; a call
  within a minute of the last answers 425.
- `GET /recurring_items` reports, per reviewed item, `expected_occurrence_dates`,
  `found_transactions` and `missing_transaction_dates` for the requested range.
- `GET /balance_history` groups monthly entries by source; an entry stands for
  the balance at or near the end of its month; `current` entries are
  ephemeral.
- v2 requests are atomic: a failed request applies nothing. Errors are 4XX
  with `{"message": ..., "errors": [{"errMsg": ...}]}`.
- There is no conditional write: no `If-Match`, `ETag`, `If-Unmodified-Since`
  or 412 anywhere in the 2.11.1 spec, no header parameters at all, and
  `updated_at` in `updateTransactionObject` is `x-updatable: false` (ignored).
  So a write cannot be made conditional on what the drift check read; an edit
  landing between the two is overwritten.
- Redirects are never followed. The API host is fixed; any `3xx` is an error
  naming the status and the `Location` host, so the bearer token is never
  sent elsewhere (urllib would otherwise resend it, even over plain HTTP).
- `POST /transactions` answers 201 with the created `transactions` (with
  ids) and `skipped_duplicates`. The helper matches planned rows to those ids
  one to one on every field it sent, and a row in a Plaid account or another
  manual account never matches.
- Redaction: the token, in any case, and every piece of it 8 or more
  characters long are removed from every error and result before any
  truncation or lowercasing (urllib's `hostname` lowercases a redirect host;
  the client reads at most 2,000 bytes of an error body, which can cut the
  token).
- Split children: the spec says an omitted `payee` takes "the original payee"
  from the parent, an omitted `date`, `category_id` or `notes` takes the
  parent's, and says nothing for `tag_ids`. The helper sends every child
  field explicitly (the parent's current values where the plan leaves them
  unset, `tag_ids: []` for none), so the outcome does not depend on those
  rules and each field can be verified.
- Attachments: `GET /transactions/{id}` always includes `files` (and
  `GET /transactions` does with `include_files=true`); each is
  `{id, uploaded_by, name, type, size, notes, created_at}` by the schema, and
  the spec's own example also carries `source`, so the helper keeps every
  field it is given. The schema says `size` is "in kilobytes", but its example
  shows a `receipt.png` of `size: 4330`, which reads as bytes. The Keeper's
  account has no attachments to settle it (0 in 4,323 transactions,
  2026-11-07), so the helper accepts a download matching `size` in either
  unit: exactly as bytes, or as kilobytes (1000 or 1024 bytes, rounded down
  or up, exactly those four values) only when the reply's `Content-Length`
  matched the body, so the coarser reading never vouches for an unchecked
  body (#137). A `size` of 0 can then be any file under 1 KB, so an empty
  body counts only with `Content-Length: 0`. Embedded split
  children and group members carry `files` too (`childTransactionObject`).
  `GET /transactions/attachments/{file_id}` answers `{url, expires_at}`, a
  signed link. The only documented size limit is on upload,
  `POST /transactions/{transaction_id}/attachments`: "The file must be less
  than 10MB in size"; downloads stop at 10 MiB (10,485,760 bytes), the larger
  reading. The download client sends no `Authorization` header, accepts
  `https` only, follows no redirect, times out after 60 seconds, and accepts
  only HTTP 200 with no `Content-Range` (a 206 or ranged reply is a failed
  save).
- A removed row (an unsplit child, an ungroup's group transaction) is
  confirmed gone by `GET /transactions/{id}` answering 404, one id at a time,
  never by its absence from a listing.
- `GET /summary` over a range that is not an aligned budget period still
  returns each category's activity totals, without budgets; budget burn uses
  that for spending through `--as-of`.
- Rate limit: 100 requests a minute per IP; a 429 carries `Retry-After`.
  Reads retry twice after waiting (at most 60 s); writes never retry.
- The public API has no transaction rules resource in v2 or v1. Rules are
  reachable only through the web app's undocumented backend with a browser
  session cookie, which this skill does not use.
