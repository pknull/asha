---
name: rpg-mechanic-analysis
description: >-
  Computes exact numbers for a particular tabletop RPG dice mechanic,
  described by the user or taken from a published game: success chances,
  outcome bands, the effect of a bonus, advantage, reroll, push, exploding
  die, card draw, hand or house rule, how stress, sanity, hit points or a
  usage die move over repeated rolls, how many rounds a fight, clock or
  journey takes, who wins a fight or a race between clocks and what going
  first is worth, and failure modes such as death spirals and dead ranges.
  Uses rpg-dice (0.5.0 or later) for dice and rpg-card (0.3.0 or later) for
  card draws; published rules are cited or marked unchecked. Use for what are
  the odds, how likely, which of these options is better, what does this push
  cost, how long will this fight last, or is this specific mechanic broken.
  For general explanations without a specific setup, use
  rpg-mechanics-fundamentals. A rules question with no numbers belongs to
  rpg-router.
---

# Mechanic analysis

Computes how a specific dice mechanic behaves: how likely each result is,
what a rule change does, where repeated rolls lead, and where the mechanic
breaks. The mechanic can come from the user's description; no rulebook is
needed for that. A rulebook matters only when the question is what a
published game says, and then the rule is cited or marked unchecked. Worked
examples come from Basic Roleplaying (2023 ORC), the D&D SRD 5.2.1, the Fate
Core SRD and the Year Zero Engine SRD 1.0.

## When to use

- "What are the odds of ...", "how likely", "how swingy", "is advantage worth
  it", "how many dice do I need".
- Comparing a published rule with an optional, variant or house rule.
- Resource loops: stress, sanity, hit points, pushing, experience, attrition.
- "How many rounds to drop this foe", "who wins this duel", "how fast does
  this clock fill".
- "Is this broken": death spirals, dominant strategies, ranges where
  modifiers stop mattering.

**Not for:** general explanations of how a family of mechanics behaves
(`rpg-mechanics-fundamentals`); a plain rules lookup with no analysis (answer
it directly, with a citation); designing a new mechanic to goals
(`rpg-mechanic-design`).

## Workflow

1. **Pin the mechanic.** Restate it precisely: dice, what counts as a
   success, modifiers, bands, rerolls, costs. Identify its family
   (`rpg-mechanics-fundamentals`). If the user described it, that description
   is the mechanic; ask about any gap that changes the answer. When the user
   cannot be asked, or the readings are few, compute each reading and report
   them side by side instead.
2. **Check published rules only when they matter.** If the question is about
   a named game's rules, name the edition and read the passage where it is:
   the user's copy or the publisher's official free text. Do not download or
   copy rulebooks. Cite the page or section heading, or mark the rule
   unchecked. Label rules as core, optional, variant, house or invented. Where
   a text is ambiguous or contradicts itself, list the readings, choose one,
   and say what changes under the others.
3. **Write the dice in rpg-dice notation** (0.5.0 or later). Each term runs:
   dice, `+N` per die, `=+N` total, success, then methods such as `kh`, `x`,
   `f`, then a total check `t`. Terms add (`1d8+1d6`); parentheses group them
   (`(1d10>=6)+(1d8>=6)`), and a total check across several terms needs a
   group (`(1d20+1d4)t>=10`). Game meaning stays outside the notation: what a
   face counts as, outcome bands, which dice a push rerolls.
4. **Compute.** Run `scripts/dice_odds.py EXPR [EXPR ...]`. Figures are exact,
   explosions included; chains beyond the depth limit are reported, and each
   chance can be higher by up to that much. A seeded sample checks them. For outcome bands on a total, pass the band
   edges: `--bands 7,10` gives 6 or less, 7-9 and 10 or more. For rules that read individual faces
   (10+ counts two, which dice to reroll), use exact face distributions or a
   seeded simulation, both in [references/simulation.md](references/simulation.md).
   For card draws (decks, hands, orientation, deck state), use rpg-card's exact
   `Odds` and seeded dealing as shown in [references/cards.md](references/cards.md).
5. **Analyse what was asked:**
   - *Odds:* the distribution, the chance of each outcome band, the mean.
   - *Variant impact:* run base and variant together; report the differences.
   - *Resource dynamics:* expected change per roll, and how many rolls until a
     threshold (exhaustion, insanity, mastery). When each round adds an
     independent amount to a total (damage to health, segments to a clock,
     stress to a track), `scripts/rounds_until.py` gives exact rounds to
     finish, and races two sides with `--rival-target`. When one roll moves
     both sides (progress and danger clocks), race them with `--joint`; the
     independent race would be wrong. Ask what happens when one roll finishes
     both. For fights with several combatants a side, where a fallen
     combatant stops acting, run `scripts/skirmish.py` (seeded; turn order
     and targeting are options, so report the readings that matter). Other
     feedback (wound penalties, healing, fleeing) needs the custom seeded
     simulation in [references/simulation.md](references/simulation.md).
   - *Failure modes:* check the extremes (lowest and highest ratings, zero
     dice, caps), feedback loops, dominant choices, and ranges where a
     modifier changes nothing.
6. **Report.** Lead with the answer. If any rule behind it is unchecked,
   say so in the first sentence, before any figure, and say what would change
   the answer. For each number say whether it is exact or sampled (with sample
   size, seed and standard error). For published
   rules, give the citation or "unchecked", the rule class, and the reading
   used with what changes under the others. List what was not modelled.

