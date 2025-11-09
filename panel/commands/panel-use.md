---
description: "Launch /panel from a named profile in Memory/panelProfiles.md"
argument-hint: "<profile> --topic '<one-line>' [--goals '+extra goal' ...] [--mode out_of_world]"
allowed-tools: ["Read"]
---

# Use Panel Profile or Roster

Resolve a preconfigured profile from `Panel/profiles.md` OR compose seats from `Panel/rosters.md` by name, apply optional overrides, then run `/panel` with the resulting spec. Eliminates copy/paste.

## Arguments

- `profile` (optional): Name in `Panel/profiles.md` (uses `roster:` to expand seats from `Panel/rosters.md`)
- `--roster` (optional): Name in `Panel/rosters.md` (compose from roster without a profile)
- `--add` (optional, repeatable): Append seat ids (from `Panel/rosters.md` seats) to the chosen roster
- `--topic` (required): One-line topic
- `--goals` (optional, repeatable): Add extra goals (prefixed with `+` indicates append)
- `--mode` (optional): Override mode (`inworld` or `out_of_world`)

## Behavior

1. If `profile` provided: read `Panel/profiles.md`, extract profile, and expand `roster:` into `experts` using `Panel/rosters.md`
2. Else if `--roster` provided: read `Panel/rosters.md`, expand roster into `experts`; apply any `--add` seats
3. Apply overrides:
   - Set `topic` to `--topic` value
   - Append `--goals` entries to goals
   - Override `mode` if provided
4. Invoke `/panel` using the merged PanelSpec

## Examples

```
/panel-use technical_architecture --topic "Design validate-repo workflow" --goals "+enforce pre-commit gate" --mode out_of_world
```

Ad-hoc from roster:

```
/panel-use --roster core --add external_models --topic "Evaluate MCP design" --goals "+latency budget"
```

Cultural sensitivity review:

```
/panel-use cultural_sensitivity_review --topic "Review Redacted379 ritual for cultural overlap"
```

This loads the `technical_architecture` profile, sets the topic, appends an extra goal, forces `out_of_world` mode, and runs `/panel` with the merged spec.

## Notes

- Profiles live in `Memory/panelProfiles.md` for versioning and discoverability; this command removes the need to copy/paste YAML.
- Phase 1.5 (Existing Infrastructure Check) still applies inside `/panel` and will consult Memory to avoid duplicative recommendations.
