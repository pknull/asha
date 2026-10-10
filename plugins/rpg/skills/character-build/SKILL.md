---
name: rpg-character-build
description: >-
  Builds or checks a tabletop RPG character within a named game's rules, for
  any system: whether each choice is legal (prerequisites, point or level
  budgets, limits), what each option is worth in that game's numbers (the
  change in the odds of the checks and attacks the character will make, and
  over a fight or a session), and the trade-offs between options, while the
  player keeps every choice and the concept stays theirs. Rules are cited by
  edition and page or marked unchecked; odds come from rpg-mechanic-analysis
  with rpg-dice (0.5.0 or later). Use for check my character, is this build
  legal, should I take +1 or this feature, which option is better for my
  concept, or how much does this item help. Designing a game's own
  advancement system goes to rpg-mechanic-design.
---

# Character build

Helps a player build or check a character: is it legal, and what does each
choice actually buy? Legality comes from the game's own build rules, read
where they are and cited, or marked unchecked. Value comes from computing the
odds the character will face, not from reputation: a +1, a reroll or a
feature is worth what it changes in the rolls that matter for this
character. The player chooses; the concept, name, story and personality are
theirs.

## When to use

- "Check my character", "is this build legal?"
- "Should I take +1 Strength or the feat?", "is this item worth it?"
- "Which option suits my concept better?"

**Not for:** designing a game's advancement or point-buy system
(`rpg-mechanic-design`); the odds of one mechanic in general
(`rpg-mechanic-analysis`); playing the character, which stays with the player;
writing the character's story, which stays with the player.

## Workflow

1. **Pin the game and its build rules.** Name the system and edition, and
   read the character-creation and advancement text where it is (the
   player's copy, or the publisher's free text). Cite pages, or mark rules
   unchecked. Ask for the sheet and the table's house rules. In a homebrew
   game, or when the GM offers a fixed set of options, the GM's word is the
   rule: note it and use it as the build rules in the steps below.
2. **Take the concept and constraints from the player.** What the character
   is for, what they are like, what the player wants to do at the table.
   Ask; do not assume a goal such as maximum damage.
3. **Check legality.** Prerequisites, budgets (add point costs and compare
   with the game's budget), limits and exclusive choices. List each problem
   with its rule and page, or the GM's ruling, and the smallest change that
   fixes it.
4. **Value the options.** Write the rolls the option changes in rpg-dice
   notation and compare them with `rpg-mechanic-analysis`'s
   `scripts/dice_odds.py`, with and without the option. Then scale to how
   often the roll happens: per attack, per fight or per session, using the
   patterns in [references/option-values.md](references/option-values.md)
   (hits over n attacks, once-per-fight effects, rerolls), or fight length
   against a known foe with `scripts/rounds_until.py`. Report the measures
   the player's concept points to (hits for a precise character, damage for
   a brutal one), and say when the answer depends on play style.
5. **Report.** The legality findings with citations; each option's value in
   plain numbers, labelled exact; the trade-offs; and the choice, left to the
   player.

## Example

*A d20 game the player describes: their fighter hits on d20+5 against 15.
Option one is +1 to every attack; option two is advantage (roll twice, keep
the higher) on the first attack of each fight.* No published rule is
claimed; the player's description is the mechanic.

- `dice_odds.py "1d20=+5t>=15" "1d20=+6t>=15" "2d20=+5kh1t>=15"` (exact):
  55%, 60% and 79.75% per attack.
- Over a fight: +1 adds 0.05 expected hits per attack; advantage on the first
  attack adds 0.2475 once. They break even at about five attacks a fight:
  with two attacks, 1.20 against 1.35 expected hits favours advantage; with
  eight, 4.80 against 4.65 favours the +1.
- Report: which is better depends on how long the table's fights run, so ask
  the player, or show both and let them choose.

## Pitfalls

- **Ranking options by reputation** → forum wisdom, other editions → compute
  them for this character's rolls.
- **One roll, not the whole fight** → a once-per-fight bonus judged per
  attack → scale to how often each option applies.
- **The player's concept overruled** → the strongest option pushed →
  present values; the player chooses.
- **Rules from the wrong edition** → builds differ between editions → cite
  the edition used, or mark the rule unchecked.
- **Budgets added by hand and wrong** → long lists of costs → total them in
  the reply line by line, and recheck against the sheet.

## Resources

- [references/option-values.md](references/option-values.md): turning one
  roll's odds into an option's worth over a fight. Read for step 4.
- Scripts used, in `rpg-mechanic-analysis`: `scripts/dice_odds.py` for each
  option's odds and `scripts/rounds_until.py` for fight length.
- Related: `rpg-mechanics-fundamentals` for what a +1 or advantage is worth
  in general; `rpg-encounter-design` for how a party fares in a fight;
  `rpg-router` for rules lookups.
