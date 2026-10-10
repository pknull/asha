# RPG Plugin

**Version**: 0.1.0

Tabletop RPG tooling outside live play: managing the game library and, as the
section grows, researching and building game systems. Running a session at the
table belongs to the `rp` plugin; this one covers the books and the rules
around it.

## How to use it

RPG contains skills, not slash commands or agents. Ask for the operation
directly; the harness selects the matching skill.

```text
Audit my DriveThruRPG library against the local RPG folder.
Check whether any backer copies were never claimed.
```

## Skills

| Skill | Purpose | Requirement |
|---|---|---|
| `dtrpg` | Audit a DriveThruRPG library against the curated local folder, fetch missing or updated files, place them, and find unclaimed comp copies | `drpg` uv tool, `DRPG_TOKEN` |

Each skill is self-contained under `skills/<name>/SKILL.md`, and its installed
name carries the namespace (`rpg-dtrpg`). Skills keep that `rpg-` prefix so
they stay distinct when other skill sets are installed alongside.

## Boundaries

Reads run immediately. Downloads, file moves and anything that changes the
library follow the skill's confirmation rules; purchases and checkouts stay
with the user. Credentials live in `~/.asha/secrets.env` and per-user state
(placements, exclusions) in `~/.asha/`, never in this repository.
