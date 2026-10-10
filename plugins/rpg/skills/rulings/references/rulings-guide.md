# Rulings guide

Supporting material for `rpg-rulings`. The guideline figures are invented for
this package; a game's own difficulty table, when it has one, comes first and
is cited.

## Intended chance (invented guideline)

| The fiction says | Intended chance | Usually |
|---|---|---|
| Routine for this character, or failure is dull | no roll | Grant it |
| Easy, but it could go wrong | 80-90% | Roll only if the cost of failure matters |
| A real challenge for a competent character | 55-65% | The common case |
| Hard | 30-45% | Expect failure often; make failure interesting |
| Desperate or heroic | 10-20% | Say the odds are long before the player commits |
| Impossible as described | no roll | Say so, and what would make it possible |

One goal that needs several rolls multiplies its chances: three rolls at 60%
all succeed 21.6% of the time. Rule one roll per goal unless the game says
otherwise. The reverse holds for retries: a 55% roll tried until it works
succeeds within three tries 90.9% of the time, so a retryable roll needs a
cost per try (time against a clock, noise, a resource) to mean anything.

## Setting difficulty in each family

| Family | What the GM sets | `difficulty.py` template |
|---|---|---|
| Target number (d20 plus modifier) | The target | `"1d20=+5t>=T"` |
| Roll-under (d100 at or under a skill) | A modifier to the skill | `"1d100t<=T"`, T being the skill after the modifier |
| Bell curve with fixed bands (2d6) | A modifier, since the bands do not move | `"2d6=+Tt>=10"` with `--range=-4..4` for the full-success band |
| Success-counting pool | The face each die needs, or successes needed | `"5d6>=T"`, or `"5d6>=5" --need T` |
| Cards | Target value, or which cards count | Use `card_ladder.py` (`rpg-mechanic-design`) or rpg-card odds (`rpg-mechanic-analysis`) |

Round to the game's printed steps where it has them, and report the chance
that results, so the table knows what it is agreeing to.

## Stake patterns

- **Pass or fail:** success gives the goal; failure costs something that
  moves the scene (time, noise, a resource, a worse position). "Nothing
  happens" is rarely a good failure.
- **Bands (full, partial, miss):** the partial is success with a cost or a
  hard choice. Offer two costs and let the player pick; do not pick for them.
- **Degrees or margin:** decide in advance what each step of margin buys.
- **Pools:** extra successes buy extra effect; complications (such as ones)
  carry their own stated cost.
- **Opposed rolls:** state the tie rule before rolling.

## Open and hidden rolls

- **Open** by default when the result is about the character's own action.
- **Hidden** when the character could not know how well they did (searching,
  sensing lies, an NPC noticing them), or when the game says so. Roll with
  `--hidden`; the output starts GM ONLY.
- **Both at once:** when a character acts and an NPC may notice (sneaking
  past a guard), the character's roll is open and the NPC's is hidden.
- After a hidden roll, narrate only what the characters perceive. A failed
  search and an empty room should look the same.
- Logs that include hidden rolls, and deck state files, are GM-only.

## Cards at the table

- **Values in the scripts:** standard aces 1, jacks 11, queens 12, kings 13,
  jokers 0; tarot minors ace 1 to 10, then page 11, knight 12, queen 13,
  king 14; major arcana 0 (The Fool) to 21 (The World). A rule such as "6 or
  higher" may or may not mean courts; state the readings and record the GM's
  choice as a ruling note.
- **Session decks:** at the start of a session, say whether the deck is new
  (`--new`, or a fresh `--state` path) or continues from last time. The state
  file shows the cards to come; keep it GM-only and never print its order.
- **Odds before a draw:** `draw.py --state FILE --count 0 --odds COND` gives
  the exact chance for the next card from the deck as it stands, so the
  table can hear what it is risking.
- **Trigger cards:** a card that reshuffles the deck or ends a scene is the
  game's rule, not the script's. Watch for it, then run `--count 0
  --reshuffle` when the game says the reshuffle happens.

## Ruling notes

Keep one note per precedent in a place the GM chooses, for example a
`rulings.md` beside the campaign notes:

```text
Ruling: Swinging from chandeliers
Date: 2026-10-10    Game: D&D 2024 (SRD 5.2.1)
Situation: no rule covers swinging across a hall on a chandelier.
Ruling (house): Acrobatics against DC 15; failure lands prone in the middle.
Why: a typical-difficulty stunt (SRD 5.2.1 p. 6 lists Medium as DC 15).
Approved by: the GM, 2026-10-10
```

Before a new ruling, search the notes for the same situation. If a new ruling
would contradict an old one, say so and let the GM choose which stands.
