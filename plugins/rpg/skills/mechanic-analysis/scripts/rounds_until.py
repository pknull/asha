#!/usr/bin/env python3
"""Rounds until a running total reaches a target: fight length, clocks, countdowns.

Usage:
  rounds_until.py --target N|EXPR --each OUTCOMES [--each OUTCOMES ...]
                  [--floor N] [--rounds N] [--json]
                  [--rival-target N|EXPR --rival-each OUTCOMES ... --order first|second|together]
                  [--rival-target N --joint OUTCOMES ...]

Each round, every --each resolves once and adds its amount to a running total
(damage dealt, segments filled, stress taken). The table gives the chance the
total has reached the target during each round and by the end of it, with the
mean and median number of rounds.

OUTCOMES lists the mutually exclusive results of one action as CHANCE:AMOUNT,
separated by commas. Whatever chance is left over adds nothing (a miss):
  1/2:1d8=+3                  hits half the time for 1d8+3
  1d20=+5t>=15:1d8=+3         chance from an rpg-dice total check (11/20 here)
  1/2:1d8=+3, 1/20:2d8=+3     a normal hit, or a critical hit with doubled dice
  5/12:1, 1/6:2               a clock: 1 segment on a partial, 2 on a full success

CHANCE is a fraction (11/20), a decimal (0.55), a percentage (55%), or an
rpg-dice expression with a total check (its pass chance). AMOUNT is a whole
number or an rpg-dice expression: its total, or its count of successes when
it sets a success rule (3d6>=5 adds the number of 5s and 6s). An AMOUNT with
a total check is refused; put the check in CHANCE. Repeat --each for several
actions a round, such as two attackers. --target is a whole number, or an
rpg-dice expression for a rolled target such as hit points (5d8=+10).
--floor N raises every amount below N to N; amounts below 0 are refused
without it.

Race: --rival-target and --rival-each describe the other side (B) working
toward taking side A down. Each side's target is one total: with several
combatants on a side, the race ends when the targeted one drops, and the
attackers of a fallen combatant stop; use skirmish.py for whole fights. --order says who acts first in each round: first
(side A), second, or together (both can go down in the same round). The two
sides are treated as independent, which is exact only without wound
penalties, healing or other feedback between them, and wrong when one roll
moves both sides.

Shared rolls: --joint OUTCOMES gives actions whose outcomes move both totals
at once, as CHANCE:A|B (A added to side A's total, B to side B's):
  --target 6 --rival-target 4 --joint "1/6:2|0, 5/12:1|1, 5/12:0|1"
is one 2d6 roll a round filling a 6-segment progress clock (10+: two, 7-9:
one) and a 4-segment danger clock (7-9 and 6 or less: one). --each and
--rival-each may be added; everything in a round happens at once, so
--order does not apply. Both totals can finish on the same roll; ask what the
game does then. Shared-roll races need fixed whole-number targets.

Figures are exact fractions. Computation stops at --rounds (default 100), or
once less than one fight in a billion is undecided; the remainder is reported.
Explosion chains deeper than 10 count as adding nothing (a miss), and are
reported; an expression leaving out more than one outcome in ten thousand is
refused.

Needs rpg-dice 0.5.0 or later for dice expressions:
  pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0"
or a checkout on PYTHONPATH. Fixed chances and whole-number amounts need no
dice engine. Exit status: 0 ok, 2 invalid input or unusable rpg-dice.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from fractions import Fraction

try:
    from dice_roller.DiceException import DiceException
    from dice_roller.DiceProbability import DiceProbability
except ImportError:  # pragma: no cover - exercised by the CLI message
    DiceProbability = None
    DiceException = Exception

EXPLODE_DEPTH = 10
STOP_BELOW = Fraction(1, 10 ** 9)
UNRESOLVED_LIMIT = Fraction(1, 10 ** 4)  # explosion mass left out beyond this is refused, not approximated
MAX_TARGET = 2000
MAX_WORK = 50_000_000  # target values x round outcomes x rounds
NUMBER = re.compile(r"\d+(?:\.\d+)?|\d+/\d+")
# A success comparator follows the sides (NdS), a custom face list or a group.
# Comparators after a method letter (t, f, x, r, ro, ns, nf) or a method count
# (kh1) are not success rules, and =+N is a total modifier.
SUCCESS_RULE = re.compile(r"(?:(?<![a-ce-z\d])\d+|[})])\s*(>=|<=|==|>|<|=(?![+-]))\s*\d")
NOTES: list[str] = []


def _exact(expr: str):
    if DiceProbability is None:
        raise ValueError(f"{expr!r} needs rpg-dice 0.5.0 or later: "
                         'pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0" or add it to PYTHONPATH')
    try:
        result = DiceProbability().exact(expr, explode_depth=EXPLODE_DEPTH)
    except DiceException as exc:
        reason = "; ".join(str(a) for a in exc.args) or "invalid expression"
        raise ValueError(f"{expr!r}: rpg-dice rejects it ({reason})") from exc
    if result.unresolved > UNRESOLVED_LIMIT:
        raise ValueError(f"{expr!r}: {float(result.unresolved):.2g} of its outcomes lie beyond explosion depth "
                         f"{EXPLODE_DEPTH}, too much to leave out; explosions this frequent need a custom simulation "
                         "that rolls every explosion")
    if result.unresolved:
        NOTES.append(f"{expr}: {float(result.unresolved):.2g} of its outcomes lie beyond explosion depth "
                     f"{EXPLODE_DEPTH} and count as adding nothing; figures can be off by up to that much for "
                     "each roll they cover, so more with more rolls")
    return result


def parse_chance(text: str) -> Fraction:
    t = text.strip()
    if t.endswith("%") and NUMBER.fullmatch(t[:-1]):
        p = Fraction(t[:-1]) / 100
    elif NUMBER.fullmatch(t):
        try:
            p = Fraction(t)
        except ZeroDivisionError as exc:
            raise ValueError(f"chance {t!r} divides by zero") from exc
    else:
        passes = _exact(t).marginal("pass")
        if None in passes:
            raise ValueError(f"chance {t!r} needs a total check such as t>=15, or a number such as 11/20")
        p = passes.get(1, Fraction(0))
    if not 0 <= p <= 1:
        raise ValueError(f"chance {t!r} is {p}, outside 0 to 1")
    return p


def split_outcomes(text: str) -> list[tuple[str, str]]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    outcomes = []
    for part in parts:
        chance, sep, amount = part.partition(":")
        if not sep or not chance.strip() or not amount.strip():
            raise ValueError(f"{part.strip()!r} needs CHANCE:AMOUNT, e.g. 1/2:1d8=+3")
        outcomes.append((chance.strip(), amount.strip()))
    return outcomes


def amount_distribution(text: str, floor: int | None = None) -> dict[int, Fraction]:
    if re.fullmatch(r"-?\d+", text):
        dist = {int(text): Fraction(1)}
    else:
        result = _exact(text)
        if None not in result.marginal("pass"):
            raise ValueError(f"amount {text!r} has a total check; put the check in CHANCE, before the colon")
        totals = result.marginal("success" if SUCCESS_RULE.search(text) else "total")
        if None in totals or any(Fraction(v).denominator != 1 for v in totals):
            raise ValueError(f"amount {text!r} must give whole numbers")
        dist = {int(v): p for v, p in totals.items()}
        if result.unresolved:  # kept, not dropped, so every round's chances still add up to 1
            dist[0] = dist.get(0, Fraction(0)) + result.unresolved
    if floor is not None:
        floored: dict[int, Fraction] = {}
        for v, p in dist.items():
            floored[max(v, floor)] = floored.get(max(v, floor), Fraction(0)) + p
        dist = floored
    if min(dist) < 0:
        raise ValueError(f"amount {text!r} can be {min(dist)}; add --floor 0 (or the rule's minimum)")
    return dist


def attack_distribution(text: str, floor: int | None = None) -> dict[int, Fraction]:
    dist: dict[int, Fraction] = {}
    total_chance = Fraction(0)
    for chance_text, amount_text in split_outcomes(text):
        p = parse_chance(chance_text)
        total_chance += p
        for v, q in amount_distribution(amount_text, floor).items():
            dist[v] = dist.get(v, Fraction(0)) + p * q
    if total_chance > 1:
        raise ValueError(f"the chances in {text!r} add up to {total_chance}, more than 1")
    if total_chance < 1:
        dist[0] = dist.get(0, Fraction(0)) + 1 - total_chance
    return {v: p for v, p in sorted(dist.items()) if p}


def joint_distribution(text: str, floor: int | None = None) -> dict[tuple[int, int], Fraction]:
    dist: dict[tuple[int, int], Fraction] = {}
    total_chance = Fraction(0)
    for chance_text, amount_text in split_outcomes(text):
        parts = [x.strip() for x in amount_text.split("|")]
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"{amount_text!r} needs A|B, one amount for each side, e.g. 1|1")
        p = parse_chance(chance_text)
        total_chance += p
        da, db = amount_distribution(parts[0], floor), amount_distribution(parts[1], floor)
        for x, q in da.items():
            for y, r in db.items():
                dist[(x, y)] = dist.get((x, y), Fraction(0)) + p * q * r
    if total_chance > 1:
        raise ValueError(f"the chances in {text!r} add up to {total_chance}, more than 1")
    if total_chance < 1:
        dist[(0, 0)] = dist.get((0, 0), Fraction(0)) + 1 - total_chance
    return {k: v for k, v in sorted(dist.items()) if v}


def target_distribution(text: str) -> dict[int, Fraction]:
    if re.fullmatch(r"-?\d+", text.strip()):
        dist = {int(text): Fraction(1)}
    else:
        result = _exact(text)
        if result.unresolved:
            raise ValueError(f"target {text!r} must not explode")
        totals = result.marginal("total")
        if None in totals or any(Fraction(v).denominator != 1 for v in totals):
            raise ValueError(f"target {text!r} must give whole numbers")
        dist = {int(v): p for v, p in totals.items()}
    if min(dist) < 1:
        raise ValueError(f"target {text!r} can be {min(dist)}; it must be 1 or more")
    if max(dist) > MAX_TARGET:
        raise ValueError(f"target {text!r} can reach {max(dist)}; the limit is {MAX_TARGET}")
    return dist


def reached_by(attacks: list[dict[int, Fraction]], target: dict[int, Fraction], rounds: int,
               stop_below: Fraction | None = None) -> list[Fraction]:
    """P(running total >= target by the end of each round 1..R), exact."""
    cap = max(target)
    per_round = {0: Fraction(1)}
    for attack in attacks:
        combined: dict[int, Fraction] = {}
        for a, p in per_round.items():
            for b, q in attack.items():
                key = min(a + b, cap)
                combined[key] = combined.get(key, Fraction(0)) + p * q
        per_round = combined
    if not any(v > 0 for v in per_round):
        raise ValueError("no outcome ever adds to the total, so the target is never reached")
    if cap * len(per_round) * rounds > MAX_WORK:
        raise ValueError(f"too much work: target up to {cap}, {len(per_round)} outcomes a round, {rounds} rounds; "
                         "lower --rounds or the target")
    den = math.lcm(*(p.denominator for p in per_round.values()))
    weights = [(v, int(p * den)) for v, p in per_round.items()]
    state = [0] * (cap + 1)
    state[0] = 1
    scale = 1
    out: list[Fraction] = []
    for _ in range(rounds):
        new = [0] * (cap + 1)
        for s, w in enumerate(state):
            if not w:
                continue
            if s == cap:  # already reached: absorbing, so it keeps its full weight
                new[cap] += w * den
                continue
            for v, wv in weights:
                new[min(s + v, cap)] += w * wv
        state, scale = new, scale * den
        suffix, reached = 0, Fraction(0)
        suffix_at = [0] * (cap + 2)
        for s in range(cap, -1, -1):
            suffix += state[s]
            suffix_at[s] = suffix
        for h, q in target.items():
            reached += q * Fraction(suffix_at[h], scale)
        out.append(reached)
        if stop_below is not None and 1 - reached < stop_below:
            break
    return out


def analyse(attacks: list[dict[int, Fraction]], target: dict[int, Fraction], max_rounds: int = 100,
            stop_below: Fraction | None = None) -> dict:
    reached = reached_by(attacks, target, max_rounds, stop_below)
    beyond = 1 - reached[-1]
    mean_at_least = sum((1 - r for r in [Fraction(0)] + reached[:-1]), Fraction(0))
    median = next((n for n, r in enumerate(reached, start=1) if r >= Fraction(1, 2)), None)
    return {"reached_by": reached, "beyond": beyond, "mean_at_least": mean_at_least,
            "mean": mean_at_least if beyond == 0 else None, "median": median}


def race(a: list[Fraction], b: list[Fraction], order: str = "first") -> dict:
    """Who reaches their target first. a and b are reached-by lists from reached_by()."""
    if order not in ("first", "second", "together"):
        raise ValueError("--order is first, second or together")
    rounds = min(len(a), len(b))

    def by(lst, n):  # P(reached by end of round n); round 0 is never
        return lst[n - 1] if n >= 1 else Fraction(0)

    a_wins = b_wins = both = Fraction(0)
    for n in range(1, rounds + 1):
        pa, pb = by(a, n) - by(a, n - 1), by(b, n) - by(b, n - 1)
        if order == "first":
            a_wins += pa * (1 - by(b, n - 1))
            b_wins += pb * (1 - by(a, n))
        elif order == "second":
            a_wins += pa * (1 - by(b, n))
            b_wins += pb * (1 - by(a, n - 1))
        else:
            a_wins += pa * (1 - by(b, n))
            b_wins += pb * (1 - by(a, n))
            both += pa * pb
    # The fight goes past round n only while neither side has finished, whatever the order.
    going = [(1 - by(a, n)) * (1 - by(b, n)) for n in range(rounds + 1)]
    return {"a": a_wins, "b": b_wins, "both": both, "undecided": 1 - a_wins - b_wins - both,
            "rounds": rounds, "order": order, **length_summary(going)}


def length_summary(going: list[Fraction]) -> dict:
    """Mean and percentiles of a race's length; going[n] is P(undecided after round n)."""
    def first(q):
        return next((n for n, g in enumerate(going) if n and 1 - g >= q), None)
    return {"mean_rounds": sum(going[:-1], Fraction(0)), "median_rounds": first(Fraction(1, 2)),
            "p10_rounds": first(Fraction(1, 10)), "p90_rounds": first(Fraction(9, 10))}


