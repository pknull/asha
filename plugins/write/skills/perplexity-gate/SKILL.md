---
name: perplexity-gate
description: Quality gate measuring prose flatness via local perplexity computation using Ollama + Ministral 8B. Returns pass/fail verdict with diagnostics for automated workflow gates.
license: MIT
---

# Perplexity Gate Skill

Automated quality checkpoint for prose using local perplexity measurement. Runs Ministral 8B via Ollama to compute token-level perplexity, detecting "flat" AI-generated prose patterns. Acts as a gate in chapter creation workflows.

## Requirements

- **Ollama** installed and running (`ollama serve`)
- **Ministral 8B** model pulled (`ollama pull mistral`)
- Python 3.8+ with `requests` library

## When to Use

- After Draft 1 completion in chapter-creation workflow
- Before committing prose to state snapshots
- When prose feels "flat" and needs objective measurement
- As automated gate in writing recipes

## Perplexity Thresholds

Based on Claude Book research:

| Metric | PASS | WARNING | FAIL |
|--------|------|---------|------|
| Sentence PPL < 22 | <10% of sentences | 10-20% | >20% |
| Low variance window | 0 spans | 1-2 spans | 3+ spans |
| Consecutive low-PPL | <3 sentences | 3 sentences | 4+ sentences |
| Overall low-PPL ratio | <20% | 20-30% | >30% below PPL 25 |

**Key insight**: Low perplexity = predictable tokens = "flat" prose. High perplexity = surprising tokens = textured prose.

## Protocol

### Input

Prose text to evaluate (minimum 250 words for reliable measurement).

### Process

1. Split text into sentences
2. For each sentence, compute perplexity via Ollama HTTP API
3. Analyze distribution: variance, consecutive low-PPL spans, overall ratio
4. Apply threshold logic
5. Return verdict with diagnostics

### Output Format

```yaml
verdict: PASS | WARNING | FAIL
metrics:
  mean_perplexity: 34.2
  median_perplexity: 31.8
  variance: 12.4
  low_ppl_ratio: 0.18          # % sentences with PPL < 25
  consecutive_low_max: 2        # longest run of PPL < 22
  low_variance_windows: 1       # spans with uniform PPL
flagged_sentences:
  - sentence: "She felt a sense of profound realization wash over her."
    perplexity: 18.3
    position: 7
  - sentence: "The morning light filtered softly through the curtains."
    perplexity: 16.9
    position: 12
recommendation: PROCEED | REVISE_WITH_VS_TAIL | ESCALATE_TO_HUMAN
rewrite_guidance: |
  [Only present if verdict = FAIL]
  Flagged patterns:
  - 4 consecutive low-PPL sentences (positions 7-10)
  - 28% of text below PPL 25 threshold
  - Low variance window detected (positions 15-22)

  Suggested intervention: VS-Tail sampling
  Target positions: 7-10, 15-22
```

## Usage

### Command Line

```bash
# Check a file
python scripts/check_perplexity.py path/to/chapter.md

# Check with custom threshold
python scripts/check_perplexity.py path/to/chapter.md --threshold 20

# JSON output for integration
python scripts/check_perplexity.py path/to/chapter.md --json
```

### From Claude

```
Use the perplexity-gate skill to evaluate this prose:

The morning light filtered through the curtains, casting long shadows across
the hardwood floor. Sarah stretched lazily, feeling the warmth of the sun on
her face. She knew today would be different. Something in the air told her
that change was coming, a subtle shift in the atmosphere that she couldn't
quite put into words.
```

### Recipe Integration

```
After Draft 1 completion, run perplexity-gate:
- If PASS: proceed to developmental-editor
- If FAIL: apply VS-Tail sampling to flagged sections, then re-check
- Max 3 rewrite iterations before human escalation
```

## Integration with Verbalized Sampling

When verdict is FAIL:

