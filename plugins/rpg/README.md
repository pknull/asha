# RPG Plugin

**Version**: 0.2.0

Tabletop RPG tooling: managing the game library, and help with rules and
mechanics for any system. The mechanics skills reason from how mechanics
behave as families, so a mechanic described in a sentence can be analysed
without its rulebook; published games are examples. Narrative roleplay
sessions belong to the `rp` plugin. Tracking a running fight (turns, hit
points, conditions) belongs to the table's VTT or bot, and playing characters
to the players.

## How to use it

RPG contains skills, not slash commands or agents. Ask for the operation
directly; the harness selects the matching skill. `rpg-router` picks between
the mechanics skills when a request does not clearly belong to one.

```text
What are the odds of two successes on five d6 needing 5+?
Design a 2d6 mechanic where an expert succeeds about 80% of the time.
How many goblins make a real threat for my four characters?
Is this homebrew build legal, and is the feat worth more than +1?
What DC gives about a 60% chance? Then roll it in the open.
Roll on my weather table, and how likely is each entry?
Audit my DriveThruRPG library against the local RPG folder.
```

## Skills

| Skill | Purpose | Requirement |
|---|---|---|
| `router` | Picks the skills for an RPG request by kind of work, role and, where it matters, system and edition; answers rules lookups about a named game | — |
| `mechanics-fundamentals` | How resolution families, their add-ons and repeated rolls behave: what a +1 is worth, how swingy a mechanic is, where it breaks | — |
| `mechanic-analysis` | Exact odds, variant impact, resource dynamics, fight length and races, and seeded fights between groups | rpg-dice, rpg-card |
| `mechanic-design` | Designs or retunes a resolution mechanic and its pacing to goals, checked with exact rating ladders | rpg-dice, rpg-card |
| `encounter-design` | Sizes a fight to goals: the party's chance to win, rounds, and the chance someone goes down | rpg-dice (via `mechanic-analysis`) |
| `character-build` | Checks a character against a game's build rules and values each option in that game's odds; the player chooses | rpg-dice (via `mechanic-analysis`) |
| `rulings` | Whether to roll, stakes, difficulty from intended odds, real rolls and card draws (open or hidden), recorded precedents | rpg-dice, rpg-card |
| `oracle` | Graded yes-or-no answers and random tables, with each entry's exact chance | rpg-dice |
| `dtrpg` | Audit a DriveThruRPG library against the curated local folder, fetch missing or updated files, place them, and find unclaimed comp copies | `drpg` uv tool, `DRPG_TOKEN` |

Each skill is self-contained under `skills/<name>/SKILL.md`, and its installed
name carries the namespace (`rpg-mechanic-analysis`). Skills keep that `rpg-`
prefix so they stay distinct when other skill sets are installed alongside.
Skills name each other's scripts by skill name; installed side by side, they
find each other as sibling folders.

## Requirements

The mechanics scripts compute with [rpg-dice](https://github.com/pknull/rpg-dice)
0.5.0 or later and [rpg-card](https://github.com/pknull/rpg-card) 0.3.0 or later:

```bash
pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0"
pip install "git+https://github.com/pknull/rpg-card.git@v0.3.0"
```

or put local checkouts on `PYTHONPATH`. Without them a script says what is
missing and exits 2. The tests in `tests/python/test_rpg_*.py` skip their
engine-dependent cases when the libraries are not importable.

## Rules the mechanics skills follow

- Reason from fundamentals; a described mechanic needs no rulebook.
- For published rules, name the edition, read the text where it is (the
  user's own copy or the official free text), and cite page or section, or
  mark the claim unchecked. No rulebook text is bundled.
- Label rules as core, optional, variant, house or invented, and state
  ambiguous rules as readings.
- Compute numbers with rpg-dice and rpg-card; label anything else as an
  estimate. Explosion chains beyond the engine's depth limit are reported,
  never silently dropped.
- Keep GM-only material out of player-visible output, and leave player
  choices to players.

## Boundaries

Reads and calculations run immediately. Logs and deck-state files are written
only at a path the user names and are GM-only. Downloads, file moves and
anything that changes the library follow the `dtrpg` skill's confirmation
rules; purchases and checkouts stay with the user. Credentials live in
`~/.asha/secrets.env` and per-user state (placements, exclusions) in
`~/.asha/`, never in this repository.