def shared_race(actions: list[dict[tuple[int, int], Fraction]], target_a: int, target_b: int, rounds: int,
                stop_below: Fraction | None = None) -> dict:
    """Exact race between two totals that one round's outcomes can move together."""
    per_round = {(0, 0): Fraction(1)}
    for action in actions:
        combined: dict[tuple[int, int], Fraction] = {}
        for (a, b), p in per_round.items():
            for (da, db), q in action.items():
                key = (min(a + da, target_a), min(b + db, target_b))
                combined[key] = combined.get(key, Fraction(0)) + p * q
        per_round = combined
    if not any(a > 0 or b > 0 for a, b in per_round):
        raise ValueError("no outcome ever adds to either total, so the race never ends")
    if (target_a + 1) * (target_b + 1) * len(per_round) * rounds > MAX_WORK:
        raise ValueError("too much work for a shared-roll race; lower --rounds or the targets")
    den = math.lcm(*(p.denominator for p in per_round.values()))
    weights = [(k, int(p * den)) for k, p in per_round.items()]
    state, scale = {(0, 0): 1}, 1
    a_wins = b_wins = both = Fraction(0)
    going = [Fraction(1)]
    for _ in range(rounds):
        new: dict[tuple[int, int], int] = {}
        for (a, b), w in state.items():
            for (da, db), wv in weights:
                key = (min(a + da, target_a), min(b + db, target_b))
                new[key] = new.get(key, 0) + w * wv
        scale *= den
        state = {}
        for (a, b), w in new.items():
            if a >= target_a and b >= target_b:
                both += Fraction(w, scale)
            elif a >= target_a:
                a_wins += Fraction(w, scale)
            elif b >= target_b:
                b_wins += Fraction(w, scale)
            else:
                state[(a, b)] = w
        going.append(1 - a_wins - b_wins - both)
        if not state or stop_below is not None and going[-1] < stop_below:
            break
    return {"a": a_wins, "b": b_wins, "both": both, "undecided": going[-1], "rounds": len(going) - 1,
            "order": "shared", **length_summary(going)}


