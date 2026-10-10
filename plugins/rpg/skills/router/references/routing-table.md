# Routing table

Every skill in the package, with the phrases and files that should select it.
Status is `authored` when the folder exists and `planned` before then.
Check folder existence when updating this table; no validator enforces it.

## Router

| Skill | Status | Pick it when the request says |
|---|---|---|
| `rpg-router` | authored | any RPG request where the right skill is unclear |

## Research

| Skill | Status | Pick it when the request says |
|---|---|---|
| `rpg-mechanics-fundamentals` | authored | "how do dice pools work", "d20 or 2d6", "bell curve", "what is +1 worth", "is advantage strong", "why is this swingy", "exploding dice", "death spiral", "which resolution mechanic should I use", "hit points or wounds", "why do fights drag", "is going first too strong", "how do clocks work", explaining a mechanic to players |
| `rpg-mechanic-analysis` | authored | "odds", "probability", "how likely", "how swingy", "compare this house rule", "what does pushing cost", "how fast does Sanity drain", "how long will this fight last", "how many hits to kill", "who wins this duel", "who wins: three goblins or a knight", "simulate this fight", "how fast does this clock fill", "how many days will the journey take", "death spiral", "degenerate strategy" |

## Build

| Skill | Status | Pick it when the request says |
|---|---|---|
| `rpg-mechanic-design` | authored | "design a dice mechanic", "design a card or tarot mechanic", "my hack needs a resolution system", "novices should succeed about X%", "too swingy", "advancement doesn't matter", "where should the bands go", "pool or target number for my game", "retune this mechanic", "how many hit points should monsters have", "fights should last about N rounds", "how big should this clock be" |
| `rpg-encounter-design` | authored | "how many goblins for my party", "is this fight too deadly", "balance this boss fight", "this encounter should last about four rounds", "how dangerous is this encounter", "pace the fights across tonight's session" |
| `rpg-character-build` | authored | "check my character", "is this build legal", "should I take +1 or the feat", "is this item worth it", "which option suits my concept" |

Adventures and settings: no separate skills (see Composition).

## Play

| Skill | Status | Pick it when the request says |
|---|---|---|
| `rpg-rulings` | authored | "should this be a roll", "what DC should this be", "roll this for me", "make a hidden check", "draw a card to resolve this", "what happens on a partial", "the rules don't cover this", "keep the deck between draws" |
| `rpg-oracle` | authored | "ask the oracle", "is the bridge guarded", "roll on my encounter table", "random weather", "what's in the chest", "draw a tarot card for the scene", "how likely is each entry on this table", "make a random table from my list", solo or GM-less play |

Keeping a running fight ("whose turn is it", "the ogre takes 9 damage",
"mark her poisoned", "tick the alarm clock") and playing a character ("take
Ivo's turn"): no skill. The table's VTT or bot keeps the tracker, or the GM's
notes if there is none, and players play their characters. A one-off roll,
initiative included, is `rpg-rulings`' "roll this for me"; this package also
answers the rules and odds questions around the fight (`rpg-rulings`,
`rpg-mechanic-analysis`).

## Composition

- General question about a kind of mechanic: `rpg-mechanics-fundamentals`.
- Designing or retuning a mechanic to goals: `rpg-mechanic-design`, which
  uses `rpg-mechanics-fundamentals` for candidate families.
- At the table: `rpg-rulings` decides, rolls and records; `rpg-oracle`
  answers what nobody has decided. Rulings use
  `rpg-mechanics-fundamentals` for why the odds are what they are, and the
  router's lookup rules for what a game says.
- Sizing a fight: `rpg-encounter-design`, which runs
  `rpg-mechanic-analysis`'s scripts.
- Character building and choices: `rpg-character-build`, with
  `rpg-mechanic-analysis` for each option's odds and the router's lookup
  rules for the game's build rules and edition.
- Adventure and setting work, mechanical parts only: random tables from the
  author's lists (`rpg-oracle`), faction, travel and doom clocks
  (kept in the table's VTT or notes; `rpg-mechanic-analysis` for how long
  they take), fights across a session (`rpg-encounter-design`).
- Writing an adventure or setting, or checking a setting's prose for
  consistency: no skill in this package. Say so, offer the mechanical help
  above, and if the user still wants writing, label it a suggestion for the
  author, never canon.
- A specific mechanic, variant or house rule: `rpg-mechanic-analysis` for
  exact numbers, with `rpg-mechanics-fundamentals` for the family's
  behaviour. Published rules are cited or marked unchecked.
