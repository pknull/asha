---
name: wolfram
description: Wolfram|Alpha computational answers via the LLM API. Use for math solving, unit/currency conversion, scientific & factual lookups, step-by-step results. Bypasses MCP — direct REST.
triggers:
  - solve / compute / evaluate a math expression
  - convert X to Y (units, currency)
  - factual/scientific lookup (physical constants, chemistry, astronomy, geography)
  - what is the derivative/integral of ...
  - plot ...
  - how far is ...
  - When a Wolfram|Alpha MCP would otherwise be reached for
---

# Wolfram|Alpha via LLM API (REST)

Single-purpose skill: send a natural-language query to Wolfram|Alpha, get back a
plain-text, LLM-formatted answer (interpretation + results + a link to the full
website result). Replaces any Wolfram MCP server with a direct `curl` — no daemon,
no Wolfram Language runtime. This is the **Wolfram|Alpha query service**, not the
full Wolfram Language engine (that needs the Engine + `RickHennigan/MCPServer`
paclet).

## Setup

The `asha-claude` / `asha-codex` wrappers source `~/.asha/secrets.env` and export
`WOLFRAM_APP_ID` into the session. If the variable is unset:

> `WOLFRAM_APP_ID not set. Add it to ~/.asha/secrets.env (see ~/Code/asha/secrets.example) and relaunch via asha-claude.`

Get an App ID (free non-commercial tier, ~2000 calls/month at time of writing) from
[developer.wolframalpha.com/portal/myapps](https://developer.wolframalpha.com/portal/myapps/).
Create an app and select **LLM API** access; copy the App ID (looks like
`ABCDEF-GHIJKLMNOP`).

## API surface

**Endpoint**: `https://www.wolframalpha.com/api/v1/llm-api`

**Auth**: query parameter `?appid=$WOLFRAM_APP_ID` (the API also accepts
`Authorization: Bearer <AppID>`; query param is simpler for one-off curls).

**Required**: `input` — the query string (URL-encode it).

**Useful optional params**: `maxchars` (response cap, default 6800), `units`
(`metric` / `nonmetric`), `currency`, `location` / `latlong` (for "near me"
queries), `timezone`.

**Response**: `text/plain`, not JSON — already shaped for an LLM to read. Ends with
a "Wolfram|Alpha website result for query: ..." link for the full interactive page.

## Recipe — ask a question

```bash
QUESTION='integrate x^2 sin(x) dx'

curl -sS --get "https://www.wolframalpha.com/api/v1/llm-api" \
  --data-urlencode "input=$QUESTION" \
  --data-urlencode "appid=$WOLFRAM_APP_ID"
```

`--get` + `--data-urlencode` sends the params as a properly encoded query string,
which prevents shell-quoting bugs when the question contains `^`, `+`, spaces, or
parentheses (Wolfram queries are full of these). This is the Wolfram analog of
gemini's `jq -n --arg` safety.

## Recipe — cap the response length

When you only want a short answer and don't need to spend context on full pods:

```bash
curl -sS --get "https://www.wolframalpha.com/api/v1/llm-api" \
  --data-urlencode "input=$QUESTION" \
  --data-urlencode "maxchars=1200" \
  --data-urlencode "appid=$WOLFRAM_APP_ID"
```

## Recipe — force units / location context

```bash
# Metric output, and answer "near me" style queries with a fixed location
curl -sS --get "https://www.wolframalpha.com/api/v1/llm-api" \
  --data-urlencode "input=distance from Denver to Chicago" \
  --data-urlencode "units=metric" \
  --data-urlencode "appid=$WOLFRAM_APP_ID"
```

## Recipe — surface the HTTP status (for error handling)

The body is plain text on success; on failure the useful signal is the status
code. Capture both:

```bash
resp=$(curl -sS --get "https://www.wolframalpha.com/api/v1/llm-api" \
  --data-urlencode "input=$QUESTION" \
  --data-urlencode "appid=$WOLFRAM_APP_ID" \
  -w $'\n---HTTP %{http_code}' )
echo "$resp"
```

## Error handling

Verified by live test (2026-07-01), which corrected the published doc:

- **401 "Invalid appid"** — token wrong, rotated, or the `appid` param didn't get
  through. Refresh `WOLFRAM_APP_ID` in `~/.asha/secrets.env` and relaunch via
  `asha-claude`. (Wolfram's doc claims 403 here; the live LLM API actually returns
  **401** — observed, not assumed. Treat 401/403 the same: bad/missing key.)
- **400** — missing or malformed `input`. Check the query actually reached the
  `--data-urlencode "input=..."` param.
- **501 "Wolfram|Alpha did not understand your input"** — the query couldn't be
  interpreted. The body often includes **suggested alternative inputs** — read
  them and retry with a rephrase rather than hammering the same string.

## When NOT to use this skill

- **Arbitrary Wolfram Language computation** — custom `LLMTool` functions,
  symbolic pipelines, notebook-grade compute. That needs the Wolfram Engine plus
  the `RickHennigan/MCPServer` paclet (a ~2 GB runtime install), not this API.
- **Fresh news / current events with citations** — use the `admin/gemini` skill
  or built-in `WebSearch`. Wolfram|Alpha is curated computational/factual data,
  not a live web index.
- **Bulk queries** — looping this burns the free-tier monthly call budget fast.
  Batch deliberately and watch the count.

## Cost & quota awareness

The non-commercial App ID free tier is ~2000 calls/month at time of writing;
overage requires a paid Wolfram|Alpha API plan. For personal ad-hoc math/lookup
use this is comfortably within free limits. One `llm-api` request = one call
regardless of `maxchars`.
