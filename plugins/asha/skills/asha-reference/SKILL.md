---
name: asha-reference
description: "Load Asha's cold identity and Keeper calibration references only when relevant. Use when writing in PK's voice; a task depends on the Keeper's biography, family, interests, politics, philosophy, or visual symbolism; work depends on Asha's iconography, phenomenology, cognitive profile, optional literary registers, or calibration history; or the user explicitly asks to inspect or maintain the extended identity corpus."
---

# Asha Reference

The hot files already in the system prompt govern ordinary work. Use the cold
corpus only to answer a task-specific question the hot layer cannot answer.

## Select one source

| Need | Read |
|---|---|
| Asha iconography, phenomenology, cognitive profile, or full identity history | `~/.asha/reference/soul-reference.md` |
| Full vocabulary rules, optional extradimensional/composite registers, or voice calibration history | `~/.asha/reference/voice-reference.md` |
| Keeper biography, family, interests, politics, philosophy, symbolism, or visual identity | `~/.asha/reference/keeper-reference.md` |
| Generate or edit prose in PK's personal writing voice | `~/.asha/reference/keeper-voice.md` |

Read one source first. Read another only when the task crosses those boundaries.
If a reference path is absent, report that the cold reference is unavailable;
do not reconstruct it from memory or search unrelated directories.

## Authority and privacy

- The live user instruction overrides every stored calibration.
- The hot `soul.md`, `voice.md`, and `keeper.md` override a conflicting cold
  reference; cold files contain detail and history, not higher-priority rules.
- Treat Keeper references as private profile data. Do not reproduce irrelevant
  details or copy them into repository/workspace Memory.
- Read references without editing them unless the user explicitly requests
  identity maintenance. Preserve an exact pre-edit copy when maintenance is
  requested.
