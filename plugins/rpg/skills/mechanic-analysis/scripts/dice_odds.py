#!/usr/bin/env python3
"""Odds for rpg-dice expressions: exact from rpg-dice, checked by a seeded sample.

Usage:
  dice_odds.py EXPR [EXPR ...] [--bands E1,E2,...] [--samples N] [--seed S]
               [--explode-depth D] [--json]

Each EXPR is one rpg-dice expression in 0.5.0 notation, e.g.
  5d6>=6f<=1              five d6, count sixes and ones
  2d20=+5kh1t>=15         advantage, +5 to the total, pass on 15
  (1d10>=6)+(1d8>=6)      a d10 and a d8 with one success count
  4d{-1,0,1}=+2t>=3       Fate dice +2 against +3

Reports exact figures from rpg-dice's exact engine: mean total, the pass
chance, the success and fail distributions and the chance of at least one
success (when the expression sets those rules), the chance of each band of
totals (with --bands 7,10: 6 or less, 7-9, 10 or more), and the probability
left unresolved by explosion chains deeper than --explode-depth.
A seeded DiceThrower sample of the same expression runs alongside as an
independent check. Two or more expressions are compared against the first.

rpg-dice decides what is valid; anything it rejects is refused with its
reason. `+N` directly after the sides adds N to each die; `=+N` adds to the
total. A total check after several terms needs a group: `(1d20+1d4)t>=10`.

Needs rpg-dice 0.5.0 or later:
  pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0"
or a checkout on PYTHONPATH. Exit status: 0 ok, 2 invalid input or unusable
rpg-dice, 3 exact and sampled figures disagree.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from fractions import Fraction

try:
    from dice_roller.DiceException import DiceException
    from dice_roller.DiceProbability import DiceProbability
    from dice_roller.DiceThrower import DiceThrower
except ImportError:  # pragma: no cover - exercised by the CLI message
    DiceProbability = DiceThrower = None
    DiceException = Exception

# A success comparator follows the sides (NdS), a custom face list or a group.
# Comparators after a method letter (t, f, x, r, ro, ns, nf) or a method count
# (kh1, dl1: a total check) are not success rules, and =+N is a total modifier.
UNRESOLVED_LIMIT = Fraction(1, 10 ** 6)  # above this, unresolved explosion mass draws a warning
SUCCESS_RULE = re.compile(r"(?:(?<![a-ce-z\d])\d+|[})])\s*(>=|<=|==|>|<|=(?![+-]))\s*\d")
# NdS followed by +/-N that is not itself a dice term: rpg-dice adds N to each die.
PER_DIE = re.compile(r"(?<![\d)])(\d+)d(\d+)\s*[+-]\s*\d+(?!\d|\s*d)")


class Refused(ValueError):
    pass


def notes_for(expr: str) -> list[str]:
    if any(int(m.group(1)) > 1 for m in PER_DIE.finditer(expr)):
        return ["+N straight after the dice adds to each die; use =+N for a total modifier"]
    return []


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


def bands_of(total: dict, edges: list[int]) -> list[tuple[str, Fraction]]:
    probs = [Fraction(0)] * (len(edges) + 1)
    for value, p in total.items():
        probs[sum(1 for e in edges if value >= e)] += p
    return list(zip(band_labels(edges), probs))


def exact(expr: str, depth: int) -> dict:
    try:
        result = DiceProbability().exact(expr, explode_depth=depth)
    except DiceException as exc:
        reason = "; ".join(str(a) for a in exc.args) or "invalid expression"
        raise Refused(f"{expr!r}: rpg-dice rejects it ({reason})") from exc
    success = result.marginal("success")
    if None in success:
        raise Refused(f"{expr!r}: odds need numeric faces")
    resolved = 1 - result.unresolved
    total = dict(sorted(result.marginal("total").items()))
    fail = result.marginal("fail")
    passes = result.marginal("pass")
    return {
        "mean": sum(Fraction(v) * p for v, p in total.items()) / resolved,
        "total": total,
        "success": dict(sorted(success.items())),
        "fail": None if None in fail else dict(sorted(fail.items())),
        "pass": None if None in passes else passes.get(1, Fraction(0)),
        "unresolved": result.unresolved,
    }


def sample(expr: str, samples: int, seed: int) -> dict:
    random.seed(seed)
    thrower = DiceThrower()
    totals, succ, passes, has_pass = {}, {}, 0, False
    for _ in range(samples):
        r = thrower.throw(expr)
        if isinstance(r, str):
            raise Refused(f"{expr!r}: {r}")
        t = Fraction(r["total"])
        totals[t] = totals.get(t, 0) + 1
        s = int(r.get("success", 0))
        succ[s] = succ.get(s, 0) + 1
        if "pass" in r:
            has_pass = True
            passes += str(r["pass"]) in ("1", "True")
    mean = float(sum(v * c for v, c in totals.items()) / samples)
    var = float(sum((v - Fraction(mean)) ** 2 * c for v, c in totals.items()) / samples)
    p = passes / samples
    return {
        "samples": samples, "seed": seed, "mean": mean, "mean_se": math.sqrt(var / samples),
        "success": {k: c / samples for k, c in sorted(succ.items())},
        "pass": p if has_pass else None,
        "pass_se": math.sqrt(p * (1 - p) / samples) if has_pass else None,
    }


def analyse(expr: str, samples: int, seed: int, depth: int = 10, bands: list[int] | None = None) -> dict:
    ex = exact(expr, depth)
    ex["bands"] = bands_of(ex["total"], bands) if bands else None
    sm = sample(expr, samples, seed)
    warnings = []
    slack = float(ex["unresolved"]) * 100 + 1e-9
    if abs(float(ex["mean"]) - sm["mean"]) > 4 * sm["mean_se"] + slack:
        warnings.append(f"exact mean {float(ex['mean']):.4f} and sampled mean {sm['mean']:.4f} disagree")
    # Standard errors come from the exact chances: a small sample can hold no rare pass or success at all.
    pass_se = math.sqrt(float(ex["pass"]) * (1 - float(ex["pass"])) / samples) if ex["pass"] is not None else 0
    if ex["pass"] is not None and sm["pass"] is not None and \
            abs(float(ex["pass"]) - sm["pass"]) > 4 * pass_se + slack:
        warnings.append(f"exact pass {float(ex['pass']):.4f} and sampled pass {sm['pass']:.4f} disagree")
    result = {"expression": expr, "exact": ex, "sampled": sm, "notes": notes_for(expr), "warnings": warnings,
              "cautions": [], "success_rule": bool(SUCCESS_RULE.search(expr))}
    if result["success_rule"]:
        exact_any = float(any_success(result))
        sampled_any = 1 - sm["success"].get(0, 0)
        expected = min(1.0, exact_any + float(ex["unresolved"]))
        se = math.sqrt(expected * (1 - expected) / samples)
        if not exact_any - 4 * se - 1e-9 <= sampled_any <= exact_any + float(ex["unresolved"]) + 4 * se + 1e-9:
            warnings.append(f"exact P(1+ success) {exact_any:.4f} and sampled {sampled_any:.4f} disagree")
    if ex["unresolved"] > UNRESOLVED_LIMIT:  # a limit of the exact figures, not a disagreement with the sample
        result["cautions"].append(f"{float(ex['unresolved']):.2g} of outcomes are unresolved at explosion depth "
                                  f"{depth}; each chance reported can be higher by up to that much, and the mean "
                                  "covers resolved outcomes only; raise --explode-depth or simulate")
    return result


def any_success(r: dict) -> Fraction:
    """Chance of at least one success among resolved outcomes (a lower bound when some are unresolved)."""
    return sum((p for k, p in r["exact"]["success"].items() if k >= 1), Fraction(0))


def fmt_p(p) -> str:
    if isinstance(p, Fraction):
        return f"{float(p):.4f} ({p})" if p.denominator < 10**6 else f"{float(p):.6f}"
    return f"{p:.4f}"


def fmt_dist(dist: dict) -> str:
    """Entries of at least 0.0001, with a count of the smaller ones."""
    shown = [f"{k}: {fmt_p(v)}" for k, v in dist.items() if v >= Fraction(1, 10000)]
    hidden = len(dist) - len(shown)
    return ", ".join(shown) + (f", +{hidden} values below 0.0001" if hidden else "")


def report(results: list[dict]) -> str:
    out = []
    for r in results:
        ex, sm = r["exact"], r["sampled"]
        u = ex["unresolved"]
        upto = f" (up to +{float(u):.2g} unresolved)" if u else ""
        status = "exact" if not u else "exact over resolved outcomes"
        out.append(f"{r['expression']}  [{status}; checked by sample n={sm['samples']}, seed={sm['seed']}]")
        out.append(("  mean total   " if not u else "  mean total (resolved outcomes) ") + fmt_p(ex["mean"]))
        if ex["pass"] is not None:
            out.append("  P(pass)      " + fmt_p(ex["pass"]) + upto)
        if ex["bands"]:
            out.append("  bands        " + ", ".join(f"{label}: {fmt_p(p)}" for label, p in ex["bands"]))
        if r["success_rule"]:
            out.append("  successes    " + fmt_dist(ex["success"]))
            out.append("  P(1+ success) " + fmt_p(any_success(r)) + upto)
        if ex["fail"] is not None:
            out.append("  fails        " + fmt_dist(ex["fail"]))
        if u:
            out.append(f"  unresolved   {float(u):.2e} (explosion chains beyond the depth limit; not counted "
                       "in any figure above, so each chance may be higher by up to this much)")
        for n in r["notes"]:
            out.append(f"  note: {n}")
        for w in r["cautions"] + r["warnings"]:
            out.append(f"  WARNING: {w}")
    if len(results) > 1:
        base = results[0]
        out.append("comparison against the first expression:")
        for r in results[1:]:
            line = f"  {r['expression']}: mean {float(r['exact']['mean'] - base['exact']['mean']):+.4f}"
            if r["success_rule"] and base["success_rule"]:
                line += f", P(1+ success) {float(any_success(r) - any_success(base)):+.4f}"
            if r["exact"]["pass"] is not None and base["exact"]["pass"] is not None:
                line += f", P(pass) {float(r['exact']['pass'] - base['exact']['pass']):+.4f}"
            if r["exact"]["bands"] and base["exact"]["bands"]:
                line += ", bands " + ", ".join(
                    f"{label} {float(p - q):+.4f}"
                    for (label, p), (_, q) in zip(r["exact"]["bands"], base["exact"]["bands"]))
            out.append(line)
    return "\n".join(out)


def to_json(results: list[dict]) -> str:
    def conv(o):
        if isinstance(o, Fraction):
            return {"fraction": str(o), "value": float(o)}
        if isinstance(o, dict):
            return {str(k): conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [conv(v) for v in o]
        return o
    return json.dumps(conv(results), indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("expressions", nargs="+", metavar="EXPR", help="rpg-dice expression")
    parser.add_argument("--samples", type=int, default=20000, help="sample size for the check (default 20000)")
    parser.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    parser.add_argument("--explode-depth", type=int, default=10,
                        help="extra dice followed per exploding die (default 10)")
    parser.add_argument("--bands", metavar="E1,E2,...",
                        help="band edges for totals, e.g. 7,10 gives 6 or less, 7-9, 10 or more")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    argv = list(sys.argv[1:] if argv is None else argv)
    # Keep negative edges such as "--bands -1,1" attached to their option.
    for i in range(len(argv) - 2, -1, -1):
        if argv[i] == "--bands" and re.match(r"-\d", argv[i + 1]):
            argv[i:i + 2] = [f"{argv[i]}={argv[i + 1]}"]
    args = parser.parse_args(argv)
    try:
        bands = parse_bands(args.bands) if args.bands else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if DiceThrower is None or not hasattr(DiceProbability, "exact"):
        print('needs rpg-dice 0.5.0 or later: pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0" '
              "or add a checkout to PYTHONPATH", file=sys.stderr)
        return 2
    if args.samples < 100:
        print("--samples must be at least 100", file=sys.stderr)
        return 2
    try:
        results = [analyse(e, args.samples, args.seed, args.explode_depth, bands) for e in args.expressions]
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(to_json(results) if args.json else report(results))
    return 3 if any(r["warnings"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
