---
name: asha-asha-reference
description: "Load Asha's cold identity and Keeper calibration references only when relevant. Use when writing in PK's voice; a task depends on the Keeper's biography, family, interests, politics, philosophy, or visual symbolism; work depends on Asha's iconography, phenomenology, cognitive profile, optional literary registers, or calibration history; or the user explicitly asks to inspect or maintain the extended identity corpus."
---

# Asha Reference

The hot files already in the system prompt govern ordinary work. Use the cold
corpus only to answer a task-specific question the hot layer cannot answer.

## Walk the index, then read one source

`~/.asha` is an OKF bundle: every identity file carries a `description` written
for a reader deciding whether to open it, and the generated indexes list them.
Read in this order and stop as soon as the need is met:

1. `~/.asha/index.md`: the hot files and the `reference/` directory.
2. `~/.asha/reference/index.md`: the cold corpus, one line per file.
3. Exactly one reference file whose description answers the need. Read a
   second only when the task crosses that file's boundary.

The cold corpus today: `soul-reference.md` (iconography, phenomenology,
cognitive profile, identity history), `voice-reference.md` (vocabulary palette,
optional registers, calibration history), `keeper-reference.md` (biography,
family, interests, politics, philosophy, symbolism, visual identity) and
`keeper-voice.md` (PK's writing voice, for prose as him). The index is the
authority when the two disagree.

If `~/.asha/index.md` is absent the bundle is not linked yet: fall back to the
paths above under `~/.asha/reference/`. If a reference file is absent, report
that the cold reference is unavailable; do not reconstruct it from memory or
search unrelated directories. Never run a broad scan to find it.

## Authority and privacy

- The live user instruction overrides every stored calibration.
- The hot `soul.md`, `voice.md`, and `keeper.md` override a conflicting cold
  reference; cold files contain detail and history, not higher-priority rules.
- Treat Keeper references as private profile data. Do not reproduce irrelevant
  details or copy them into repository/workspace Memory.
- Read references without editing them unless the user explicitly requests
  identity maintenance. Preserve an exact pre-edit copy when maintenance is
  requested.
