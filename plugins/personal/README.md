# Personal Plugin

**Version**: 0.2.0

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
| `chat` | Open conversation about the Keeper, philosophy and current events, with a private OKF index of conversations, threads and observations | Asha identity files; `okf-bundle`; a grounded-search skill or web search for current events |
| `tracker` | On-demand views of what needs doing (today, close of day, weekly review) and follow-ups in both directions, ranked by time and importance; task, draft and invite changes only on the Keeper's word | `admin-todoist`; `gws` for Calendar and Gmail; optional `admin-proton-mail`; optional `PERSONAL_WEATHER_LOCATION` |

Each skill is self-contained under `skills/<name>/SKILL.md`.

## Private state

Per-user state lives under `$ASHA_HOME` (default `~/.asha`), never in this
repository. `chat` keeps an independent private OKF bundle in `$ASHA_HOME/chat/`,
with indexed conversation records, threads and observations. Source statements
and tentative interpretations remain distinct; corrections and original
migration evidence are retained. The bundle stays excluded from the parent
identity bundle, refuses writes inside a git work tree, and never copies its
contents into Memory. The chat skill uses the OKF writer to reindex, log and
strictly validate each update. `tracker`
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
