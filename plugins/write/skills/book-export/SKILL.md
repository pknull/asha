---
name: book-export
description: Export fiction manuscripts to professional book formats (PDF, ePub) with research-informed styling profiles. Includes profiles for manuscript drafts, beta readers, and publication-ready output.
license: MIT
---

# Book Export Skill

Export fiction manuscripts to professional book formats (PDF, ePub) with research-informed styling profiles.

## What This Does

Wraps the Pandoc MCP (`mcp__mcp-pandoc__convert-contents`) with professional publishing standards for fiction books:

- Research-informed formatting profiles (manuscript draft → publication-ready)
- Automatic profile selection based on use case
- Fiction-specific conventions (chapter breaks, scene markers, front matter)
- Professional typography and spacing standards
- Multi-format export (PDF for print/review, ePub for digital distribution)

## Usage

```
Use the book-export skill to export [file] as [profile]
Use the book-export skill to create manuscript draft from chapter
Use the book-export skill to generate publication-ready PDF
Use the book-export skill for beta reader ePub
```

## What It Wraps

- **Pandoc MCP** - `mcp__mcp-pandoc__convert-contents`
- **Professional Standards** - Fiction publishing conventions (BISAC, margins, typography)
- **Profile System** - Pre-configured export profiles for common use cases
- **Format Detection** - Automatic output format from profile selection

## Profiles

### 1. manuscript-draft

**Use Case**: Internal review, quick iteration
**Output**: PDF
**Specifications**:

- Page size: A4 (21 × 29.7 cm)
- Font: Lora 12pt
- Spacing: Double-spaced (2.0)
- Margins: 1" all sides
- Headers: Author name, page number (top right)
- Chapter breaks: New page, centered

**Pandoc Parameters**:

```yaml
pdf-engine: xelatex
geometry:
  - a4paper
  - margin=1in
fontsize: 12pt
linestretch: 2
mainfont: "Lora"
```

### 2. beta-reader-pdf

**Use Case**: Beta reader distribution, comfortable reading
**Output**: PDF
**Specifications**:

- Page size: 6" × 9" (15.24 × 22.86 cm) - professional fiction standard
- Font: Lora 11pt
- Spacing: 1.5 line spacing
- Margins: 0.75" all sides (simplified, no binding consideration)
- No headers/footers: Clean reading experience
- Chapter breaks: New page, centered

**Pandoc Parameters**:

```yaml
pdf-engine: xelatex
geometry:
  - paperwidth=6in
  - paperheight=9in
  - margin=0.75in
fontsize: 11pt
linestretch: 1.5
mainfont: "Lora"
```

### 3. beta-reader-epub

**Use Case**: Digital beta reader distribution
**Output**: ePub3
**Specifications**:

- Fonts: Lora embedded
- CSS: Minimal styling, 1.5em first-line indent, 1.5 line-height
- Cover: Optional
- Table of contents: Linked navigation
- Scene breaks: Centered `***`

**Pandoc Parameters**:

```yaml
css: beta-reader.css
epub-cover-image: cover.jpg (if provided)
epub-embed-font:
  - Lora-Regular.ttf
  - Lora-Bold.ttf
  - Lora-Italic.ttf
  - Lora-BoldItalic.ttf
toc: true
```

**CSS Specifications** (beta-reader.css):

```css
body {
  font-family: "Lora", Georgia, serif;
  font-size: 1em;
  line-height: 1.5;
  text-align: left;
}

p {
  text-indent: 1.5em;
  margin: 0;
}

.no-indent {
  text-indent: 0;
}

h1 {
  font-size: 1.5em;
  text-align: center;
  margin-top: 2em;
  margin-bottom: 1em;
  page-break-before: always;
}

.scene-break {
  text-align: center;
  margin: 1em 0;
}
```

### 4. publication-print

**Use Case**: KDP/IngramSpark print-ready
**Output**: PDF (press-ready)
**Specifications**:

- Page size: 6" × 9"
- Font: Lora 11pt
- Spacing: 1.5 line spacing
- Margins (300-500 page book):
  - Inner (gutter): 0.625"
  - Outer: 0.5"
  - Top/Bottom: 0.75"
- Front matter: Copyright, dedication (lowercase Roman numerals)
- Headers: Chapter title (left), book title (right), page numbers
- Chapter breaks: New page, centered 1/3 down page

**Pandoc Parameters**:

```yaml
pdf-engine: xelatex
template: print-template.latex
geometry:
  - paperwidth=6in
  - paperheight=9in
  - inner=0.625in
  - outer=0.5in
  - top=0.75in
  - bottom=0.75in
fontsize: 11pt
linestretch: 1.5
mainfont: "Lora"
toc: true
```

**Requirements**:

