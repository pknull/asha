# Ollama (Local Models)

## Critical First Step

**ALWAYS ask which model is running.** Llama3, Mistral, Qwen2.5, CodeLlama, Phi all behave differently.

## Best Practices

- System prompt is most impactful lever (Modelfile `SYSTEM` or API `system` param)
- Shorter, simpler prompts outperform complex ones
- Local models lose coherence with deeply nested instructions
- Temperature: 0.1 for deterministic/coding, 0.7-0.8 for creative
- Context window varies by model and VRAM — don't assume large context

## Model Selection

- Coding: CodeLlama or Qwen2.5-Coder (not general Llama)
- General: Llama3, Mistral
- Structured output: Qwen2.5

## Output Format

Include system prompt in generated output so user can set in Modelfile:

```
SYSTEM """
[system prompt here]
"""
```
