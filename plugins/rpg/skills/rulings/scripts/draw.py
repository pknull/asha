#!/usr/bin/env python3
"""Draw cards for play with rpg-card, keeping the deck's state between draws.

Usage:
  draw.py --deck DECK [--count N] [--state FILE] [--reshuffle] [--new]
          [--reversals] [--odds COND] [--hidden] [--log FILE] [--seed S] [--json]

DECK is standard (54 with jokers), standard52, tarot (78), majors (22) or
minors (56). The deck is shuffled when created; tarot cards are all upright
unless --reversals is given, which turns each card reversed half the time.

--state FILE keeps the deck between draws: drawn cards stay out until
--reshuffle returns every card and shuffles. The file is created on first use
and rewritten after each draw. It records the order of the cards still to
come, so it is GM-only material: keep it where players cannot read it.
--new replaces whatever the file holds with a fresh deck. Without --state,
each call draws from a fresh shuffled deck.

Drawing more cards than are left is refused; the game decides whether to
reshuffle. --reshuffle with --count 0 reshuffles without drawing, for a game
that reshuffles at the end of an action. Watch for trigger cards yourself:
the script does not know the game's rules.

--odds COND prints the exact chance that the next card meets COND, from the
deck as it stands before this call draws. COND uses value, rank, suit (a name
such as 'Cups'; '' for jokers and major arcana), colour, arcana ('major' or
'minor'), name and orientation, with comparisons, and/or/not, in, numbers,
strings and + - * // %. Values: standard aces 1, jacks 11, queens 12, kings
13, jokers 0; tarot minors ace 1 to 10, page 11, knight 12, queen 13, king
14; major arcana 0 (The Fool) to 21 (The World). Example:
  --odds "suit in ('Cups', 'Pentacles') and value >= 6" --count 0

--hidden marks the output GM ONLY. --log FILE appends one JSON line per call
(time, deck, cards, reshuffle, cards left, seed); nothing is written without
it. Play draws are unseeded; --seed is for tests and replays.

Needs rpg-card 0.3.0 or later:
  pip install "git+https://github.com/pknull/rpg-card.git@v0.3.0"
or a checkout on PYTHONPATH. Exit status: 0 ok, 2 refused (invalid input,
unusable rpg-card, or state/log-open failure), 3 draw completed but logging
failed. Status 3 still prints the result and saves any requested state;
do not repeat the draw. Check the log before repairing a missing/partial entry.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

try:
    from card_picker import Deck, Odds, SnapshotError, restore, snapshot
except ImportError:  # pragma: no cover - exercised by the CLI message
    Deck = Odds = restore = snapshot = None
    SnapshotError = ValueError

FORMAT = "rpg-skills/draw-state/1"
MAX_STATE_BYTES = 5_000_000
GM_ONLY = "GM ONLY (do not show players)"
BUILDERS = {
    "standard": lambda seed: Deck.standard(seed=seed),
    "standard52": lambda seed: Deck.standard(jokers=False, seed=seed),
    "tarot": lambda seed: Deck.tarot(seed=seed),
    "majors": lambda seed: Deck.tarot(arcana="major", seed=seed),
    "minors": lambda seed: Deck.tarot(arcana="minor", seed=seed),
}


CARD_NAMES = {"value", "rank", "suit", "colour", "arcana", "name", "orientation"}
NUMERIC_NAMES = {"value", "rank"}
SAFE_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
              ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
              ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod, ast.Name, ast.Load,
              ast.Constant, ast.Tuple, ast.List)


def _numeric(node) -> bool:
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
    """A condition on one card; only the whitelisted expression language is evaluated."""
    if len(text) > 200:
        raise ValueError("conditions are limited to 200 characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"{text!r} is not a valid condition") from exc
    for node in ast.walk(tree):
        if not isinstance(node, SAFE_NODES):
            raise ValueError(f"{text!r} uses {type(node).__name__}, which is not allowed")
        if isinstance(node, ast.Name) and node.id not in CARD_NAMES:
            raise ValueError(f"{text!r} uses unknown name {node.id!r}; allowed: {', '.join(sorted(CARD_NAMES))}")
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool)
                                               or not isinstance(node.value, (int, str, type(None)))):
            raise ValueError(f"{text!r} uses a constant of type {type(node.value).__name__}")
        if isinstance(node, ast.BinOp) and not (_numeric(node.left) and _numeric(node.right)) \
                or isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) \
                and not _numeric(node.operand):
            raise ValueError(f"{text!r} does arithmetic on something other than numbers")
    code = compile(tree, "<condition>", "eval")

    def condition(card) -> bool:
        fields = {"value": card.value, "rank": card.rank_index + 1,
                  "suit": card.SUITS[card.suit] if card.suit else "", "colour": card.colour,
                  "arcana": card.arcana, "name": card.name(), "orientation": card.orientation}
        return bool(eval(code, {"__builtins__": {}}, fields))
    return condition


def next_card_odds(deck, text: str) -> Fraction:
    if not deck.cards:
        raise ValueError("no cards left to give odds for; --reshuffle first")
    return Odds(deck).count(1, compile_condition(text)).get(1, Fraction(0))


def json_number(value):
    value = Fraction(value)
    return value.numerator if value.denominator == 1 else str(value)


def fresh(deck_name: str, reversals: bool, seed: int | None):
    deck = BUILDERS[deck_name](seed)
    deck.shuffle(upright_probability=0.5 if reversals else 1)
    return deck


def load(path: Path, deck_name: str):
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            raise ValueError(f"larger than {MAX_STATE_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path} as a deck state ({exc}); --new starts a fresh deck") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(f"{path} is not a draw.py deck state; --new starts a fresh deck")
    if data.get("deck") != deck_name:
        raise ValueError(f"{path} holds a {data.get('deck')} deck, not {deck_name}")
    try:
        deck, _ = restore(data["snapshot"])
    except (SnapshotError, KeyError, TypeError) as exc:
        raise ValueError(f"{path} holds a damaged deck state ({exc}); --new starts a fresh deck") from exc
    return deck, bool(data.get("reversals"))


def save(path: Path, deck_name: str, reversals: bool, deck) -> None:
    payload = json.dumps({"format": FORMAT, "deck": deck_name, "reversals": reversals,
                          "snapshot": snapshot(deck)})
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def card_text(card, reversals: bool) -> str:
    name = card.name()
    return f"{name} (reversed)" if reversals and card.orientation == "reversed" else name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deck", required=True, help="standard, standard52, tarot, majors or minors")
    parser.add_argument("--count", type=int, default=1, metavar="N", help="cards to draw (default 1)")
    parser.add_argument("--state", metavar="FILE", help="deck state kept between draws (GM-only)")
    parser.add_argument("--reshuffle", action="store_true", help="return every card and shuffle before drawing")
    parser.add_argument("--new", action="store_true", help="replace the state file with a fresh deck")
    parser.add_argument("--reversals", action="store_true", help="cards can come up reversed")
    parser.add_argument("--odds", metavar="COND", help="exact chance the next card meets COND, before drawing")
    parser.add_argument("--hidden", action="store_true", help="mark the result GM ONLY")
    parser.add_argument("--log", metavar="FILE", help="append one JSON line per call to FILE")
    parser.add_argument("--seed", type=int, help="seed for tests and replays; omit in play")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if Deck is None:
        print('needs rpg-card 0.3.0 or later: pip install "git+https://github.com/pknull/rpg-card.git@v0.3.0" '
              "or add a checkout to PYTHONPATH", file=sys.stderr)
        return 2
    try:
        if args.deck not in BUILDERS:
            raise ValueError(f"unknown deck {args.deck!r}; use one of {', '.join(BUILDERS)}")
        if args.count < 0:
            raise ValueError("--count must be 0 or more")
        path = Path(args.state) if args.state else None
        reversals = args.reversals
        if path and path.exists() and not args.new:
            deck, reversals = load(path, args.deck)
            if args.reversals and not reversals:
                raise ValueError(f"{path} was created without --reversals; use --new to change that")
        else:
            deck = fresh(args.deck, reversals, args.seed)
        if args.reshuffle:
            deck.reshuffle(all_cards=True, upright_probability=0.5 if reversals else 1)
        before = len(deck.cards)
        odds = next_card_odds(deck, args.odds) if args.odds else None
        if args.count > len(deck.cards):
            raise ValueError(f"only {len(deck.cards)} cards left; draw fewer, or add --reshuffle if the game "
                             "reshuffles now")
        # Refuse log-open failures before changing state; later failures cannot undo a saved draw.
        log = open(args.log, "a", encoding="utf-8") if args.log else None
        log_error = None
        try:
            cards = deck.draw(args.count)
            if path:
                save(path, args.deck, reversals, deck)
            if log:
                record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "deck": args.deck,
                          "state": str(path) if path else None, "cards": [card_text(c, reversals) for c in cards],
                          "hidden": args.hidden, "reshuffled": args.reshuffle, "left": len(deck.cards),
                          "seed": args.seed}
                try:
                    log.write(json.dumps(record) + "\n")
                except OSError as exc:
                    log_error = exc
        finally:
            if log:
                try:
                    log.close()
                except OSError as exc:
                    # Preserve a state-save exception already in flight, or the first log error.
                    if log_error is None:
                        log_error = exc
    except (ValueError, OSError, ArithmeticError, TypeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if log_error is not None:
        state_status = f"state saved in {path}" if path else "without a state file"
        print(f"draw completed ({state_status}); log failed: {log_error}; do not repeat this draw. "
              "Check the log before repairing a missing/partial entry.", file=sys.stderr)
    if args.json:
        print(json.dumps({"deck": args.deck, "hidden": args.hidden, "left": len(deck.cards),
                          "reshuffled": args.reshuffle, "odds": str(odds) if odds is not None else None,
                          "cards": [{"name": c.name(), "value": json_number(c.value),
                                     "orientation": c.orientation if reversals else "upright"}
                                    for c in cards]}, indent=2))
        return 3 if log_error is not None else 0
    lines = [GM_ONLY] if args.hidden else []
    if odds is not None:
        lines.append(f"Chance the next card meets {args.odds!r}: {odds} ({float(odds):.4f}), "
                     f"from the {before} cards left before this draw (exact)")
    if args.reshuffle:
        lines.append("Reshuffled every card back into the deck")
    if cards:
        lines.append(f"Drew {len(cards)} from {args.deck}: " + "; ".join(card_text(c, reversals) for c in cards))
    lines.append(f"{len(deck.cards)} left" + (f" (state in {path})" if path else ""))
    print("\n".join(lines))
    return 3 if log_error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
