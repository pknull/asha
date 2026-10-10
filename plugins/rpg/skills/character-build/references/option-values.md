# Valuing options over a fight

Patterns for turning one roll's odds into an option's worth over a fight or
a session. Each uses `rpg-mechanic-analysis`'s `scripts/dice_odds.py` (exact,
rpg-dice 0.5.0 or later); the example numbers are exact for a d20+4 attack
against 14 (55% to hit) dealing 1d8+3.

## Hits over a fixed number of attacks

Write n attacks as a pool with the bonus on each die and a success
threshold: `3d20+4>=14` counts the hits in three attacks. Here `+4` straight
after the dice is meant: it adds to each die, so each die is one attack. The
success distribution gives the expected hits (1.65 for three attacks) and
the chance of landing nothing (9.1%).

## Bonuses that apply to every attack

A change that applies to every attack adds the same amount each time: +1 to
hit adds 0.05 hits an attack, so 0.15 over three attacks; +2 damage adds
0.55 × 2 = 1.1 damage an attack. Damage per fight is expected hits times
mean damage (take the mean from `dice_odds.py "1d8=+3"`).

## Once-per-fight effects

- **A bonus on the first attack only** (advantage, a sure hit): its gain on
  one attack, once per fight. Compare it with a per-attack bonus times the
  attacks a fight; they break even where the totals meet.
- **Reroll the first miss, once per fight:** the hits equal the smaller of
  n and the hits in n + 1 attacks. Take the success distribution of
  `4d20+4>=14` and cap it at 3 for a three-attack fight: 2.108 expected hits
  instead of 1.65, and a fight with no hits falls from 9.1% to 4.1%. Its
  value levels off as fights lengthen, since it is at most one extra attack.

## Readings to report side by side

- **Fight length:** count attacks, not rounds, when the character spends
  rounds on other things; report two or three lengths when the table's
  fights vary.
- **Critical hits:** add the critical as its own outcome, or rerun with the
  game's rule; report whether the ranking changes.
- **Defences:** rerun at the lowest and highest defences the character
  usually faces.
- **Foes' hit points:** when known, use `scripts/rounds_until.py` with the
  option applied, since extra damage can cut the hits a foe needs.
