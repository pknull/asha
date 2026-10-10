# Goals and levers

## Goal sheet

Ask only for what the request leaves open.

- **Ratings:** how many steps, and what the lowest and highest mean in the
  fiction.
- **Target chances:** at the bottom, middle and top rating, for the main
  outcome.
- **Outcomes:** pass or fail only, or partial results, criticals, degrees of
  success or extra effects.
- **Feel:** steady and competent, or swingy and dramatic. Should underdogs
  sometimes win?
- **Advancement:** should every step matter equally, or should early or late
  steps matter more?
- **Opposition:** do characters roll against each other, or against fixed
  targets?
- **Handling:** dice to gather, additions, lookups; must it be readable at a
  glance?
- **Hooks:** anything the dice must support, such as pushing, spending a
  resource, helping or gear.
- **Pacing:** how many rounds a fight or a clock should take, how often a
  fight may end in round one, who should usually win a race between clocks,
  and whether a full danger clock means failure or a complication.

## Families for design

| Family | Value of a step | Built-in degrees | Handling | Suits |
|---|---|---|---|---|
| Flat die plus rating (d20, d100) | Equal everywhere (5 points on a d20, 1 on a d100) | Margin or fractions of the rating | One die, one addition or comparison | Many small steps; transparent maths |
| Summed curve plus rating (2d6, 3d6, 4dF) | Largest near the middle, small at the edges | Bands on the total | One sum, one addition | Few steps; stable middle; built-in partials |
| Success-counting pool | Each die adds less to "any success" | Count of successes | Gather and count dice | "More dice" fantasy; diminishing returns |
| Highest die of a pool | More dice mostly remove bad results | Bands on the best die | Read the best die | Quick reads; partials common |
| Step dice | Each size step adds less | Thresholds per die | Pick die sizes | Few, chunky ratings |
| Cards, rating as hand size | Each extra card adds less to "any match" | Count of matching cards | Draw and read a hand | Choice and hidden information; decks that run down |
| Cards, rating as a bonus | Equal everywhere (1/13 per point on a 52-card deck) | Margin over the target | One card, one addition | Simple reads; deck memory between draws |
| Diceless | Depends on the procedure | Comparison margins | Varies | Not computed by the scripts |

## Levers

- **Target number or band edge.** Moves every rating's chance together. On a
  curve, moving an edge toward the middle changes the most.
- **Rating step size.** Bigger steps spread the range; on a flat die it is the
  only way to cover a wide range in few steps.
- **Dice count.** In a pool, it is the rating; in a summed curve, more dice
  make the middle steadier and extremes rarer.
- **Success threshold in a pool.** Sets how many dice a coin flip needs (four
  dice at 6 only, two at 5+).
- **Band widths.** A wide partial band makes mixed results the norm.
- **Keep or drop dice.** Keeping the best of more dice raises floors; keeping
  the worst lowers ceilings.
- **Explosions.** Raise the ceiling and add tails, with dead spots at the top
  face.
- **Rerolls and pushes.** Add player choice; analyse the gain and the cost
  separately.
- **Health against damage per round.** Sets fight length; chunky damage
  against small health adds spread and one-round finishes.
- **Armour as avoidance or reduction.** Avoidance cuts every attack by the
  same fraction; reduction erases weak attacks and spares heavy ones.
- **Clock sizes and what moves them.** Each segment shifts a race by a
  similar amount; outcomes that move both clocks make the same-roll rule
  matter.

## Checklist before recommending

- The bottom and top ratings meet their targets.
- The change column has no step that does nothing, unless that is intended.
- No automatic results or unreachable targets inside the rating range.
- The chance that a scene's several rolls all succeed is acceptable.
- If the mechanic deals damage or fills clocks, fights and clocks take the
  intended number of rounds (`rpg-mechanic-analysis` computes rounds to
  finish), and any race between clocks has its intended win rate and a stated
  same-roll rule.
- Equal opposed characters are near 50% under the stated tie rule.
- A roll can be read in the time the table can afford.
- The design is labelled invented, or the published rule it adapts is cited
  or marked unchecked.