1. **Load technique**: Reference `asha/modules/verbalized-sampling.md`
2. **Select variant**: VS-Tail sampling (p < 0.10) for maximum novelty
3. **Target sections**: Focus on flagged sentence positions
4. **Rewrite**: Generate low-probability variants preserving meaning
5. **Re-check**: Run perplexity-gate on rewritten passage
6. **Iterate**: Max 3 loops, then escalate to human

### Rewriting Techniques (from Claude Book)

Nine methods for improving perplexity:

| Technique | Description | When to Use |
|-----------|-------------|-------------|
| Verbalized Sampling | Generate N variants with probability estimates, select p < 0.10 | Default first attempt |
| Fragmentation | Break long sentences, vary length | Metronomic rhythm detected |
| Character Voice | Inject character-specific speech patterns | Generic dialogue |
| Rare Vocabulary | Substitute common words with less frequent synonyms | High word-level predictability |
| Syntactic Inversion | Rearrange clause order | Repetitive structure |
| Sensory Details | Add specific, unusual sensory observations | Generic descriptions |
| Broken Rhythm | Intentionally disrupt sentence flow | Uniform cadence |
| Cliche Subversion | Twist expected phrases | Predictable metaphors |
| Narrative Ellipsis | Remove expected information, imply instead | Over-explanation |

## Coordination with Other Agents

| Agent | Relationship |
|-------|--------------|
| **ai-detector** | Deprecated for this use case. Use perplexity-gate for local measurement |
| **prose-analysis** | Complementary. prose-analysis = subjective craft; perplexity-gate = objective metrics |
| **continuity-reviewer** | Sequential. Continuity runs first, perplexity-gate runs after draft generation |

## Limitations

**What this measures**:

- Token-level predictability (perplexity)
- Sentence-level variance (burstiness proxy)
- Pattern uniformity (consecutive low-PPL spans)

**What this does NOT measure**:

- Prose quality (textured prose can still be bad)
- Narrative coherence (use continuity-reviewer)
- Voice adherence (use prose-analysis --voice)
- Craft issues (use prose-analysis --craft)

**Important**: A PASS verdict means the prose has sufficient variance in predictability. It does NOT mean the prose is good. Always run prose-analysis for craft evaluation.

## Recipe Gate Configuration

For `chapter-creation.yaml`:

```yaml
gates:
  - name: perplexity
    description: "Prose has sufficient token-level variance"
    blocking: true
    max_retries: 3
    on_max_retries: escalate_to_user
    bypass: "Human override with justification"
```

## Troubleshooting

### Ollama Connection Failed

**Symptom**: Script errors with connection refused.

**Solution**: Ensure Ollama is running (`ollama serve`) and model is pulled (`ollama pull mistral`).

### Gate Always Fails

**Symptom**: Every draft fails regardless of content quality.

**Causes**:

- Heavy interiority/philosophical abstraction (naturally low PPL)
- Genre conventions that naturally read as "flat"
- Model-specific calibration needed

**Solution**: Adjust PPL thresholds for project, or bypass with human override.

### Gate Always Passes

**Symptom**: Obviously AI-generated content passes.

**Causes**:

- Short sample size (need 250+ words)
- Dialogue-heavy content (naturally high variance)
- Thresholds too lenient

**Solution**: Ensure minimum sample size, tighten thresholds.

### Computation Too Slow

**Symptom**: Takes >30 seconds per check.

**Solution**:

- Use `--sample` flag to check subset of sentences
- Consider smaller model (mistral 7B vs 8B)
- Ensure GPU acceleration enabled in Ollama

## Files

| File | Purpose |
|------|---------|
| `SKILL.md` | This documentation |
| `scripts/check_perplexity.py` | Main perplexity computation script |

## Related

- `asha/modules/verbalized-sampling.md` — Rewrite technique reference
- `write/agents/prose-analysis.md` — Craft quality evaluation
- `write/skills/novel-state/` — State management for metrics tracking