def table(result: dict) -> str:
    lines = ["| round | reached this round | reached by now |", "|---|---|---|"]
    previous = Fraction(0)
    for n, r in enumerate(result["reached_by"], start=1):
        lines.append(f"| {n} | {float(r - previous):.4f} | {float(r):.4f} |")
        previous = r
        if r >= Fraction(999, 1000):
            break
    hidden = len(result["reached_by"]) - (len(lines) - 2)
    if hidden:
        lines.append(f"\nLater rounds hold the remaining {float(1 - previous):.4f}; --json lists every round.")
    if result["mean"] is not None:
        lines.append(f"\nMean rounds: {float(result['mean']):.3f}" + (" (exact)." if not NOTES else "."))
    else:
        last = len(result["reached_by"])
        note = "" if result["beyond"] < Fraction(1, 10 ** 6) else ", a lower bound"
        lines.append(f"\nMean rounds: {float(result['mean_at_least']):.3f}{note}; computation stopped after round "
                     f"{last} with {float(result['beyond']):.2g} of cases unfinished, counted as ending there.")
    median = result["median"]
    lines.append(f"Median: {'round ' + str(median) if median else 'beyond the rounds computed'}. "
                 f"Reached in the first round: {float(result['reached_by'][0]):.4f}.")
    return "\n".join(lines)


