#!/usr/bin/env python3
"""Seeded simulation of a fight between two sides with any number of combatants.

Usage:
  skirmish.py --unit UNIT --unit UNIT [...] [--order listed|random|once|sides]
              [--target POLICY | --target A=POLICY,B=POLICY] [--fights N]
              [--seed S] [--max-rounds R] [--floor N]
              [--count NAME=LO..HI] [--hp-ladder NAME=HP1,HP2,...] [--json]

Each UNIT is a semicolon-separated description:
  "side=A; name=Fighter; hp=28; attack=9/20:1d8=+3, 1/20:2d8=+3"
  "side=B; name=Ogre; hp=5d8=+10; attack=3/5:2d6=+4"
side is A or B; name is unique; hp is a whole number or an rpg-dice
expression rolled for each fight; each attack= is one attack a round, as
CHANCE:AMOUNT outcomes exactly as in rounds_until.py (repeat attack= for
several attacks; a unit with none only soaks hits).

Each round every standing unit acts once, in --order: listed (the order the
units are given, the default), random (shuffled every round), once (shuffled
once per fight, like initiative rolled at the start) or sides (all of A in
listed order, then all of B). Each attack picks a standing enemy by --target:
first (the first listed; focus fire, the default), random, or weakest (fewest
hit points left, first listed on a tie). Give the sides different policies
with --target A=first,B=random. A unit at 0 hit points or less is down at
once and does not act again. The fight ends when a side has no one standing,
or after --max-rounds (default 200) as undecided.

Reports each side's chance of winning with its standard error, the rounds a
fight takes (mean, median, 10th and 90th percentiles, and the mean when side
A wins), each unit's chance of standing at the end, the chance that each side
ends with anyone down, and side A's mean hit points left when it wins. That
mean does not chain fights: a next fight run at it loses the spread of hit
points, who is down and the fights already lost, so its odds are a rough
estimate at best; a sequence of fights needs a custom simulation
(references/simulation.md). --json gives the same figures, with any notes
about explosion depth under "notes".

Ladders for sizing an encounter print one row per setting:
  --count NAME=LO..HI       the named unit repeated LO to HI times (0 removes
                            it; copies are named NAME 1, NAME 2, ...)
  --hp-ladder NAME=V1,V2    the named unit's hit points set to each value
Together they print every combination.

Outcomes are sampled from rpg-dice's exact distributions, so only the fight
itself is simulated. Report the seed and the number of fights with every
figure; the default is 20000 fights, seed 1.

Not modelled: anything outside these rules, such as healing, fleeing,
morale, conditions, movement, reactions or area attacks. Encode those in a
custom simulation (references/simulation.md).

Needs rpg-dice 0.5.0 or later for dice expressions, and rounds_until.py in the
same folder. Exit status: 0 ok, 2 invalid input or unusable rpg-dice.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import re
import sys
from fractions import Fraction
from pathlib import Path

_spec = importlib.util.spec_from_file_location("rounds_until", Path(__file__).with_name("rounds_until.py"))
assert _spec is not None and _spec.loader is not None
ru = importlib.util.module_from_spec(_spec)
_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # keep the skill folder free of caches
try:
    _spec.loader.exec_module(ru)
finally:
    sys.dont_write_bytecode = _bytecode
DiceProbability = ru.DiceProbability

KEYS = {"side", "name", "hp", "attack"}
ORDERS = ("listed", "random", "once", "sides")
TARGETS = ("first", "random", "weakest")


class Sampler:
    """Draws from an exact distribution {value: probability}."""

    def __init__(self, dist: dict):
        self.values = list(dist)
        total = sum(dist.values())
        self.cum = []
        acc = Fraction(0)
        for v in self.values:
            acc += dist[v]
            self.cum.append(float(acc / total))

    def draw(self, rng: random.Random):
        x = rng.random()
        for value, edge in zip(self.values, self.cum):
            if x < edge:
                return value
        return self.values[-1]


def parse_unit(text: str, floor: int | None = None) -> dict:
    unit: dict = {"attacks": []}
    for part in text.split(";"):
        if not part.strip():
            continue
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in KEYS or not value:
            raise ValueError(f"{part.strip()!r} in {text!r}: use side=, name=, hp= and attack=, separated by ';'")
        if key == "attack":
            unit["attacks"].append((value, ru.attack_distribution(value, floor)))
        elif key in unit:
            raise ValueError(f"{key}= appears twice in {text!r}")
        else:
            unit[key] = value
    for key in ("side", "name", "hp"):
        if key not in unit:
            raise ValueError(f"{text!r} needs {key}=")
    if unit["side"] not in ("A", "B"):
        raise ValueError(f"side must be A or B in {text!r}")
    unit["hp_dist"] = ru.target_distribution(unit["hp"])
    return unit


def parse_targets(target) -> dict[str, str]:
    """One policy for both sides, or A=POLICY,B=POLICY."""
    if isinstance(target, dict):
        policies = dict(target)
    elif "=" in target:
        policies = {}
        for part in target.split(","):
            side, _, policy = part.partition("=")
            policies[side.strip()] = policy.strip()
    else:
        policies = {"A": target.strip(), "B": target.strip()}
    if set(policies) != {"A", "B"} or any(p not in TARGETS for p in policies.values()):
        raise ValueError(f"--target is one of {', '.join(TARGETS)}, or A=POLICY,B=POLICY for both sides")
    return policies


def simulate(units: list[dict], order: str = "listed", target="first", fights: int = 20000,
             seed: int = 1, max_rounds: int = 200) -> dict:
    if order not in ORDERS:
        raise ValueError(f"--order is one of {', '.join(ORDERS)}")
    policy = parse_targets(target)
    if {u["side"] for u in units} != {"A", "B"}:
        raise ValueError("each side needs at least one unit")
    names = [u["name"] for u in units]
    if len(set(names)) != len(names):
        raise ValueError("unit names must be unique")
    if fights < 1:
        raise ValueError("--fights must be 1 or more")
    rng = random.Random(seed)
    hp_samplers = [Sampler(u["hp_dist"]) for u in units]
    attack_samplers = [[Sampler(d) for _, d in u["attacks"]] for u in units]
    wins = {"A": 0, "B": 0}
    any_down = {"A": 0, "B": 0}
    undecided = 0
    standing = {n: 0 for n in names}
    lengths, a_win_lengths = [], []
    a_names = [u["name"] for u in units if u["side"] == "A"]
    hp_left = {n: 0 for n in a_names}
    for _ in range(fights):
        hp = [s.draw(rng) for s in hp_samplers]
        fixed = list(range(len(units)))
        if order == "once":
            rng.shuffle(fixed)
        elif order == "sides":
            fixed.sort(key=lambda i: units[i]["side"])
        winner, rounds = None, 0
        while winner is None and rounds < max_rounds:
            rounds += 1
            turn = fixed
            if order == "random":
                turn = list(range(len(units)))
                rng.shuffle(turn)
            for i in turn:
                if hp[i] <= 0:
                    continue
                side_policy = policy[units[i]["side"]]
                for sampler in attack_samplers[i]:
                    foes = [j for j, u in enumerate(units) if u["side"] != units[i]["side"] and hp[j] > 0]
                    if not foes:
                        break
                    if side_policy == "first":
                        j = foes[0]
                    elif side_policy == "random":
                        j = rng.choice(foes)
                    else:
                        j = min(foes, key=lambda k: hp[k])
                    hp[j] -= sampler.draw(rng)
                for side in ("A", "B"):
                    if not any(hp[k] > 0 for k, u in enumerate(units) if u["side"] == side):
                        winner = "B" if side == "A" else "A"
                if winner:
                    break
        if winner:
            wins[winner] += 1
            lengths.append(rounds)
            if winner == "A":
                a_win_lengths.append(rounds)
                for k, u in enumerate(units):
                    if u["side"] == "A":
                        hp_left[u["name"]] += max(0, hp[k])
        else:
            undecided += 1
        for k, u in enumerate(units):  # final state when the fight stopped, decided or not
            standing[u["name"]] += hp[k] > 0
        for side in ("A", "B"):
            any_down[side] += any(hp[k] <= 0 for k, u in enumerate(units) if u["side"] == side)
    lengths.sort()

    def pct(q):
        return lengths[min(len(lengths) - 1, math.ceil(q * len(lengths)) - 1)] if lengths else None

    share = {s: wins[s] / fights for s in wins}
    target_text = policy["A"] if policy["A"] == policy["B"] else f"A={policy['A']},B={policy['B']}"
    return {"fights": fights, "seed": seed, "order": order, "target": target_text, "wins": share,
            "se": {s: math.sqrt(p * (1 - p) / fights) for s, p in share.items()},
            "undecided": undecided / fights, "standing": {n: c / fights for n, c in standing.items()},
            "any_down": {s: c / fights for s, c in any_down.items()},
            "mean_rounds": sum(lengths) / len(lengths) if lengths else None,
            "mean_rounds_a_wins": sum(a_win_lengths) / len(a_win_lengths) if a_win_lengths else None,
            "hp_left_a_wins": {n: v / len(a_win_lengths) for n, v in hp_left.items()} if a_win_lengths else None,
            "median_rounds": pct(0.5), "p10_rounds": pct(0.1), "p90_rounds": pct(0.9)}


def ladder(units: list[dict], count: tuple | None = None, hp: tuple | None = None, **options) -> list[dict]:
    """Rows for each hit point value of one unit (hp=(name, values)) and each
    number of copies of a unit (count=(name, counts)); both together give a grid."""
    for setting, label in ((count, "--count"), (hp, "--hp-ladder")):
        if setting and not any(u["name"] == setting[0] for u in units):
            raise ValueError(f"{label} names {setting[0]!r}, which is not a unit")
    hp_values = list(hp[1]) if hp else [None]
    counts = list(count[1]) if count else [None]
    rows = []
    for value in hp_values:
        roster = [{**u, "hp": value, "hp_dist": ru.target_distribution(value)}
                  if hp and u["name"] == hp[0] else u for u in units]
        for k in counts:
            if k is None:
                final = roster
            else:
                final = []
                for u in roster:
                    if u["name"] == count[0]:
                        final += [{**u, "name": f"{count[0]} {i}" if k > 1 else count[0]} for i in range(1, k + 1)]
                    else:
                        final.append(u)
            row = simulate(final, **options)
            if hp:
                row = {"hp": value, **row}
            if count:
                row = {"count": k, **row}
            rows.append(row)
    return rows


def count_ladder(units: list[dict], name: str, counts, **options) -> list[dict]:
    """Simulate with the named unit repeated each number of times in counts."""
    return ladder(units, count=(name, counts), **options)


def ladder_report(rows: list[dict], count_name: str | None, hp_name: str | None) -> str:
    first = rows[0]
    head = ([f"{hp_name} hp"] if hp_name else []) + ([f"{count_name} count"] if count_name else [])
    head += ["side A wins", "standard error", "undecided", "mean rounds (decided)", "80% within rounds",
             "mean rounds when A wins", "anyone on side A down"]
    lines = [f"{first['fights']} fights per row, seed {first['seed']}, order {first['order']}, "
             f"targeting {first['target']} (sampled):", "", "| " + " | ".join(head) + " |",
             "|" + "---|" * len(head)]
    for r in rows:
        spread = f"{r['p10_rounds']}-{r['p90_rounds']}" if r["p10_rounds"] else "-"
        mean = f"{r['mean_rounds']:.2f}" if r["mean_rounds"] is not None else "-"
        mean_a = f"{r['mean_rounds_a_wins']:.2f}" if r["mean_rounds_a_wins"] is not None else "-"
        cells = ([str(r["hp"])] if hp_name else []) + ([str(r["count"])] if count_name else [])
        cells += [f"{r['wins']['A']:.4f}", f"{r['se']['A']:.4f}", f"{r['undecided']:.4f}", mean, spread, mean_a,
                  f"{r['any_down']['A']:.4f}"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def report(r: dict) -> str:
    lines = [f"{r['fights']} fights, seed {r['seed']}, order {r['order']}, targeting {r['target']} (sampled):",
             f"- side A wins: {r['wins']['A']:.4f} (standard error {r['se']['A']:.4f})",
             f"- side B wins: {r['wins']['B']:.4f} (standard error {r['se']['B']:.4f})"]
    if r["undecided"]:
        lines.append(f"- undecided at the round limit: {r['undecided']:.4f}")
    if r["mean_rounds"] is not None:
        lines.append(f"- rounds (decided fights): mean {r['mean_rounds']:.2f}, median {r['median_rounds']}, "
                     f"80% within rounds {r['p10_rounds']}-{r['p90_rounds']}")
    if r["mean_rounds_a_wins"] is not None:
        lines.append(f"- rounds when side A wins: mean {r['mean_rounds_a_wins']:.2f}")
        lines.append("- side A hit points left when it wins (mean, 0 if down): " +
                     ", ".join(f"{n} {v:.1f}" for n, v in r["hp_left_a_wins"].items()))
    lines.append("- standing at the end: " + ", ".join(f"{n} {p:.4f}" for n, p in r["standing"].items()))
    lines.append(f"- anyone down at the end: side A {r['any_down']['A']:.4f}, side B {r['any_down']['B']:.4f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--unit", action="append", default=[], metavar="UNIT", help="one combatant; repeat")
    parser.add_argument("--order", default="listed", help="listed (default), random, once or sides")
    parser.add_argument("--target", default="first", help="first (default), random, weakest, or A=...,B=...")
    parser.add_argument("--fights", type=int, default=20000, metavar="N", help="fights to simulate (default 20000)")
    parser.add_argument("--seed", type=int, default=1, metavar="S", help="random seed (default 1)")
    parser.add_argument("--max-rounds", type=int, default=200, metavar="R", help="round limit (default 200)")
    parser.add_argument("--floor", type=int, metavar="N", help="raise damage below N to N")
    parser.add_argument("--count", metavar="NAME=LO..HI", help="repeat one unit LO to HI times, one row each")
    parser.add_argument("--hp-ladder", metavar="NAME=V1,V2", help="one unit's hit points, one row each value")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    ru.NOTES.clear()
    try:
        if not 1 <= args.max_rounds <= 10000:
            raise ValueError("--max-rounds must be 1 to 10000")
        if args.fights > 1_000_000:
            raise ValueError("--fights is limited to 1000000")
        units = [parse_unit(u, args.floor) for u in args.unit]
        options = {"order": args.order, "target": args.target, "fights": args.fights, "seed": args.seed,
                   "max_rounds": args.max_rounds}
        count = hp = None
        if args.count:
            m = re.fullmatch(r"\s*(.+?)\s*=\s*(\d+)\s*\.\.\s*(\d+)\s*", args.count)
            if not m or not 0 <= int(m.group(2)) <= int(m.group(3)) <= 50:
                raise ValueError("--count needs NAME=LO..HI with 0 <= LO <= HI <= 50, e.g. --count Goblin=0..6")
            count = (m.group(1), range(int(m.group(2)), int(m.group(3)) + 1))
        if args.hp_ladder:
            name, sep, values = args.hp_ladder.partition("=")
            values_list = [v.strip() for v in values.split(",") if v.strip()]
            if not sep or not name.strip() or not values_list or len(values_list) > 50:
                raise ValueError("--hp-ladder needs NAME=V1,V2,... (whole numbers or expressions, at most 50)")
            hp = (name.strip(), values_list)
        if count or hp:
            payload = ladder(units, count=count, hp=hp, **options)
            text = ladder_report(payload, count[0] if count else None, hp[0] if hp else None)
        else:
            payload = simulate(units, **options)
            text = report(payload)
    except (ValueError, ArithmeticError, TypeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    notes = sorted(set(ru.NOTES))
    for row in payload if isinstance(payload, list) else [payload]:
        row["notes"] = notes
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(text + "\n\nOutcomes from rpg-dice's exact distributions; fights sampled." +
              "".join(f"\nNote: {n}" for n in notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
