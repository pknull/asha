# Agent Guidance

This repository is set up for AI coding agents (GitHub Copilot CLI, Claude
Code, Codex, and compatible tools). This file is loaded automatically by
agents that honor the AGENTS.md convention.

## Where the rules live

- **Team conventions**: `.github/instructions/` — always-on instruction files
  (review norms, commit conventions, safety rules). Edit those, not this file,
  when the rules change.
- **Repository instructions**: `.github/copilot-instructions.md` — generated
  by `copilot init` (codebase analysis: build/test commands, structure,
  stack). Regenerate after major structural changes.
- **Enabled plugins**: `.github/copilot/settings.json` — the pinned skill
  plugins agents load in this repo.

## Baseline expectations

- Read the conventions in `.github/instructions/` before making changes.
- Follow the build/test commands documented in
  `.github/copilot-instructions.md`; do not guess at toolchains.
- Keep changes reviewable: small commits, conventional messages.