def race_text(result: dict) -> str:
    who = {"first": "side A acts first each round", "second": "side B acts first each round",
           "together": "both act at once",
           "shared": "one roll moves both sides; both can finish in the same round"}[result["order"]]
    if result["order"] == "shared":
        lines = [f"Race ({who}):", f"- side A first: {float(result['a']):.4f}",
                 f"- side B first: {float(result['b']):.4f}",
                 f"- both in the same round: {float(result['both']):.4f}"]
    else:
        lines = [f"Race ({who}):", f"- side A wins: {float(result['a']):.4f}",
                 f"- side B wins: {float(result['b']):.4f}"]
    if result["order"] == "together":
        lines.append(f"- both down in the same round: {float(result['both']):.4f}")
    lines.append(f"- undecided after round {result['rounds']}: {float(result['undecided']):.2g}")
    median, p10, p90 = result["median_rounds"], result["p10_rounds"], result["p90_rounds"]
    spread = f"; 80% decided in rounds {p10}-{p90}" if p10 and p90 else ""
    bound = ", a lower bound" if result["undecided"] >= Fraction(1, 10 ** 6) else ""
    lines.append(f"- rounds until it is decided: mean {float(result['mean_rounds']):.3f}{bound}, median "
                 f"{'round ' + str(median) if median else 'beyond the rounds computed'}{spread}")
    return "\n".join(lines)