## Examples

*"I roll a d6 per point of skill; any 6 succeeds and each 1 costs a point of
stress. With skill 4, what does a fifth die buy?"*

- No rulebook needed: the description is the mechanic, a success-counting
  pool succeeding on 6 only.
- Run `scripts/dice_odds.py "4d6>=6f<=1" "5d6>=6f<=1"`.
- Result (exact): the chance of any success rises from 51.8% to 59.8% (+8.0
  points). Sixes and ones are equally likely, so the chance of taking stress
  rises by the same amount, and expected stress per roll goes from 0.67 to
  0.83. The fifth die buys as much risk as success.

*With a +5 modifier against DC 15 in D&D 2024, what is advantage worth?* This
names a published game, so its rules are read and cited.

- Rule: a D20 Test is 1d20 plus modifiers and succeeds at or above the target
  (SRD 5.2.1 p. 6, core). Advantage rolls two d20s and keeps the higher, and
  several sources still give two dice (pp. 7-8, core).
- Run `scripts/dice_odds.py "1d20=+5t>=15" "2d20=+5kh1t>=15"`.
- Result (exact): 11/20 = 0.55 without advantage, 319/400 = 0.7975 with it,
  so advantage adds 24.75 percentage points at this target.

*"My fighter hits half the time for 1d8+3, with a 1-in-20 critical for
2d8+3. How long does a 5d8+10 hit point ogre last, and what if two of us
attack?"* No published rule is claimed; the user's description is the
mechanic.

- Run `scripts/rounds_until.py --target "5d8=+10" --each "9/20:1d8=+3, 1/20:2d8=+3"`,
  then again with a second, identical `--each` for the other attacker. The
  normal-hit chance is 9/20 because the critical is part of the half that
  hits.
- Result (exact): a median of 9 rounds alone (mean 9.17) and 5 with two
  attackers (mean 4.84). Say what was not modelled, such as the ogre hitting
  back. `--rival-target` and `--rival-each` add that as a race for one
  fighter; with two, the race only says whether the ogre drops before its
  target does, so simulate the whole fight with `scripts/skirmish.py`, once
  per targeting and turn-order reading.

## Pitfalls

- **`2d6+3` comes out 3 too high per die.** In rpg-dice, `+N` after the dice
  adds to each die. Use `2d6=+3` for a total modifier; the script notes this.
- **An expression is refused.** rpg-dice rejects tokens out of order, a total
  check after several ungrouped terms, and a bare `d20` without a count. The
  script prints rpg-dice's reason; fix the order or add a group.
- **Odds look wrong on an older rpg-dice.** Before 0.5.0, pool sums such as
  `1d8+1d6` and dice used as values (`=+1d4`) gave wrong exact odds. The
  script refuses to run on versions without the exact engine.
- **`rounds_until.py` or `skirmish.py` refuses an exploding amount** → it
  explodes so often (`1d10x>=2`) that over one outcome in ten thousand lies
  beyond the depth limit → a higher `dice_odds.py --explode-depth` narrows
  one roll's odds (the unresolved share is reported); fight length needs a
  custom simulation that rolls every explosion
  ([references/simulation.md](references/simulation.md)).
- **Rerolled dice look unchanged in `natural`.** `natural` is each die's first
  roll, before rerolls and without exploded dice.
- **Numbers that look plausible but are wrong.** Mental arithmetic on
  distributions fails. Compute, or label the figure as an estimate.
- **Editions blur together.** Rules change between editions; cite the one
  used and do not fill gaps from another.
- **A fight is modelled as independent rounds when it is not.** Wound
  penalties, healing and lost combatants link rounds, and `rounds_until.py`
  would miss that → simulate both sides.
- **Two clocks raced as independent sides when one roll moves both** → the
  race is badly wrong → use `--joint`.
- **A contradiction in the source gets resolved silently.** State both
  readings; an example is the BRP fumble range in
  [references/pilot-notes.md](references/pilot-notes.md).

## Resources

- `scripts/dice_odds.py`: odds for one or more expressions, with comparison.
  Needs rpg-dice 0.5.0 or later
  (`pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0"`, or a
  checkout on `PYTHONPATH`). Run with `--help`.
- `scripts/rounds_until.py`: exact rounds until a running total reaches a
  target (fight length, clocks, stress), with rolled targets, critical hits,
  several actions a round, two-sided races and shared-roll races. Run with
  `--help`.
- `scripts/skirmish.py`: seeded fights between two sides of any size, with
  turn order and targeting options, outcomes drawn from rpg-dice's exact
  distributions. Run with `--help`.
- [references/pilot-notes.md](references/pilot-notes.md): checked findings for
  BRP, D&D 5.2.1, Fate Core and Year Zero Engine. Read when analysing those
  systems.
- [references/simulation.md](references/simulation.md): seeded simulation for
  multi-roll rules and resource loops.
- [references/cards.md](references/cards.md): exact card odds and seeded
  dealing with rpg-card 0.3.0 or later
  (`pip install "git+https://github.com/pknull/rpg-card.git@v0.3.0"`). Read
  whenever the mechanic uses cards.
- Related: `rpg-mechanics-fundamentals` for how each family behaves in
  general; `rpg-router` picks skills.
