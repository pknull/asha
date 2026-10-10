---
name: rpg-router
description: >-
  Routes tabletop RPG requests to the skill that fits:
  rpg-mechanics-fundamentals for how kinds of mechanics behave,
  rpg-mechanic-analysis for exact numbers on a particular mechanic,
  rpg-mechanic-design for building or tuning a mechanic to goals,
  rpg-encounter-design for sizing fights, rpg-character-build for checking
  and valuing characters, and at the table rpg-rulings and rpg-oracle;
  fight tracking stays with the table's VTT or bot, characters with players.
  Use for rules lookups about a named game, which it answers itself, for
  adventure and setting requests, where it offers the package's mechanical
  help and leaves the writing to the author, and whenever an RPG request
  does not clearly belong to one skill. It classifies the request as
  research, building or play, notes whether it serves a researcher,
  designer, GM or player, settles system and edition when the answer depends
  on them, and applies shared ground rules: cite published rules or mark
  them unchecked, compute numbers, keep GM-only material private.
---

# RPG router

The entry point for RPG work. It works out which game and edition a request is
about, which kind of work it is, and who the answer is for, then names the
smallest set of skills to load and reads them before acting. It handles sourced
rules lookups directly and dispatches other work to the matching skills.

## When to use

- At the start of an RPG request: a rules question, a mechanic to model or
  analyse, a rulebook or SRD to add, a design task, or help running or playing
  a game.
- When the request names a system or mechanic and it is unclear which skill
  applies.

**Not for:** continuing work already inside a loaded skill. Route again only
when the request moves to a different system or kind of work.

## Routing

1. **System and edition.** Take them from the request, the files in view, or
   the user's own rulebook. If the edition matters and is unknown, ask. Never
   assume the newest edition.
2. **Kind of work.** Classify the request:
   - **research**: what a rule says, where it comes from, how a mechanic
     behaves, odds, variant impact, resource dynamics, failure modes;
   - **build**: designing or changing rules, settings, adventures, encounters,
     characters;
   - **play**: preparing or running a session.
3. **Role and visibility.** Note who the answer serves: researcher, designer,
   human GM, agent GM or agent player. For play, decide what is GM-only and
   what a player may see before writing anything.
4. **Resolve.** Pick the skills from
   [references/routing-table.md](references/routing-table.md). Prefer the
   smallest set.
5. **Read.** Open only the chosen skills' `SKILL.md`; load their references
   when a step asks for them.
6. **Compose.** Order: find and cite the rules text, then analysis or
   design, then play.
7. **Gaps.** If the right skill is only `planned`, or the table says none,
   say so and offer what the package can do (a ruling, a one-off roll, the
   odds around it). Continue under the ground rules below, and do not claim
   a missing skill's checks were done.

## Skills by kind of work

| Kind | Skills | Status |
|---|---|---|
| Research | `rpg-mechanics-fundamentals`, `rpg-mechanic-analysis` | authored |
| Research: rules lookup about a named game | none; answer under the ground rules | |
| Build | `rpg-mechanic-design` (resolution mechanics) | authored |
| Build: encounters | `rpg-encounter-design` | authored |
| Build: characters | `rpg-character-build` | authored |
| Build: adventures and settings | none: mechanical parts route to the oracle, analysis and encounter design | |
| Play: rulings, difficulty, rolls and draws | `rpg-rulings` | authored |
| Play: oracles and random tables | `rpg-oracle` | authored |
| Play: tracking initiative, hit points, conditions, clocks | none: the table's VTT or bot, or the GM's notes; one-off rolls such as initiative are `rpg-rulings`, odds such as "how bad is this hit" `rpg-mechanic-analysis` | |
| Play: playing a character | none: player characters are their players', NPCs the GM's | |

The full table, with trigger phrases, is in
[references/routing-table.md](references/routing-table.md).

## Ground rules for every route

- Reason from fundamentals first. A mechanic the user describes needs no
  rulebook.
- When a claim is about what a published game says, name the edition, read
  the text where it is (the user's copy or the official free text online),
  and cite the page or section heading, or mark the claim unchecked. Do not
  download or copy rulebooks.
- Label rules as core, optional, variant, house or invented.
- Surface ambiguous or contradictory rules as readings; do not pick silently.
- Paraphrase; never reproduce rulebook text at length.
- Compute numbers with scripts, or label them as estimates.
- If an answer rests on an unchecked rule, say so before giving any figure.
- Keep GM-only material out of player-visible output, and leave player
  characters' choices to their players.