def source_line() -> str:
    if NOTES:
        return "Exact figures from rpg-dice, except as the notes below say."
    return "Exact figures" + (" from rpg-dice." if DiceProbability else ".")


def to_json(payload: dict) -> str:
    def conv(o):
        if isinstance(o, Fraction):
            return {"fraction": str(o), "value": float(o)}
        if isinstance(o, dict):
            return {str(k): conv(v) for k, v in o.items()}
        if isinstance(o, list):
            return [conv(v) for v in o]
        return o
    return json.dumps(conv(payload), indent=2)


def shared_main(args) -> int:
    if not args.rival_target:
        raise ValueError("--joint needs --rival-target for side B's total")
    if args.order not in (None, "together"):
        raise ValueError("in a shared-roll race everything in a round happens at once; drop --order")
    if not 1 <= args.rounds <= 1000:
        raise ValueError("--rounds must be 1 to 1000")
    targets = []
    for text in (args.target, args.rival_target):
        if not re.fullmatch(r"\d+", text.strip()) or int(text) < 1:
            raise ValueError(f"target {text!r}: a shared-roll race needs fixed whole-number targets of 1 or more")
        targets.append(int(text))
    if max(targets) > MAX_TARGET:
        raise ValueError(f"targets are limited to {MAX_TARGET}")
    actions = [joint_distribution(j, args.floor) for j in args.joint]
    actions += [{(a, 0): p for a, p in attack_distribution(e, args.floor).items()} for e in args.each]
    actions += [{(0, b): p for b, p in attack_distribution(e, args.floor).items()} for e in args.rival_each]
    result = shared_race(actions, targets[0], targets[1], args.rounds, STOP_BELOW)
    if args.json:
        print(to_json({"race": result, "notes": NOTES}))
    else:
        print("\n\n".join([race_text(result), source_line()] + [f"Note: {n}" for n in NOTES]))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, metavar="N|EXPR", help="total to reach, fixed or rolled")
    parser.add_argument("--each", action="append", default=[], metavar="OUTCOMES",
                        help="one action a round, as CHANCE:AMOUNT[, CHANCE:AMOUNT ...]; repeat for more")
    parser.add_argument("--floor", type=int, metavar="N", help="raise amounts below N to N")
    parser.add_argument("--rounds", type=int, default=100, metavar="N", help="most rounds to compute (default 100)")
    parser.add_argument("--rival-target", metavar="N|EXPR", help="side B's target: side A's hit points")
    parser.add_argument("--rival-each", action="append", default=[], metavar="OUTCOMES", help="side B's actions")
    parser.add_argument("--joint", action="append", default=[], metavar="OUTCOMES",
                        help="an action moving both totals, as CHANCE:A|B[, ...]; needs --rival-target")
    parser.add_argument("--order", help="first (A acts first, the default), second or together")
    parser.add_argument("--json", action="store_true", help="print JSON instead of tables")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 2, -1, -1):
        if argv[i] in ("--floor", "--target", "--rival-target") and re.match(r"-\d", argv[i + 1]):
            argv[i:i + 2] = [f"{argv[i]}={argv[i + 1]}"]
    args = parser.parse_args(argv)
    NOTES.clear()
    try:
        if args.joint:
            return shared_main(args)
        if not args.each:
            raise ValueError("give at least one --each")
        if not 1 <= args.rounds <= 1000:
            raise ValueError("--rounds must be 1 to 1000")
        if bool(args.rival_target) != bool(args.rival_each):
            raise ValueError("a race needs both --rival-target and --rival-each")
        side_a = analyse([attack_distribution(e, args.floor) for e in args.each], target_distribution(args.target),
                         args.rounds, STOP_BELOW)
        side_b = contest = None
        if args.rival_target:
            side_b = analyse([attack_distribution(e, args.floor) for e in args.rival_each],
                             target_distribution(args.rival_target), args.rounds, STOP_BELOW)
            contest = race(side_a["reached_by"], side_b["reached_by"], args.order or "first")
    except (ValueError, ArithmeticError, TypeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(to_json({"side_a": side_a, "side_b": side_b, "race": contest, "notes": NOTES}))
        return 0
    parts = [("Side A: rounds to reach its target\n\n" if side_b else "") + table(side_a)]
    if side_b and contest:
        parts.append("Side B: rounds to reach its target\n\n" + table(side_b))
        parts.append(race_text(contest))
    parts.append(source_line())
    parts += [f"Note: {n}" for n in NOTES]
    print("\n\n".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
