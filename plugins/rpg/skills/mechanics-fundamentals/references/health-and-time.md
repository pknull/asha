# Health, harm and time pressure

How damage, health, initiative and clocks behave over a fight or a scene. The
examples are invented to show each family; none claims to state a published
game's rules. Figures are exact: fights, harm, armour, initiative and clocks
come from `rounds_until.py` in `rpg-mechanic-analysis` (its `--joint` mode for
two clocks moved by one roll), and usage dice from summing each step's exact
expected length. Fight lengths, duel odds and the clock race were also checked
by seeded simulation or a separate exact count.

## Fight length is a total over a rate

A foe goes down when the damage dealt reaches its health. The average number
of rounds is roughly health divided by damage per round (hit chance times
average damage), but the spread around that average matters as much.

One attacker hitting half the time for 1d8+3 (3.75 damage a round):

| Health | Mean rounds | 80% of fights end in rounds | Over in round 1 |
|---|---|---|---|
| 8 | 3.0 | 1-6 | 25.0% |
| 15 | 4.9 | 2-8 | 0.0% |
| 30 | 9.0 | 5-13 | 0.0% |
| 60 | 17.0 | 12-23 | 0.0% |
| 120 | 33.0 | 25-41 | 0.0% |

- **Low health against damage is swingy.** At 8 health a quarter of fights
  end on the first swing, and some last six rounds.
- **High health is predictable.** The spread grows more slowly than the mean,
  so long fights land close to their average. Past a point that becomes a
  slog: many rolls, little doubt.
- **Health and damage scale together.** If both grow with advancement, fight
  length stays put; if health outgrows damage, fights drag.

### Same average, different spread

Three attacks that each average 4.5 damage a round, against 30 health:

| Attack | Mean rounds | 80% end in rounds | Done in 3 rounds | Done in 10 rounds |
|---|---|---|---|---|
| Hits 90% for 5 | 6.7 | 6-8 | 0.0% | 99.8% |
| Hits 50% for 9 | 8.0 | 5-12 | 0.0% | 82.8% |
| Hits 30% for 15 | 6.7 | 3-12 | 21.6% | 85.1% |

- Reliable small hits give fights of a predictable length; rare big hits give
  sudden endings and long whiffs.
- **Breakpoints.** The 9-damage attack is the slowest despite the same
  average: three hits leave 3 health, so it needs a fourth. When damage is
  chunky relative to health, the number of hits needed, not the average,
  decides the fight. Check the hits needed at each health value that matters.

## Health models

| Model | How it works | Rewards | Watch for |
|---|---|---|---|
| Hit points | Damage subtracts from a pool | Steady, legible attrition | Slogs; one-shots when damage outgrows health |
| Thresholds | A hit that meets toughness causes wounds; a few wounds take you out | Big hits; armour that matters | Weak attacks that can never wound |
| Wound track with penalties | Each wound also worsens your rolls | Retreat, caution | Death spirals |
| Harm you choose | Players spend stress or a resource to avoid harm | Player agency, dramatic trade-offs | Budgets that run out too fast or never |
| Conditions | Named effects (shaken, bleeding) instead of numbers | Fiction-first play | Unclear stacking and removal |

### Thresholds and wound counts

Hits land half the time for 2d6 against toughness 7; 7-10 is one wound, 11 or
more is two, and three wounds take the target out:

- Per hit: no wound 41.7%, one wound 50.0%, two wounds 8.3%.
- Rounds to three wounds: mean 9.4, 80% within 4-16 rounds.
- The same attack against 18 hit points takes 6.1 rounds (3-10); against 21,
  7.0 rounds (4-11).

A threshold model throws away every hit below toughness, so fights run longer
and swing more. A 1d6 weapon against toughness 7 never wounds at all (a dead
range), while against 21 hit points it still finishes in 13.0 rounds on
average. Thresholds make weapon choice and armour decisive; hit points make
every hit count a little.

### Wound penalties and death spirals

When each wound lowers the chance to hit back, the side that falls behind
falls further. The pool example in
[dynamics-and-failure-modes.md](dynamics-and-failure-modes.md) shows the
shape. The two sides' fights are then linked, so fight length needs a seeded
simulation of both sides rather than the exact independent calculation.

### Harm you choose

When a player may spend stress to avoid harm, the question is how many
resists the budget buys. A track of 8, where the resist that takes it past 8
breaks the character:

| Cost per resist | Mean safe resists | Four safe resists |
|---|---|---|
| 1d4 | 3.0 | 25.8% |
| 1d6 | 2.0 | 5.4% |
| 2d4 | 1.3 | 0.0% |

The player chooses when to spend; the budget sets how often the choice is
real. A cost so high that two resists exhaust the track makes the choice rare;
a cost so low that resisting is always safe removes it.

### Armour: avoidance or reduction

Armour either makes hits rarer (avoidance) or makes each hit smaller
(reduction). Base hit chance 11/20 against 20 health; the armour is -2 to hit
or -2 damage per hit (never below 0):

| Weapon | Armour | Damage a round | Mean rounds |
|---|---|---|---|
| 1d8+3 | none | 4.13 | 5.7 |
| 1d8+3 | avoidance | 3.38 | 7.0 |
| 1d8+3 | reduction | 3.03 | 7.5 |
| 1d4 | none | 1.38 | 15.3 |
| 1d4 | avoidance | 1.13 | 18.7 |
| 1d4 | reduction | 0.41 | 49.3 |

