---
name: rpg-rulings
description: >-
  Makes and applies rulings at the tabletop RPG table, for any system and
  starting from a named game's own procedure: whether an action needs a roll, what success and failure will mean, how hard it is
  (a target number set from the intended chance), rolling the dice or drawing
  standard or tarot cards for real to resolve actions (open, or hidden and
  GM-only, with a log and a deck kept between draws), reading the result into consequences without deciding for
  player characters, and recording rulings so later ones agree. Uses rpg-dice
  (0.5.0 or later) and rpg-card (0.3.0 or later). Serves a human GM's
  assistant or an agent GM. Use for should I call for a roll, what DC should
  this be, roll this for me, make a hidden check, draw a card to resolve
  this, what happens on a partial success, or how to rule on something the rules do
  not cover. Rules lookups go to rpg-router, odds for a mechanic to
  rpg-mechanic-analysis, and designing a mechanic to rpg-mechanic-design.
---

# Rulings at the table

Turns an action at the table into a ruling: roll or not, the stakes, the
difficulty, the roll or draw itself, and what the result means. It works for
any system. A game's own procedure comes first and is cited or marked
unchecked; where the rules are silent, the ruling is labelled house. Dice and
cards are rolled for real with rpg-dice and rpg-card, and target numbers come
from exact odds, not guesses.

## When to use

- "Should this be a roll?", "what DC is this?", "roll Stealth for her",
  "make a hidden Perception check for the guard", "draw for the scene",
  "what happens on a 7-9 here?", "the rules don't cover swinging from the
  chandelier".

**Not for:** what a game's rules say on their own (`rpg-router` answers
lookups); odds or variants for a mechanic (`rpg-mechanic-analysis`); building
a mechanic (`rpg-mechanic-design`); narration, scene voice and NPC dialogue,
which stay with the GM.

## Roles and visibility

- **Human GM with an assistant:** propose rulings, targets and stakes; the GM
  decides. **Agent GM:** decide within the game's rules and the table's
  agreements. **Agent player:** roll for its own character when asked; never
  set difficulty or read GM-only material.
- **Player-visible:** stakes, open rolls and their results, consequences as
  the characters experience them.
- **GM-only:** hidden rolls (`--hidden` output starts GM ONLY), targets the
  game keeps secret, consequences not yet revealed, deck state files (they
  show the cards still to come) and logs containing hidden rolls.
- Never decide what a player character does, thinks or feels. On a partial
  result, offer the choice; the player makes it.

## Workflow

1. **Pin the game and procedure.** Name the system and edition and the
   resolution procedure for this kind of action. Cite its page or section, or
   mark it unchecked; a mechanic the user describes is that mechanic. If the
   game says when to roll or what a roll can do, follow it.
2. **Decide whether to roll.** Roll when the outcome is uncertain and both
   success and failure would change the scene. Otherwise grant it, or grant it
   with a cost or a condition. Do not reroll the same goal unless the
   situation has changed. Where the game allows free retries, a roll means
   something only if failure costs time or risk; give it that cost (a clock,
   a returning patrol) or grant the result.
3. **Set the stakes before the dice.** Say what success gives, what failure
   costs, and the partial result if the game has one, in player-visible terms
   unless the game hides stakes. The player may choose not to act.
4. **Set difficulty from the intended chance.** Decide how likely the fiction
   makes it (guideline in the reference, labelled invented), then find the
   target with `scripts/difficulty.py`, adding `--steps` with the game's
   printed difficulty steps so their chances show too. Round to those steps,
   and say which step and chance result. If one goal
   needs several rolls, the chances multiply (see `rpg-mechanics-fundamentals`).
5. **Roll or draw for real.** `scripts/roll.py EXPR --label WHAT` for dice,
   `scripts/draw.py --deck DECK --state FILE` for cards kept between draws
   (say whether the session deck is new or continuing). Unseeded in play;
   `--hidden` for GM-only checks; `--log FILE` keeps a session record for
   both. `draw.py --odds COND` gives the exact chance for the next card from
   the deck as it stands.
6. **Read the result into consequences.** Apply the stakes as stated, the
   game's degrees or bands as written, and offer choices on partials. Never
   move the target after seeing the dice.
   When the GM asks for the ruling and the roll at once, settle every reading
   and default the target depends on before rolling and say which you used;
   if a choice only the GM can make would change the target, propose and
   wait instead of rolling.
