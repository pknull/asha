# Personal Plugin

**Version**: 0.1.0

Skills that work with the Keeper's own life rather than a single service:
open conversation, and tracking what needs doing or following up. They sit
above the `admin` integrations and call them; `admin` stays service plumbing.

## How to use it

Personal contains skills, not slash commands or agents. Say what you want and
the harness selects the matching skill.

```text
I just feel like chatting.
What's on my plate today?
Who am I waiting on?
Let's do the weekly review.
```

The same request form works on Claude, Codex, Copilot, and OpenCode.

## Skills

| Skill | Purpose | Requirement |
|---|---|---|
| `chat` | Open conversation about the Keeper, philosophy and current events, with private notes carrying threads and observations between conversations | Asha identity files; a grounded-search skill or web search for current events |
| `tracker` | On-demand views of what needs doing (today, close of day, weekly review) and follow-ups in both directions, ranked by time and importance; task, draft and invite changes only on the Keeper's word | `admin-todoist`; `gws` for Calendar and Gmail; optional `admin-proton-mail`; optional `PERSONAL_WEATHER_LOCATION` |

Each skill is self-contained under `skills/<name>/SKILL.md`.

## Private state

Per-user state lives under `$ASHA_HOME` (default `~/.asha`), never in this
repository. `chat` keeps its notes in `$ASHA_HOME/chat/`, refuses to write
them inside a git work tree, and never copies them into Memory. `tracker`
keeps no state of its own: follow-ups it tracks, and ones the Keeper
dismisses, live as tasks in the task manager.

## Configuration

`tracker` reads two optional variables from `~/.asha/secrets.env`:
`PERSONAL_TASKS` (task manager; default and only supported value `todoist`)
and `PERSONAL_WEATHER_LOCATION` (a wttr.in location for the today view).

## Installation

```bash
./install.sh --only personal --target claude
./install.sh --only personal --target codex
./install.sh --only personal --target copilot
./install.sh --only personal --target opencode
```

## License

MIT
