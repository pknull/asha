#!/usr/bin/env python3
"""Oracle answers and random tables for solo play, GM-less play and improvising.

Usage:
  oracle.py ask --likelihood WORD|N [--question TEXT] [--odds] [--hidden]
                [--seed S] [--log FILE] [--json]
  oracle.py table FILE|--items "A; B; C" [--dice EXPR] [--odds] [--hidden]
                [--seed S] [--log FILE] [--json]
  oracle.py note TEXT --log FILE
  oracle.py recall --log FILE

ask rolls 1d100 against a chance of yes: a word (very unlikely 10, unlikely
30, even 50, likely 70, very likely 90) or a whole number from 1 to 99. The
answer has six grades (an invented scheme, not any published oracle's):
  yes, and   the lowest fifth of the yes range: yes, with something extra
  yes        the rest of the yes range
  yes, but   the five numbers just under the chance: yes, with a catch
  no, but    the five numbers just over the chance: no, with something gained
  no         the rest of the no range
  no, and    the highest fifth of the no range: no, and it gets worse
--odds prints the exact chance of each grade instead of rolling.

table rolls on a random table. A FILE has one entry per line; blank lines and
lines starting with # are skipped. Plain lines are equally likely (a 1dN
roll). Numbered lines ("2-6 Quiet road", "2 to 6 Quiet road", "7: Merchant",
"12) Fog", or signed ones for Fate dice, --dice '4d{-1,0,1}': "-4--2: Bad",
"-1 to 1: Mixed", "2-4: Good"; a line starting "+1" is plain text, not a total)
are rolled with --dice EXPR, an rpg-dice expression (quote it in the shell)
whose possible totals the ranges must cover exactly, with no gaps or
overlaps; numbered lines without --dice are refused rather than treated as
equally likely. --items "A; B; C" gives a table inline. --odds prints each
entry's exact chance instead of rolling; with --json the chances come under
"odds", beside "hidden".

note TEXT records an established fact or an interpretation of an answer in
the log; recall prints the log (questions, answers, table rolls and notes) to
read before asking again.

--hidden marks the output GM ONLY. --log FILE appends one JSON line per call,
at a path the user names; nothing is written without it. Play rolls are unseeded; --seed is for tests
and replays.

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
from fractions import Fraction
from pathlib import Path

try:
    from dice_roller.DiceException import DiceException
    from dice_roller.DiceProbability import DiceProbability
    from dice_roller.DiceThrower import DiceThrower
except ImportError:  # pragma: no cover - exercised by the CLI message
    DiceProbability = DiceThrower = None
    DiceException = Exception

LIKELIHOOD = {"very unlikely": 10, "unlikely": 30, "even": 50, "likely": 70, "very likely": 90}
ANSWERS = ("yes, and", "yes", "yes, but", "no, but", "no", "no, and")
GM_ONLY = "GM ONLY (do not show players)"
RANGED = re.compile(r"^(-?\d+)(?:\s*(?:-|to)\s*([+-]?\d+))?(?:\s*[:.)]\s*|\s+)(.+)$")
MAX_TABLE_BYTES = 1_000_000


def need_engine() -> None:
    if DiceProbability is None:
        raise ValueError('needs rpg-dice 0.5.0 or later: pip install "git+https://github.com/pknull/rpg-dice.git@v0.5.0" '
                         "or add a checkout to PYTHONPATH")


def parse_chance(text: str) -> int:
    t = text.strip().lower().replace("-", " ")
    if t in LIKELIHOOD:
        return LIKELIHOOD[t]
    if re.fullmatch(r"\d+", t) and 1 <= int(t) <= 99:
        return int(t)
    raise ValueError(f"likelihood {text!r}: use {', '.join(LIKELIHOOD)} or a whole number from 1 to 99")


def answer_bands(chance: int) -> dict[str, tuple[int, int]]:
    """Inclusive d100 ranges for each grade; empty ranges have lo > hi."""
    and_yes = chance // 5
    and_no = 100 - (100 - chance) // 5
    return {"yes, and": (1, and_yes), "yes": (and_yes + 1, max(and_yes, chance - 5)),
            "yes, but": (max(and_yes, chance - 5) + 1, chance), "no, but": (chance + 1, min(chance + 5, and_no)),
            "no": (min(chance + 5, and_no) + 1, and_no), "no, and": (and_no + 1, 100)}


def answer_for(roll: int, bands: dict[str, tuple[int, int]]) -> str:
    return next(name for name, (lo, hi) in bands.items() if lo <= roll <= hi)


def totals(expr: str) -> dict[int, Fraction]:
    need_engine()
    try:
        result = DiceProbability().exact(expr)
        dist = result.marginal("total")
    except DiceException as exc:
        reason = "; ".join(str(a) for a in exc.args) or "invalid expression"
        raise ValueError(f"{expr!r}: rpg-dice rejects it ({reason})") from exc
    if result.unresolved:
        raise ValueError(f"{expr!r} has unresolved exploding-die outcomes; "
                         "a ranged table needs a complete finite distribution")
    if None in dist or any(Fraction(v).denominator != 1 for v in dist):
        raise ValueError(f"{expr!r} must give whole-number totals")
    return {int(v): p for v, p in dist.items()}


def answer_odds(chance: int) -> dict[str, Fraction]:
    d100 = totals("1d100")
    bands = answer_bands(chance)
    return {name: sum((p for v, p in d100.items() if lo <= v <= hi), Fraction(0)) for name, (lo, hi) in bands.items()}


def roll_total(expr: str) -> int:
    need_engine()
    result = DiceThrower().throw(expr)
    if not isinstance(result, dict) or "total" not in result:
        raise ValueError(f"{expr!r}: rpg-dice rejects it ({result})")
    return int(result["total"])


def parse_table(text: str, dice: str | None) -> list[tuple[int, int, str]] | list[str]:
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise ValueError("the table has no entries")
    ranged = [RANGED.match(ln) for ln in lines]
    if not any(ranged):
        if dice:
            raise ValueError("plain tables roll 1dN; number the entries as ranges to use other dice")
        return lines
    if not all(ranged):
        bad = next(ln for ln, m in zip(lines, ranged) if not m)
        raise ValueError(f"mix of ranged and plain lines; {bad!r} has no range")
    if not dice:
        raise ValueError("these entries are numbered, so the table needs --dice (e.g. --dice 2d6); remove the "
                         "numbers for an equally likely list")
    entries = []
    for m in ranged:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if hi < lo:
            raise ValueError(f"range {lo}-{hi} runs backwards")
        entries.append((lo, hi, m.group(3).strip()))
    support = set(totals(dice))
    covered: list[int] = []
    for lo, hi, _ in entries:
        covered += range(lo, hi + 1)
    if len(covered) != len(set(covered)):
        raise ValueError("table ranges overlap")
    if set(covered) != support:
        missing, extra = sorted(support - set(covered)), sorted(set(covered) - support)
        raise ValueError(f"ranges must cover exactly what {dice} can roll; missing {missing}, impossible {extra}")
    return entries


def table_odds(entries, dice: str | None) -> dict[str, Fraction]:
    odds: dict[str, Fraction] = {}
    if entries and isinstance(entries[0], str):
        for name in entries:
            odds[name] = odds.get(name, Fraction(0)) + Fraction(1, len(entries))
        return odds
    dist = totals(dice)
    for lo, hi, name in entries:
        odds[name] = odds.get(name, Fraction(0)) + sum((p for v, p in dist.items() if lo <= v <= hi), Fraction(0))
    return odds


def roll_table(entries, dice: str | None) -> tuple[int, str]:
    if isinstance(entries[0], str):
        n = roll_total(f"1d{len(entries)}")
        return n, entries[n - 1]
    total = roll_total(dice)
    return total, next(name for lo, hi, name in entries if lo <= total <= hi)


def recall(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"no log at {path}")
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        kind = r.get("type") or ("ask" if "chance" in r else "table" if "entry" in r else "note")
        hidden = " (GM only)" if r.get("hidden") else ""
        if kind == "ask":
            lines.append(f"- asked{hidden}: {r.get('question') or '(no question recorded)'} at {r.get('chance')}%: "
                         f"rolled {r.get('roll')}, {r.get('answer')}")
        elif kind == "table":
            lines.append(f"- table{hidden} {Path(str(r.get('table'))).name}: rolled {r.get('roll')}, {r.get('entry')}")
        else:
            lines.append(f"- note: {r.get('text')}")
    return "\n".join(lines) if lines else "(the log is empty)"


def fmt_odds(odds: dict[str, Fraction]) -> str:
    return "\n".join(f"- {name}: {p} ({float(p):.4f})" for name, p in odds.items())


def odds_output(heading: str, odds: dict[str, Fraction], hidden: bool, as_json: bool) -> str:
    if as_json:
        return json.dumps({"hidden": hidden, "odds": {k: str(v) for k, v in odds.items()}}, indent=2)
    return "\n".join(([GM_ONLY] if hidden else []) + [heading, fmt_odds(odds)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("note", help="record a fact or interpretation in the log")
    p.add_argument("text")
    p.add_argument("--log", required=True, metavar="FILE")
    p = sub.add_parser("recall", help="print the log")
    p.add_argument("--log", required=True, metavar="FILE")
    for name in ("ask", "table"):
        p = sub.add_parser(name, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        p.add_argument("--odds", action="store_true", help="print exact chances instead of rolling")
        p.add_argument("--hidden", action="store_true", help="mark the result GM ONLY")
        p.add_argument("--seed", type=int, help="seed for tests and replays; omit in play")
        p.add_argument("--log", metavar="FILE", help="append one JSON line per call to FILE")
        p.add_argument("--json", action="store_true", help="print JSON instead of text")
        if name == "ask":
            p.add_argument("--likelihood", required=True, metavar="WORD|N", help="chance of yes")
            p.add_argument("--question", help="the question, for the record")
        else:
            p.add_argument("file", nargs="?", metavar="FILE", help="table file, one entry per line")
            p.add_argument("--items", help='inline table, "A; B; C"')
            p.add_argument("--dice", metavar="EXPR", help="rpg-dice expression for a ranged table")
    args = parser.parse_args(argv)
    if getattr(args, "seed", None) is not None:
        random.seed(args.seed)  # DiceThrower draws from the module-level generator
    try:
        if args.command == "note":
            if not args.text.strip():
                raise ValueError("a note needs text")
            record = {"type": "note", "text": args.text.strip(),
                      "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            with open(args.log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            print(f"noted: {record['text']}")
            return 0
        if args.command == "recall":
            print(recall(Path(args.log)))
            return 0
        if args.command == "ask":
            chance = parse_chance(args.likelihood)
            if args.odds:
                print(odds_output(f"Chance of yes {chance}%; exact chance of each answer:", answer_odds(chance),
                                  args.hidden, args.json))
                return 0
            roll = roll_total("1d100")
            record = {"type": "ask", "question": args.question, "chance": chance, "roll": roll,
                      "answer": answer_for(roll, answer_bands(chance))}
            text = (f"{args.question} " if args.question else "") + f"({chance}% yes): rolled {roll}, " \
                f"{record['answer']}"
        else:
            if bool(args.file) == bool(args.items is not None):
                raise ValueError("give a table FILE or --items, not both or neither")
            if args.file:
                path = Path(args.file)
                if not path.is_file() or path.stat().st_size > MAX_TABLE_BYTES:
                    raise ValueError(f"cannot read {path} as a table (missing, or over {MAX_TABLE_BYTES} bytes)")
                source = path.read_text(encoding="utf-8")
            else:
                source = "\n".join(x.strip() for x in args.items.split(";"))
            entries = parse_table(source, args.dice)
            if args.odds:
                print(odds_output("Exact chance of each entry:", table_odds(entries, args.dice), args.hidden,
                                  args.json))
                return 0
            roll, entry = roll_table(entries, args.dice)
            record = {"type": "table", "table": str(Path(args.file).resolve()) if args.file else "inline",
                      "dice": args.dice, "roll": roll, "entry": entry}
            text = f"rolled {roll}: {entry}"
        record.update({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "hidden": args.hidden, "seed": args.seed})
        if args.log:
            with open(args.log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print("\n".join(([GM_ONLY] if args.hidden else []) + [text]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
