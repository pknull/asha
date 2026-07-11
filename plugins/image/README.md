# Image Plugin

**Version**: 2.0.0

AI image generation workflows for Stable Diffusion and ComfyUI.

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
./install.sh
```

## Usage

The skill triggers when you describe concepts needing translation to SD prompts, request ComfyUI workflow creation, or mention Stable Diffusion, ComfyUI, LoRA, or image prompts.

```
Design a prompt for: ethereal forest scene with bioluminescent mushrooms
Create a ComfyUI workflow for: txt2img with upscaling
```

## Version History

- **2.0.0**: Converted `image-engineer` agent to `generation` skill (`image-generation`); moved generator templates into the skill directory
- **1.1.0**: Agent-based release
