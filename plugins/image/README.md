# Image Plugin

**Version**: 2.1.0

Image and asset generation with two backends: local Stable Diffusion through
ComfyUI workflows, and hosted generation through fal.ai.

## When to use it

Use this plugin when the output is a Stable Diffusion prompt, parameter set or
ComfyUI workflow, or a generated asset: an image, a transparent sprite, a
texture, a 3D model or rig, a sound effect, music, a voice line or a video.

The plugin owns the choice between its two backends:

- **Local first.** Placeholder and alpha art, quick iteration and anything
  where rough quality is acceptable use local ComfyUI (`image-generation`).
- **fal on demand.** Hosted fal.ai generation (`image-fal`) is paid per job.
  Use it only when the user asks for fal, or for finished-quality assets and
  media local ComfyUI cannot produce here (3D, rigging, audio, video). Every
  submission needs a price lookup, a stated estimate and the user's go-ahead.

The full routing rule and price gate live in `skills/fal/SKILL.md`. For a
direct bitmap-generation tool supplied by a harness, use that tool instead.

## Invocation by harness

The plugin has no command or agent. Ask for the task naturally or name the
skill (`image-generation` or `image-fal`) on Claude, Codex, Copilot, or
OpenCode.

```text
Use image-generation to turn this scene into an SDXL prompt and negative prompt.
Use image-generation to build a ComfyUI txt2img → upscale workflow.
Refine this prompt for the named LoRA without changing the composition.
Use image-fal to price a transparent drone sprite, then wait for my go-ahead.
Use image-fal to turn art/drone-concept.png into a GLB model.
```

## Skills

### generation (installs as `image-generation`)

Stable Diffusion prompt engineering and ComfyUI workflow design. Use when you need:

- Image generation prompts from concept descriptions
- ComfyUI workflow JSON construction
- LoRA/model selection guidance
- Prompt iteration based on output feedback

Skill contents:

- `skills/generation/SKILL.md` — reference (weighting syntax, sampler/CFG/resolution tables, LoRA stacking) and procedures (prompt construction, workflow JSON, parameters, API submission)
- `skills/generation/examples.md` — worked examples (concept-to-prompt, img2img refinement, LoRA research)
- `skills/generation/templates/` — prompt templates for other generators: `dalle.md`, `midjourney.md`, `runway.md`, `sora.md`

### fal (installs as `image-fal`)

Paid hosted generation through fal.ai's REST API: finished-quality images,
transparent sprites, seamless and PBR textures, image-to-3D, rigging, sound
effects, music, voice and video. Requires `FAL_KEY`, exported from
`~/.asha/secrets.env` by the `asha` wrapper (see `secrets.example`).

- Free reads: model search, endpoint schemas, prices and fal's historical
  per-call estimates, and a spend total of the generation log.
- Paid jobs run through `skills/fal/scripts/fal_api.py` (Python standard
  library, no SDK or MCP server). It re-prices each job and refuses one whose
  estimate exceeds the amount the user approved, sends the key only to fal's
  API hosts (including the status and result URLs fal returns), uploads only
  files named with `--file`, never retries a paid submission, and logs model,
  request id, price and output files to `Work/fal/generations.jsonl`.

Skill contents:

- `skills/fal/SKILL.md` — routing rule, price gate, upload and key rules, generation log, starting endpoints
- `skills/fal/scripts/fal_api.py` — the fal REST helper

## Installation

```bash
./install.sh --only image --target claude
./install.sh --only image --target codex
./install.sh --only image --target copilot
```

## Usage

`image-generation` triggers when you describe concepts needing translation to
SD prompts, request ComfyUI workflow creation, or mention Stable Diffusion,
ComfyUI, LoRA, or image prompts. `image-fal` triggers when you ask for fal, or
for finished-quality assets, 3D models, rigs, audio or video.

```text
Design a prompt for: ethereal forest scene with bioluminescent mushrooms
Create a ComfyUI workflow for: txt2img with upscaling
```

Supply the target model family, checkpoint, LoRAs, output dimensions, and
available ComfyUI nodes when they matter. If omitted, the skill states its
assumptions rather than inventing a locally installed model or node.

## Version History

- **2.1.0**: Added the `fal` skill (`image-fal`) for paid hosted fal.ai generation, with the local-first routing rule and a per-job price gate
- **2.0.0**: Converted `image-engineer` agent to `generation` skill (`image-generation`); moved generator templates into the skill directory
- **1.1.0**: Agent-based release
