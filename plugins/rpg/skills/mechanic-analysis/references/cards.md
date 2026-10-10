# Card odds with rpg-card

Exact card odds come from rpg-card 0.3.0 or later (`Odds`), and multi-step
procedures from seeded dealing with its decks and hands. Install with
`pip install "git+https://github.com/pknull/rpg-card.git@v0.3.0"`, or put a
checkout on `PYTHONPATH`. Every example below runs as written and asserts its
own result.

## Decks and card attributes

- `Deck.standard()` has 54 cards (jokers in); `Deck.standard(jokers=False)` 52.
- `Deck.tarot()` has 78; `arcana="major"` gives 22, `arcana="minor"` 56.
- Each card has `suit` (an index; 0 holds jokers and major arcana), `rank_index`,
  `value` (standard ace to king 1-13, jokers 0; tarot minors 1-14, majors
  0-21), `colour`, `arcana` and `orientation`. Pass `value_map={0: 14}` for
  ace high.
- `Odds(deck)` reads the deck's current draw pile, so removed or drawn cards
  are already accounted for. Results are exact `Fraction`s. Queries above the
  library's bounds raise `OddsLimitError`; they are never approximated.
- Impossible outcomes are left out of results, so read them with
  `.get(k, 0)`: once a miss cannot happen, `result[0]` raises `KeyError`.

## How many target cards in n draws

```python
from fractions import Fraction
from card_picker import Deck, Odds

deck = Deck.standard()                              # 54 cards, two jokers
jokers = Odds(deck).count(12, lambda c: c.suit == 0)
assert 1 - jokers.get(0, 0) == Fraction(190, 477)          # 39.8%: at least one joker in 12 draws
returned = Odds(deck).count(12, lambda c: c.suit == 0, replacement=True)
assert 1 - returned.get(0, 0) == 1 - Fraction(52, 54) ** 12  # 36.4% if every card goes back
```

```python
from fractions import Fraction
from card_picker import Deck, Odds

deck = Deck.tarot(arcana="major")
for value in list(range(15)) + [15, 16, 17, 18, 19, 20]:
    deck.remove(suit=0, rank=value)                  # only one card is left
last = Odds(deck).count(1, lambda c: c.value >= 15)
assert 0 not in last and last.get(0, 0) == 0          # a miss is impossible, so it is absent
```

## Hands and best-of draws

```python
from fractions import Fraction
from card_picker import Deck, Odds

deck = Deck.standard()
hand = Odds(deck).count(3, lambda c: c.suit > 0 and c.value >= 10)
assert 1 - hand.get(0, 0) == Fraction(1364, 2067)           # 66.0%: a hand of three holds a 10+
best = Odds(Deck.standard(jokers=False)).reading(2, kind="highest")
assert sum(p for v, p in best.items() if v >= 10) == Fraction(116, 221)   # best of two
```

## Comparisons

```python
from fractions import Fraction
from card_picker import Deck, Odds

deck = Deck.standard(jokers=False)
same = Odds(deck).compare()                          # two cards from one deck
assert same == {"win": Fraction(8, 17), "tie": Fraction(1, 17), "loss": Fraction(8, 17)}
plus2 = Odds(deck).compare(value=lambda c: c.value + 2, other_value=lambda c: c.value)
assert round(float(plus2["win"]), 3) == 0.602        # +2 to one side
apart = Odds(deck).compare(other=Deck.standard(jokers=False))   # separate decks
assert apart["tie"] == Fraction(1, 13)
```

## Orientation and deck state

```python
import random
from fractions import Fraction
from card_picker import Deck, Odds

majors = Deck.tarot(arcana="major", rng=random.Random(1))
majors.shuffle(upright_count=11)                     # exactly 11 of 22 upright
upright = Odds(majors).count(3, lambda c: c.orientation == "upright")
assert upright.get(3, 0) == Fraction(3, 28)                 # three upright in a row

deck = Deck.standard(jokers=False)
for rank in (0, 1, 2):
    deck.remove(suit=1, rank=rank)                   # three hearts are gone
assert Odds(deck).count(1, lambda c: c.suit == 1).get(1, 0) == Fraction(10, 49)
```

When each card is turned at random (`shuffle()` with a probability), `Odds`
sees the orientation of that one shuffle. For the chance per draw across all
shuffles, use the turning probability itself: every draw is upright with
probability p.

## Multi-step procedures: seeded dealing

Fights of several flips, hands refilled over a scene, or a deck running out
need a simulation. Deal with a seeded deck, apply the game's rules in plain
Python, and check the first step against an exact figure.

```python
import math
from fractions import Fraction
from card_picker import Deck, Hand

def scene(seed, actions=3):
    """Hold three cards; each action plays a 10+ if held (else the lowest) and refills."""
    deck = Deck.standard(seed=seed)
    deck.shuffle()
    hand = Hand(deck)
    hand.draw(3)
    results = []
    for _ in range(actions):
        good = [c for c in hand.cards if c.suit > 0 and c.value >= 10]
        card = good[0] if good else min(hand.cards, key=lambda c: c.value)
        results.append(bool(good))
        deck.discard([hand.play(card)])
        hand.draw(1)
    return results

n = 20000
runs = [scene(seed) for seed in range(n)]
first = sum(r[0] for r in runs) / n
exact = float(Fraction(1364, 2067))
assert abs(first - exact) < 4 * math.sqrt(exact * (1 - exact) / n)   # matches the exact hand odds
later = sum(r[2] for r in runs) / n                  # third action: fewer good cards left in reach
assert later < first
```

State the rules the simulation assumes (here: a joker is worth 0, so it is
never a 10+ and goes first when no 10+ is held), the sample size, the seeds
and the standard error.