7. **Record precedents.** When a ruling covers something the rules do not, or
   chooses between readings, write a ruling note (format in the reference),
   class it house, and have the human GM approve it before it binds the
   table. Check earlier notes before ruling on the same thing again.

## Examples

*D&D 2024: a character with +5 Athletics climbs a rain-slick wall. "What DC,
and roll it."* Published rules are involved, so they are cited.

- Procedure: a D20 Test, 1d20 plus modifiers against a DC; the SRD's typical
  DCs run Easy 10, Medium 15, Hard 20 (SRD 5.2.1 p. 6, core).
- Intended chance for "hard but possible": about 40% (invented guideline).
  `scripts/difficulty.py "1d20=+5t>=T" --chance 0.4 --steps 10,15,20` gives
  T = 18 at 40% exactly, and the printed steps nearby are Medium (15, 55%) and
  Hard (20, 30%); pick one and say which.
- Stakes before rolling: success reaches the balcony unseen; failure slides
  back down loudly enough for the guards to hear. No damage is claimed.
- `scripts/roll.py "1d20=+5t>=15" --label "Athletics (climb)"` rolls it.

*An invented 2d6 game: 10+ full success, 7-9 success at a cost, 6 or less the
GM's move. The character has +1.*

- Stakes: 10+ picks the lock quietly; 7-9 picks it but the player chooses
  between making noise and breaking the pick; 6 or less, the door opens from
  the other side.
- `scripts/roll.py "2d6=+1" --label "Pick the lock"`; read the bands on the
  total.

*A hidden check: does the guard notice the thief?*

- `scripts/roll.py "1d20=+2t>=13" --hidden --label "Guard notices"`. The
  result stays GM-only; the players learn only what their characters
  perceive, so a failure looks like nothing happening.

*A card-driven game with one deck for the session (procedure invented).*

- `scripts/draw.py --deck tarot --state campaign/session-deck.json` draws
  and keeps the deck; drawn cards stay out until the game says to reshuffle.
  Add `--odds "suit in ('Cups', 'Pentacles') and value >= 6"` to report the
  chance of the card that matters before it is drawn, and use `--count 0
  --reshuffle` when the game reshuffles at the end of an action. Keep the
  state file GM-only.

## Pitfalls

- **The roll stalls the game** → failure was dull → grant it, or attach a
  cost, before rolling.
- **The outcome feels arbitrary** → stakes were set after the dice → state
  them first.
- **A hidden result leaks** → narration reported the roll → describe only
  what characters perceive; keep GM ONLY output out of player text.
- **The ruling took a player's choice** → the partial was resolved for them
  → offer the options and let the player pick.
- **A "standard DC" presented as the game's** → invented guideline passed off
  as published → cite the game's table or label it invented.
- **A pool checked with `t>=`** → `t` compares the sum, not successes → use
  `difficulty.py "5d6>=T"` or `--need`.
- **Rulings drift between sessions** → precedents were not recorded → keep
  ruling notes and check them first.
- **"6 or higher" on a tarot minor is ambiguous** → court cards carry no
  printed number → state the readings (the scripts count page to king as 11
  to 14) and record the one the GM picks.

## Resources

- `scripts/roll.py`: roll rpg-dice expressions, open or `--hidden`, with an
  optional `--log`. Run with `--help`.
- `scripts/draw.py`: draw from standard or tarot decks, one-off or with a
  `--state` file kept between draws, with exact `--odds` for the next card
  and an optional `--log`. Exit 3 means the draw completed and any requested
  state was saved, but logging failed: use the printed result, do not repeat
  the draw, and inspect the log before repairing a missing/partial entry.
  Run with `--help`.
- `scripts/difficulty.py`: the target number for an intended chance, for
  totals, roll-under and pools (`--need`), with the game's printed steps
  (`--steps`). Run with `--help`. Opposed checks are odds questions for
  `rpg-mechanic-analysis`.
- [references/rulings-guide.md](references/rulings-guide.md): the intended
  chance guideline, setting difficulty in each family, stake patterns, open
  and hidden rolls, and the ruling note format. Read for steps 3, 4 and 7.
- Related: `rpg-router` for rules lookups; `rpg-mechanics-fundamentals` for
  why a mechanic behaves as it does; `rpg-mechanic-analysis` for exact odds
  and fight length; `rpg-mechanic-design` for building mechanics.
