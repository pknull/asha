#!/usr/bin/env python3
"""Find the target number that gives an intended chance of success, from rpg-dice.

Usage:
  difficulty.py TEMPLATE --chance P [--need K|T] [--range LO..HI] [--steps T1,T2,...] [--json]

TEMPLATE is an rpg-dice expression with an uppercase T where the target goes:
  1d20=+5t>=T      d20 plus 5 against a DC (a total check: t>=, t<= ...)
  2d6=+1t>=T       2d6 plus 1 against a target
  1d100t<=T        percentile roll-under: T is the skill value
  5d6>=T           a pool: T is the face each die needs; success is at least
                   --need K dice meeting it (default 1)
or, for a pool whose difficulty is the number of successes needed, a pool
without T and --need T:
  5d6>=5 --need T  five dice succeeding on 5 or 6: T is successes needed

A total check (t) compares the sum of the dice, never the success count, so
pools use --need. P is the intended chance: 0.6, 3/5 or 60%. The script
computes exact chances for T = LO, LO+1, ... (default 1..100), reports the T
whose chance is closest to P, and shows the targets around it so the GM can
round toward the game's printed difficulty steps. It stops three targets after
the chance starts moving away from P, assuming chances move one way as T
grows; exact-match checks (t==T) scan the whole range. --steps 10,15,20 also
reports the exact chance at each of the game's printed difficulty steps,
wherever the search stopped. Explosion chains beyond rpg-dice's depth limit
are left out and reported with the chances (each can be that much higher); a
template leaving out more than one outcome in ten thousand is refused.

Needs rpg-dice 0.5.0 or later:
  pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0"
or a checkout on PYTHONPATH. Exit status: 0 ok, 2 invalid input or unusable
rpg-dice.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from fractions import Fraction
from typing import Iterable

try:
    from dice_roller.DiceException import DiceException
    from dice_roller.DiceProbability import DiceProbability
except ImportError:  # pragma: no cover - exercised by the CLI message
    DiceProbability = None
    DiceException = Exception

# A success comparator follows the sides (NdS), a custom face list or a group.
# Comparators after a method letter (t, f, x, r, ro, ns, nf) or a method count
# (kh1) are not success rules, and =+N is a total modifier.
UNRESOLVED_LIMIT = Fraction(1, 10 ** 4)  # explosion mass left out beyond this is refused
SUCCESS_RULE = re.compile(r"(?:(?<![a-ce-z\d])\d+|[})])\s*(>=|<=|==|>|<|=(?![+-]))\s*\d")


def parse_chance(text: str) -> Fraction:
    t = text.strip()
    try:
        p = Fraction(t[:-1]) / 100 if t.endswith("%") else Fraction(t)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"--chance {text!r} must be a number such as 0.6, 3/5 or 60%") from exc
    if not 0 < p < 1:
        raise ValueError(f"--chance {text!r} must be between 0 and 1")
    return p


def parse_range(text: str) -> range:
    m = re.fullmatch(r"\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*", text)
    if not m or int(m.group(1)) > int(m.group(2)) or int(m.group(2)) - int(m.group(1)) > 1000:
        raise ValueError("--range needs LO..HI, at most 1000 apart, e.g. --range 5..30")
    return range(int(m.group(1)), int(m.group(2)) + 1)


def success_chance(expr: str, need: int, need_given: bool) -> tuple[Fraction, Fraction]:
    """The chance over resolved outcomes, and the probability explosion chains leave unresolved."""
    try:
        result = DiceProbability().exact(expr)
    except DiceException as exc:
        reason = "; ".join(str(a) for a in exc.args) or "invalid expression"
        raise ValueError(f"{expr!r}: rpg-dice rejects it ({reason})") from exc
    if result.unresolved > UNRESOLVED_LIMIT:
        raise ValueError(f"{expr!r}: {float(result.unresolved):.2g} of outcomes lie beyond the explosion depth, "
                         "too much to give an exact chance; explosions this frequent need a custom simulation "
                         "that rolls every explosion")
    passes = result.marginal("pass")
    if None not in passes:
        if need_given:
            raise ValueError(f"{expr!r} has a total check, which reads the sum; --need applies to pools")
        return passes.get(1, Fraction(0)), result.unresolved
    if not SUCCESS_RULE.search(expr):
        raise ValueError(f"{expr!r} has no total check (t>=T) and no success rule (>=N per die)")
    return (sum((p for k, p in result.marginal("success").items() if k is not None and k >= need), Fraction(0)),
            result.unresolved)


def row(target: int, expr: str, need: int, need_given: bool) -> dict:
    chance, unresolved = success_chance(expr, need, need_given)
    return {"target": target, "expr": expr, "chance": chance, "unresolved": unresolved}


def rows_for(template: str, targets: Iterable[int], need: str = "1") -> Iterable[dict]:
    has_t = template.count("T")
    if need == "T":
        if has_t:
            raise ValueError("put T in the template or in --need, not both")
        for t in targets:
            if t >= 1:
                yield row(t, template, t, True)
        return
    if not re.fullmatch(r"\d+", need) or int(need) < 1:
        raise ValueError(f"--need {need!r} must be a whole number of 1 or more, or T")
    if has_t != 1:
        raise ValueError(f"{template!r} needs exactly one uppercase T where the target goes, or use --need T")
    for t in targets:
        expr = template.replace("T", str(t)).replace("=+-", "=-")
        yield row(t, expr, int(need), need != "1")


def ladder(template: str, targets: Iterable[int], need: str = "1") -> list[dict]:
    return list(rows_for(template, targets, need))


def closest(rows: list[dict], chance: Fraction) -> dict:
    return min(rows, key=lambda r: (abs(r["chance"] - chance), r["target"]))


def search(template: str, targets: Iterable[int], chance: Fraction, need: str = "1") -> tuple[list[dict], dict]:
    """Rows in target order, stopping after three rows that each move further from chance. Exact-match
    checks (t==T) do not move one way as T grows, so they scan the whole range."""
    rows: list[dict] = []
    rising = 0
    scan_all = "==" in template
    for row in rows_for(template, targets, need):
        distance = abs(row["chance"] - chance)
        rising = rising + 1 if rows and distance > abs(rows[-1]["chance"] - chance) else 0
        rows.append(row)
        if rising >= 3 and not scan_all:
            break
    if not rows:
        raise ValueError("no targets to try")
    return rows, closest(rows, chance)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("template", metavar="TEMPLATE", help="rpg-dice expression with T for the target")
    parser.add_argument("--chance", required=True, metavar="P", help="intended chance of success")
    parser.add_argument("--need", default="1", metavar="K|T", help="successes a pool needs (default 1), or T")
    parser.add_argument("--range", default="1..100", metavar="LO..HI", help="targets to try (default 1..100)")
    parser.add_argument("--steps", metavar="T1,T2", help="the game's printed targets to report as well")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 2, -1, -1):  # keep negative values such as --range -4..4 attached
        if argv[i] in ("--range", "--chance") and re.match(r"-\d", argv[i + 1]):
            argv[i:i + 2] = [f"{argv[i]}={argv[i + 1]}"]
    args = parser.parse_args(argv)
    if DiceProbability is None:
        print('needs rpg-dice 0.5.0 or later: pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0" '
              "or add a checkout to PYTHONPATH", file=sys.stderr)
        return 2
    try:
        chance = parse_chance(args.chance)
        rows, best = search(args.template, parse_range(args.range), chance, args.need)
        try:
            step_targets = [int(x) for x in args.steps.split(",")] if args.steps else []
        except ValueError as exc:
            raise ValueError("--steps needs whole numbers, e.g. --steps 10,15,20") from exc
        steps = ladder(args.template, step_targets, args.need) if step_targets else []
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    def as_json(r):
        return {**r, "chance": str(r["chance"]), "unresolved": str(r["unresolved"])}
    if args.json:
        print(json.dumps({"closest": as_json(best), "rows": [as_json(r) for r in rows],
                          "steps": [as_json(r) for r in steps]}, indent=2))
        return 0
    unresolved = max(r["unresolved"] for r in rows + steps)
    source = (f"Exact over resolved outcomes, from rpg-dice: up to {float(unresolved):.1e} of probability is in "
              "explosion chains beyond the depth limit, so each chance can be higher by up to that much."
              if unresolved else "Exact figures from rpg-dice.")
    i = rows.index(best)
    lines = ["| T | chance |", "|---|---|"]
    for r in rows[max(0, i - 3):i + 4]:
        mark = " (closest)" if r is best else ""
        lines.append(f"| {r['target']} | {float(r['chance']):.4f}{mark} |")
    lines += ["", f"closest: T = {best['target']}, chance {float(best['chance']):.4f} "
                  f"({best['chance']}) for an intended {float(chance):.4f}. {source}"]
    if steps:
        lines.append("printed steps: " + ", ".join(f"{r['target']} {float(r['chance']):.4f}" for r in steps))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
