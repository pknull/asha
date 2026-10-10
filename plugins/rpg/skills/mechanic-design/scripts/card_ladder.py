#!/usr/bin/env python3
"""Rating ladder for a card mechanic: exact odds at each rating R, from rpg-card.

Usage:
  card_ladder.py --deck DECK --range LO..HI --draw EXPR SUCCESS [options]

DECK is standard (54 with jokers), standard52, tarot (78), majors (22) or
minors (56). --draw sets how many cards are drawn at rating R, e.g. R, R+1 or 3.

SUCCESS is one of:
  --where COND [--at-least K]   at least K drawn cards meet COND (default K=1)
  --best-target T [--bonus EXPR]   highest card value, plus the bonus, reaches T
  --total-target T [--bonus EXPR]  total of the drawn values, plus the bonus, reaches T

COND is a condition on one card using: value, rank (1 = ace), suit (a name such
as 'Hearts' or 'Swords'; '' for jokers and major arcana), colour ('red',
'black' or None), arcana ('major', 'minor' or None), name, and the rating R
(so targets can move with the rating: "value + 2*R >= 13"). It may use
comparisons, and/or/not, in, numbers, strings and + - * // %. Nothing else is
evaluated. Examples: "value >= 10", "suit == 'Hearts'", "colour == 'red' and value > 10".

Options:
  --ace-high               standard aces are worth 14 instead of 1
  --remove COND            remove matching cards first (e.g. "name == 'Joker'")
  --replacement            each drawn card goes back before the next
  --count-bands E1,E2      with --where, also show bands of the match count
  --json                   print JSON instead of a table

Draws are without replacement unless --replacement is given. Figures are exact
fractions from rpg-card's Odds. Needs rpg-card 0.3.0 or later:
  pip install "git+https://github.com/pknull/rpg-card.git@v0.3.0"
or a checkout on PYTHONPATH. Exit status: 0 ok, 2 invalid input or unusable
rpg-card.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from fractions import Fraction

try:
    from card_picker import Deck, Odds
except ImportError:  # pragma: no cover - exercised by the CLI message
    Deck = Odds = None

DECKS = {
    "standard": lambda ace_high: Deck.standard(value_map={0: 14} if ace_high else None),
    "standard52": lambda ace_high: Deck.standard(jokers=False, value_map={0: 14} if ace_high else None),
    "tarot": lambda ace_high: Deck.tarot(),
    "majors": lambda ace_high: Deck.tarot(arcana="major"),
    "minors": lambda ace_high: Deck.tarot(arcana="minor"),
}
CARD_NAMES = {"value", "rank", "suit", "colour", "arcana", "name", "R"}
SAFE_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
              ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
              ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod, ast.Name, ast.Load,
              ast.Constant, ast.Tuple, ast.List)


def _check(text: str, names: set[str], nodes: tuple) -> ast.Expression:
    if len(text) > 200:
        raise ValueError("expressions are limited to 200 characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"{text!r} is not a valid expression") from exc
    for node in ast.walk(tree):
        if not isinstance(node, nodes):
            raise ValueError(f"{text!r} uses {type(node).__name__}, which is not allowed")
        if isinstance(node, ast.Name) and node.id not in names:
            raise ValueError(f"{text!r} uses unknown name {node.id!r}; allowed: {', '.join(sorted(names))}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, str, type(None))) \
                or isinstance(node, ast.Constant) and isinstance(node.value, bool):
            raise ValueError(f"{text!r} uses a constant of type {type(node.value).__name__}")
        if isinstance(node, ast.BinOp) and not (_numeric(node.left) and _numeric(node.right)) \
                or isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) \
                and not _numeric(node.operand):
            raise ValueError(f"{text!r} does arithmetic on something other than numbers")
    return tree


NUMERIC_NAMES = {"value", "rank", "R"}


def _numeric(node) -> bool:
    """True when node can only be a number: value, rank, R, an integer, or arithmetic on those."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool)
    if isinstance(node, ast.Name):
        return node.id in NUMERIC_NAMES
    if isinstance(node, ast.BinOp):
        return _numeric(node.left) and _numeric(node.right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _numeric(node.operand)
    return False


def compile_condition(text: str):
    code = compile(_check(text, CARD_NAMES, SAFE_NODES), "<condition>", "eval")

    def condition(card, rating: int = 0) -> bool:
        fields = {"value": card.value, "rank": card.rank_index + 1,
                  "suit": card.SUITS[card.suit] if card.suit else "",
                  "colour": card.colour, "arcana": card.arcana, "name": card.name(), "R": rating}
        return bool(eval(code, {"__builtins__": {}}, fields))
    return condition


def draws(text: str, rating: int) -> int:
    nodes = (ast.Expression, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.UnaryOp, ast.USub,
             ast.Name, ast.Load, ast.Constant)
    tree = _check(text, {"R"}, nodes)
    value = eval(compile(tree, "<draw>", "eval"), {"__builtins__": {}}, {"R": rating})
    if not isinstance(value, int):
        raise ValueError(f"{text!r} must give a whole number")
    return value


MAX_RATINGS = 200


def parse_range(text: str) -> list[int]:
    m = re.fullmatch(r"\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*", text)
    if not m or int(m.group(1)) > int(m.group(2)):
        raise ValueError("--range needs LO..HI with LO no greater than HI, e.g. --range 1..5")
    if int(m.group(2)) - int(m.group(1)) >= MAX_RATINGS:  # checked before anything is allocated
        raise ValueError(f"--range covers at most {MAX_RATINGS} ratings")
    return list(range(int(m.group(1)), int(m.group(2)) + 1))


def parse_edges(text: str) -> list[int]:
    try:
        edges = [int(x) for x in text.split(",") if x.strip()]
    except ValueError:
        edges = []
    if not edges or edges[0] < 1 or any(b <= a for a, b in zip(edges, edges[1:])):
        raise ValueError("--count-bands needs increasing whole numbers from 1, e.g. 1,2")
    return edges


def count_labels(edges: list[int]) -> list[str]:
    labels = ["0" if edges[0] == 1 else f"{edges[0] - 1} or fewer"]
    for lo, hi in zip(edges, edges[1:]):
        labels.append(str(lo) if hi - lo == 1 else f"{lo}-{hi - 1}")
    labels.append(f"{edges[-1]} or more")
    return [f"{label} match" if label == "1" else f"{label} matches" for label in labels]


def ladder(deck_name: str, ratings: list[int], draw: str, where: str | None = None, at_least: int = 1,
           best_target: int | None = None, total_target: int | None = None, bonus: str | None = None,
           remove: str | None = None, replacement: bool = False, ace_high: bool = False,
           count_edges: list[int] | None = None) -> list[dict]:
    if deck_name not in DECKS:
        raise ValueError(f"unknown deck {deck_name!r}; use one of {', '.join(DECKS)}")
    modes = [where is not None, best_target is not None, total_target is not None]
    if sum(modes) != 1:
        raise ValueError("give exactly one of --where, --best-target or --total-target")
    if at_least < 1:
        raise ValueError("--at-least must be 1 or more")
    if where is None and at_least != 1:
        raise ValueError("--at-least needs --where")
    if where is not None and bonus is not None:
        raise ValueError("--bonus needs --best-target or --total-target; with --where, put R in COND")
    condition = compile_condition(where) if where is not None else None
    removal = compile_condition(remove) if remove is not None else None
    rows = []
    for rating in ratings:
        deck = DECKS[deck_name](ace_high)
        if removal:
            deck.remove(predicate=lambda c, r=rating: removal(c, r))
        n = draws(draw, rating)
        if n < 0:
            raise ValueError(f"rating {rating} draws {n} cards")
        if not replacement and n > len(deck.cards):
            raise ValueError(f"rating {rating} draws {n} cards but the deck has {len(deck.cards)}")
        odds = Odds(deck)
        row = {"rating": rating, "cards": n, "bands": None}
        if condition:
            dist = odds.count(n, lambda c, r=rating: condition(c, r), replacement=replacement)
            row["success"] = sum((p for k, p in dist.items() if k >= at_least), Fraction(0))
            if count_edges:
                bands = [Fraction(0)] * (len(count_edges) + 1)
                for k, p in dist.items():
                    bands[sum(1 for e in count_edges if k >= e)] += p
                row["bands"] = bands
        else:
            extra = draws(bonus, rating) if bonus else 0
            kind = "highest" if best_target is not None else "sum"
            target = best_target if best_target is not None else total_target
            dist = odds.reading(n, kind=kind, replacement=replacement)
            row["success"] = sum((p for v, p in dist.items() if v + extra >= target), Fraction(0))
            row["bonus"] = extra
        rows.append(row)
    return rows


def table(rows: list[dict], count_edges: list[int] | None) -> str:
    head = ["R", "cards", "P(success)"]
    if rows[0].get("bonus") is not None and any(r.get("bonus") for r in rows):
        head.insert(2, "bonus")
    if count_edges:
        head += count_labels(count_edges)
    head.append("change in P(success)")
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    previous = None
    for r in rows:
        cells = [str(r["rating"]), str(r["cards"])]
        if "bonus" in head:
            cells.append(f"{r['bonus']:+d}")
        cells.append(f"{float(r['success']):.4f}")
        if count_edges:
            cells += [f"{float(p):.4f}" for p in r["bands"]]
        cells.append("" if previous is None else f"{float(r['success'] - previous):+.4f}")
        previous = r["success"]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "Exact figures from rpg-card."]
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
    parser.add_argument("--deck", required=True, help="standard, standard52, tarot, majors or minors")
    parser.add_argument("--range", required=True, metavar="LO..HI", help="ratings to tabulate")
    parser.add_argument("--draw", required=True, metavar="EXPR", help="cards drawn at rating R, e.g. R or R+1")
    parser.add_argument("--where", metavar="COND", help="condition a drawn card must meet")
    parser.add_argument("--at-least", type=int, default=1, metavar="K", help="matching cards needed (default 1)")
    parser.add_argument("--best-target", type=int, metavar="T", help="highest value plus bonus must reach T")
    parser.add_argument("--total-target", type=int, metavar="T", help="total value plus bonus must reach T")
    parser.add_argument("--bonus", metavar="EXPR", help="bonus at rating R for best or total, e.g. R")
    parser.add_argument("--remove", metavar="COND", help="remove matching cards before drawing")
    parser.add_argument("--ace-high", action="store_true", help="standard aces worth 14")
    parser.add_argument("--replacement", action="store_true", help="each card goes back before the next")
    parser.add_argument("--count-bands", metavar="E1,E2", help="with --where, bands of the match count")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 2, -1, -1):
        if argv[i] in ("--range", "--bonus", "--draw") and re.match(r"-\d", argv[i + 1]):
            argv[i:i + 2] = [f"{argv[i]}={argv[i + 1]}"]
    args = parser.parse_args(argv)
    if Odds is None:
        print('needs rpg-card 0.3.0 or later: pip install "git+https://github.com/pknull/rpg-card.git@v0.3.0" '
              "or add a checkout to PYTHONPATH", file=sys.stderr)
        return 2
    try:
        edges = parse_edges(args.count_bands) if args.count_bands else None
        if edges and args.where is None:
            raise ValueError("--count-bands needs --where")
        rows = ladder(args.deck, parse_range(args.range), args.draw, where=args.where, at_least=args.at_least,
                      best_target=args.best_target, total_target=args.total_target, bonus=args.bonus,
                      remove=args.remove, replacement=args.replacement, ace_high=args.ace_high,
                      count_edges=edges)
    except (ValueError, ArithmeticError, TypeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(to_json(rows) if args.json else table(rows, edges))
    return 0


if __name__ == "__main__":
    sys.exit(main())
