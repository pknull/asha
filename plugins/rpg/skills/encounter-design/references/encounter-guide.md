# Encounter guide

Supporting material for `rpg-encounter-design`. Figures for a specific
encounter come from `rpg-mechanic-analysis`'s scripts; the targets below are
invented starting points.

## Goal sheet

| Question | Typical answers |
|---|---|
| What is the fight for? | A speed bump, a real threat, a climax, a resource drain |
| Party's chance to win | 98%+ speed bump; 90-97% real threat; 75-90% climax the table accepts can go wrong |
| Rounds | 2-3 quick; 4-5 standard; 6+ only if each round brings change (read the mean when the party wins, since losses end differently) |
| Anyone on the party side down | Under 10% speed bump; 20-35% real threat; higher for a climax |
| Cost | Hit points, spells or consumables the party should spend |

## Turning stat blocks into combatants

- **Hit points:** fixed, or the stat block's dice as an rpg-dice expression
  (`hp=5d8=+10`), rolled for each simulated fight.
- **Attacks:** one `attack=` per attack in a round. The chance is the attack
  roll against a typical defence: `dice_odds.py "1d20=+5t>=15"` gives the
  chance against defence 15. When defences differ a lot, use the most likely
  target's, or run each.
- **Criticals and extra effects:** add an outcome, such as
  `9/20:1d8=+3, 1/20:2d8=+3` for a natural-20 critical that doubles dice.
- **Multiattack and extra actions:** more `attack=` entries.
- **Healing, area attacks, conditions, spells:** not modelled. Approximate
  an area attack as several attacks, or note it as left out.

## Tactics to test

| Reading | Options | Use it for |
|---|---|---|
| Neutral | `--order random --target random` | A baseline that favours no one |
| Focus fire | `--target A=random,B=first`, fragile character listed first | Foes that gang up: the worst case for risk |
| Pick off the weak | `--target A=random,B=weakest` | Cunning foes that finish the wounded |
| Party focuses the boss | `--target A=first,B=random`, boss listed first | A party that ignores minions |
| Initiative rolled once | `--order once` | Games that roll initiative at the start |
| Side initiative | `--order sides` | Games where a whole side acts together |

Report every reading the foes might plausibly use. Focus fire usually
changes who goes down far more than who wins, so a high win chance and a low
chance of anyone going down can be impossible together; say which goal gives
way.

## Failure-mode checklist

| Failure mode | Symptom | Check |
|---|---|---|
| Alpha strike | The first round decides it | Fights ending in rounds 1-2; first-mover advantage |
| Dropped before acting | A character is down before their first turn | Focused fire on the lowest-health character |
| Slog | Many rounds, no doubt | Mean rounds above the goal with win chance near 100% |
| Swamped boss | A lone foe dies before it matters | Its rounds alive against the party's damage (`rounds_until.py`) |
| Too swingy | The same roster sometimes wipes the party | Win chance below the goal under any plausible reading |
| Hidden cost | Fine alone, deadly in sequence | Rerun the next fight at the mean hit points `skirmish.py` reports and again with the most fragile character down, both labelled rough estimates; the sequence's odds need a simulation carrying the roster forward |
