# Resolution families

Figures are exact. "Checked example" means the rule was read in the cited
text. "Also seen in" names games from general knowledge, not checked against
their texts here; verify before relying on the detail.

## Percentile roll-under

Roll d100; succeed at or under the rating. The chance is the rating, so each
point of rating is one percentage point.

- **Modifiers.** Flat modifiers move every rating by the same amount.
  Multiplying (halving, doubling) moves high ratings more:

  | Rating | Halved | Flat -20 |
  |---|---|---|
  | 30% | 15% (-15) | 10% |
  | 50% | 25% (-25) | 30% |
  | 80% | 40% (-40) | 60% |

- **Degrees.** Fractions of the rating give better results (for example 1/5
  and 1/20), so degree chances scale with the rating too.
- **Bands.** A fixed failure band at the top (for example 96-00) caps the
  chance below 100%.
- Checked example: Basic Roleplaying (2023 ORC): roll under, criticals at 1/20
  and specials at 1/5 of the rating, 96+ always fails, Difficult halves the
  rating (pp. 127-128, 132).

## Target number on a single die (d20)

Roll d20, add modifiers, succeed at or over the target. Each +1 is worth 5
points until the target is out of reach (above 20 + modifier) or automatic.

- Where a game makes a natural 20 always succeed and a natural 1 always fail,
  a 5% chance remains at each end.
- Rolling two and keeping one (advantage) is covered in subsystems.md.
- Checked example: D&D SRD 5.2.1 D20 Tests (p. 6).

## Bell-curve sums

Several dice are summed, so middle totals are common and extremes rare. A +1
shifts the target by one, and its value is the chance of the total that was
just brought into reach.

**2d6, chance of at least t, and what +1 adds at that target:**

| t | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|
| P(at least t) | 91.7% | 83.3% | 72.2% | 58.3% | 41.7% | 27.8% | 16.7% | 8.3% | 2.8% |
| +1 adds | 5.6 | 8.3 | 11.1 | 13.9 | 16.7 | 13.9 | 11.1 | 8.3 | 5.6 |

**2d6 + stat with three bands (10+ full, 7-9 partial, 6- miss):**

| Stat | 10+ | 7-9 | 6- |
|---|---|---|---|
| -1 | 8.3% | 33.3% | 58.3% |
| +0 | 16.7% | 41.7% | 41.7% |
| +1 | 27.8% | 44.4% | 27.8% |
| +2 | 41.7% | 41.7% | 16.7% |
| +3 | 58.3% | 33.3% | 8.3% |

The partial band stays large across ordinary stats, so most rolls give mixed
results. Also seen in (unchecked): many Powered by the Apocalypse games.

**3d6 roll-under, chance of t or less, and what +1 adds:**

| t | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| P(t or less) | 9.3% | 16.2% | 25.9% | 37.5% | 50.0% | 62.5% | 74.1% | 83.8% | 90.7% | 95.4% | 98.1% |
| +1 adds | 6.9 | 9.7 | 11.6 | 12.5 | 12.5 | 11.6 | 9.7 | 6.9 | 4.6 | 2.8 | 1.4 |

Also seen in (unchecked): GURPS.

**Four Fate dice (each -1, 0 or +1):**

| Total | -4 | -3 | -2 | -1 | 0 | +1 | +2 | +3 | +4 |
|---|---|---|---|---|---|---|---|---|---|
| Exactly | 1.2% | 4.9% | 12.3% | 19.8% | 23.5% | 19.8% | 12.3% | 4.9% | 1.2% |
| At least | 100% | 98.8% | 93.8% | 81.5% | 61.7% | 38.3% | 18.5% | 6.2% | 1.2% |

Checked example: Fate Core SRD (Taking Action > Rolling the Dice; The Four
Outcomes): skill + 4dF against opposition, shifts decide fail, tie, success,
or success with style. Reading ordinary d6s as Fate dice (5-6 plus, 1-2
minus; What You Need to Play) gives identical odds.

## Success-counting pools

Roll a pool and count dice at or over a threshold; one or more successes
usually succeeds, extra successes improve the result.

**Chance of at least one success by pool size:**

| Success on | 1 | 2 | 3 | 4 | 5 | 6 | 8 | 10 | Expected per die |
|---|---|---|---|---|---|---|---|---|---|
| 6 only | 16.7% | 30.6% | 42.1% | 51.8% | 59.8% | 66.5% | 76.7% | 83.8% | 0.167 |
| 5+ | 33.3% | 55.6% | 70.4% | 80.2% | 86.8% | 91.2% | 96.1% | 98.3% | 0.333 |
| 4+ | 50.0% | 75.0% | 87.5% | 93.8% | 96.9% | 98.4% | 99.6% | 99.9% | 0.500 |

- The chance of any success rises with diminishing returns; expected
  successes rise by the same amount per die.
- The threshold sets the scale: a coin flip needs 4 dice at 6-only but 2 at
  5+.
- Exploding the top face adds successes: d10 succeeding on 8+ with 10s
  rolling again averages 0.333 successes per die instead of 0.300.
- Checked example: Year Zero Engine SRD v1.0 dice pool: d6s for attribute plus
  skill, each 6 a success (p. 8). Also seen in (unchecked): Shadowrun (5+),
  Chronicles of Darkness (d10, 8+, 10 again).

## Highest die

Roll a pool, keep the best die, read it against bands. On d6 with 6 full,
4-5 partial and 1-3 bad:

| Dice | 6 | 4-5 | 1-3 | Two or more 6s |
|---|---|---|---|---|
| 1 | 16.7% | 33.3% | 50.0% | 0% |
| 2 | 30.6% | 44.4% | 25.0% | 2.8% |
| 3 | 42.1% | 45.4% | 12.5% | 7.4% |
| 4 | 51.8% | 42.0% | 6.2% | 13.2% |
| 5 | 59.8% | 37.1% | 3.1% | 19.6% |
| 6 | 66.5% | 31.9% | 1.6% | 26.3% |

Extra dice mostly remove the bad band; the partial band stays common. Also
seen in (unchecked): Blades in the Dark and Forged in the Dark games.

## Step dice

The rating is a die size. Bigger dice raise the chance of each threshold, but
by less at each step.

| Die | 6 or more | 10 or more |
|---|---|---|
| d6 | 16.7% | 0% |
| d8 | 37.5% | 0% |
| d10 | 50.0% | 10.0% |
| d12 | 58.3% | 25.0% |

**Two dice, a success on 6+, two on 10+ (chance of any success / expected
successes):**

| | d6 | d8 | d10 | d12 |
|---|---|---|---|---|
| d6 | 30.6% / 0.33 | 47.9% / 0.54 | 58.3% / 0.77 | 65.3% / 1.00 |
| d8 | 47.9% / 0.54 | 60.9% / 0.75 | 68.8% / 0.97 | 74.0% / 1.21 |
| d10 | 58.3% / 0.77 | 68.8% / 0.97 | 75.0% / 1.20 | 79.2% / 1.43 |
| d12 | 65.3% / 1.00 | 74.0% / 1.21 | 79.2% / 1.43 | 82.6% / 1.67 |

Checked example: Year Zero Engine SRD v1.0 step dice: ratings A-D are d12 to
d6, 6+ succeeds, 10+ counts two (pp. 5, 8). Also seen in (unchecked): Cortex.

## Cards and diceless

Cards remember what has been drawn, which dice never do. Their behaviour,
with exact figures and checked examples, is in cards-and-diceless.md.
