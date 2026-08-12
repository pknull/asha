# Image Plugin

**Version**: 2.0.0

AI image generation workflows for Stable Diffusion and ComfyUI.

## When to use it

Use this plugin when the output is a Stable Diffusion prompt, parameter set, or
ComfyUI workflow. It does not itself provide a hosted image generator. For a
direct bitmap-generation tool supplied by a harness, use that tool instead.

## Invocation by harness

The plugin has no command or agent. Ask for the task naturally or name the
`image-generation` skill on Claude, Codex, Copilot, or OpenCode.

```text
Use image-generation to turn this scene into an SDXL prompt and negative prompt.
Use image-generation to build a ComfyUI txt2img → upscale workflow.
Refine this prompt for the named LoRA without changing the composition.
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

## Installation

```bash
./install.sh --only image --target claude
./install.sh --only image --target codex
./install.sh --only image --target copilot
```

## Usage

The skill triggers when you describe concepts needing translation to SD prompts, request ComfyUI workflow creation, or mention Stable Diffusion, ComfyUI, LoRA, or image prompts.

```text
Design a prompt for: ethereal forest scene with bioluminescent mushrooms
Create a ComfyUI workflow for: txt2img with upscaling
```

Supply the target model family, checkpoint, LoRAs, output dimensions, and
available ComfyUI nodes when they matter. If omitted, the skill states its
assumptions rather than inventing a locally installed model or node.

## Version History

- **2.0.0**: Converted `image-engineer` agent to `generation` skill (`image-generation`); moved generator templates into the skill directory
- **1.1.0**: Agent-based release
