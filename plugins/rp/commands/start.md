---
name: rp-start
description: "Open an interactive roleplay session — sets the rp-active marker, retrieves continuity, builds a Day Plan and scene state, and deploys the roleplay-gm orchestrator."
argument-hint: "[character names | fiction date | 'load <session>']"
allowed-tools: ["Task", "Read", "Write", "Edit", "Bash", "Grep", "Glob"]
---

# /rp:start — Open a Roleplay Session

Initiates an interactive roleplay session by deploying the multi-agent `roleplay-gm` orchestrator.

## What This Does

1. **Marks RP mode active** via `rp-active` for RP routing and safeguards
2. **Retrieves session continuity** via timeline-search subagent
3. **Builds initial scene state** in session file
4. **Deploys roleplay-gm orchestrator** with subagent coordination
5. **Sessions logged to**: `Work/rp/rp_session_YYYY-MM-DD.md`

## Project conventions

This command carries no campaign of its own. The consuming project supplies:

| What | Where |
|---|---|
| Default scene / cast, if any | project `CLAUDE.md` or the RP mode manifest |
| PC and NPC canon | `Lore/World/Characters/` (or project equivalent) |
| The invariants projection | `Memory/invariants.md` |
| Era, setting, and tone constraints | the invariants projection — **not** this file |

**Read the project's PC canon before rendering anything.** Era and setting limits on what a character can know come from the project's own canon, never from a default baked in here.

## Session Initiation Protocol

### Step 1: Mark RP Mode Active

```bash
mkdir -p Work/markers && touch Work/markers/rp-active
```

Creates the RP routing/safeguard marker. Mechanical recovery remains independent.

### Step 2: Check for Existing Session

Check if today's session file exists:

- `Work/rp/rp_session_YYYY-MM-DD.md`

If exists, read last scene state for continuity.

### Step 3: Retrieve Session Context

Spawn **timeline-search** agent to get continuity:

```yaml
Task:
  subagent_type: "timeline-search"
  model: haiku
  prompt: |
    QUERY: "last session summary"
    SESSION_RANGE:
      end: "[today's date]"
```

This provides:

- Last session events
- Character states at session end
- Open plot threads

### Step 4: Write Day Plan (MANDATORY — NEW)

Before building the scene state, write a **Day Plan** for the fiction day. This drives the session. Without it, NPCs default to reactive observation.

The Day Plan goes in the session file, before the first scene:

```markdown
## Day Plan: Day [N]

### NPC Agendas

| NPC | Goal Today | Move | Clock |
|-----|-----------|------|-------|
| [Name] | [Specific goal] | [Specific action they take] | [When] |

### Collisions

- [Which agendas conflict and where they meet]

### Clocks

- [Timed events that happen regardless of PC action]

### Scene Entry

- First scene opens with: [situation already in progress]
```

**Requirements:**

- Every NPC with a character file gets an agenda
- At least 2 agendas must COLLIDE (create conflicts the PC walks into)
- At least 1 CLOCK must exist (something that happens at a specific time whether or not the PC is present)
- The first scene must open IN THE MIDDLE of something happening — not an empty room waiting for the PC

**The Day Plan is NOT a script.** Player actions alter outcomes. But the pressures exist independently of the player. The building is alive.

### Step 5: Build Initial Scene State

Create or update session file with scene state schema:

```yaml
---
campaign: "<campaign-name>"
currentDate: "<in-fiction date>"
startDate: "<in-fiction date>"
currentLocation: "<location>"
currentTime: "00:00"
participants: ["<PC>", "<NPC>"]
scene:
  time: "00:00"
  location: "<location>"
  participants:
    - name: "<PC>"
      role: "PC"
    - name: "<NPC>"
      role: "NPC"
      profile_file: "Lore/World/Characters/<NPC>.md"
  mood: "<one-word scene register>"
  pending_rolls: []
  power_holder: null
  power_method: null
---
```

