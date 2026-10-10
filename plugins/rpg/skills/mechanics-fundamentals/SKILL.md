---
name: rpg-mechanics-fundamentals
description: >-
  Explains how tabletop RPG resolution mechanics behave as families, without
  needing each game's rulebook: percentile roll-under, d20 against a target,
  bell curves (2d6, 3d6, Fate dice), success-counting pools, highest-die
  pools, step dice, card draws (standard decks and tarot), diceless
  resolution, the layers on top such as advantage, rerolls, exploding dice,
  opposed rolls, degrees of success and resource loops, and what happens over
  a fight or scene: hit points, wounds, armour, stress, initiative, action
  economy, clocks, countdowns and usage dice. Gives the
  concepts, general figures and trade-offs; exact numbers for a particular
  setup come from rpg-mechanic-analysis, and designing to goals from
  rpg-mechanic-design. Use to explain why a kind of mechanic feels swingy or
  what a bonus is generally worth, to compare families (d20 or 2d6, pool or
  target number, hit points or wounds), to explain why fights drag or end too
  fast, or to explain a mechanic to players.
---

# Mechanics fundamentals

How RPG dice mechanics behave as families, so questions can be answered and
mechanics compared or designed from first principles. A mechanic described in
a sentence ("roll a d6 per point of skill, sixes succeed") is enough; no
rulebook is needed. Published games appear only as examples. A claim about
what a particular game's rules say still needs that game's text, or a label
saying it was not checked.

All figures in this skill are exact, from enumeration, and were cross-checked
against rpg-dice or, for cards, by seeded dealing.

## When to use

- Explaining how a kind of mechanic works, or why it feels the way it does.
- Comparing families: d20 against 2d6, pools against target numbers.
- Judging what a modifier, an extra die or advantage is worth.
- Spotting likely problems: swinginess, whiffing, death spirals, dead ranges.

**Not for:** exact numbers for a specific configuration (`rpg-mechanic-analysis`
computes them); designing or tuning a mechanic to goals (`rpg-mechanic-design`);
what a published game's rules actually say (read its text and cite it).

## Workflow

1. **Identify the family** from how the roll is made and read (table below).
   Hybrids are common: note each layer.
2. **Apply the family's properties**: its probability shape, what a modifier
   is worth, where it stops mattering. See
   [references/resolution-families.md](references/resolution-families.md).
3. **Add the layers** on top: advantage, rerolls, explosions, opposed rolls,
   degrees of success. See [references/subsystems.md](references/subsystems.md).
4. **Look across repeated rolls**: compounding, attrition, feedback loops,
   advancement, and the failure-mode checklist. See
   [references/dynamics-and-failure-modes.md](references/dynamics-and-failure-modes.md).
   For fights, harm, armour, initiative and clocks, see
   [references/health-and-time.md](references/health-and-time.md).
5. **Compute when it matters.** Quote the reference figures for general
   points. For a specific setup (this target, these modifiers, this house
   rule) use `rpg-mechanic-analysis`, and say which figures are general and
   which are computed for the case.
6. **Name the trade-offs.** Say what the mechanic rewards, what it makes
   unreliable, and which lever (die count, target, band edges, modifier size)
   would change that.

## The families

| Family | Recognise it by | Shape | What +1 (or one more die) is worth |
|---|---|---|---|
| Percentile roll-under | d100 at or under a rating | Flat | 1 point everywhere; halving costs high ratings most |
| Target number | d20 + modifier at or over a target | Flat | 5 points everywhere, until the target leaves the die's range |
| Bell-curve sum | Sum of several dice against a target or bands | Peaked | Large near the middle, small at the edges (2d6: 16.7 points at the centre, 5.6 at the extremes) |
| Success-counting pool | Count dice at or over a threshold | Binomial | Each extra die adds less to "at least one success", the same to expected successes |
| Highest die | Keep the best die of a pool, read bands | Skewed up | More dice mostly remove the worst band |
| Step dice | Die size encodes the rating | Flat per die | Each step up adds less (d6 to d8 +20.8 points on 6+, d10 to d12 +8.3) |
| Cards | Draws from a deck: orientation, rank, suit, hands | Without replacement: odds shift as the deck thins | Early draws barely differ from dice; hands and suits change the picture |
| Diceless | Comparisons, bids, resource spends | Economic rather than random | Ask how fast resources run out and whether one spend dominates |

## Core ideas

- **Flat against peaked.** On a flat die every +1 is worth the same. On a
  summed curve a +1 matters most near the average and little at the edges, so
  the same bonus helps an average character more than an expert or a no-hoper.
- **Ceilings and floors.** Automatic success or failure bands, and targets
  outside the dice's range, are where modifiers stop doing anything.
- **Chance of success against amount of success.** Pools and degree systems
  separate "did it work" from "how well". A change can move one and not the
  other.
- **Variance is a design choice.** Swingy mechanics favour underdogs and
  drama; tight ones favour competence and planning.
- **One roll is not the scene.** Three rolls at 75% all succeed only 42.2% of
  the time. Count the rolls a goal really needs.

## Pitfalls

- **"+1 is always 5%"** → true only on a flat die → on bell curves, look up
  the value at that target.
- **"More dice is always better"** → not for exploding single dice: an
  exploding d6 reaches 8 more often than an exploding d8 → compute targets
  that matter.
- **Confusing expected successes with the chance of any success** → pools
  report both, and they move differently → state which one is meant.
- **Opposed rolls ignoring ties** → equal d20s favour whoever wins ties by 5
  points → state the tie rule.
- **Judging damage by its average** → hits needed decide fights; a fixed 9
  damage needs four hits on 30 health, no fewer than a fixed 8, while 10
  needs three → count hits at the health values that matter.
- **Two clocks moved by one roll treated as independent** → the same result
  advances both → analyse them as one process, and settle what happens when
  one roll fills both.
- **Treating a deck like dice** → cards drawn are gone, so odds shift and
  decks run out → state whether cards return, and when the deck reshuffles.
- **Naming a published game's rule from memory** → editions differ → check the
  text, or mark the example as unchecked.

## Resources

- [references/resolution-families.md](references/resolution-families.md): each
  family with exact tables. Read for step 2.
- [references/subsystems.md](references/subsystems.md): advantage, rerolls,
  exploding dice, opposed rolls, degrees of success, pushing. Read for step 3.
- [references/dynamics-and-failure-modes.md](references/dynamics-and-failure-modes.md):
  compounding, attrition, feedback, advancement and a failure-mode checklist.
  Read for step 4.
- [references/health-and-time.md](references/health-and-time.md): fight
  length, hit points against wounds, harm you choose, armour, initiative and
  action economy, clocks and usage dice. Read for fights, harm and time
  pressure.
- [references/cards-and-diceless.md](references/cards-and-diceless.md): card
  resolution, oracles and hands; drawing without replacement, orientation,
  decks as budgets, suits; diceless mechanics. Read whenever cards or
  diceless resolution are involved.
- Related: `rpg-mechanic-analysis` for exact numbers on a specific setup;
  `rpg-router` to pick skills.
