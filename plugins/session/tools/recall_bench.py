#!/usr/bin/env python3
"""Warn-only question-to-memory recall benchmark (hit@k)."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from memory_retrieval import build_entries, discover_memory_dirs, rank


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_fixtures(path: Path) -> list[dict[str, str]]:
    """Load the deliberately tiny q/expect YAML schema without dependencies."""
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        raw = json.loads(text)
        return [{"q": str(row["q"]), "expect": str(row["expect"])} for row in raw]
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split(" #", 1)[0].rstrip()
        match = re.match(r"^\s*-\s*q\s*:\s*(.+)$", line)
        if match:
            if current:
                rows.append(current)
            current = {"q": _scalar(match.group(1))}
            continue
        match = re.match(r"^\s+expect\s*:\s*(.+)$", line)
        if match and current:
            current["expect"] = _scalar(match.group(1))
    if current:
        rows.append(current)
    bad = [i + 1 for i, row in enumerate(rows) if not row.get("q") or not row.get("expect")]
    if bad:
        raise ValueError(f"fixtures missing q/expect at entries: {bad}")
    return rows


def find_fixtures(project_dir: Path, explicit: str | None) -> tuple[Path, bool]:
    if explicit:
        return Path(explicit).expanduser(), False
    local = project_dir / "Memory" / "recall_fixtures.yaml"
    if local.is_file():
        return local, False
    return Path.home() / ".asha" / "recall_fixtures.yaml", True


def _load_prior(path: Path) -> dict[str, bool]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): bool(v) for k, v in data.get("hits", {}).items()}
    except (OSError, ValueError, TypeError):
        return {}


def run_benchmark(fixtures: list[dict[str, str]], entries, *, k: int,
                  prior: dict[str, bool]) -> dict:
    cases = []
    hits: dict[str, bool] = {}
    for fixture in fixtures:
        ranked = rank(fixture["q"], entries, limit=k)
        expected = fixture["expect"]
        hit = any(row["id"] == expected for row in ranked)
        key = f"{fixture['q']}\x00{expected}"
        new_miss = not hit and prior.get(key, True)
        hits[key] = hit
        cases.append({
            "q": fixture["q"], "expect": expected, "hit": hit,
            "new_miss": new_miss,
            "top": [{"id": row["id"], "score": row["score"]} for row in ranked],
        })
    count = sum(1 for case in cases if case["hit"])
    return {
        "status": "ok", "metric": f"hit@{k}", "hits": count,
        "total": len(cases), "score": round(count / len(cases), 4) if cases else 0.0,
        "new_misses": sum(1 for case in cases if case["new_miss"]),
        "cases": cases, "state": hits,
    }


def human(result: dict) -> str:
    if result.get("status") != "ok":
        return f"recall benchmark WARN: {result.get('error', 'unknown error')}"
    lines = ["recall benchmark:"]
    for case in result["cases"]:
        mark = "HIT " if case["hit"] else "MISS"
        fresh = " NEW" if case["new_miss"] else ""
        ids = ", ".join(item["id"] for item in case["top"]) or "(no matches)"
        lines.append(f"  {mark}{fresh}  {case['expect']}  <-  {ids}")
    lines.append(
        f"  {result['metric']}: {result['hits']}/{result['total']} "
        f"({result['score'] * 100:.1f}%), new misses: {result['new_misses']}"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Warn-only memory recall benchmark")
    parser.add_argument("--fixtures")
    parser.add_argument("--project-dir", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    parser.add_argument("--memory-dir", action="append", default=[])
    parser.add_argument("--learnings-dir", default=str(Path.home() / ".asha" / "learnings"))
    parser.add_argument("--state-file", default=str(Path.home() / ".cache" / "asha" / "recall-bench.json"))
    parser.add_argument("--no-state", action="store_true")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--format", choices=("human", "json", "both"), default="both")
    args = parser.parse_args()

    try:
        project = Path(args.project_dir).resolve()
        fixture_path, global_fixtures = find_fixtures(project, args.fixtures)
        fixtures = load_fixtures(fixture_path)
        memory_dirs = ([Path(item).expanduser() for item in args.memory_dir]
                       if args.memory_dir else
                       discover_memory_dirs(project, all_projects=global_fixtures))
        entries = build_entries(memory_dirs, Path(args.learnings_dir).expanduser())
        state_path = Path(args.state_file).expanduser()
        prior = {} if args.no_state else _load_prior(state_path)
        result = run_benchmark(fixtures, entries, k=max(1, args.k), prior=prior)
        result.update({"fixtures": str(fixture_path), "entries": len(entries)})
        if not args.no_state:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_path.with_name(f".{state_path.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps({"hits": result.pop("state")}, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, state_path)
        else:
            result.pop("state", None)
    except Exception as exc:  # warn-only boundary: malformed fixtures cannot block save
        result = {"status": "warning", "error": str(exc), "hits": 0, "total": 0,
                  "score": 0.0, "new_misses": 0, "cases": []}

    if args.format in {"human", "both"}:
        print(human(result), file=sys.stderr if args.format == "both" else sys.stdout)
    if args.format in {"json", "both"}:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
