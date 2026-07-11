---
name: generation
description: "Stable Diffusion prompt engineering and ComfyUI workflow design: prompt structure and weighting, negative prompts, sampler/scheduler selection, workflow JSON construction, iteration on generated outputs. Use when crafting or refining image-generation prompts, building ComfyUI workflows, or the user mentions Stable Diffusion, ComfyUI, LoRA, or image prompts."
---

# Image Generation

Guidance for translating conceptual descriptions into effective Stable Diffusion prompt syntax, designing ComfyUI node graphs for generation tasks (txt2img, img2img, upscaling, inpainting), and iterating based on output feedback. Covers prompt engineering techniques, model/LoRA selection, sampler optimization, and workflow architecture.

Core tasks:

1. **Concept-to-Prompt Translation**: Convert natural language descriptions into SD-effective token sequences
2. **Workflow Design**: Build ComfyUI JSON workflows for txt2img, img2img, upscaling, inpainting
3. **Parameter Optimization**: Tune CFG, samplers, schedulers, steps, and denoise values
4. **LoRA/Model Selection**: Recommend models and LoRAs for specific aesthetics
5. **Negative Prompt Crafting**: Create effective negative prompts to exclude unwanted elements
6. **Iterative Refinement**: Adjust prompts and parameters based on generation feedback

## When to Apply

**Apply when:**

- A concept needs translation to SD prompt syntax
- ComfyUI workflow creation or modification is needed
- Image generation returned unsatisfactory results requiring prompt/parameter iteration
- The question concerns LoRA selection, sampler choices, or CFG tuning
- Batch generation or workflow automation is needed
- img2img refinement workflow construction is required

**Out of scope:**

- Browsing/selecting from existing generated images (file management, not generation)
- ComfyUI installation, configuration, or troubleshooting (devops/system administration)
- Model training or fine-tuning (different domain entirely)
- General image editing unrelated to SD generation (use appropriate image tools)
- Simple file operations on existing prompts (Read/Write directly)

Prompt templates for **other generators** (DALL-E 3, Midjourney, Runway Gen-3, Sora) live in [templates/](templates/) alongside this skill: `dalle.md`, `midjourney.md`, `runway.md`, `sora.md`.

## Reference

### Prompt Engineering Syntax

- **Weighting**: `(token:weight)` where 1.0 is default, 0.5-2.0 typical range
  - Example: `(ethereal:1.3)` increases emphasis, `(blur:0.5)` decreases
- **BREAK Token**: Separates concepts for cleaner composition
  - Example: `portrait of librarian, detailed face BREAK gothic library background, bookshelves`
- **Token Order**: Earlier tokens have stronger influence
- **Specificity**: Concrete tokens work better than abstract concepts
  - Bad: `beautiful woman`
  - Good: `woman with sharp cheekbones, defined jawline, clear skin`

### Sampler/Scheduler Combinations

| Sampler | Scheduler | Use Case | Steps |
|---------|-----------|----------|-------|
| dpmpp_2m_sde | karras | High quality, general use | 25-35 |
| euler_ancestral | normal | Variety, exploration | 20-30 |
| dpmpp_3m_sde | exponential | Fine details | 30-40 |
| euler | normal | Fast iteration | 15-20 |
| ddim | ddim_uniform | Consistency across seeds | 20-30 |

### CFG Scale Guidelines

| CFG | Effect |
|-----|--------|
| 3-5 | Maximum creative freedom, may drift from prompt |
| 5-7 | Balanced creativity and adherence (recommended for art) |
| 7-10 | Strong prompt adherence, less variation |
| 10-15 | Very literal interpretation, can cause artifacts |
| 15+ | Over-saturated, typically avoid |

### SDXL Resolution Standards

| Aspect | Resolution | Use Case |
|--------|------------|----------|
| Square | 1024x1024 | Portraits, centered subjects |
| Portrait | 896x1152 | Full body, standing figures |
| Landscape | 1152x896 | Scenes, environments |
| Cinematic | 1216x832 | Wide shots, panoramas |

### LoRA Stacking Guidelines

- Stack 2-4 LoRAs maximum for clean results
- Reduce individual strengths when stacking: 0.5-0.8 each
- Total combined strength ideally under 2.5
- Order matters: style LoRAs before detail LoRAs
- Test one LoRA at a time before combining

### img2img Refinement

- **Low denoise (0.20-0.35)**: Preserve composition, clean edges/artifacts
- **Medium denoise (0.35-0.50)**: Moderate changes, enhance details
- **High denoise (0.50-0.75)**: Significant changes, may alter subject
- Use add-detail LoRAs at low strength (0.3-0.5) during refinement

## Procedure

### 1. Context Gathering

**Understand the Request**:

- What concept/subject is being generated?
- What aesthetic/style is desired (realistic, anime, painterly, etc.)?
- What aspect ratio/resolution needed?
- Is this txt2img (from scratch) or img2img (refinement)?
- What models/LoRAs are available? Check `Work/sd-prompts/README.md` for project defaults

**Access Existing Resources**:

- Read `Work/sd-prompts/README.md` for project-specific learnings
- Check `Work/sd-prompts/*.json` for reusable workflow templates
- Identify relevant LoRAs from project history

**Checkpoint**: Confirm understanding of:

- [ ] Subject/concept description
- [ ] Target aesthetic
- [ ] Generation type (txt2img/img2img/upscale/inpaint)
- [ ] Available models/LoRAs

### 2. Prompt Construction

**Positive Prompt Assembly**:

1. **Subject first**: Core subject with key attributes
2. **Style/medium**: Artistic style, rendering type
3. **Environment**: Setting, background, lighting
4. **Technical**: Quality boosters (detailed, high resolution, etc.)

**Pattern**:

```
[subject with attributes], [style/medium], [environment/setting], [lighting/mood], [technical quality terms]
```

**Negative Prompt Assembly**:

- Start with universal negatives: `ugly, deformed, blurry, low quality`
- Add concept-specific exclusions: Things that conflict with desired output
- Include style exclusions if targeting specific aesthetic: `anime` for realism, `photorealistic` for illustration

### 3. Workflow JSON Construction

**Node Numbering Convention**:

- `1`: CheckpointLoaderSimple (model loading)
- `2-4`: LoraLoader chain (if using LoRAs)
- Next: CLIPTextEncode (positive prompt)
- Next: CLIPTextEncode (negative prompt)
- Next: EmptyLatentImage (for txt2img) or LoadImage+VAEEncode (for img2img)
- Next: KSampler
- Next: VAEDecode
- Final: SaveImage

**Connection Syntax**:

```json
["node_id", output_index]
```

- CheckpointLoaderSimple outputs: [0]=model, [1]=clip, [2]=vae
- LoraLoader outputs: [0]=model, [1]=clip
- CLIPTextEncode outputs: [0]=conditioning
- KSampler outputs: [0]=latent
- VAEDecode outputs: [0]=image

**Standard txt2img Workflow Structure**:

```json
{
  "prompt": {
    "1": { "class_type": "CheckpointLoaderSimple", "inputs": { "ckpt_name": "model.safetensors" } },
    "2": { "class_type": "CLIPTextEncode", "inputs": { "text": "positive prompt", "clip": ["1", 1] } },
    "3": { "class_type": "CLIPTextEncode", "inputs": { "text": "negative prompt", "clip": ["1", 1] } },
    "4": { "class_type": "EmptyLatentImage", "inputs": { "width": 1024, "height": 1024, "batch_size": 1 } },
    "5": { "class_type": "KSampler", "inputs": {
      "seed": 12345, "steps": 30, "cfg": 6.5,
      "sampler_name": "dpmpp_2m_sde", "scheduler": "karras", "denoise": 1.0,
      "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]
    }},
    "6": { "class_type": "VAEDecode", "inputs": { "samples": ["5", 0], "vae": ["1", 2] } },
    "7": { "class_type": "SaveImage", "inputs": { "filename_prefix": "output", "images": ["6", 0] } }
  }
}
```

### 4. Parameter Selection

**For initial generation**:

- CFG: 6.0-7.0 (balanced)
- Steps: 28-35 (quality without diminishing returns)
- Sampler: dpmpp_2m_sde + karras (reliable quality)
- Denoise: 1.0 (full generation)

**For refinement (img2img)**:

- CFG: Same or slightly lower
- Steps: Same or slightly fewer
- Denoise: 0.25-0.35 (preserve composition)
- Consider add-detail LoRA at 0.3-0.5

### 5. API Submission

Submit workflow to ComfyUI:

```bash
curl -X POST http://127.0.0.1:8188/prompt \
  -H "Content-Type: application/json" \
  -d @/path/to/workflow.json
```

### 6. Delivery

**Output Structure**:

1. **Prompt Set**: Positive and negative prompts with rationale
2. **Workflow JSON**: Complete, submission-ready workflow
3. **Parameter Explanation**: Why specific values were chosen
4. **Iteration Suggestions**: What to adjust if results unsatisfactory

**File Outputs**:

- Save workflows to `Work/sd-prompts/` with descriptive names
- Naming convention: `{subject}-{variant}.json`
- Update README.md if significant new learnings discovered
- Temporary/testing workflows: `/tmp/sd-workflow-{timestamp}.json`

**API Execution** (if requested):

- Write workflow to file
- Submit via curl to ComfyUI API
- Report queue status

## Tools and Data Sources

**Working with project resources**:

- **Read**: Access existing workflows in `Work/sd-prompts/`, project README for learnings
- **Write**: Create new workflow JSON files, update documentation
- **Bash**: Submit workflows to ComfyUI API via curl, check available models/LoRAs
- **WebFetch**: Look up LoRA/model info from CivitAI when needed
- **Grep**: Search existing prompts for reusable elements
- **Glob**: Find workflow files matching patterns

