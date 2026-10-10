---
name: rpg-oracle
description: >-
  Answers questions about the game world that nobody at the table has
  decided, and rolls on random tables, for solo play, GM-less play and a GM
  improvising: yes or no answers graded from yes-and to no-and at a stated
  likelihood, random tables from a file or a list (plain lists or ranges covering complete finite dice distributions,
  with each entry's exact chance), and standard or tarot card oracles
  drawn with rpg-rulings' deck. Uses rpg-dice (0.5.0 or later); the oracle scheme is
  invented, and published oracles are cited, never copied. Use for is the
  bridge guarded, ask the oracle, roll on my encounter table, what is in the
  chest, random weather, draw a tarot card for the scene, make a random
  table from my list, or how likely is each entry on this table. Resolving
  a character's action goes to rpg-rulings.
---

# Oracle

Answers what the fiction has not settled: is anyone home, what is in the
crate, which way the storm moves. A yes-or-no question gets a graded answer
at a likelihood set from the fiction; a random table gets a roll on the
user's own entries, with exact chances on request. The oracle answers about
the world; what characters do stays with their players. The grading scheme
here is invented for this package. Published oracles and tables are cited by
edition and page, or marked unchecked, and never reproduced.

## When to use

- "Is the bridge guarded?", "ask the oracle", "does the merchant know her?"
- "Roll on my encounter table", "random weather", "what's in the chest?"
- "How likely is each entry on this table?"

**Not for:** whether a character's action succeeds (`rpg-rulings`, using
the game's own mechanic); designing a table to a target distribution
(`rpg-mechanic-design`); odds of a mechanic (`rpg-mechanic-analysis`).

## Roles and visibility

- **Solo player:** both asks and decides what the character does next.
- **GM-less group:** anyone may ask; the table agrees on the likelihood
  before the roll.
- **GM improvising:** answers about things the players have not found yet
  are GM-only; roll with `--hidden` and reveal only what characters perceive.
- An answer never decides a player character's choice, feelings or
  knowledge. It decides facts about the world.

## Workflow

1. **Frame the question about the world.** "Is the door locked?" suits the
   oracle; "Can I pick the lock?" is an action for the game's mechanic
   (`rpg-rulings`). Check established facts first: never ask about what is
   already settled.
2. **Set the likelihood from the fiction, before rolling.** Very unlikely
   (10%), unlikely (30%), even (50%), likely (70%), very likely (90%), or a
   number from 1 to 99. Say which, and why. In solo play state it and roll
   unless the player asked to set it; at a group table agree it first. When
   one answer bears on another (the weather on who shelters inside), ask
   the one the other depends on first.
3. **Roll.** `scripts/oracle.py ask --likelihood likely --question "..."`.
   Read the grade: *and* adds something in the same direction, *but* adds a
   catch or a consolation. Interpret within established facts; when two
   readings fit, pick the more interesting one that contradicts nothing, and
   say it was an interpretation.
4. **Random tables.** Use the user's entries: a file with one entry per line,
   or `--items "A; B; C"`. Numbered entries ("2-4 Thunderstorm" or
   "2 to 4: Thunderstorm"; for Fate dice "-4--2: Bad", "-1 to 1: Mixed",
   "2-4: Good" with `--dice '4d{-1,0,1}'`, quoted for the shell; "+1
   longsword" stays plain text) need `--dice` and must cover every total
   exactly; the script refuses gaps, overlaps, and numbered entries without
   dice. Dice with unresolved exploding outcomes are refused: the finite
   table cannot cover every possible roll, even when the missing chance is tiny.
   `--odds` shows each entry's exact chance before anyone relies on the
   table.
5. **Cards as an oracle.** Draw with `rpg-rulings`' `scripts/draw.py`, with
   `--state` for a deck kept through the session. Card meanings come from the
   user's game or chosen reading; label any interpretation as one.
6. **Keep a record.** `--log FILE`, at a path the user names, appends each
   answer; `note "TEXT" --log FILE` records how an answer was read and any
   fact the player established; `recall --log FILE` prints it all. Recall
   before asking, so later answers stay consistent. Do not ask the same
   question again unless the situation has changed; a second roll for an
   unwanted answer is not a new answer.

## Examples

*Solo play: the character reaches a bridge at night. "Is it guarded?"* A
toll bridge on a trade road makes guards likely.

- `scripts/oracle.py ask --likelihood likely --question "Is the bridge guarded?"`
- 70% yes. Exact chances (from `--odds`): yes-and 14%, yes 51%, yes-but 5%,
  no-but 5%, no 19%, no-and 6%.
- Reading a *yes, but*: guarded, but the guard is asleep or bribable; the
  player decides what the character does about it.

*A road encounter table on 2d6.*

- File `road.txt`: `2-6: Quiet road`, `7-9: Travellers`, `10-12: Bandits`.
- `scripts/oracle.py table road.txt --dice 2d6 --odds` (exact): Quiet road
  5/12, Travellers 5/12, Bandits 1/6. Then roll without `--odds`.

*The GM improvises whether a hidden passage exists.*

- `scripts/oracle.py ask --likelihood unlikely --hidden --question "Is there a passage behind the shelves?"`
- The answer stays GM-only until a character finds the passage, or fails to.

## Pitfalls

- **The oracle decided a character's action** → the question was about the
  character → ask about the world, and roll the action with the game's
  mechanic.
- **Rerolling an unwanted answer** → the oracle stops meaning anything →
  keep the first answer; ask a new question only when things change.
- **Likelihood set after seeing the roll** → the answer was steered → set
  and state it first.
- **Answers contradict earlier ones** → nothing was recorded → log answers
  and read the log before asking.
- **A published oracle's table reproduced** → copied commercial text → cite
  it, or use the user's own copy; this package's scheme is its own.
- **A ranged table that skips a total** → some rolls land nowhere → the
  script refuses it; fix the ranges.

## Resources

- `scripts/oracle.py`: `ask` for graded yes-or-no answers and `table` for
  random tables, each with exact `--odds`, `--hidden` and `--log`. Run with
  `--help`.
- [references/oracle-guide.md](references/oracle-guide.md): choosing a
  likelihood, reading each grade, building tables, and keeping answers
  consistent. Read for steps 2, 3 and 4.
- Related: `rpg-rulings` for actions and card draws; `rpg-mechanic-design`
  to shape a table's distribution; `rpg-mechanics-fundamentals` for why a
  2d6 table favours its middle entries.
