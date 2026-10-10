# Pilot notes

Findings for the four pilot texts, paraphrased with citations. Figures marked
*exact* were checked against brute-force enumeration; *sampled* figures used a
seeded simulation. Read the cited page before relying on a note.

## Basic Roleplaying: Universal Game Engine (2023 ORC content document)

- Roll d100 under the chance; 96 or higher always fails unless the action is
  automatic, and 00 always fumbles (p. 127).
- Critical is at or below 1/20 of the rating and special at or below 1/5,
  rounded up: the p. 2 example rounds 3.75 to 4, and the p. 128 table agrees
  at every chance from 1 to 120 (exact check).
- **Fumble range, two readings.** The text sets the fumble chance at 1/20 of
  the failure chance, with no rounding rule (p. 127). The p. 128 table applies
  one range per 5-point band. They differ at chance 20, 40, 60 and 80, where
  the table gives one more fumble value.
- Difficulty: Easy doubles the rating, and an Easy success earns no
  experience check; Difficult halves it, rounding up (p. 132). Whether
  critical and special ranges use the adjusted rating is not stated; treat it
  as a reading.
- Experience roll: d100 plus half INT (rounded up) must exceed the rating;
  success adds 1D6, or a fixed +3 declared before rolling (1D8 or +4 epic,
  1D10 or +5 superhuman) (p. 138). Example, exact: rating 60, INT 13 improves
  with probability 47/100, expected gain 1.645.
- The Optional Rule Checklist (pp. 229-230) lists every optional rule; use it
  to class rules as core or optional.

## D&D System Reference Document 5.2.1

- D20 Test: 1d20 plus modifiers succeeds at or above the target (p. 6).
- Advantage keeps the higher of two d20s and Disadvantage the lower; several
  sources still mean two dice, and having both means one d20 (pp. 7-8).
- A natural 20 on an *attack roll* always hits and a natural 1 always misses
  (p. 7); ability checks and saving throws have no such rule.
- Notation: `2d20=+Mkh1t>=DC` (advantage), `2d20=+Mkl1t>=DC` (disadvantage).
  Exact: +5 against 15 is 11/20 plain and 319/400 with advantage.

## Fate Core SRD (CC-BY 3.0)

- Roll four Fate dice (+1, 0 or -1 each) and add the skill (Taking Action >
  Rolling the Dice). Ratings use the ladder from Terrible (-2) to Legendary
  (+8) (The Ladder).
- Outcome by shifts over the opposition: below fails, equal ties, 1-2
  succeeds, 3 or more succeeds with style (The Four Outcomes).
- Notation: `4d{-1,0,1}=+S`. Exact: skill +2 against Good (+3) fails 50/81,
  ties 16/81, succeeds 14/81 and succeeds with style 1/81.
- Ordinary d6s may replace Fate dice, reading 5-6 as + and 1-2 as -
  (What You Need to Play). As `4d6>=5f<=2`, successes minus fails gives
  identical odds at every skill and opposition (exact check).

## Year Zero Engine SRD v1.0 (Free Tabletop License v1.1)

- Dice pool version: roll d6s for attribute plus skill (gear adds dice); each
  6 is a success (p. 8). Modifiers add or remove dice, skill dice first
  (p. 10).
- Step dice version: ratings A-D are D12, D10, D8 and D6 (p. 5); a die showing
  6 or more is a success, 10 or more counts two (p. 8). The chance of any
  success is `(1d10>=6)+(1d8>=6)`; counting 10+ twice needs the face
  distribution (see simulation.md). Exact: a B attribute with a C skill
  succeeds 11/16 of the time, averaging 0.975 successes.
- **Push, two readings.** Pushing rerolls dice not showing 1 (p. 9), but the
  SRD's terms conflict: p. 8 calls every pool die a base die, p. 9 separates
  base, skill and gear dice and names only base-die 1s as banes. Either every
  1 stays locked, or only attribute dice's 1s do. Example (exact, 3 attribute
  and 2 skill dice after a failed roll): the push succeeds 51.1% if every 1
  is locked and 54.8% if only attribute 1s are.
- Push costs are options (p. 9): damage and stress, attribute damage, stress
  dice, conditions, doom points. Under damage and stress, whether skill and
  gear banes cost you is a second reading; in the example above the expected
  damage is 1.00 per push if only attribute dice count. If all pool dice
  count, it is 1.67 when every 1 is locked and 1.33 when only attribute 1s
  are (exact; a sampled check agreed).
