#!/usr/bin/env python3
"""Roll dice for play with rpg-dice, open or hidden, with an optional log.

Usage:
  roll.py EXPR [EXPR ...] [--label TEXT] [--hidden] [--seed S] [--log FILE] [--json]

Each EXPR is an rpg-dice expression (0.5.0 notation), rolled once:
  1d20=+5t>=15      d20 plus 5 against 15
  2d20=+5kh1t>=15   the same with advantage
  4d6>=6f<=1        four d6, count sixes and ones
  2d6=+1            2d6 plus 1; read bands from the total

The output lists the dice as rolled, the kept dice, the total, and the
successes, failures and pass/fail when the expression sets those rules.
Write modifiers before methods: 1d8=+1x, not 1d8x=+1. To keep the higher of
two different dice (a trait die and a wild die), roll both in one call,
"1d8x" "1d6x", and read the higher total.

--hidden marks the output GM ONLY: a hidden roll's dice and result must not
appear in anything players see. --log FILE appends one JSON line per roll
(time, label, expression, hidden flag, seed, result); nothing is written
without it. Play rolls are unseeded; --seed is for tests and replays.

Needs rpg-dice 0.5.0 or later:
  pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0"
or a checkout on PYTHONPATH. Exit status: 0 ok, 2 invalid input or unusable
rpg-dice.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import datetime, timezone

try:
    from dice_roller.DiceException import DiceException
    from dice_roller.DiceThrower import DiceThrower
except ImportError:  # pragma: no cover - exercised by the CLI message
    DiceThrower = None
    DiceException = Exception

GM_ONLY = "GM ONLY (do not show players)"
# A success comparator follows the sides (NdS), a custom face list or a group.
# Comparators after a method letter (t, f, x, r, ro, ns, nf) or a method count
# (kh1) are not success rules, and =+N is a total modifier. Without one,
# rpg-dice still counts top faces, which is not a success the game defined.
SUCCESS_RULE = re.compile(r"(?:(?<![a-ce-z\d])\d+|[})])\s*(>=|<=|==|>|<|=(?![+-]))\s*\d")


def throw(expr: str) -> dict:
    try:
        result = DiceThrower().throw(expr)
    except DiceException as exc:
        reason = "; ".join(str(a) for a in exc.args) or "invalid expression"
        raise ValueError(f"{expr!r}: rpg-dice rejects it ({reason})") from exc
    if isinstance(result, str) or not isinstance(result, dict) or "total" not in result:
        raise ValueError(f"{expr!r}: rpg-dice rejects it ({result})")
    return result


def describe(expr: str, result: dict, label: str | None) -> str:
    parts = [f"{label}: {expr}" if label else expr, f"rolled {result.get('natural')}"]
    if result.get("modified") != result.get("natural"):
        parts.append(f"kept or modified {result.get('modified')}")
    parts.append(f"total {result['total']}")
    if SUCCESS_RULE.search(expr):
        parts.append(f"successes {result.get('success', 0)}")
    if "fail" in result:
        parts.append(f"failures {result['fail']}")
    if "pass" in result:
        parts.append("pass" if str(result["pass"]) in ("1", "True") else "fail")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("expr", nargs="+", metavar="EXPR", help="rpg-dice expression(s) to roll")
    parser.add_argument("--label", help="what the roll is for, e.g. Stealth")
    parser.add_argument("--hidden", action="store_true", help="mark the result GM ONLY")
    parser.add_argument("--seed", type=int, help="seed for tests and replays; omit in play")
    parser.add_argument("--log", metavar="FILE", help="append one JSON line per roll to FILE")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if DiceThrower is None:
        print('needs rpg-dice 0.5.0 or later: pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0" '
              "or add a checkout to PYTHONPATH", file=sys.stderr)
        return 2
    if args.seed is not None:
        random.seed(args.seed)  # DiceThrower draws from the module-level generator
    try:
        results = [(expr, throw(expr)) for expr in args.expr]
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = [{"time": stamp, "label": args.label, "expr": expr, "hidden": args.hidden, "seed": args.seed,
                "result": result} for expr, result in results]
    if args.log:
        try:
            with open(args.log, "a", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            print(f"refused: cannot append to {args.log}: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(records, indent=2))
        return 0
    lines = [describe(expr, result, args.label) for expr, result in results]
    print("\n".join(([GM_ONLY] if args.hidden else []) + lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
