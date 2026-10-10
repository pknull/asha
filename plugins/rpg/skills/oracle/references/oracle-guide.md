# Oracle guide

Supporting material for `rpg-oracle`. The grading scheme and the likelihood
words are invented for this package.

## Choosing a likelihood

| The fiction says | Likelihood |
|---|---|
| It would be strange if it were true | very unlikely (10%) |
| Possible, but the situation leans against it | unlikely (30%) |
| No reason either way | even (50%) |
| The situation leans toward it | likely (70%) |
| It would be strange if it were false | very likely (90%) |
| Certain or impossible | do not ask; it is already settled |

Set it from what the characters know and what the setting implies, before
rolling, and say why. If the table disagrees about the likelihood, take the
more cautious of the two or discuss before rolling.

## The grades

Each grade's share of the d100 range at a given chance (exact):

| Chance of yes | yes, and | yes | yes, but | no, but | no | no, and |
|---|---|---|---|---|---|---|
| 10% | 2% | 3% | 5% | 5% | 67% | 18% |
| 30% | 6% | 19% | 5% | 5% | 51% | 14% |
| 50% | 10% | 35% | 5% | 5% | 35% | 10% |
| 70% | 14% | 51% | 5% | 5% | 19% | 6% |
| 90% | 18% | 67% | 5% | 5% | 3% | 2% |

- **yes, and:** true, with something extra in the same direction (the guard
  is there and so is his captain).
- **yes:** simply true.
- **yes, but:** true with a catch (guarded, but the guard is drunk).
- **no, but:** false with a consolation (no guard, but fresh tracks).
- **no:** simply false.
- **no, and:** false, and it gets worse (no guard, and the bridge is
  rotten).

The emphatic grades sit at the ends of the range, so a long-shot question
that fails often fails emphatically: at 10%, "no, and" comes up 18% of the
time. If that is too harsh for the table, ask a less loaded question.

## Interpreting

- Start from established facts; an answer that would contradict one is
  read in a way that fits, or the question was badly framed.
- Prefer the reading that gives the characters something to act on.
- Say when an answer was interpreted, so the table can object.
- Answers are facts about the world. They never decide what a player
  character does, wants or feels.

## Building tables

- Plain lists are equally likely. Ranged tables on a summed roll (2d6, 3d6)
  make middle entries common and edge entries rare; put the ordinary in the
  middle and the remarkable at the ends.
- Check the shape before relying on it: `oracle.py table FILE --dice EXPR
  --odds` prints each entry's exact chance.
- An entry that should feel rare at about one in twenty needs about 5%; on
  2d6 a single total of 2 or 12 is 2.8%, and 2-3 or 11-12 is 8.3%.
- Use the user's own tables or published tables they own; cite a published
  table by edition and page and never copy it into the package.

## Consistency

- Log answers (`--log FILE`) and read the log before a new question.
- Ask a question once. Ask again only when the situation has changed, and
  say what changed.
- Hidden answers (`--hidden`) stay GM-only until characters discover them.