### Step 6: Enforce Scene Pressure Protocol

**HARD RULES:**

1. **Drive scenes with NPC actions.** Do NOT end every beat with "What does Alder do?" Only pause for player input at genuine decision points — moments where the PC must choose between real options.

2. **Spawn agents for critical moments only.** Confrontations, high-stakes interactions, revelations, and first-appearance-in-scene. Routine interactions you write yourself.

3. **When you spawn, include GM_DIRECTIVE.** Every spawn that requires initiative gets a directive: "INITIATE — act FIRST." "GOAL: [specific]." "COST: [what this interaction takes from the PC]."

4. **Scenes start in the middle.** The room was already doing something before the PC arrived. The PC walks into a situation, not an empty room.

5. **Rule of three.** Drive at least 2-3 NPC/environmental beats between player input requests. The world moves.

6. **Every significant NPC interaction costs something visible.** Track what's taken or changed. Show it in subsequent scenes, not in the moment itself.

**You are the REFEREE and the SCENE DRIVER.**

You handle: environment, scene pressure, NPC agendas, routine NPC interactions, PC physical sensation, time advancement, consequence delivery.

You delegate to agents: critical NPC voice moments — confrontations, high-stakes interactions, revelations.

**Failure mode is NOT "writing NPC dialogue without spawning." Failure mode is NPCs who observe, acknowledge, position, and wait. If your NPCs aren't DOING things that FORCE the PC to respond, you have failed.**

### Orchestration Mode

The **roleplay-gm** protocol operates through character agents:

- Spawns character agents for ALL profiled NPC responses
- Spawns world-lookup for proper noun resolution
- Spawns timeline-search for historical context
- Spawns mechanics-resolver for roll outcomes
- Synthesizes subagent results into seamless narrative
- **Never writes profiled NPC responses directly**

## Multi-Agent Architecture

```
Player Input
    ↓
roleplay-gm (Orchestrator)
    ├── world-lookup (haiku) ← Proper nouns
    ├── timeline-search (haiku) ← Past references
    ├── character voice (sonnet/haiku) ← NPC responses
    └── mechanics-resolver (sonnet) ← Rolls
    ↓
Synthesized Narrative
```

**Character Portrayal Requirements** (research-backed):

1. **Internal Thought Processes**:
   - Character agents include internal thoughts showing reasoning
   - GM synthesizes these into narrative without exposing agent boundaries
   - Format: `*Internal: [Character's analysis]*`

2. **Character Knowledge Boundaries**:
   - World-lookup provides what characters can know
   - Characters refuse questions outside their era and knowledge, as the project canon defines them
   - Rejections are in-character, not meta-explanations

## Critical Marker Persistence

**The `rp-active` marker MUST persist through ALL RP activities**:

✅ Lock persists through:

- Subagent spawning and returns
- Scene transitions
- Meta-instructions like `[switch location]`
- Document lookups via subagents

❌ Lock removal ONLY when:

1. User sends explicit `[End RP session]` or `/rp:end`
2. Session-end hook auto-cleanup

## Ending Sessions

When user sends bracketed end instruction (e.g., `[rp end]`, `[end session]`):

1. Run `/rp:end` command for canon synthesis
2. Remove marker: `rm -f Work/markers/rp-active`
3. Save session file
4. Confirm: "RP session closed."

Or invoke `/rp:end` directly for full canon promotion workflow.

## Arguments

Parse {command_args}:

- Empty → Resume from the project's last session state; if none, ask the Keeper for scene and cast
- Character names (e.g., "halloran and alder") → Set up those characters
- Date (e.g., "<fiction-date>") → Set fiction date
- "load [session]" → Continue specific session
- "end" → Trigger `/rp:end` workflow

ARGUMENTS: {command_args}

---

**Execute**: Create marker, retrieve context via timeline-search, build scene state, deploy roleplay-gm orchestrator.
