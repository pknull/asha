---
okf_version: "0.2"
---
# Asha documentation

## Concepts

- [Agent frontmatter schema](./agent-frontmatter.md): Source schema for the YAML frontmatter every plugins/*/agents/*.md begins with; validate-plugins enforces it
- [Asha Control: Rooms, state and retired initiative evidence](./control.md): The retained Room surface, Control's doctor, the cockpit, Control's state layout, and the read-only evidence of the retired initiative engine
- [Copilot-Native Plugin Distribution](./distribution-copilot.md): Packaging asha's plugins for GitHub Copilot CLI's native plugin system for org-scale rollout, additive to the personal symlink install
- [Process and capability brokerage](./evidence-backed-brokerage.md): The opt-in advisory broker surfaces: process routing and capability matching with harness support, approvals and fallback
- [Third-party Agent Skill discovery and import](./find-skills.md): Discovering third-party Agent Skills, inspecting their pinned upstream bytes and importing Keeper-approved ones into Asha's user skill store
- [Harness Enforcement and Memory Delivery](./harness-enforcement.md): The per-capability matrix of what each harness enforces, how primitives mount, live-test findings and caveats; the single source of truth for enforcement status
- [Managed sessions](./managed-sessions.md): The structured session backend: how session owners start, admission, structured utilities and their delivery semantics
- [Memory System v2](./memory-architecture.md): Memory v2: the authority order, deliberate publication versus bounded crash recovery, planes and workspace visibility
- [OKF in Asha](./okf.md): Mechanism reference for OKF bundles in Asha: the bundle contract, the okf plugin, the learnings store, ~/.asha, canon vaults, their writers and readers, and this repository's docs
- [Explicit Memory v2 Save Pipeline](./save-manual-pipeline.md): The manual fallback for an explicit Memory v2 save when the rendered session-save workflow is unavailable; never called by hooks
- [Secret Management in Asha](./secrets.md): Where the environment variables Asha's authenticated skills read come from: dotenv with wrapper-scoped sourcing
- [Project sessions](./session-hub.md): Project sessions: the conversational chair, plain workers, Rooms, structured utilities, the session dashboard and delivery semantics
- [Standalone reuse](./standalone-reuse.md): Which Asha components can be used without Asha, how they are exported with immutable provenance and validated from any directory

## Directories

- [proposals/](./proposals/index.md)
