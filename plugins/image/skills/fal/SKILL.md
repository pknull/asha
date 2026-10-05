---
name: image-fal
description: "Paid hosted generation through fal.ai: finished-quality images, transparent sprites, seamless and PBR textures, image-to-3D, rigging, sound effects, music, voice and video. Use only when the user asks for fal, or for quality or media the local ComfyUI route cannot give. Look up the price and get the user's go-ahead before every submission. Placeholder art, alpha art and quick iteration go to image-generation (local ComfyUI) instead."
metadata:
  triggers: "use fal / generate with fal.ai | final or production-quality game art, key art, sprite sheet frames | transparent PNG sprite at finished quality | turn this image into a 3D model / GLB | rig this character | sound effect, music track, voice line, narration | image-to-video clip or trailer shot | what would this cost on fal / how much have we spent on fal"
---

# fal.ai generation

fal.ai is a paid hosted model API. This skill reaches it through plain REST
with a bundled helper, `scripts/fal_api.py` (Python standard library only, no
SDK, no MCP server). The local alternative is the `image-generation` skill,
which builds ComfyUI workflows for the local server.

## Choose the route first

This choice is the core of the skill. Apply it before anything else.

1. **On demand only.** Call fal only when the user asks for fal, or asks for
   quality or media the local route cannot give. Never reach for fal on your
   own because it would be easier.
2. **Local first for cheap work.** Alpha and placeholder art, quick iteration,
   explorations and anything where rough quality is acceptable go to the local
   ComfyUI route (`image-generation`, which submits workflows to
   `http://127.0.0.1:8188/prompt`).
3. **fal for finished quality and for what ComfyUI here cannot do:** 3D
   models, rigging, sound effects, music, voice and video.
4. **Cost is a reason for discipline, not avoidance.** fal is a paid tool like
   the harnesses themselves. When the user wants a finished asset or a medium
   only fal provides, use it with the price gate below; do not talk the user
   out of it.

| Request | Route |
|---|---|
| Placeholder sprite, mock-up, mood board, "rough is fine" | `image-generation` (local) |
| Prompt or workflow tuning, LoRA and sampler questions | `image-generation` (local) |
| Final key art, shippable sprite, finished texture set | fal, after the price gate |
| Image to 3D model, rigging, SFX, music, voice, video | fal, after the price gate |
| The user names fal | fal, after the price gate |

When unsure whether rough quality is acceptable, ask the user rather than
choosing the paid route.

Local-route gap: `image-generation` submits a ComfyUI workflow and reports the
queue status; it has no step that collects the finished images (ComfyUI
`/history` and `/view`) or a transparent-background workflow. When local
output is wanted, retrieve it from the ComfyUI output directory or extend that
workflow. The gap is not a reason to switch to fal.

## Setup

The `asha <harness>` wrapper sources `~/.asha/secrets.env` and exports
`FAL_KEY` into the session. If it is unset, the helper says so; tell the user:

> `FAL_KEY not set. Add it to ~/.asha/secrets.env (see secrets.example in the asha repo) and relaunch through asha.`

Keys come from the fal dashboard (`https://fal.ai/dashboard/keys`). Never ask
the user to paste the key into chat, never print it, never write it to a file,
and never read the secrets file. Do not build requests that carry the key by
hand (curl or otherwise): the helper is the only path that applies the host
rules below. If it lacks something, say so instead of working around it.

Set `SCRIPT` to the installed skill's `scripts/fal_api.py` path. Run commands
from the project root so the default log and output paths land in its `Work/`
directory. Every command prints one JSON document; errors print a JSON error to
stderr and exit non-zero.

## Free reads

These cost nothing. Run them whenever they help:

```bash
python3 "$SCRIPT" search "image to 3d" --category image-to-3d   # current models
python3 "$SCRIPT" schema fal-ai/trellis-2                       # input/output fields
python3 "$SCRIPT" price fal-ai/nano-banana-2 --units 4          # unit price x units
python3 "$SCRIPT" price openai/gpt-image-2 --calls 4            # fal's per-call history
python3 "$SCRIPT" spend                                         # total of the log
```

Model ids change quickly. Confirm an endpoint with `search` and its field
names with `schema` before proposing a job.

## Price gate (every paid submission)

1. Pick the endpoint and inputs, and read its `schema`.
2. Look up the price. `price ENDPOINT --units N` multiplies fal's unit price by
   the billing units the job will use (images, megapixels, seconds, minutes,
   1000 characters). When the unit does not map onto the request (`units`,
   `compute seconds`, `1000 tokens`), use `price ENDPOINT --calls N`, fal's
   historical average cost per call. When both are available, quote the higher.
   When fal has neither a mappable unit nor call history, say the cost is
   uncertain and propose a ceiling.
3. Tell the user, before submitting: the endpoint, what it will produce and how
   many, the estimated cost with its basis, and any local files that will be
   uploaded. Ask for an explicit go-ahead for that job or batch.
4. Submit only after the user says yes. A yes covers the jobs and the total
   that were stated, nothing more. A changed prompt, model, count, resolution
   or duration, a retry after a failure, or a "let's try again" is a new job
   that needs a new estimate and a new yes.
5. Pass the approved amount for that job as `--approved-cost`, with the same
   `--units` or `--calls` basis you quoted. The helper re-prices the job and
   refuses to submit when the estimate exceeds the approved amount.

