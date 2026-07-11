# Worked Examples

End-to-end applications of the image-generation skill ([SKILL.md](SKILL.md)): context gathering, execution, delivery.

## Example 1: Concept-to-Prompt Translation

```
Input: "Create a prompt for an ethereal librarian character - mysterious, ancient, vaguely unsettling"

Process:
  1. Context Gathering:
     - Read Work/sd-prompts/README.md for project learnings
     - Note existing "Victorian automaton librarian" prompts as reference
     - Identify available LoRAs from project

  2. Execution:
     - Translate "ethereal" → specific visual tokens: translucent skin, soft glow, undefined edges
     - Translate "ancient" → aged parchment texture, faded colors, antique clothing
     - Translate "unsettling" → uncanny valley elements, too-perfect symmetry, wrong proportions
     - Assemble prompt with priority ordering
     - Craft negative prompt excluding contradictory elements

  3. Delivery:
     - Complete prompt set with rationale
     - Workflow JSON ready for submission
     - Iteration suggestions if results need adjustment

Output:
---
## Positive Prompt
ethereal librarian, (translucent pale skin:1.2), soft ambient glow emanating from body, antique victorian robes with aged patina, (unnaturally perfect symmetry:1.1), ancient manuscript in hands, faded muted colors, gothic library background with towering bookshelves, atmospheric dust motes, diffused lighting, detailed, high quality

## Negative Prompt
vibrant saturated colors, modern clothing, casual pose, warm skin tones, asymmetric features, cartoon, anime, blurry, low quality, deformed hands

## Rationale
- Weighted "translucent pale skin" (1.2) to push ethereal quality
- "Unnaturally perfect symmetry" (1.1) creates subtle unease without obvious distortion
- Placed subject attributes first for strongest influence
- Added "atmospheric dust motes" for age without heavy weathering
- Negative excludes warm/vibrant which would undermine ethereal mood
---
```

## Example 2: img2img Refinement Workflow

```
Input: "The generated image is good but has some rough edges and the hands look weird. Create a refinement workflow."

Process:
  1. Context Gathering:
     - Read asha-refine-template.json for existing refinement pattern
     - Note project uses add-detail-xl LoRA for refinement
     - Identify input image path needed from user

  2. Execution:
     - Build img2img workflow with LoadImage → VAEEncode flow
     - Set denoise low (0.28) to preserve composition
     - Add add-detail-xl at 0.5 strength for edge cleanup
     - Keep same positive/negative prompts (hands should improve with detail LoRA)

  3. Delivery:
     - Complete refinement workflow
     - Parameter explanation
     - Additional iterations if hands still problematic

Output:
---
## Workflow: refinement-cleanup

**Type**: img2img
**Model**: juggernautXL_ragnarok.safetensors
**LoRAs**: add-detail-xl @ 0.5

### Parameters
| Parameter | Value | Rationale |
|-----------|-------|-----------|
| CFG | 6.5 | Same as original, maintains style |
| Steps | 28 | Sufficient for refinement pass |
| Sampler | dpmpp_2m_sde | Consistent with original |
| Denoise | 0.28 | Low - preserves composition, cleans edges |

### Usage
1. Place source image in ComfyUI input folder
2. Update LoadImage node with filename
3. Submit workflow

### If Hands Still Problematic
- Try inpainting workflow targeting hands specifically
- Or increase denoise to 0.35 and add "(detailed hands:1.3)" to positive prompt
- Consider regenerating with different seed if hands fundamentally malformed
---
```

## Example 3: LoRA Research and Selection

```
Input: "I want to generate images in a gothic horror style. What LoRAs would work well with Juggernaut XL?"

Process:
  1. Context Gathering:
     - Check Work/sd-prompts/README.md for existing LoRA inventory
     - Note carcosaCity_XL.safetensors already in project for "eldritch/gothic aesthetic"
     - WebFetch CivitAI if additional options needed

  2. Execution:
     - Compile known compatible LoRAs from project
     - Suggest combinations with strength recommendations
     - Note stacking considerations

  3. Delivery:
     - LoRA recommendations with usage guidance
     - Example prompt incorporating style
     - Workflow template if requested

Output:
---
## Gothic Horror LoRAs for Juggernaut XL

### From Project Inventory
| LoRA | Strength | Effect |
|------|----------|--------|
| carcosaCity_XL | 0.6-0.8 | Eldritch architecture, otherworldly atmosphere |

### Recommended CivitAI Options
(Would need WebFetch to confirm compatibility, but commonly paired with SDXL):
- **Dark Fantasy Style**: Enhances moody lighting, dramatic shadows
- **Victorian Gothic**: Period-appropriate architecture and costume details
- **Horror Elements**: Adds unsettling textures, decay, atmosphere

### Stacking Suggestion
For gothic horror, try:
```

carcosaCity_XL @ 0.7 + add-detail-xl @ 0.4

```
Total strength: 1.1 (safe for stacking)

### Example Prompt Addition
Add to existing prompts: `gothic horror atmosphere, dramatic chiaroscuro lighting, looming shadows, decayed elegance`
---
```
