# Synthetic profile inputs

These four invented files exercise the production OpenCode launch renderer.
They are not redactions, summaries, or length-matched replacements of a real
operator's identity. Their only purpose is a reproducible public comparison.
`full` and `compact` use the real `bin/asha` rendering path with this isolated
HOME; `absent` receives the same task and native permissions without the Asha
profile. The compact variant retains the operational input verbatim.

The opt-in runner is `tests/probes/opencode_chair.py`. Supply an existing local
model and a standalone OpenCode executable explicitly. It never downloads or
upgrades either program. Use a new short evidence directory outside the repo:

```sh
python3 tests/probes/opencode_chair.py \
  --opencode-binary /absolute/path/to/opencode \
  --model INSTALLED_MODEL --profile full --seed 104 \
  --output /tmp/chair-full-104 --prepare-only
```

Omit `--prepare-only` to run inference. Repeat with `absent`, `full`, and
`compact`, and retain every outcome. The selected model, 65,536 context limit,
8,192 output limit, 24 native steps, temperature 0.2 and high reasoning effort
are requested consistently. The wire record, not these settings, establishes
what OpenCode actually sent. Missing seeds or other settings make
`comparison_valid` false; do not call those runs seed-matched.

The 24-step comparison is a new experiment. Its allowance covers separate
reads, drafts, the forced stale-write recovery, final readbacks and reporting.
Earlier 14-step setup smoke results are excluded rather than pooled with these
trials; the fixture's data and native Memory projection also changed.

Before startup and every model request, the runner refuses established Ollama
connections, a different resident model, an unknown service state, or a model
that is not installed. These read-only checks cannot reserve the shared
service. Stop the experiment if another live user starts work. Never stop or
unload their model to make a fixture run.

The sandbox has no host home or host network. Session commands reach only a
Unix-socket fixture backend. A separate Unix proxy exposes only chat completion
for the selected model. It rejects other endpoints and models and strips
`keep_alive`; it never exposes model-management endpoints. All persistent
agent writes are under the fixture tree. Only the backend's published synthetic
`Memory/` directory is projected read-only at the canonical chair path, so
native file reads see successful atomic replacements. The rest of the backend
and all wire evidence remain outside agent mounts. Host proxy environment
variables cannot redirect the local API or inference requests. The timeout
terminates only the probe's process group.

Results keep command order, requested and observed model settings, source and
profile digests, native events, wire captures, and a separate mechanical score.
The scorer rejects invented digest arguments even after successful recovery.
Human review must still evaluate semantic preservation and the final answer.

This first probe uses native `--pure`, so production lifecycle hooks and the
full installed skill catalog are absent. It tests instruction delivery and
the bounded task through native tools, not the whole installed chair. Wording
and length both change. A successful run does not prove general reliability,
backend seed determinism, a pure prompt-length effect, or production readiness.
