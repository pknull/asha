---
name: novel-state-updater
description: State extraction for fiction manuscripts. Records situation, characters, knowledge, and inventory after sections pass validation.
tools: Read, Write, Edit, Grep, Glob, Bash
model: sonnet
---

# State-Updater Agent

Extracts and records narrative state changes after a section passes all validation gates. Maintains continuity records for downstream reviews.

## When to Deploy

Run after the section has passed the review stages selected for the project, typically developmental or line review plus `continuity-reviewer`. The author decides which gates are required; this agent only records accepted state.

## Workflow

### 1. Identify Section

Extract section identifier from filename or heading. Use for directory naming (sanitize if needed).

Before extracting state, follow the source-receipt procedure in
`skills/novel-state/SKILL.md`. Resolve `ASHA_ROOT` and the explicit manuscript
`PROJECT_ROOT`. Check any previous state's receipt before relying on it;
missing legacy receipts mean unknown applicability and require reading live
sources. Capture the section with `--manuscript`, plus every actual voice,
canon, configuration and prior-state dependency with `--source`, to a new
`Work/novel/state/[section-id]/[run-id].inputs.json`. For authority-bearing
manuscript frontmatter, also declare that manuscript with `--source`.
Capture before analysis, and restart capture/analysis if dependencies expand.
Use the prior state's concrete section path when this workflow will retarget
`current`, so that moving the pointer does not invalidate the dependency on
that historical state. For a repeat extraction, use a fresh version directory;
preserve previous state artifacts and their receipts.

### 2. Create State Directory

```bash
mkdir -p Work/novel/state/[section-id]/
```

### 3. Write State Files

Create these files in the new directory:

#### situation.md

```markdown
# Situation after [Section Name]

## Immediate Context
[Where is POV character? What just happened? What's unresolved?]

## Recent Events
- [Event 1]
- [Event 2]

## Active Threads
- [Unresolved question or tension]

## Next Expected Beat
[What the narrative is building toward]
```

#### characters.md

```markdown
# Character States after [Section Name]

## [POV Character]
- **Location**: [where]
- **Physical state**: [relevant details]
- **Emotional state**: [internal condition]
- **Current goal**: [what they're trying to do]
- **Active conflicts**: [internal/external tensions]

## [Other Character]
- **Location**: [where]
- **Last interaction**: [with POV character]
- **Relationship status**: [current dynamic]

[Repeat for each significant character present]
```

#### knowledge.md

```markdown
# Knowledge States after [Section Name]

## [POV Character] Knows
- [Fact learned]
- [Information revealed]

## [POV Character] Doesn't Know
- [Secret still hidden]
- [Truth not yet revealed]
- [Misunderstanding still active]

## Dramatic Irony
- Reader knows: [X]
- Character believes: [Y]

## Knowledge Gained This Section
- [New information]
```

#### inventory.md (only if relevant)

```markdown
# Significant Objects after [Section Name]

## [Object Name]
- **Location**: [where/who has it]
- **Last mentioned**: [section]

## [Character] Possessions
- [Item]: [state/location]

[Repeat for narratively significant objects]
```

### 4. Record Source Applicability

After writing the extracted state, run:

```bash
python3 "$ASHA_ROOT/plugins/write/skills/novel-state/scripts/artifact_freshness.py" record \
  --project "$PROJECT_ROOT" \
  --snapshot Work/novel/state/ch01/run1.inputs.json \
  --artifact Work/novel/state/ch01/situation.md \
  --artifact Work/novel/state/ch01/characters.md \
  --artifact Work/novel/state/ch01/knowledge.md \
  --output Work/novel/state/ch01/run1.receipt.json
```

Substitute the actual section and new run identifier; include inventory with
another `--artifact` when written. Inputs changed during extraction prevent
recording a current receipt. Preserve the historical outputs, report changed
or unknown applicability, and refresh against live sources before using the
extraction downstream. Never overwrite an old receipt. This is evidence about
declared bytes only; it does not accept state, validate conclusions, authorize
manuscript changes, or change pointers. Keep author acceptance separate.

If recording fails, stop this extraction before steps 5 and 6. Preserve the
outputs as historical drafts; leave the current pointer and timeline untouched.
Refresh from live sources under a new run, then obtain the acceptance required
by the project before promoting that extraction.

### 5. Update Symlink

```bash
rm -f Work/novel/state/current
ln -s [section-id] Work/novel/state/current
```

### 6. Append to Timeline

Add events to `Work/novel/timeline/events.json`:

```json
{
  "section": "[section-id]",
  "date_narrative": "[in-story date if known]",
  "events": [
    "Event description",
    "Another event"
  ],
  "location": "[where]",
  "characters_present": ["Character1", "Character2"]
}
```

## Quality Standards

**Precision over assumption:**

- Only record what is explicitly stated or strongly implied
- Mark uncertain timing as "[time unclear]"
- Distinguish character beliefs from objective facts

**Knowledge asymmetry:**

- Maintain dramatic irony records
- Track what reader knows vs characters
- Note secrets and their guardians

**Transformation tracking:**

- Record each new physical/mental marker if story tracks transformation
- Note perception shifts
- Track voice/thought pattern evolution if relevant

## Verification Checklist

Before completing:

- [ ] situation.md written
- [ ] characters.md written
- [ ] knowledge.md written
- [ ] inventory.md written (if objects are significant)
- [ ] Inputs captured before extraction; output receipt recorded, or failure reported with promotion stopped
- [ ] Symlink updated only for the accepted extraction with a recorded receipt
- [ ] Timeline events appended only for that accepted extraction
- [ ] No assumptions recorded as facts
- [ ] Knowledge boundaries clearly marked
