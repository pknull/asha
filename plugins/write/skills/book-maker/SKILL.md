---
name: book-maker
description: Python-based markdown to PDF/EPUB converter with custom font embedding. Generates both formats in a single run using pypandoc.
license: MIT
---

# Book Maker Skill

Python-based markdown to PDF/EPUB converter with custom font embedding. Generates both formats in a single run.

## What This Does

Standalone Python script using pypandoc for document conversion with:
- Custom font embedding (Quivira, GoudyBookletter1911)
- Unicode character support (Symbola-compatible)
- Static LaTeX and CSS styling
- Simultaneous PDF and EPUB generation

## Usage

```bash
# Basic usage
python book_maker.py input.md

# With custom output name
python book_maker.py input.md "My Book"
```

## When to Use

Use this skill when:
- You need both PDF and EPUB from one command
- You require specific Unicode font support (Quivira)
- You want a simple, standalone conversion without profiles
- The `book-export` skill's Pandoc MCP is unavailable

For professional publishing profiles (KDP, IngramSpark), use the `book-export` skill instead.

## Included Files

| File | Purpose |
|------|---------|
| `book_maker.py` | Main conversion script |
| `latex_styles.tex` | PDF styling (XeLaTeX) |
| `epub_styles.css` | EPUB styling |
| `fonts/Quivira.otf` | Unicode-complete font |
| `fonts/GoudyBookletter1911.otf` | Classic book font |
| `requirements.txt` | Python dependencies (pypandoc) |

## Requirements

- Python 3.x
- pypandoc (`pip install pypandoc`)
- XeLaTeX (`sudo apt install texlive-xetex`)

## Output

Generates in the current directory:
- `{basename}.pdf` - PDF with embedded fonts
- `{basename}.epub` - EPUB with embedded fonts

## Comparison with book-export

| Feature | book-maker | book-export |
|---------|------------|-------------|
| Profiles | None (fixed) | 5 professional |
| Output | Both PDF+EPUB | One per profile |
| Fonts | Local embedded | System/embedded |
| Use case | Quick conversion | Publishing-ready |
