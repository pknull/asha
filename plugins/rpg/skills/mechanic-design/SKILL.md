---
name: rpg-mechanic-design
description: >-
  Designs or tunes a tabletop RPG resolution mechanic to stated goals: target
  success chances at each rating, how often partial or critical results occur,
  how much a step of advancement should matter, how swingy it should feel and
  how quick it is to read; and the pacing around it, such as hit points,
  damage, armour and clock sizes set so fights and scenes take the intended
  number of rounds or a race between clocks has the intended chance. Proposes candidate families, sets their parameters,
  checks them with exact rating ladders from bundled scripts (rpg-dice 0.5.0
  or later for dice, rpg-card 0.3.0 or later for cards), and reports
  trade-offs and alternatives. Use to design a dice or card mechanic for a
  game or hack, replace or retune an existing one, make a mechanic less
  swingy, place band edges or target numbers, decide how ratings should
  scale, or size damage, health and clocks to a pace.
---

# Mechanic design

Turns design goals into a concrete resolution mechanic: the dice, the ratings,
the targets or bands, and a rating ladder showing that the goals are met. Every
proposal is checked with exact numbers and labelled as invented material. When
a design adapts a published game's mechanic, that mechanic is cited or marked
unchecked.

## When to use

- "Design a resolution mechanic for my game or hack."
- "Novices should succeed about a third of the time, experts most of the
  time."
- "My mechanic is too swingy, or advancement doesn't feel like it matters."
- Choosing band edges, target numbers, pool sizes or die steps.
- "Fights should last about four rounds", "the alarm should win about half
  the time": sizing health, damage, armour and clocks to a pace.

**Not for:** explaining how a family behaves in general
(`rpg-mechanics-fundamentals`); exact numbers for a mechanic that already
exists (`rpg-mechanic-analysis`); sizing a particular fight
(`rpg-encounter-design`); writing settings or adventures, which stays
with the author (`rpg-router` sends their tables, clocks and fight pacing
to the skills that own them).

## Workflow

1. **Turn the goals into numbers.** Ask only for what is missing; the goal
   sheet in [references/goals-and-levers.md](references/goals-and-levers.md)
   lists the questions. At minimum: the rating range, target chances at the
   bottom, middle and top, which outcomes exist (pass or fail, partial,
   critical), and how quick a roll must be to read.
2. **Shortlist two or three families** whose shape fits: a flat die when every
   step should be worth the same, a summed curve when the middle should be
   stable, a pool when more dice is the fantasy, step dice when ratings are
   die sizes, cards when hands, decks running down or tarot matter. Use
   `rpg-mechanics-fundamentals` for how each behaves.
3. **Set parameters and build the ladder.** Write each candidate as an
   rpg-dice template with `R` for the rating and run
   `scripts/ladder.py TEMPLATE --range LO..HI`. Add `--bands` for outcome
   bands on a total, or `--success-bands 1,2` for a pool read as 0 fail,
   1 partial, 2 or more full. For cards, run `scripts/card_ladder.py` with a
   deck, `--draw` (cards drawn at rating `R`) and a success rule: `--where`
   for matching cards (the condition may use `R`, as in
   `"value + 2*R >= 13"`), or `--best-target` or `--total-target` with an
   optional `--bonus`. Adjust targets, band edges, dice counts, hand sizes or
   rating steps until the ladder meets the goals. For pacing, try each
   candidate health, damage or clock size with `rpg-mechanic-analysis`'s
   `scripts/rounds_until.py` (`--joint` when one roll moves two clocks) and
   tabulate the rounds it takes.
4. **Check before recommending.** The bottom and top ratings, the size of
   each step, dead ranges and automatic results, how often a scene's several
   rolls all succeed, opposed parity if characters roll against each other,
   and handling time. The checklist is in the reference.
5. **Report.** Recommend one candidate:
   - the rule in plain words, labelled as invented (or as a variant of a
     cited published rule);
   - its ladder table;
   - how it meets each goal and where it falls short;
   - the trade-offs against the other candidates;
   - open questions for the designer.

## Example

*Goals: five ratings; novices get at least a partial result about 30% of the
time, typical characters about 60%, experts about 85%; full successes should
be rarer; one quick addition per roll.*

