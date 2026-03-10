---
name: style-analyzer
description: Quantified prose analysis for voice.md generation. Extracts sentence metrics, dialogue profiles, vocabulary patterns, and forbidden word detection from exemplar texts.
model: sonnet
---

# Style Analyzer

Extracts measurable style patterns from exemplar texts to build objective voice documentation. Transforms subjective "write like X" into quantified constraints.

## When to Use

- Building voice.md from exemplar novels
- Analyzing author style for replication
- Creating benchmarks for manuscript validation
- Comparing draft style against targets

## Usage

### Command Line

```bash
# Analyze single file (markdown output)
python "${CLAUDE_PLUGIN_ROOT}/skills/style-analyzer/scripts/analyze_style.py" source.txt

# Analyze directory of texts
python "${CLAUDE_PLUGIN_ROOT}/skills/style-analyzer/scripts/analyze_style.py" exemplars/

# JSON output for programmatic use
python "${CLAUDE_PLUGIN_ROOT}/skills/style-analyzer/scripts/analyze_style.py" source.txt --json
```

### Agent Integration

The book-analyzer agent uses this script for metric extraction:

```
@task: Analyze Ishiguro's Never Let Me Go
→ style-analyzer produces metrics
→ book-analyzer interprets for voice.md
```

## Metrics Extracted

### Sentence Metrics

| Metric | Description |
|--------|-------------|
| Mean length | Average words per sentence |
| Median length | Middle value (less skewed by outliers) |
| Std deviation | Rhythm variation indicator |
| Short ratio | % sentences < 8 words |
| Long ratio | % sentences > 25 words |

### Dialogue Profile

| Metric | Description |
|--------|-------------|
| Dialogue ratio | % of text in quotation marks |
| Quote style | Double vs single quotes |
| Tag distribution | said/asked/replied frequencies |
| Attribution style | "said Name" vs "Name said" |

### Vocabulary Profile

| Metric | Description |
|--------|-------------|
| Unique word ratio | Vocabulary diversity |
| Rare word ratio | Words appearing only once |
| Adverb density | -ly words per 1000 words |
| AI signal density | Known flat-prose indicators per 1000 |

### Forbidden Patterns

Detects and counts:

- **Filter words**: "he saw", "she heard", "they felt"
- **Hedging**: "seemed to", "appeared to", "somewhat"
- **Clichés**: "heart pounded", "blood ran cold"
- **AI signals**: "delve", "utilize", "palpable", etc.

### Repetition Analysis

- Overused content words (> 1% of text)
- Repeated bigrams and trigrams
- Phrase echoes

## Output Formats

### Markdown (default)

Human-readable report with tables and grep patterns for validation.

### JSON (--json flag)

Machine-readable output for integration with other tools:

```json
{
  "source": "chapter01.txt",
  "word_count": 5432,
  "sentence_metrics": {
    "mean_length": 14.2,
    "std_dev": 8.3,
    "short_ratio": 0.15,
    "long_ratio": 0.08
  },
  "dialogue_metrics": {
    "dialogue_ratio": 0.35,
    "quote_style": {"dominant": "double"}
  },
  "vocabulary_metrics": {
    "adverb_density_per_1000": 12.5,
    "ai_signals": {"density_per_1000": 0.8}
  },
  "forbidden_patterns": {
    "totals": {"filter_words": 3, "hedging": 12, "cliches": 1}
  }
}
```

## Integration Points

| Component | Relationship |
|-----------|--------------|
| book-analyzer agent | Uses this script for metric extraction |
| bible-merger agent | Consumes analysis outputs |
| novel-style-linter | Validates against derived thresholds |
| perplexity-gate | Complements with AI flatness detection |

## AI Signal Words

The script detects 60+ known AI-prose indicators including:

- Hedging: seemingly, apparently, somewhat
- Overused transitions: furthermore, moreover, consequently
- Generic intensifiers: incredibly, fundamentally, essentially
- Flat descriptors: various, numerous, significant
- AI-favored verbs: delve, utilize, leverage, facilitate
- Emotional tells: palpable, visceral, resonated

## Example Analysis

```markdown
# Style Analysis: ishiguro_sample.txt

## Sentence Metrics
| Metric | Value |
|--------|-------|
| Mean length | 16.2 words |
| Std deviation | 9.4 |
| Short sentences (<8 words) | 12.3% |
| Long sentences (>25 words) | 8.7% |

## Dialogue Profile
| Metric | Value |
|--------|-------|
| Dialogue ratio | 28.5% |
| Most common tag | "said" (78%) |
| Attribution style | Name said |

## Forbidden Patterns Found
- Filter words: 2 occurrences
- Hedging: 8 occurrences
- Clichés: 0 occurrences
```

## Requirements

- Python 3.10+
- No external dependencies (uses only stdlib)
