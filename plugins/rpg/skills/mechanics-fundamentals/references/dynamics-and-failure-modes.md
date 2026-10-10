# Dynamics and failure modes

What happens across many rolls, and the common ways mechanics go wrong.
Figures are exact.

## Compounding

A goal that needs several rolls in a row succeeds far less often than any
single roll:

| Chance per roll | 1 roll | 2 rolls | 3 rolls | 5 rolls |
|---|---|---|---|---|
| 90% | 90.0% | 81.0% | 72.9% | 59.0% |
| 75% | 75.0% | 56.2% | 42.2% | 23.7% |
| 60% | 60.0% | 36.0% | 21.6% | 7.8% |
| 50% | 50.0% | 25.0% | 12.5% | 3.1% |

Count the rolls an outcome really needs before judging a mechanic's odds.

## Attrition and death spirals

When losses reduce the chance to avoid further losses, the mechanic spirals.
Example: a d6 pool succeeding on 6, losing a die per wound:

| Dice left | 5 | 4 | 3 | 2 | 1 |
|---|---|---|---|---|---|
| Chance of a success | 59.8% | 51.8% | 42.1% | 30.6% | 16.7% |

To detect a spiral, compute the chance of success at each resource level. If
it falls as the resource falls, losses accelerate. Wound penalties, dice pools
tied to health, and stress that reduces ratings all do this.

## Positive feedback

Some mechanics make risk raise power: dice added as stress builds add
successes while also adding the chance of a bad result. Report both curves;
the mechanic is working as intended only if the risk catches up.

## Advancement

- **Improve by use, roll over the rating.** Improvement gets harder as the
  rating rises, so growth slows by itself. With a d100 + 7 needing to beat the
  rating and 1D6 gained on success:

  | Rating | Chance to improve |
  |---|---|
  | 30 | 77.0% |
  | 50 | 57.0% |
  | 70 | 37.0% |
  | 90 | 17.0% |

  Expected improvement checks: 9.1 to climb 30 to 50, 13.0 for 50 to 70, 23.0
  for 70 to 90. Checked example: Basic Roleplaying (2023 ORC) experience
  rolls (p. 138), with INT 13 giving the +7 bonus.
- **Fixed costs** (points per level) grow at a steady rate; any slowing comes
  from rising costs.

## Failure-mode checklist

| Failure mode | Symptom | How to check |
|---|---|---|
| Whiffing | Turns pass with nothing happening | Chance of no effect per action, and per round with compounding |
| Dead range | A modifier changes nothing | Is the target outside what the dice can reach, or already automatic? |
| Ceiling or floor | Experts and novices converge | Automatic success or failure bands; compare top and bottom ratings |
| Swinginess | Experts lose to novices too often | Opposed odds at realistic rating gaps |
| Death spiral | Losing makes losing likelier | Success chance at each resource level |
| Runaway leader | Winning makes winning likelier | The same check in reverse |
| Dominant choice | One option is always best | Compare options across the plausible range of ratings |
| Too many rolls | Good odds per roll, bad odds per goal | Compounding table above |
| Handling time | Rolls are slow to read | Count steps per roll: dice to sort, numbers to add, bands to check |
