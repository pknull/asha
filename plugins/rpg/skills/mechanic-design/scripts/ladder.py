#!/usr/bin/env python3
"""Rating ladder for a dice mechanic: exact odds at each rating, from rpg-dice.

Usage:
  ladder.py TEMPLATE --range LO..HI [--bands E1,E2,...] [--success-bands E1,...] [--json]

TEMPLATE is an rpg-dice expression (0.5.0 notation) with an uppercase R where
the rating goes, for example:
  Rd6>=5              a pool of R dice, success on 5 or more
  1d20=+Rt>=15        d20 plus R against 15
  2d6=+R              2d6 plus R; add --bands 7,10 for 6 or less, 7-9, 10 or more
  (1d8>=6)+(1dR>=6)   a d8 plus a die of size R

For each rating the table shows the expression, the mean total, the pass
chance (when the expression has a total check), the chance of at least one
success (when it sets a success rule), the band chances on the total (with
--bands) and on the number of successes (with --success-bands 1,2: 0, 1, 2 or
more, the usual fail, partial and full reading of a pool). The
last column is the change from the previous rating in the headline measure:
pass chance, else chance of any success, else the top band, else the mean.

A negative rating after `=+` becomes `=-N`. A rating used as a dice count must
not be negative. Figures are exact, except that explosion chains beyond
rpg-dice's depth limit are left out, and the footer then says how much;
rpg-dice decides what is valid.

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
from typing import Callable

try:
    from dice_roller.DiceException import DiceException
    from dice_roller.DiceProbability import DiceProbability
except ImportError:  # pragma: no cover - exercised by the CLI message
    DiceProbability = None
    DiceException = Exception

# A success comparator follows the sides (NdS), a custom face list or a group.
# Comparators after a method letter or a method count (kh1) are not success rules.
SUCCESS_RULE = re.compile(r"(?:(?<![a-ce-z\d])\d+|[})])\s*(>=|<=|==|>|<|=(?![+-]))\s*\d")
COUNT_R = re.compile(r"(?<![\w}])Rd")


def fill(template: str, rating: int) -> str:
    if rating < 0 and COUNT_R.search(template):
        raise ValueError(f"rating {rating} would be a negative dice count in {template!r}")
    expr = template.replace("R", str(rating))
    return expr.replace("+-", "-").replace("--", "+")


MAX_RATINGS = 200


def parse_range(text: str) -> list[int]:
    m = re.fullmatch(r"\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*", text)
    if not m or int(m.group(1)) > int(m.group(2)):
        raise ValueError("--range needs LO..HI with LO no greater than HI, e.g. --range -1..3")
    if int(m.group(2)) - int(m.group(1)) >= MAX_RATINGS:  # checked before anything is allocated
        raise ValueError(f"--range covers at most {MAX_RATINGS} ratings")
    return list(range(int(m.group(1)), int(m.group(2)) + 1))


def parse_bands(text: str) -> list[int]:
    try:
        edges = [int(x) for x in text.split(",") if x.strip()]
    except ValueError:
        edges = []
    if not edges or any(b <= a for a, b in zip(edges, edges[1:])):
        raise ValueError("--bands needs increasing whole numbers, e.g. --bands 7,10")
    return edges


def band_labels(edges: list[int]) -> list[str]:
    labels = [f"{edges[0] - 1} or less"]
    for lo, hi in zip(edges, edges[1:]):
        labels.append(str(lo) if hi - lo == 1 else f"{lo} to {hi - 1}" if lo < 0 else f"{lo}-{hi - 1}")
    return labels + [f"{edges[-1]} or more"]


def count_labels(edges: list[int]) -> list[str]:
    labels = ["0" if edges[0] == 1 else f"{edges[0] - 1} or fewer"]
    for lo, hi in zip(edges, edges[1:]):
        labels.append(str(lo) if hi - lo == 1 else f"{lo}-{hi - 1}")
    labels.append(f"{edges[-1]} or more")
    return [f"{label} success" if label == "1" else f"{label} successes" for label in labels]


def ladder(template: str, ratings: list[int], edges: list[int] | None,
           success_edges: list[int] | None = None) -> list[dict]:
    if "R" not in template:
        raise ValueError(f"{template!r} has no R where the rating goes")
    rows = []
    for rating in ratings:
        expr = fill(template, rating)
        try:
            result = DiceProbability().exact(expr)
        except DiceException as exc:
            reason = "; ".join(str(a) for a in exc.args) or "invalid expression"
            raise ValueError(f"{expr!r}: rpg-dice rejects it ({reason})") from exc
        total = result.marginal("total")
        resolved = 1 - result.unresolved
        passes = result.marginal("pass")
        has_success = bool(SUCCESS_RULE.search(expr))
        if success_edges and not has_success:
            raise ValueError(f"{expr!r}: --success-bands needs a success rule such as >=5")
        success_bands = None
        if success_edges:
            success_bands = [Fraction(0)] * (len(success_edges) + 1)
            for count, p in result.marginal("success").items():
                success_bands[sum(1 for e in success_edges if count >= e)] += p
        bands = None
        if edges:
            probs = [Fraction(0)] * (len(edges) + 1)
            for value, p in total.items():
                probs[sum(1 for e in edges if value >= e)] += p
            bands = probs
        rows.append({
            "rating": rating,
            "expression": expr,
            "mean": sum(Fraction(v) * p for v, p in total.items()) / resolved,
            "pass": None if None in passes else passes.get(1, Fraction(0)),
            "any_success": (sum((p for k, p in result.marginal("success").items() if k is not None and k >= 1),
                                Fraction(0)) if has_success else None),
            "bands": bands,
            "success_bands": success_bands,
            "unresolved": result.unresolved,
        })
    return rows


def headline(rows: list[dict]) -> tuple[str, Callable[[dict], Fraction]]:
    if rows[0]["pass"] is not None:
        return "P(pass)", lambda r: r["pass"]
    if rows[0]["any_success"] is not None:
        return "P(1+ success)", lambda r: r["any_success"]
    if rows[0]["bands"] is not None:
        return "top band", lambda r: r["bands"][-1]
    return "mean", lambda r: r["mean"]


def table(rows: list[dict], edges: list[int] | None, success_edges: list[int] | None = None) -> str:
    name, measure = headline(rows)
    head = ["R", "expression", "mean"]
    if rows[0]["pass"] is not None:
        head.append("P(pass)")
    if rows[0]["any_success"] is not None:
        head.append("P(1+ success)")
    if edges:
        head += band_labels(edges)
    if success_edges:
        head += count_labels(success_edges)
    head.append(f"change in {name}")
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    previous = None
    for r in rows:
        cells = [str(r["rating"]), r["expression"], f"{float(r['mean']):.2f}"]
        if r["pass"] is not None:
            cells.append(f"{float(r['pass']):.4f}")
        if r["any_success"] is not None:
            cells.append(f"{float(r['any_success']):.4f}")
        if r["bands"] is not None:
            cells += [f"{float(p):.4f}" for p in r["bands"]]
        if r["success_bands"] is not None:
            cells += [f"{float(p):.4f}" for p in r["success_bands"]]
        value = measure(r)
        cells.append("" if previous is None else f"{float(value - previous):+.4f}")
        previous = value
        lines.append("| " + " | ".join(cells) + " |")
    unresolved = max(r["unresolved"] for r in rows)
    lines.append("")
    lines.append(f"Exact over resolved outcomes, from rpg-dice: up to {float(unresolved):.1e} of probability is in "
                 "explosion chains beyond the depth limit, so each chance can be higher by up to that much and the "
                 "mean covers resolved outcomes only." if unresolved else "Exact figures from rpg-dice.")
    return "\n".join(lines)


def to_json(rows: list[dict]) -> str:
    def conv(o):
        if isinstance(o, Fraction):
            return {"fraction": str(o), "value": float(o)}
        if isinstance(o, dict):
            return {k: conv(v) for k, v in o.items()}
        if isinstance(o, list):
            return [conv(v) for v in o]
        return o
    return json.dumps(conv(rows), indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("template", help="rpg-dice expression with R for the rating")
    parser.add_argument("--range", required=True, metavar="LO..HI", help="ratings to tabulate, e.g. -1..3")
    parser.add_argument("--bands", metavar="E1,E2,...",
                        help="band edges for totals, e.g. 7,10 gives 6 or less, 7-9, 10 or more")
    parser.add_argument("--success-bands", metavar="E1,E2,...",
                        help="band edges for success counts, e.g. 1,2 gives 0, 1, 2 or more")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    argv = list(sys.argv[1:] if argv is None else argv)
    # Keep negative values such as "--range -1..3" or "--bands -1,1" attached to their option.
    for i in range(len(argv) - 2, -1, -1):
        if argv[i] in ("--range", "--bands") and re.match(r"-\d", argv[i + 1]):
            argv[i:i + 2] = [f"{argv[i]}={argv[i + 1]}"]
    args = parser.parse_args(argv)
    if DiceProbability is None or not hasattr(DiceProbability, "exact"):
        print('needs rpg-dice 0.5.0 or later: pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0" '
              "or add a checkout to PYTHONPATH", file=sys.stderr)
        return 2
    try:
        ratings = parse_range(args.range)
        edges = parse_bands(args.bands) if args.bands else None
        success_edges = parse_bands(args.success_bands) if args.success_bands else None
        if success_edges and success_edges[0] < 1:
            raise ValueError("--success-bands edges start at 1 or more")
        rows = ladder(args.template, ratings, edges, success_edges)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(to_json(rows) if args.json else table(rows, edges, success_edges))
    return 0


if __name__ == "__main__":
    sys.exit(main())
