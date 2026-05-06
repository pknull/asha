---
name: todoist
description: Direct Todoist REST API access for task create/find/update/complete. Bypasses MCP server fragility. Use when user mentions Todoist, asks to create/check/complete a todo, or needs full read+write Todoist coverage.
triggers:
  - Todoist task creation
  - "add a todo" / "remind me to" / "track this"
  - Querying tasks by date or content
  - Completing or updating tasks
  - When local todoist-ai MCP is down or task-manager agent only exposes read tools
---

# Todoist via REST API

The `task-manager` agent provides read-only Todoist access through the local MCP server (`mcp__todoist-ai__get-overview`, `find-tasks`, `find-tasks-by-date`, `user-info`). When you need write operations (create, update, complete, delete) — or when the MCP server is down — call the REST API directly.

## Setup

User must export their Todoist API token before invoking any operation:

```bash
export TODOIST_API_TOKEN=<their-token>
```

Tokens come from Todoist Settings → Integrations → Developer.

If the env var is missing, halt and tell the user to set it. Do **not** prompt them to paste the token into chat — that puts it in conversation history.

## API Surface

**Base URL**: `https://api.todoist.com/api/v1`

The legacy `/rest/v2/*` endpoints return **HTTP 410 deprecated** as of 2026-05. Always use `/api/v1/`.

**Auth header**: `Authorization: Bearer $TODOIST_API_TOKEN`

**Content type**: `application/json` for POST/PATCH bodies.

## Priority is inverted vs the UI

The REST API and the Todoist UI use opposite priority scales. Always translate before calling:

| User intent | API value | UI label |
|-------------|-----------|----------|
| Highest    | `4`       | p1 (red)    |
| High       | `3`       | p2 (orange) |
| Medium     | `2`       | p3 (blue)   |
| Default    | `1`       | p4 (none)   |

If the user says "high priority", that's API `3`. If they say "p1", that's API `4`.

## Recipes

### Create a task

```bash
curl -sS -X POST "https://api.todoist.com/api/v1/tasks" \
  -H "Authorization: Bearer $TODOIST_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Task title here",
    "due_date": "YYYY-MM-DD",
    "priority": 3
  }'
```

Optional fields: `description`, `project_id`, `labels` (array of strings), `due_string` (natural language like `"next monday"` — Todoist parses it), `parent_id` (for subtasks).

Response includes the new task's `id` — capture it if the user might want to reference the task later in the same session.

### Find tasks by content

```bash
curl -sS "https://api.todoist.com/api/v1/tasks?filter=search:%20ABMX" \
  -H "Authorization: Bearer $TODOIST_API_TOKEN"
```

The filter string is URL-encoded. Common filters: `search: <text>`, `today`, `overdue`, `p1`, `@labelname`, `#projectname`. Combine with `&` (logical AND) or `|` (OR).

### Find tasks by date

```bash
curl -sS "https://api.todoist.com/api/v1/tasks?filter=due:%202026-05-08" \
  -H "Authorization: Bearer $TODOIST_API_TOKEN"
```

For a date range, prefer the read-only MCP agent's `find-tasks-by-date` which has clean range parameters.

### Update a task

```bash
curl -sS -X POST "https://api.todoist.com/api/v1/tasks/$TASK_ID" \
  -H "Authorization: Bearer $TODOIST_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"priority": 4, "due_date": "2026-05-09"}'
```

Send only the fields you want to change.

### Complete a task

```bash
curl -sS -X POST "https://api.todoist.com/api/v1/tasks/$TASK_ID/close" \
  -H "Authorization: Bearer $TODOIST_API_TOKEN"
```

Returns 204 No Content on success. To uncomplete: `/reopen` instead of `/close`.

### Delete a task

```bash
curl -sS -X DELETE "https://api.todoist.com/api/v1/tasks/$TASK_ID" \
  -H "Authorization: Bearer $TODOIST_API_TOKEN"
```

Returns 204. Destructive — confirm with user before invoking unless they explicitly asked to delete.

## Error handling

- **401 Unauthorized** — token missing or expired. Tell user to refresh the env var.
- **403 Forbidden** — token valid but lacks scope for the operation (rare; usually means a paid-tier feature).
- **404 Not Found** — task ID doesn't exist or was already deleted.
- **410 Gone** — using the deprecated `/rest/v2/` path. Switch to `/api/v1/`.
- **429 Too Many Requests** — rate limited. Back off and retry; Todoist's limit is generous (~450 req/15min) so this is rarely hit.

Always check exit status of `curl` and parse the response. A successful POST returns the created object as JSON; if the body isn't JSON, surface the raw response to the user.

## When NOT to use this skill

- **Read-only operations**: prefer the `task-manager` agent or its underlying `mcp__todoist-ai__*` tools when available — they're already wired up and handle pagination/parsing.
- **Bulk operations**: more than ~10 tasks at once, use the Todoist Sync API (`/sync/v9/sync`) instead of looping `/api/v1/tasks` calls. Different shape — see Todoist's developer docs.
- **Real-time updates**: this skill is fire-and-forget. For watching changes, the Sync API supports webhooks.

## Verifying after creation

After creating a task, optionally verify by calling `find-tasks` (read-only MCP) for the same content string. This catches silent failures where the API returned 200 but the task didn't actually persist (very rare, but worth the courtesy when the user is acting on the create).