```bash
python3 "$SCRIPT" run openai/gpt-image-2 --calls 1 --approved-cost 0.25 --name drone \
  --input '{"prompt": "rusty scrap drone, side view, 16-bit pixel art", "background": "transparent", "output_format": "png", "quality": "high"}'

python3 "$SCRIPT" run fal-ai/trellis-2 --calls 1 --approved-cost 0.35 --name drone \
  --file image_url=art/drone-concept.png --input '{"texture_size": 2048}'
```

Put a prompt containing quotes in a JSON file and pass `--input-file FILE`.
`run` waits up to `--timeout` seconds (default 900). A job that outlives the
wait is already paid and logged: fetch it later with
`python3 "$SCRIPT" result REQUEST_ID`; never resubmit it. Never set
`sync_mode` to true: outputs then come back inline instead of as files.

## Uploads: explicit files only

Upload only files the user explicitly names for this job, each with
`--file FIELD=PATH` (repeat the flag for a list field such as `image_urls`).
Never upload a path because it appears in an input, was generated earlier, or
merely exists. The helper sends every `--input` value verbatim: a string that
happens to be a local path stays a string and is never uploaded.

An upload sends the file to fal storage, and fal serves uploads and generated
outputs from URLs that anyone holding the URL can read (the helper downloads
outputs without the key). Say this when proposing an upload, and do not upload
private or confidential material unless the user confirms it.

## Key and host rules (enforced by the helper)

- `FAL_KEY` is read from the environment only (no `.env` files) and sent only
  over https to `queue.fal.run`, `api.fal.ai` and `rest.fal.ai`, the hosts
  fal's documentation names for the queue, platform and storage APIs.
- That check also covers the `status_url` and `response_url` values fal
  returns. A returned URL on any other host is refused, `result` refuses it
  too, and the logged request id lets the user check the job on the fal
  dashboard.
- Requests that carry the key never follow redirects.
- Output downloads (fal CDN) and the presigned upload PUT never carry the key.
- A paid submission is never retried automatically.

## Generation log

Each `run` appends a `submitted` record (endpoint, request id, unit price and
unit, estimate and its basis, approved amount, uploads, input) to
`Work/fal/generations.jsonl` before it waits, and a `completed` record with the
output files afterwards. Outputs go to `Work/fal/outputs/` unless `--out` names
another directory. `spend` totals the estimates by currency; billed amounts
are on the fal dashboard.

Before the first paid run in a project, confirm the log path is ignored by
version control (`git check-ignore -q Work/fal/generations.jsonl`). If it is
not, tell the user and pass `--log` and `--out` to a location they choose.

## Starting endpoints

Checked against fal's catalog, schemas and price list on 2026-10-05. Confirm
with `search`, `schema` and `price` before use; never quote a price from this
table.

| Asset | Endpoint | Key inputs | Billing unit |
|---|---|---|---|
| Finished image | `fal-ai/nano-banana-2` | `prompt`, `aspect_ratio`, `resolution`, `num_images` | images |
| Edit or variant of references | `fal-ai/nano-banana-2/edit` | `prompt`, `image_urls` | images |
| Transparent sprite | `openai/gpt-image-2` | `prompt`, `background: "transparent"`, `output_format: "png"`, `quality` | units (use `--calls`) |
| Cut out a background | `fal-ai/birefnet/v2` | `image_url` | compute seconds (use `--calls`) |
| Image to pixel art | `fal-ai/image2pixel` | `image_url`, `max_colors`, `transparent_background` | compute seconds (use `--calls`) |
| Upscale | `fal-ai/seedvr/upscale/image` | `image_url`, `upscale_factor` | megapixels |
| Seamless texture | `fal-ai/z-image/turbo/tiling` | `prompt`, `tiling_mode`, `image_size` | megapixels |
| PBR material maps | `fal-ai/patina/material` | `prompt`, `maps`, `tiling_mode` | megapixels |
| Image to 3D (GLB) | `fal-ai/trellis-2` | `image_url`, `texture_size`, `decimation_target` | units (use `--calls`) |
| Image to 3D, alternative | `fal-ai/hunyuan-3d/v3.1/pro/image-to-3d` | `input_image_url`, `face_count`, `enable_pbr` | units (use `--calls`) |
| Rig a humanoid | `fal-ai/meshy/rigging` | `model_url` (a GLB), `enable_animation`, `height_meters` | generations |
| Sound effect | `fal-ai/elevenlabs/sound-effects/v2` | `text`, `duration_seconds`, `loop` | seconds |
| Music | `elevenlabs/music/v2.5` | `prompt`, `music_length_ms`, `force_instrumental` | minutes |
| Voice line | `fal-ai/elevenlabs/tts/eleven-v3` | `text`, `voice`, `stability` | 1000 characters |
| Image to video | `bytedance/seedance-2.5/image-to-video` | `image_url`, `prompt`, `duration`, `resolution`, `generate_audio` | 1000 tokens (no call history on 2026-10-05) |

A transparent sprite from a model without a transparent-background option is
two paid jobs: generate on a plain background, then cut it out with
`fal-ai/birefnet/v2`. Price both before asking.

## Reporting

After a run, report the output files, the request id and the estimated cost.
Leave aesthetic judgement to the user. On a failure, report the helper's error
as given; a failed or refused job is not retried without a new go-ahead.
