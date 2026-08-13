# Asha project

At session start, read the pair coherently with `memory_v2.py read --project-dir
"$PROJECT_DIR"`, then verify its claims against current disk. It reflects only
the most recent explicit `/session:save` publication.

Do not treat `Work/session-state/*.json` as authoritative. These ignored,
bounded files are unpublished crash-recovery hints. Do not write published
Memory from hooks, lifecycle events, timers, or host transcripts.

Use `/session:save` to publish the four-section active handoff and current
binding decisions. Use `/session:consolidate` for reviewed legacy migration.