**External endpoints**:

```
ComfyUI API (via Bash/curl)
Endpoint: http://127.0.0.1:8188/prompt
Method: POST
Content-Type: application/json
Body: {"prompt": { ...node definitions... }}
Response: Queue ID on success, error on failure
Example: curl -X POST http://127.0.0.1:8188/prompt -H "Content-Type: application/json" -d @workflow.json

CivitAI (via WebFetch)
Purpose: Research LoRA/model capabilities, find trigger words, check compatibility
Example URLs:
- https://civitai.com/models/{model_id}
- https://civitai.com/api/v1/models/{model_id}
Edge cases: API may require different prompt format than site search
```

**Data sources**:

- `Work/sd-prompts/` - Project-specific workflows and learnings
- `Work/sd-prompts/README.md` - Documented techniques and model inventory
- ComfyUI API at http://127.0.0.1:8188 - Workflow submission endpoint
- CivitAI (via WebFetch) - LoRA/model documentation when needed

**Fallback Strategies:**

- **ComfyUI API unavailable**: Write workflow JSON for manual loading, provide path
- **LoRA not found**: Suggest alternatives from project inventory, or WebFetch from CivitAI
- **Generation fails**: Check workflow JSON syntax, verify node connections, validate model paths
- **User feedback unclear**: Ask for specific aspects to improve (composition, style, details, colors)

Artistic quality of outputs cannot be judged programmatically — leave aesthetic evaluation to the user, and ask for clarification when requirements remain unclear, the ComfyUI connection fails, or a requested model/LoRA is unavailable.

## Output Formats

### Prompt Delivery

```markdown
## Positive Prompt
[prompt text with formatting explanation]

## Negative Prompt
[negative prompt text]

## Rationale
- [Key token choices explained]
- [Weighting decisions]
- [BREAK placement rationale if used]
```

### Workflow Delivery

```markdown
## Workflow: [name]
**Type**: txt2img | img2img | upscale | inpaint
**Model**: [checkpoint name]
**LoRAs**: [list with strengths]

### Parameters
| Parameter | Value | Rationale |
|-----------|-------|-----------|
| CFG | X.X | [why] |
| Steps | XX | [why] |
| Sampler | name | [why] |
| Resolution | WxH | [why] |

### File
Saved to: `Work/sd-prompts/[filename].json`

### Submission
\`\`\`bash
curl -X POST http://127.0.0.1:8188/prompt -H "Content-Type: application/json" -d @Work/sd-prompts/[filename].json
\`\`\`
```

### Iteration Delivery

```markdown
## Iteration: [what changed]

### Adjustments Made
- [Change 1]: [rationale]
- [Change 2]: [rationale]

### If Results Still Unsatisfactory
- Try: [adjustment A]
- Try: [adjustment B]
- Consider: [alternative approach]
```

**Required Elements:**

- Positive prompt (always)
- Negative prompt (always)
- Workflow JSON or path (for generation requests)
- Parameter rationale (for new workflows)
- Iteration suggestions (for refinement requests)

## Quality Standards

**Success Criteria:**

- Prompt syntax is valid SD format (proper weighting, BREAK usage)
- Workflow JSON is valid and submission-ready
- Parameters are justified with rationale
- Iteration suggestions are actionable
- File paths and naming conventions followed

**Validation Checklist:**

- [ ] Positive prompt contains subject, style, and quality terms
- [ ] Negative prompt excludes conflicting elements
- [ ] Workflow JSON has correct node connections
- [ ] KSampler receives model, positive, negative, and latent inputs
- [ ] Resolution matches SDXL standards (multiples of 64, typically 896-1152 range)
- [ ] CFG is within reasonable range (4-12 for most use cases)
- [ ] Denoise appropriate for generation type (1.0 txt2img, 0.2-0.5 img2img)

**Validation Question:**
"Would this workflow execute successfully in ComfyUI and produce results matching the user's concept?"

- If NO: Identify specific issues, fix before delivery
- If YES: Deliver with iteration suggestions

**Failure Modes:**

- **Concept too vague**: Ask for specific visual attributes (what does "ethereal" look like?)
- **Conflicting requirements**: Flag conflict, ask user to prioritize (e.g., "photorealistic anime" is contradictory)
- **Model/LoRA unavailable**: Suggest alternatives from known inventory or recommend CivitAI search
- **Workflow execution fails**: Verify JSON syntax, check node IDs, validate connections
- **User unhappy with results**: Ask what specific aspects to change (composition, colors, style, subject details)

## Worked Examples

Three end-to-end examples (concept-to-prompt translation, img2img refinement workflow, LoRA research and selection) live in [examples.md](examples.md).
