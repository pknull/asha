# Book Export Skill

Export fiction manuscripts to professional book formats with research-informed profiles.

## Quick Usage

### From Claude Code

```
Use the book-export skill to export [file] as [profile]
```

**Examples**:
```
Use the book-export skill to export Callum_Ch01.md as manuscript-draft
Use the book-export skill to create beta-reader-pdf from Example_Novel.md
Use the book-export skill to generate publication-ebook for manuscript.md
```

## Profiles

| Profile | Output | Use Case |
|---------|--------|----------|
| **manuscript-draft** | PDF (A4) | Internal review, quick iteration |
| **beta-reader-pdf** | PDF (6×9") | Beta reader distribution (comfortable reading) |
| **beta-reader-epub** | ePub3 | Digital beta readers (e-readers/tablets) |
| **publication-print** | PDF (6×9") | KDP/IngramSpark print-ready |
| **publication-ebook** | ePub3 | Amazon/Apple/Kobo distribution |

## What It Does

- Wraps Pandoc MCP with professional publishing standards
- Research-informed formatting (margins, typography, spacing)
- Fiction-specific conventions (chapter breaks, scene markers)
- 75% context reduction (5-8k → 1-2k tokens)

## Requirements

**PDF Export**:
- LaTeX distribution (TeX Live, MiKTeX)
- Lora font installed (or alternative serif font)
- XeLaTeX engine (`xelatex --version`)

**ePub Export**:
- Pandoc MCP (already available)
- Optional: EPUBCheck for validation
- Optional: Lora font files for embedding

## Files

- `SKILL.md` - Complete documentation with all profiles, parameters, standards
- `README.md` - This quick reference

## See Also

Full documentation in `SKILL.md`
