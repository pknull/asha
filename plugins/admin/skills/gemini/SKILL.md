---
name: gemini
description: Google search with citations via Gemini API grounded-search. Use when user asks for current info, recent events, "what's the latest", news, version checks, or anything that needs fresh web data with sources. Bypasses MCP — direct REST.
triggers:
  - search the web for X / google X / look up X
  - what's the latest / news on X / recent updates to Y
  - Version checks, release info, current pricing
  - Citation-required research questions
  - When the gemini-google-search MCP is unavailable
---

# Gemini Grounded Search via REST API

Single-purpose skill: ask Gemini a question, get back an answer plus the web sources it grounded against. Replaces the `gemini-google-search` MCP server with a direct API call — no daemon to maintain, no port to keep alive.

## Setup

The `asha-claude` / `asha-codex` wrappers source `~/.asha/secrets.env` and export `GEMINI_API_KEY` into the session. If the variable is unset:

> `GEMINI_API_KEY not set. Add it to ~/.asha/secrets.env (see secrets.example in the asha repo) and relaunch via asha-claude.`

Tokens come from [Google AI Studio](https://aistudio.google.com/app/apikey).

## API surface

**Endpoint**: `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`

**Auth**: query parameter `?key=$GEMINI_API_KEY` (the API also accepts an `x-goog-api-key` header; query param is simpler for one-off curls).

**Default model**: `gemini-2.5-flash` — fast, supports grounding, sufficient quality for search/citation tasks. Use `gemini-2.5-pro` for harder reasoning when latency is acceptable.

**Grounding**: include the `google_search` tool in the request to attach live web grounding. Without it, you get the model's training-cutoff knowledge with no citations.

## Recipe — grounded search with citations

```bash
QUESTION='What is the latest stable Postgres release as of today?'

curl -sS -X POST \
  "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg q "$QUESTION" '{
    contents: [{parts: [{text: $q}]}],
    tools: [{google_search: {}}]
  }')" \
  | jq -r '
      .candidates[0].content.parts[0].text,
      "",
      "Sources:",
      (.candidates[0].groundingMetadata.groundingChunks // []
        | to_entries[]
        | "  [\(.key+1)] \(.value.web.title) — \(.value.web.uri)")
    '
```

The `jq -n --arg` form prevents shell-quoting bugs when the question contains backticks or special characters. The output `jq` filter formats answer + sources cleanly so you don't have to ship the full response into your context.

## Recipe — terse answer, no sources

When the user just wants a quick fact without source cards:

```bash
curl -sS -X POST \
  "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg q "$QUESTION" '{
    contents: [{parts: [{text: $q}]}],
    tools: [{google_search: {}}],
    generationConfig: {temperature: 0.2, maxOutputTokens: 256}
  }')" \
  | jq -r '.candidates[0].content.parts[0].text'
```

Lowering temperature for fact retrieval reduces hallucinations. Capping output keeps the response in your context when you only want a sentence.

## Recipe — switch to gemini-2.5-pro for harder questions

```bash
# Replace gemini-2.5-flash with gemini-2.5-pro in the URL.
# Same body shape; ~3-5x slower, better at multi-step reasoning.
"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key=$GEMINI_API_KEY"
```

Use pro when: question requires synthesizing multiple sources, comparing options, or producing structured output. Stick with flash for: lookups, fact checks, news headlines.

## Response shape (for parsing)

```
{
  "candidates": [{
    "content": {"parts": [{"text": "answer text"}], "role": "model"},
    "finishReason": "STOP",
    "groundingMetadata": {
      "groundingChunks": [
        {"web": {"uri": "...", "title": "..."}}
      ],
      "groundingSupports": [...]   // links text spans to chunks
    }
  }]
}
```

`groundingChunks` is the citation list. `groundingSupports` maps specific phrases in the answer to specific chunks — useful for inline footnoting but usually overkill for a chat response.

## Error handling

- **400 `INVALID_ARGUMENT`** — body shape wrong; common cause is malformed `tools` array. Verify with `jq -n` construction.
- **400 `API_KEY_INVALID`** — token bad or rotated. Refresh `GEMINI_API_KEY` in `~/.asha/secrets.env` and relaunch.
- **429 `RESOURCE_EXHAUSTED`** — quota hit. Free tier has rate limits per-minute and per-day. Back off; for repeated use, set up a paid project at [console.cloud.google.com](https://console.cloud.google.com) and rotate the key.
- **`finishReason: "SAFETY"`** — Gemini's safety filters blocked the response. Rephrase or accept the refusal; don't try to bypass.
- **Empty `groundingChunks`** — Gemini chose not to ground (deemed the question general knowledge). The text answer is still valid; just no citations.

## When NOT to use this skill

- **Claude already has WebSearch** — for simple lookups, the built-in `WebSearch` tool is faster and integrates directly. Reach for Gemini when you specifically want grounded citations or a different model's perspective.
- **Multi-turn research with follow-ups** — make successive grounded queries, carrying forward the relevant sources and unresolved question. This skill is single-shot per invocation.
- **Bulk queries** — looping this skill across many questions burns API quota fast. Batch via the SDK or use `responseSchema` for structured extraction in a single call.

## Cost & quota awareness

`gemini-2.5-flash` is free up to 15 RPM / 1M TPM / 1500 req-day at the time of writing. Grounded search counts as a tool call — billed slightly differently from plain generation but still within the free tier for personal use. Pro tier is paid; flash is the safe default for ad-hoc lookups.