- Metadata file (metadata.yaml) with copyright, ISBN, BISAC codes
- Custom LaTeX template for headers/footers
- Front matter structure (copyright, dedication, etc.)

### 5. publication-ebook

**Use Case**: Amazon/Apple/Kobo distribution
**Output**: ePub3
**Specifications**:

- Fonts: System defaults (Georgia fallback) - respects reader preferences
- CSS: Minimal, relative units only
- Line height: 1.5 (1.2 minimum for Kindle compatibility)
- Cover: 2,560 × 1,600 px REQUIRED
- Metadata: Full (ISBN, BISAC codes, description, keywords)
- Validation: EPUBCheck required post-export

**Pandoc Parameters**:

```yaml
epub-cover-image: cover-2560x1600.jpg
css: publication.css
toc: true
toc-depth: 1
```

**CSS Specifications** (publication.css):

```css
/* Minimal styling - respect reader preferences */
body {
  font-family: Georgia, serif;
  font-size: 1em;
  line-height: 1.5;
  text-align: left;
}

p {
  text-indent: 1.5em;
  margin: 0;
}

.no-indent {
  text-indent: 0;
}

h1 {
  font-size: 1.5em;
  text-align: center;
  margin-top: 2em;
  margin-bottom: 1em;
}

.scene-break {
  text-align: center;
  margin: 1em 0;
}

/* Kindle compatibility */
p {
  line-height: 1.2; /* Minimum for Kindle */
}

/* Hyphenation control */
.no-hyphen {
  adobe-hyphenate: none;
  -webkit-hyphens: none;
  -moz-hyphens: none;
  -ms-hyphens: none;
  -epub-hyphens: none;
  hyphens: none;
}
```

**Post-Processing Required**:

```bash
# Validate ePub
java -jar epubcheck.jar output.epub

# Convert to MOBI (if needed)
ebook-convert output.epub output.mobi
```

## When to Use

- **manuscript-draft**: Quick exports for internal review, iteration speed priority
- **beta-reader-pdf**: Comfortable PDF reading for beta readers (no print intent)
- **beta-reader-epub**: Digital reading on e-readers/tablets for beta readers
- **publication-print**: Final print-ready PDF for KDP, IngramSpark, or print-on-demand
- **publication-ebook**: Final ePub for Amazon Kindle, Apple Books, Kobo stores

## Context Savings

**Before**: ~5-8k tokens (Pandoc parameters, format handling, professional standards research)
**After**: ~1-2k tokens (profile selection + file path)
**Savings**: ~75% context reduction

## Parameters

### Required

- `input_file` - Source markdown file path
- `profile` - Export profile name (see Profiles section)

### Optional

- `output_file` - Custom output filename (default: derived from input + profile)
- `metadata_file` - YAML metadata file for publication profiles
- `cover_image` - Cover image path for ePub exports
- `font_files` - Array of font file paths for ePub embedding

## Markdown Structure Requirements

**For optimal conversion**, structure markdown as:

```markdown
---
title: "Book Title"
author: "Author Name"
---

# Book Title

## Front Matter Section

### Copyright

Copyright © 2025 Author Name. All rights reserved.

### Dedication

For [person/group]

## Part I

### Chapter 1: The Opening

First paragraph (no indent).

Second paragraph (1.5em indent).

***

Scene break marker.

First paragraph after break (no indent).

### Chapter 2: Continuation

...
```

**Heading Hierarchy**:

- `#` - Book title (appears once at start)
- `##` - Part/section divisions
- `###` - Chapter titles (triggers page breaks in PDF)

**Scene Breaks**:

- Use centered `***` or `#` for scene breaks
- Markdown: `***` on its own line, or wrap in div: `<div class="scene-break">***</div>`

**First Paragraphs**:

- After chapter breaks: No indent (automatic in CSS)
- After scene breaks: No indent (use `.no-indent` class if needed)

## Font Installation

**Lora Font** (required for PDF profiles):

**macOS**:

```bash
brew tap homebrew/cask-fonts
brew install font-lora
```

**Ubuntu/Debian**:

```bash
sudo apt-get install fonts-lora
```

**Manual Installation**:

1. Download Lora from Google Fonts: https://fonts.google.com/specimen/Lora
2. Install TTF files to system fonts directory
3. Verify: `fc-list | grep Lora`

## Advanced Usage

### With Metadata File (publication profiles)

Create `metadata.yaml`:

```yaml
---
title: "Example Novel"
subtitle: "A Subtitle"
author: "Author Name"
date: "2025-01-01"
description: |
  Book description for back cover and online stores.
  Can span multiple lines.
keywords: [cosmic horror, dark fantasy, transformation]
subject: "Fiction"
rights: "© 2025 Author Name. All rights reserved."
publisher: "Publisher Name"
language: "en-US"

# Publication metadata
identifier:
  - scheme: ISBN-13
    text: "978-1-234567-89-0"

# BISAC codes (up to 3)
category:
  - "FIC015040 - FICTION / Horror / Occult & Supernatural"
  - "FIC009120 - FICTION / Fantasy / Dark Fantasy"
  - "FIC015050 - FICTION / Horror / Psychological"
---
```

**Usage**:

```
Use book-export skill with metadata.yaml to create publication-print from Example_Novel.md
```

### Custom Output Location

```
Use book-export skill to export chapter.md as beta-reader-pdf to /path/to/output/review-copy.pdf
```

### Multiple Chapters

For multi-file books, concatenate before export:

```bash
cat chapters/*.md > complete-manuscript.md
# Then use skill on complete-manuscript.md
```

### Custom Font Embedding (pypandoc script)

For custom/Unicode font embedding without the Pandoc MCP — or to generate PDF and ePub in a single run — use the bundled pypandoc script (absorbed from the retired book-maker skill):

```bash
# Generates {basename}.pdf AND {basename}.epub in the current directory
python3 scripts/book_maker.py input.md
python3 scripts/book_maker.py input.md "My Book"   # custom output basename
```

**How it embeds fonts**: the script scans its `scripts/fonts/` directory for `.ttf`/`.otf` files matching the requested font name (default: Quivira). For PDF it uses XeLaTeX with `scripts/latex_styles.tex`; for ePub it passes each matched font file via `--epub-embed-font` and applies `scripts/epub_styles.css`. Drop additional font files into `scripts/fonts/` to make them embeddable.

**Bundled resources**:

| File | Purpose |
|------|---------|
| `scripts/book_maker.py` | Conversion script (pypandoc, PDF+ePub in one run) |
| `scripts/latex_styles.tex` | PDF styling (XeLaTeX include-in-header) |
| `scripts/epub_styles.css` | ePub styling |
| `scripts/fonts/Quivira.otf` | Unicode-complete font (default) |
| `scripts/fonts/GoudyBookletter1911.otf` | Classic book font |
| `scripts/requirements.txt` | Python dependencies (pypandoc) |

**Requirements**: Python 3.x, `pip install pypandoc`, XeLaTeX (`sudo apt install texlive-xetex`).

**When to prefer it over the profiles**: you need both formats from one command, you need specific Unicode coverage (Quivira), or the Pandoc MCP is unavailable. For professional publishing profiles (KDP, IngramSpark), use the profiles above.

## Output

Returns:

- Conversion success/failure status
- Output file location (absolute path)
- File size of generated document
- Profile used
- Any conversion warnings or errors
- Validation results (for publication-ebook profile)

## Professional Standards Summary

**Based on research findings**:

**Print Standards**:

- 6" × 9" is industry standard for fiction novels
- Gutter margins scale with page count (0.375"-0.875")
- Chapter breaks MUST start new page
- First paragraphs after breaks: No indent
- Scene breaks: Centered ornament or `***`

**ePub Standards**:

- Respect reader preferences (no forced fonts for publication)
- Relative units only (em, rem, %)
- Line-height ≥1.2 for Kindle compatibility
- Cover image: 2,560 × 1,600 px minimum
- EPUBCheck validation required

**Metadata Standards**:

- ISBN-13 format (13 digits with hyphens)
- BISAC codes: Up to 3, most specific categories
- Copyright page: © symbol, year, rights statement, fiction disclaimer
- Edition statement: "First Edition" even if implied

## Troubleshooting

**"Font not found" error**:

- Install Lora font (see Font Installation section)
- Or edit profile to use different font: `-V mainfont="Times New Roman"`

**"PDF engine failed"**:

- Ensure LaTeX installed (TeX Live, MiKTeX)
- Verify `xelatex` command available: `xelatex --version`

**ePub validation failures**:

- Run EPUBCheck: `java -jar epubcheck.jar output.epub`
- Common issues: Missing cover, broken internal links, invalid metadata

**Chapter breaks not working**:

- Verify markdown uses `###` for chapter headings
- Check heading hierarchy (# → ## → ### progression)

## Related Tools

- **Pandoc MCP**: `mcp__mcp-pandoc__convert-contents`
- **LanguageTool**: Grammar/style checking before export
- **EPUBCheck**: ePub validation (download from https://github.com/w3c/epubcheck)
- **Calibre**: Multi-format ebook management and conversion

## References

- Research findings: 2025-11-05 professional book formatting standards
- User requirements: A4, Lora font, chapter page breaks (from reference conversations)
- BISAC codes: https://bisg.org/page/BISACEdition
- Pandoc documentation: https://pandoc.org/MANUAL.html