Avoidance cuts every weapon by the same fraction. Reduction barely touches
big hits and erases small ones: here it removes 27% of the 1d8+3's damage and
70% of the 1d4's. Reduction rewards heavy weapons and makes swarms of weak
attackers harmless.

## Who acts first

Two equal sides, each hitting half the time for 1d6. The first column is how
long one side takes to drop the other if unopposed:

| Health each | Mean rounds | First mover wins | Both down, if simultaneous |
|---|---|---|---|
| 3 | 2.7 | 60.7% | 21.4% |
| 6 | 4.3 | 56.5% | 13.1% |
| 12 | 7.8 | 54.6% | 9.2% |
| 24 | 14.7 | 53.2% | 6.4% |
| 48 | 28.4 | 52.2% | 4.5% |

- **Going first matters most in short fights.** In a fight of two or three
  rounds it is worth more than ten points; in a long one, about two.
- **Order formats.** Fixed order rolled once gives one side that edge for the
  whole fight. Rerolling each round evens it out over the fight. Side
  initiative concentrates actions, so alpha strikes grow. Simultaneous
  resolution replaces the edge with mutual knockouts, common in short fights.
- **Action economy beats almost everything.** Against 30 health with attacks
  hitting half the time for 1d8+3: one attacker needs 9.0 rounds, two need
  4.7, four need 2.6. A side with twice the actions usually wins regardless of
  quality, which is why solo foes need extra actions or ways to act out of
  turn. Losing a combatant mid-fight cuts that side's actions, a feedback that
  needs simulation (`skirmish.py` in `rpg-mechanic-analysis`).

## Clocks and countdowns

### Progress clocks

A clock fills as actions succeed. With 2d6 bands where 10+ fills two
segments, 7-9 fills one, and 6 or less fills none:

| Segments | Mean actions | Done within 4 | Done within 8 |
|---|---|---|---|
| 4 | 5.6 | 35.5% | 89.2% |
| 6 | 8.3 | 4.5% | 58.7% |
| 8 | 11.0 | 0.1% | 22.9% |

Clock size is a time budget: count how many actions the scene can afford
before choosing it.

### Two clocks driven by one roll

Often one roll moves two clocks: success fills progress, failure fills
danger. With 10+ adding two progress, 7-9 one of each, and 6 or less one
danger:

| Progress | Danger | Progress first | Danger first | Same roll |
|---|---|---|---|---|
| 4 | 4 | 35.7% | 55.3% | 9.0% |
| 6 | 4 | 12.9% | 82.6% | 4.5% |
| 6 | 6 | 35.6% | 57.1% | 7.3% |
| 8 | 6 | 15.5% | 80.2% | 4.3% |

The clocks are not independent: one 7-9 moves both. Treating them as two
separate races gives wrong odds (5.7% for progress at 6 against 4, not
12.9%); analyse them as one process. Decide, and say, what happens when one
roll fills both clocks: the same-roll column is often large enough to change
the verdict. Danger advances
on five rolls in six while progress averages 0.75 segments a roll, so equal
clocks favour danger, and two segments added to either clock move the race by
about twenty points.

### Usage and countdown dice

A usage die is rolled each time a resource is used; on 1-2 it steps down
(d20, d12, d10, d8, d6, d4) and after the d4 the resource is gone. Each step
lasts half its die size on average, so the mean uses add up:

| Starting die | Mean uses | At least 5 uses |
|---|---|---|
| d4 | 2 | 6.2% |
| d6 | 5 | 46.8% |
| d8 | 9 | 87.8% |
| d12 | 20 | 100.0% |
| d20 | 30 | 100.0% |

Usage dice replace bookkeeping with uncertainty: nobody knows when the torches
run out, only that bigger dice last longer. Doom pools and escalation dice
are the reverse: a pool that grows each round or each failure, spent by the
GM, so tension rises on a schedule the table can see.

## Failure modes

| Failure mode | Symptom | Check |
|---|---|---|
| One-shot | Fights end before choices matter | Chance of ending in round 1 at typical health |
| Slog | Many rounds with a foregone result | Mean rounds and spread as health grows |
| Breakpoint | An upgrade changes nothing | Hits needed at each health value, not average damage |
| Dead range | Some attacks can never hurt | Thresholds and reduction against the weakest attacks |
| Alpha strike | Whoever acts first wins | First-mover advantage at the expected fight length |
| Action economy | Numbers beat quality | Rounds to defeat with one, two and four attackers |
| Death spiral | The first wound decides the fight | Hit chance at each wound level; simulate both sides |
| Clock mismatch | Clocks fill too fast or never | Mean actions against the actions the scene allows |

## Computing a specific case

Use `rpg-mechanic-analysis`: its `rounds_until.py` takes a target (health,
clock segments, stress track), each round's actions as chance and amount, and
an optional rival side, and gives exact rounds-to-finish, the mean and median,
and who wins a race; `--joint` handles one roll moving both sides. Fights
with several combatants a side need its `skirmish.py`, and other feedback
between rounds (wound penalties, healing) a custom seeded simulation.