- `scripts/ladder.py "2d6=+R" --range -2..2 --bands 7,10` (exact): partial or
  better is 27.8%, 41.7%, 58.3%, 72.2% and 83.3% from rating -2 to +2, and full
  success (10+) is 2.8% to 41.7%. Each step adds 11.1 to 16.7 points.
- `scripts/ladder.py "Rd6>=5" --range 1..5` (exact): at least one success is
  33.3% to 86.8% over one to five dice, but each die adds less (+22.2 points
  for the second die, +6.6 for the fifth), and two successes are rare at low
  ratings.
- `scripts/ladder.py "1d20=+Rt>=15" --range -1..5`: every step is 5 points,
  so spanning 30% to 85% needs eleven steps or a larger bonus per step.
- Recommendation: 2d6 + rating with bands, invented for this design. It meets
  all three targets in five steps, builds partial results in, and costs one
  addition. Trade-off: steps near the middle matter more than steps at the
  ends, and experts still miss 16.7% of the time.

*Cards: should a rating set hand size or add to the card?* Success is a 10 or
higher from a 52-card deck.

- `scripts/card_ladder.py --deck standard52 --range 1..4 --draw R --where "value >= 10"`
  (exact): 30.8%, 52.5%, 67.7% and 78.2% for hands of one to four. Each extra
  card adds less (+21.7, +15.2, +10.6 points), like a dice pool.
- `scripts/card_ladder.py --deck standard52 --range 0..3 --draw 1 --best-target 10 --bonus R`
  (exact): 30.8% to 53.8%, a flat 7.7 points per step, like a flat die.

*Pacing: two characters each hit half the time for 1d8+3; fights against a
standard foe should last about four rounds and rarely end on the first.*

- `rounds_until.py --target HP --each "1/2:1d8=+3" --each "1/2:1d8=+3"` for
  18, 24 and 30 health (exact): mean 3.1, 3.9 and 4.7 rounds; 80% of fights
  end within rounds 2-5, 2-6 and 3-7; a first-round finish 5.9%, 0% and 0%.
- Recommendation: 24 health. It centres on four rounds, never ends in round
  one, and finishes by round six at least nine times in ten.

*Pacing: a 6-segment heist clock races an alarm clock on one 2d6 roll (10+:
two heist; 7-9: one each; 6 or less: one alarm). The crew should win about
half the time.*

- `rounds_until.py --target 6 --rival-target A --joint "1/6:2|0, 5/12:1|1, 5/12:0|1"`
  for alarm sizes 4 to 8 (exact): the crew finishes first 12.9%, 23.2%,
  35.6%, 48.8% and 61.2% of the time; both fill on the same roll 4.5% to 7.4%.
- An alarm of 7 is the even race. Settle what a same-roll finish means, since
  it is decisive about one time in fourteen.

## Pitfalls

- **Designing on averages** → players feel chances, not means → set targets
  as chances and check them in the ladder.
- **Checking only the typical rating** → novices whiff and experts auto-win →
  always read the bottom and top rows.
- **Steps that barely move the odds** → advancement feels pointless → look at
  the change column; small late steps are a choice, not an accident.
- **Good single rolls, bad scenes** → several rolls compound → check the
  chance that a scene's rolls all succeed.
- **Too many steps per roll** → slow play → count dice, additions and lookups.
- **Presenting a design as a published rule** → label it invented, or cite
  the rule it adapts.
- **Pacing set by average damage** → breakpoints and spread decide fights →
  tabulate rounds for each candidate, not health divided by damage.

## Resources

- `scripts/ladder.py`: exact rating ladders from a template with `R`. Needs
  rpg-dice 0.5.0 or later
  (`pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0"`, or a
  checkout on `PYTHONPATH`). Run with `--help`.
- `scripts/card_ladder.py`: exact rating ladders for card draws. Needs
  rpg-card 0.3.0 or later
  (`pip install "git+https://github.com/pknull/rpg-card.git@v0.3.0"`, or a
  checkout on `PYTHONPATH`). Run with `--help`.
- [references/goals-and-levers.md](references/goals-and-levers.md): the goal
  sheet, family properties for design, levers and their effects, and the
  pre-recommendation checklist. Read at steps 1, 2 and 4.
- Related: `rpg-mechanics-fundamentals` for family behaviour,
  `rpg-mechanic-analysis` to analyse the finished mechanic in depth,
  `rpg-router` to pick skills.
