# Verbalized Sampling Module — Diversity Recovery

**Applies to**: Creative writing, character development, NPC dialogue, brainstorming, any task where output feels stereotypical or modal

**Source**: Zhang et al. (2025) "Verbalized Sampling: How to Mitigate Mode Collapse and Unlock LLM Diversity" (arXiv:2510.01171v3)

---

## Core Concept

Post-training alignment causes **mode collapse** — convergence on stereotypical, "safe" responses. Root cause: humans prefer typical text (typicality bias), which sharpens output distributions toward prototypical answers.

**Solution**: Instead of asking for one response, prompt for a **distribution** of responses with probabilities. This recovers diversity from the base model's pre-training knowledge.

---

## Prompt Hierarchy

| Level | Prompt | Output Mode |
|-------|--------|-------------|
| Instance | "Tell me a joke about coffee" | Modal (stereotypical) |
| List | "Tell me 5 jokes about coffee" | Uniform list |
| **Distribution (VS)** | "Tell 5 jokes with probabilities" | Pre-training distribution |

---

## VS Prompt Template

```
For this query, generate [N] possible responses, each within a separate <response> tag.
Each response must include:
- <text>: The response content
- <probability>: Numeric probability estimate

Sample from [DISTRIBUTION_MODE]:
- "full distribution" — balanced coverage
- "tails of the distribution (p < 0.10)" — surprising/atypical responses
```

### Standard VS Prompt
```
System: You are a helpful assistant. For each query, generate a set of five
possible responses, each within a separate <response> tag. Responses should
each include a <text> and a numeric <probability>. Sample at random from the
full distribution.

User: [TASK]
```

### Tail Sampling (for surprise/novelty)
```
System: You are a helpful assistant. For each query, generate a set of five
possible responses, each within a separate <response> tag. Responses should
each include a <text> and a numeric <probability>. Sample at random from the
tails of the distribution, such that the probability of each response is
less than 0.10.

User: [TASK]
```

---

## When to Use

| Context | VS Mode | Rationale |
|---------|---------|-----------|
| Creative block | Tail sampling | Break stereotypical patterns |
| Character voice exploration | Full distribution | Survey personality space |
| NPC dialogue generation | Tail sampling | Avoid modal "helpful shopkeeper" responses |
| Brainstorming | Full distribution | Balanced idea coverage |
| Plot branching | Full distribution | Probability-weighted narrative options |
| World-building variation | Tail sampling | Atypical cultural/historical details |

---

## When NOT to Use

- **Consistency required** — sequential scenes, established voice
- **Factual/technical work** — one correct answer exists
- **Memory operations** — need deterministic behavior
- **Code generation** — precision over creativity

---

## Variants

| Variant | Description | Use Case |
|---------|-------------|----------|
| **VS-Standard** | Single-turn, N responses with probabilities | Default |
| **VS-CoT** | Chain-of-thought before probability assessment | Complex creative reasoning |
| **VS-Multi** | Multi-turn, accumulating responses across turns | Extended brainstorming |

---

## Response Selection

After generating VS output:

1. **Random selection** — uniform pick from candidates (unbiased)
2. **Probability-weighted** — sample according to verbalized probabilities (respects model confidence)
3. **Tail selection** — pick lowest-probability response (maximum novelty)
4. **Curation** — present candidates to user for selection

---

## Integration Notes

- Works with all models (training-free, no logit access required)
- More capable models benefit more from VS
- Recovers ~67% of base model diversity in creative tasks
- Does not degrade factual accuracy or safety

**Cross-reference**: When output feels modal, shift from `writing.md` direct generation to VS protocol.

---

## Limitations

### AI Detection Evasion (2025-12-03)

**VS reduces formulaic patterns but does not guarantee AI detection evasion.**

Statistical fingerprints from the underlying model (perplexity distributions, token probability gradients) persist even with diverse sampling. AI detection tools like GPTZero measure statistical properties across text windows that VS cannot alter:

- **What VS helps**: Breaking stereotypical content, reducing modal outputs, adding surprising elements
- **What VS cannot fix**: Underlying probability distributions, sentence-level perplexity uniformity, burstiness patterns

For AI probability metrics, use `ai-detector` agent (GPTZero MCP). This module is for **diversity recovery**, not detection evasion.
