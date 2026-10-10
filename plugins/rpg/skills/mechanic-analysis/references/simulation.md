# Face-level rules and multi-roll simulation

Two patterns for rules the notation cannot express by itself. Both need
rpg-dice 0.5.0 or later.

## Exact: rules that read individual faces

When a rule depends on which faces came up (10+ counts two, which dice may be
rerolled), ask rpg-dice for the exact distribution of kept faces and apply the
rule to each outcome:

```python
from dice_roller.DiceProbability import DiceProbability

# Step dice, d10 and d8: a face of 6+ is one success, 10+ is two (YZE SRD p. 8).
result = DiceProbability().exact("(1d10)+(1d8)", faces=True)
expected = sum(p * sum((f >= 6) + (f >= 10) for f in kept)
               for (kept, natural, outcome), p in result.faces.items())
print(expected)  # 39/40, exact
```

Keys are (sorted kept faces, sorted natural faces, outcome). Pools are limited
to `faces_limit` multisets (default 250,000: about 28d6, 11d10 or 6d20).

## Seeded simulation: rules that need several rolls

Use this when a rule needs more than one roll, such as a push, an opposed
roll, or a resource that changes over many rolls. Roll with rpg-dice's
`DiceThrower`, apply the game's rules in plain Python, and report the sample
size, seed and standard error.

```python
import math
import random
from dice_roller.DiceThrower import DiceThrower

random.seed(1)  # DiceThrower uses the module-level random
throw = DiceThrower().throw

def faces(n, sides=6):
    """Natural faces of n dice; empty for n == 0."""
    return throw(f"{n}d{sides}")["natural"] if n > 0 else []

def yze_push(attribute, skill, lock_all_ones=True):
    """YZE dice pool with one push after a failed roll (SRD pp. 8-9)."""
    a, s = faces(attribute), faces(skill)
    if 6 in a + s:
        return True
    a = [f if f == 1 else faces(1)[0] for f in a]
    s = [f if (f == 1 and lock_all_ones) else faces(1)[0] for f in s]
    return 6 in a + s

n = 20000
hits = sum(yze_push(3, 2) for _ in range(n))
p = hits / n
print(f"P(success) = {p:.4f} ± {math.sqrt(p * (1 - p) / n):.4f} (n={n}, seed=1)")
```

`DiceThrower` returns `total`, `success`, `fail` and `pass` as strings
(`'7'`); convert with `int()` before arithmetic.

## Checklist

- Fix the seed and state it with the result.
- Use enough samples that the standard error is small next to the
  differences being reported. 20,000 gives about ±0.003 on a probability near
  0.5.
- Check the simulation against an exact figure where one exists, such as
  the no-push case from `scripts/dice_odds.py`, before trusting the rest.
- Put each reading behind a parameter (`lock_all_ones` above) and report every
  reading the question touches.
- For resource loops, track the state per run and report the distribution of
  outcomes (rolls until exhaustion, final value), not just the mean.
