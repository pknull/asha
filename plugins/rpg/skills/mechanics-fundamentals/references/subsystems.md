# Subsystems layered on a roll

Figures are exact. Checked examples were read in the cited text; "also seen
in" examples were not checked.

## Advantage and disadvantage (roll twice, keep one)

For a pass or fail roll with base chance p, advantage gives 1-(1-p)² and
disadvantage p². The gain is largest at a coin flip.

| Base chance | Advantage | Gain | Worth about | Disadvantage |
|---|---|---|---|---|
| 10% | 19.0% | +9.0 | +1.8 on a d20 | 1.0% |
| 25% | 43.8% | +18.8 | +3.8 | 6.2% |
| 40% | 64.0% | +24.0 | +4.8 | 16.0% |
| 50% | 75.0% | +25.0 | +5.0 | 25.0% |
| 60% | 84.0% | +24.0 | +4.8 | 36.0% |
| 75% | 93.8% | +18.8 | +3.8 | 56.2% |
| 90% | 99.0% | +9.0 | +1.8 | 81.0% |

The same formula applies to any pass or fail roll, not just d20s. Checked
example: D&D SRD 5.2.1 Advantage/Disadvantage (pp. 7-8), where several
sources still give two dice and having both gives one.

## Rerolls

- "Reroll a failure once" has the same pass chance as advantage: 1-(1-p)².
- Rerolling only some dice (for example everything but a locked face) gives
  less than that; compute the case.
- A reroll that costs something turns a probability question into a choice:
  report the gain and the price separately.

## Exploding dice

A die that rolls again and adds on its top face has a higher mean and an
unbounded tail, but it can never total exactly its own top face, which creates
dead spots.

| Die | Mean | 6+ | 7+ | 8+ | 9+ | 10+ | 12+ |
|---|---|---|---|---|---|---|---|
| d4 | 3.33 | 18.8% | 12.5% | 6.2% | 6.2% | 4.7% | 1.6% |
| d6 | 4.20 | 16.7% | 16.7% | 13.9% | 11.1% | 8.3% | 2.8% |
| d8 | 5.14 | 37.5% | 25.0% | 12.5% | 12.5% | 10.9% | 7.8% |
| d10 | 6.11 | 50.0% | 40.0% | 30.0% | 20.0% | 10.0% | 9.0% |
| d12 | 7.09 | 58.3% | 50.0% | 41.7% | 33.3% | 25.0% | 8.3% |

- A bigger die is not always better: an exploding d6 reaches 8 more often
  than an exploding d8 (13.9% against 12.5%), and an exploding d10 reaches 12
  more often than an exploding d12 (9.0% against 8.3%).
- Reaching N or N+1 on an exploding dN is the same chance, because a roll of
  N always continues.
- Also seen in (unchecked): Savage Worlds.

## Opposed rolls

Both sides roll and compare. The difference of two equal dice is peaked, and
the tie rule matters.

**d20 + m against d20 (attacker wins; ties to defender / ties to attacker):**

| m | 0 | 1 | 2 | 3 | 5 | 8 | 10 |
|---|---|---|---|---|---|---|---|
| Win | 47.5% / 52.5% | 52.5% / 57.2% | 57.2% / 61.8% | 61.8% / 66.0% | 70.0% / 73.8% | 80.5% / 83.5% | 86.2% / 88.8% |

Equal sides are not 50-50: whoever wins ties gets a 5-point edge on d20s.

## Degrees of success

- **Bands on a total** (10+, 7-9, 6-): set by band edges; see the 2d6 table
  in resolution-families.md.
- **Margin over a target** (shifts): each point of margin is one band step;
  on a peaked curve large margins are rare.
- **Fractions of a rating** (1/5, 1/20 on d100): degree chances grow with the
  rating.
- **Extra successes in a pool**: expected successes per die times the pool
  size.

## Pushing and press-your-luck

A second roll that improves the result at a risk. Analyse it as two parts:
the chance the push turns failure into success, and the cost distribution it
adds. Which dice may be rerolled changes both. Checked example: Year Zero
Engine SRD v1.0 pushing (p. 9), where the text supports two readings of which
dice stay locked; `rpg-mechanic-analysis` has the computed difference.
