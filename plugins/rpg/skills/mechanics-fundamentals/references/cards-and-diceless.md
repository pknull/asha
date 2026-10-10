# Cards and diceless mechanics

Cards differ from dice in one way that drives everything else: a drawn card
changes what is left. Figures here are exact (enumeration) and were
cross-checked by seeded dealing. Checked examples were read in the cited
books; page numbers are the printed ones.

## Three jobs cards do

- **Resolution:** the draw decides the outcome (orientation, rank against a
  target, one card against another).
- **Oracle:** the draw picks content (a scene, an event, a character), and
  something else resolves it.
- **Hand:** players hold cards and choose when to play them, which turns luck
  into decisions.

Checked examples:

- **Memento Mori** (*Ex Velum* 2.0): a shared deck of the 22 major arcana. A player
  may draw instead of rolling; upright succeeds at a cost, reversed fails with
  a small benefit, and character traits play no part (pp. 11-14). Drawn cards
  stay out until the adventure ends, and a player may draw only if every other
  player has drawn as many cards (pp. 11, 13). The cards resolve actions and
  form a shared budget.
- **LOOM** (*Portent of the Vale*): solo play with a 54-card deck. Each new
  map square draws a card; rank picks the scene page and suit the paragraph,
  then 2d6 fills in its details and difficulty, and dice resolve every action
  (pp. 1-2, 12). Cards are an oracle only.
- **The Covens of Midnight** (BW edition): a 78-card tarot kept as major and
  minor piles. Daily spreads of five minor cards drive events by rank
  (pp. 102-103); combat compares rank with a suit-element advantage over best
  of five flips, while a hand of three majors gives optional strikes that cost
  Influence (pp. 83-84). All three jobs appear.

### Draw or roll? (Memento Mori)

The Corebook (1.1) rolls a pool of d6s: one Organ's dice (1-3) plus three
Blood dice, one of them always black. Each die of 5 or more is a success; a
black 6 counts as three successes but costs a point of Blood Corruption, and a
black 1 cancels a success. Fewer successes than the Difficulty fails, equal
succeeds at a price, more succeeds fully (pp. 26-27, 31, 35, 112-113). An
upright card gives success at a price whatever the Difficulty or character.

**Chance the dice succeed at all (exact):**

| Pool | Difficulty 1 | Difficulty 2 | Difficulty 3 |
|---|---|---|---|
| 3 white + 1 black (the smallest pool) | 72.8% | 42.0% | 22.8% |
| 4 white + 1 black | 80.2% | 52.3% | 29.2% |
| 5 white + 1 black | 85.7% | 61.6% | 36.9% |

- A draw succeeds as often as the deck has upright cards, about half the time.
  It beats the dice whenever the dice would succeed less than half the time:
  typically Difficulty 2 or more with a small pool.
- The dice can succeed fully; an upright card never does, and a reversed card
  softens failure with a benefit.
- Each black die risks a Dark Success and its corruption 16.7% of the time; a
  draw never does, except through particular cards.
- The shared budget of 22 cards rations exactly the moments when drawing is
  the better gamble.

## The deck remembers

**Chance of at least one target card, drawing without and with replacement:**

| Draw | Without | With |
|---|---|---|
| An ace in 5 cards (52-card deck) | 34.1% | 33.0% |
| A joker in 10 cards (54 cards) | 33.9% | 31.4% |
| A major arcanum in 3 cards (78-card tarot) | 63.6% | 63.0% |
| A specific card in 10 cards (52 cards) | 19.2% | 17.6% |

- Early draws barely differ; the gap grows as the deck thins.
- What came out changes what comes next: after three hearts, the next card is
  a heart 20.4% of the time (25.0% fresh); after ten cards with no ace, the
  next is an ace 9.5% of the time (7.7% fresh). Players who track cards play
  differently.
- A specific card is equally likely to be anywhere; on average it turns up
  after half the deck (26.5 draws of 52).

