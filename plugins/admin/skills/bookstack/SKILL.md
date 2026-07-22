---
name: bookstack
description: Self-hosted BookStack wiki access via REST API. Use for searching the wiki, reading/creating/updating pages, listing books and chapters, managing attachments and images. Replaces the bookstack MCP. Requires BOOKSTACK_BASE_URL and BOOKSTACK_API_TOKEN.
triggers:
  - look up X in the wiki / search bookstack for X
  - what does the wiki say about Y
  - Creating, updating, or reading a wiki page
  - Listing books, chapters, pages, shelves
  - Uploading attachments or images to a page
  - Page export (HTML, PDF, plain text, markdown)
---

# BookStack via REST API

Self-hosted BookStack wiki at `$BOOKSTACK_BASE_URL`. This skill covers the operations you actually do day-to-day; the full API has ~50 endpoints (see [BookStack API docs](https://demo.bookstackapp.com/api/docs)). Fall back to direct curl with the patterns below for anything not listed here.

## Setup

The `asha-claude` / `asha-codex` wrappers export these env vars from `~/.asha/secrets.env`:

- `BOOKSTACK_BASE_URL` — full base, including `/api` (e.g. `https://wiki.example.com/bookstack/api`)
- `BOOKSTACK_API_TOKEN` — combined `id:secret` from BookStack → Edit Profile → API Tokens

If either is missing, halt and tell the user to set them. Do **not** read `~/.asha/secrets.env` directly.

## API basics

**Auth header**: `Authorization: Token $BOOKSTACK_API_TOKEN` (where the token is already in `id:secret` form).

**Pagination**: most list endpoints accept `count` (default 50, max 500) and `offset`. Use `?count=20` for shallow lists; `?count=500` is the practical cap.

**Filters**: list endpoints support `filter[<field>]=<value>`, e.g. `?filter[name]=foo`. Field names match the response schema.

**Sorting**: `?sort=name` ascending, `?sort=-updated_at` descending.

## Hierarchy primer

```
Shelves (optional grouping) → Books → Chapters (optional) → Pages
                                                          ↓
                                              Attachments / Images
```

Pages can live directly under a Book or inside a Chapter. The `book_id` is mandatory; `chapter_id` is optional.

## Recipe — search across everything

```bash
QUERY='deployment'   # supports BookStack search syntax: tags, [book], [page], etc.
curl -sS \
  "$BOOKSTACK_BASE_URL/search?query=$(printf %s "$QUERY" | jq -sRr @uri)&count=10" \
  -H "Authorization: Token $BOOKSTACK_API_TOKEN" \
  | jq -r '.data[] | "\(.type)\t\(.url)\t\(.name)"'
```

Search syntax cheatsheet: `"exact phrase"`, `tag:value`, `[book]` / `[page]` / `[chapter]` / `[shelf]` to restrict by type, `name:foo` for field-specific match. Combine with implicit AND.

## Recipe — list books

```bash
curl -sS "$BOOKSTACK_BASE_URL/books?count=50&sort=name" \
  -H "Authorization: Token $BOOKSTACK_API_TOKEN" \
  | jq -r '.data[] | "\(.id)\t\(.name)"'
```

## Recipe — read a page (full HTML + markdown)

```bash
PAGE_ID=42
curl -sS "$BOOKSTACK_BASE_URL/pages/$PAGE_ID" \
  -H "Authorization: Token $BOOKSTACK_API_TOKEN" \
  | jq '{name, book_id, chapter_id, html, markdown}'
```

The response includes both `html` (rendered) and `markdown` (source if the page was authored in markdown mode). Prefer `markdown` for round-tripping.

## Recipe — create a page

```bash
BOOK_ID=3
curl -sS -X POST "$BOOKSTACK_BASE_URL/pages" \
  -H "Authorization: Token $BOOKSTACK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
        --arg name 'New page title' \
        --arg md   '# Heading\n\nBody in markdown.' \
        --argjson book "$BOOK_ID" \
        '{book_id: $book, name: $name, markdown: $md}')"
```

Required: `name` plus exactly one of `book_id` or `chapter_id`. Body is exactly one of `markdown` or `html`. Capture `.id` from the response if you need to reference the new page.

## Recipe — update a page

```bash
PAGE_ID=42
curl -sS -X PUT "$BOOKSTACK_BASE_URL/pages/$PAGE_ID" \
  -H "Authorization: Token $BOOKSTACK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg md 'Updated body in markdown.' '{markdown: $md}')"
```

Only send the fields you want to change. To rename a page, send `{name: "..."}`. To move it to a different chapter, send `{chapter_id: <new_id>}`.

## Recipe — list pages in a book

```bash
BOOK_ID=3
curl -sS "$BOOKSTACK_BASE_URL/pages?filter[book_id]=$BOOK_ID&count=500" \
  -H "Authorization: Token $BOOKSTACK_API_TOKEN" \
  | jq -r '.data[] | "\(.id)\t\(.name)"'
```

## Recipe — export a page

```bash
PAGE_ID=42
FORMAT=markdown   # one of: html, pdf, plaintext, markdown
curl -sS "$BOOKSTACK_BASE_URL/pages/$PAGE_ID/export/$FORMAT" \
  -H "Authorization: Token $BOOKSTACK_API_TOKEN" \
  -o "page-$PAGE_ID.$FORMAT"
```

PDF export downloads a binary; redirect to file. The other three return text — pipe to a file or stdout as needed.

## Recipe — upload an attachment to a page

```bash
PAGE_ID=42
FILE=/path/to/file.pdf
curl -sS -X POST "$BOOKSTACK_BASE_URL/attachments" \
  -H "Authorization: Token $BOOKSTACK_API_TOKEN" \
  -F "name=$(basename "$FILE")" \
  -F "uploaded_to=$PAGE_ID" \
  -F "file=@$FILE"
```

For an external URL link instead of a file upload, swap `-F "file=@..."` for `-F "link=https://..."` and drop the file.

## Error handling

- **401 Unauthorized** — token bad. Verify `BOOKSTACK_API_TOKEN` is in `id:secret` format with no quotes or whitespace.
- **403 Forbidden** — token valid, but the user lacks permission for that resource. BookStack's permission model is per-content; check at `Settings → Roles` or via `/api/permissions`.
- **404 Not Found** — wrong endpoint or the resource was deleted. Check `BOOKSTACK_BASE_URL` ends with `/api` (no trailing slash).
- **422 Unprocessable Entity** — request body validation failed. Response has `errors` keyed by field name.
- **429 Too Many Requests** — BookStack default rate limit is 180 req/min. Back off.

## When NOT to use this skill

- **Bulk content migration** — for moving many pages, use BookStack's built-in import/export at the book/chapter level (`/books/{id}/export/{format}`) instead of looping page calls.
- **User/role/permission management** — possible via API but rarely the right path; do it in the BookStack web UI to keep audit trails legible.
- **Real-time collaboration** — BookStack's API is request/response. For live editing, use the web UI.

## Common patterns

**Refresh a page from a markdown source file**: read the file with `Read`, then `PUT /pages/$PAGE_ID` with the markdown body. Round-trips cleanly.

**Find-and-update**: search → grab a page ID from results → read full page → modify markdown → PUT update. Three calls; cleaner than trying to do it in one.

**Cross-page link discovery**: BookStack's search returns hits across all entities. Use `?filter[book_id]=...` on `/pages` to scope when you know the book.

## See also

- BookStack API docs: `$BOOKSTACK_BASE_URL/docs` (the running instance serves them)
- Pattern doc: `docs/secrets.md` in the asha repo (checkout path = `asha_root` in `~/.asha/config.json`)
- Sibling: `plugins/admin/skills/todoist/SKILL.md` in the asha repo (same dotenv-bootstrap pattern)
