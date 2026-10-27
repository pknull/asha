# Image Plugin

**Version**: 2.2.0

Image and asset generation with two backends, local Stable Diffusion through
ComfyUI workflows and hosted generation through fal.ai, plus the tool layer for
driving Blender from scripts.

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

Blender work (`image-blender`) is separate from generation: it covers running
Blender headless, `bpy` scripting, render setup and devices, colour management,
import and export, and proving a run worked. Modelling and art judgement belong
to a craft skill, and installing Blender to the packages layer.

## Invocation by harness

The plugin has no command or agent. Ask for the task naturally or name the
skill (`image-generation`, `image-fal` or `image-blender`) on Claude, Codex,
Copilot, or OpenCode.

```text
Use image-generation to turn this scene into an SDXL prompt and negative prompt.
Use image-generation to build a ComfyUI txt2img → upscale workflow.
Refine this prompt for the named LoRA without changing the composition.
Use image-fal to price a transparent drone sprite, then wait for my go-ahead.
Use image-fal to turn art/drone-concept.png into a GLB model.
Use image-blender to render scene.blend headless on the GPU and show which device ran.
Use image-blender to export the selected objects of scene.blend to GLB and check the file.
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

### blender (installs as `image-blender`)

The tool layer for Blender 5.2 LTS: headless invocation (`-b`,
`--factory-startup`, `--python-exit-code`), `bpy` script shape, Cycles, EEVEE
and Workbench settings, choosing and proving the Cycles device on any vendor,
colour management, glTF, FBX, OBJ, USD and STL import and export, 4.x to 5.x
API traps, and the checks that prove a run worked. Every snippet ran on 5.2.3.

Skill contents:

- `skills/blender/SKILL.md` — invocation, etiquette, script shape, render, colour, I/O, traps, verification
- `skills/blender/references/` — `invocation.md`, `render.md`, `io.md`, `version-traps.md`
- `skills/blender/scripts/cycles_devices.py` — probe Cycles backends and enable one in the running process
- `skills/blender/scripts/smoke.py` — render, export and re-import a tiny scene to prove an install

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
for finished-quality assets, 3D models, rigs, audio or video. `image-blender`
triggers on Blender, bpy, headless renders, .blend files, Cycles devices and
glTF or FBX export.

```text
Design a prompt for: ethereal forest scene with bioluminescent mushrooms
Create a ComfyUI workflow for: txt2img with upscaling
```

Supply the target model family, checkpoint, LoRAs, output dimensions, and
available ComfyUI nodes when they matter. If omitted, the skill states its
assumptions rather than inventing a locally installed model or node.

## Version History

- **2.2.0**: Added the `blender` skill (`image-blender`), the tool layer for scripting Blender 5.2 LTS headless
- **2.1.0**: Added the `fal` skill (`image-fal`) for paid hosted fal.ai generation, with the local-first routing rule and a per-job price gate
- **2.0.0**: Converted `image-engineer` agent to `generation` skill (`image-generation`); moved generator templates into the skill directory
- **1.1.0**: Agent-based release
