# Midjourney Prompt Template

## Format Rules

- Comma-separated descriptors, NOT prose
- Subject first, then style, mood, lighting, composition
- Parameters at end: `--ar 16:9 --v 6 --style raw`
- Negative prompts via `--no [elements]`

## Structure

```
[subject], [action/pose], [setting], [style], [mood], [lighting], [color palette], [composition], [quality] --ar [ratio] --v 6 --style [style] --no [unwanted]
```

## Parameters

| Param | Values | Notes |
|-------|--------|-------|
| `--ar` | 16:9, 1:1, 9:16, 4:3 | Aspect ratio |
| `--v` | 6, 5.2 | Version (6 is current) |
| `--style` | raw, cute, scenic | Style preset |
| `--no` | blur, watermark, text | Negative elements |
| `--q` | .25, .5, 1, 2 | Quality (higher = slower) |
| `--s` | 0-1000 | Stylization (higher = more artistic) |

## Example

```
massive troll warrior, weathered green skin, bone armor, riding giant dire wolf, murky swamp, twisted dead trees, dark fantasy art, muted earth tones, cinematic lighting, dramatic low angle --ar 16:9 --v 6 --style raw --no blur, watermark, extra limbs
```

## Common Mistakes

- Writing prose sentences (use commas)
- Forgetting `--no` for negative elements
- Not specifying aspect ratio
- Vague style terms ("cool", "epic")