## Return, discard and reshuffle rules are state

- **Returned after each draw:** the deck behaves like a die, and repeats come
  fast. Some card repeats within 5 draws from 54 cards 17.3% of the time,
  within 10 draws 58.8%, within 20 draws 98.2%.
- **Kept out:** the deck becomes a budget or a clock, and running out is part
  of the design.
- **Rulebooks often leave this out.** LOOM never says whether drawn cards
  return; The Covens of Midnight lets you leave the deck unshuffled for five
  days or shuffle daily (p. 81) but has no discard rule; Memento Mori states it
  outright. State the reading used.

## Orientation

- If each card is turned at random when the deck is prepared, every draw is
  an independent coin flip.
- If the number of upright cards is fixed, draws push against each other:
  three upright in a row is 10.7% with exactly 11 of 22 upright, against
  12.5% for independent flips.
- Turning at preparation fixes the table's luck for the whole deck. With 22
  cards turned at random, 9-13 end up upright 71.4% of the time, but 7 or
  fewer happens 6.7% of the time, and 15 or more another 6.7%.

## A deck as a shared budget

Twenty-two cards shared in turn by N players: 11 each for two players, 7 for
three (one gets an eighth), 5 for four (two get a sixth), 4 for five (two get
a fifth), 3 for six (four get a fourth). Every draw one player spends is gone
for the others.

## Comparisons and totals

**High card, ace low to king high (ace = 1, king = 13), with +m to one side:**

| m | One shared deck: win, tie | Two separate decks: win, tie |
|---|---|---|
| 0 | 47.1%, 5.9% | 46.2%, 7.7% |
| 1 | 52.9%, 7.2% | 53.8%, 7.1% |
| 2 | 60.2%, 6.6% | 60.9%, 6.5% |
| 3 | 66.8%, 6.0% | 67.5%, 5.9% |
| 5 | 78.3%, 4.8% | 78.7%, 4.7% |

- Drawing both cards from one deck makes ties rarer: the second card cannot
  match the first exactly.
- Two-card totals barely change: the spread of the total is 5.24 from one
  deck against 5.29 for independent cards. Deck dependence matters more with
  many cards from a small deck.

## Hands are agency

**Chance a hand holds at least one card of 10 or higher (10, J, Q, K):**

| Hand | 1 | 2 | 3 | 5 |
|---|---|---|---|---|
| Chance | 30.8% | 52.5% | 67.7% | 85.5% |

- Best of two cards reaches 10+ 52.5% of the time and worst of two 9.0%,
  against 30.8% for one card. "Draw two, keep the better" is a strong boon.
- A hand also hides information and lets players time their play; count
  those effects alongside the odds.

## Suits

- Two cards share a suit 23.5% of the time; some two of three cards 60.2%.
- Any five cards from four suits always include two of the same suit. A rule
  triggered by "two of your cards share a suit" in a five-card hand fires
  every time.
- All four suits appear among five cards 26.4% of the time.

## Oracle tables

- On a 52-card deck each rank is 7.7%, each suit 25%, each card 1.9%.
- Tables that group ranks into buckets weight results by bucket size: a
  bucket of three ranks comes up 23.1% of the time, three times a single rank.
- Rank and suit together give 52 outcomes; aces and jokers are often special.

## Diceless mechanics

Resolution without randomness compares ratings directly, spends resources or
bids, or relies on judgement. The numbers question becomes economic: how
fast resources run out, whether one spend dominates, and what information
each side has. Drawing tokens from a bag works like drawing cards without
replacement.

## Computing card odds

For a specific setup, compute exactly with counting (the hypergeometric
formula for "how many of these in n cards"), or by enumeration of every
possible draw, using exact fractions. Multi-step procedures (a fight of
several flips, a week of spreads, a deck running out) need a seeded
simulation that deals real decks; state the sample size, seed and standard
error. rpg-mechanic-analysis does both with rpg-card (0.3.0 or later).
