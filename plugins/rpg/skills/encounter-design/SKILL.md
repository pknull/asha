---
name: rpg-encounter-design
description: >-
  Sizes a tabletop RPG combat encounter to stated goals, for any system: how
  likely the party is to win, how many rounds it should take, how likely
  someone on the party side goes down, and how much it should cost. Turns
  characters and foes into simple combatants (hit points, attacks as chance
  and damage), runs exact fight-length figures and seeded fights through
  rpg-mechanic-analysis's scripts with rpg-dice (0.5.0 or later), ladders the
  number of foes, and reports the composition that meets the goals under
  each tactic that matters; several fights in a row without a rest need a
  custom simulation, and are flagged as such. A game's own encounter budget
  is used as a starting point, cited or marked unchecked, and then checked.
  Use for how many goblins for my party, is this fight too deadly, balance
  this boss fight, or make this encounter last about four rounds. Computing
  one fight's length or odds goes to rpg-mechanic-analysis; tuning a damage
  system's pacing in general to rpg-mechanic-design.
---

# Encounter design

Turns "a hard but fair fight for four level-two characters" into a concrete
roster with numbers behind it: the party's chance to win, how long it lasts,
and how often someone drops. Characters and foes become simple combatants
(hit points and attacks as chance and damage), and the fight is checked with
`rpg-mechanic-analysis`'s `scripts/skirmish.py` (seeded fights) and
`scripts/rounds_until.py` (exact rounds). A game's encounter budget, if it
has one, is only a starting point: it is cited or marked unchecked, and the
roster is checked anyway. Every recommended encounter is labelled invented.

## When to use

- "How many goblins for my party?", "is this fight too deadly?"
- "Balance a boss against four characters", "this fight should last about
  four rounds and be scary but survivable."

**Not for:** pacing a damage system in general (`rpg-mechanic-design`); a
single duel or fight-length question (`rpg-mechanic-analysis`); running the
fight at the table (`rpg-rulings`; tracking it stays with the table's VTT
or bot).

## Workflow

1. **Set the goals as numbers.** Ask only for what is missing: the party's
   chance to win (often 90-99% for an ordinary fight), the rounds it should
   take, the chance that anyone on the party side goes down, and what the
   fight is for in the session. The goal sheet is in
   [references/encounter-guide.md](references/encounter-guide.md).
2. **Turn everyone into combatants.** For each character and foe: hit
   points, and each attack as `CHANCE:AMOUNT` against a typical target. Work
   the chance from the game's resolution (`rpg-mechanic-analysis`'s
   `dice_odds.py`, for example `1d20=+5t>=15`). Stat blocks from published
   games are cited or marked unchecked. Note what this leaves out (spells,
   healing, terrain, movement, reactions).
3. **Start from the game's budget, if any.** Cite it or mark it unchecked;
   otherwise start from action economy (as many foe actions as party actions
   is already a real fight).
4. **Ladder the foe count.** `skirmish.py --unit ... --count Foe=0..HI`
   (0 is the roster without it), once with `--order random --target random`
   (a neutral baseline) and once with `--order random --target A=random,B=first`
   (foes focusing one character, the worst case for risk; list the most
   fragile character first). Use `--order once` where initiative is rolled
   once per fight. For a boss, `--hp-ladder Boss=30,40,50` ladders its hit
   points, alone or with `--count` as a grid; give it extra attacks to match
   the party's actions.
   Several fights in a row are not modelled. `skirmish.py` reports the mean
   hit points left when the party wins, but a next fight run at those
   (rounded) values drops the spread, who is down and the fights already
   lost: label it a rough estimate, never the odds of surviving the
   sequence, which need a custom simulation carrying the whole roster
   forward (`rpg-mechanic-analysis`).
5. **Pick and check.** Choose the row that meets the goals under the tactics
   the foes will actually use. If no row does, including no extra foes at
   all, say so plainly, then ladder what can change (the boss's hit points or
   damage, the number of foes) and report the nearest rosters. Win and risk
   goals can conflict, especially under focus fire; say which one gives way.
   Check the failure modes: alpha strike (the first round decides it), a
   character dropped before acting, a slog, and a lone boss swamped by action
   economy.
6. **Report.** The roster, labelled invented; the ladder rows that justify
   it; the tactics assumed; and what was not modelled.

## Example

*Four characters, each 24 hit points, hitting 3 in 5 for 1d8+3. Goblins have
7 hit points and hit half the time for 1d6+2. Goal: the party wins at least
95% of the time, in about four or five rounds, with someone down at most one
time in three.*

- Neutral baseline, `--order random --target random`, 20000 fights a row
  (seed 1, sampled): against six goblins the party wins 99.5% of the time in
  3.9 rounds with someone down 12.4%; against seven, 96.8% in 4.6 rounds with
  someone down 27.6%; against eight, 88.4% and 47.3%.
- Goblins focusing one character while the party spreads its attacks,
  `--target A=random,B=first`: with six goblins the party still wins 97.1% of
  the time, but someone is down 69.4%; four goblins keep that to 17.6% but
  the fight ends in 2.7 rounds; five already put someone down 43.0% of the
  time.
- Recommendation (invented): seven goblins if they scatter their attacks.
  If they gang up, no number meets all three goals; say so, and offer the
  choice between four (safe but short) and fewer, tougher goblins laddered
  with `--hp-ladder`. Tactics move the risk far more than the win chance, so
  decide how the goblins fight before choosing the number.

## Pitfalls

- **Balancing on the win chance alone** → parties almost always win; the
  risk is someone dropping → read the anyone-down column.
- **One tactic only** → focus fire roughly multiplies the risk → run the
  random and the focused readings.
- **A lone boss that dies in two rounds** → action economy → give it more
  actions or minions, then ladder again.
- **Budget treated as proof** → published budgets assume their own tables
  and tactics → cite them, then check with the ladder.
- **Precision beyond the model** → spells, terrain and healing are not
  simulated → report figures as guides and name what was left out.

## Resources

- [references/encounter-guide.md](references/encounter-guide.md): the goal
  sheet, turning stat blocks into combatants, tactics to test, and the
  failure-mode checklist. Read for steps 1, 2 and 5.
- Scripts used, in `rpg-mechanic-analysis`: `scripts/skirmish.py` (`--count`
  and `--hp-ladder` ladders, turn order, targeting per side, rounds when the
  party wins) and `scripts/rounds_until.py` (exact rounds for one side's
  damage against one target).
- Related: `rpg-mechanic-design` for damage and health pacing in general;
  `rpg-rulings` for rulings during the fight.
