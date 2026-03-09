---
description: "Check prose for AI flatness using local perplexity measurement"
argument-hint: "<file-or-text> [--threshold N] [--json]"
allowed-tools: ["Bash", "Read"]
---

# Perplexity Check

Measure prose "flatness" using local Ollama + Ministral. Detects predictable AI-generated patterns via perplexity scoring.

## Usage

```bash
/write:perplexity path/to/chapter.md
/write:perplexity "The morning light filtered through the curtains..."
/write:perplexity chapter.md --threshold 20
/write:perplexity chapter.md --json
```

## Requirements

- Ollama running (`ollama serve`)
- Ministral model (`ollama pull mistral`)

## Execution

Run the perplexity check script:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/perplexity-gate/scripts/check_perplexity.py" <input> [options]
```

## Thresholds

| Metric | PASS | WARNING | FAIL |
|--------|------|---------|------|
| Sentence PPL < 22 | <10% | 10-20% | >20% |
| Consecutive low-PPL | <3 | 3 | 4+ |
| Overall low-PPL ratio | <20% | 20-30% | >30% |

## Output

```
Perplexity Gate: + PASS

Metrics:
  mean_perplexity: 34.2
  low_ppl_ratio: 0.18
  consecutive_low_max: 2

Recommendation: PROCEED
```

On failure, includes rewrite guidance with flagged sentence positions.

## Integration

- Part of chapter-creation recipe (runs after Draft 1)
- On FAIL: triggers VS-Tail sampling rewrite
- Max 3 rewrite iterations before human escalation

## Options

| Option | Description |
|--------|-------------|
| `--threshold N` | PPL threshold for flagging (default: 22) |
| `--json` | Output JSON instead of human-readable |
| `--sample N` | Check only N random sentences (faster) |
| `--verbose` | Show per-sentence details |
| `--model NAME` | Ollama model (default: mistral) |
