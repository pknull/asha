#!/usr/bin/env python3
"""Asha's gate over the vendored OKF validator.

Runs the pinned upstream validate.py on a bundle with an exclude list, then
refuses the pass unless the validator's own summary line names the number of
files Asha expected it to see. A validator that saw nothing (`0 file(s)`) is
never a pass. Under --strict three Asha lints join the upstream set, covering
what the upstream validator does not check: every concept reachable from the
root index.md (the upstream orphan lint accepts a disconnected cycle), log.md
dates newest first, and `verified` entries that are not mappings (the upstream
helper drops them silently).

Exit codes: 0 pass; 1 errors, lints or an unexpected file count; 2 the validator
could not run (PyYAML absent, bad path, no summary). Exit 2 is reported as
"validator unavailable" and is never a pass.

Usage:
    okf_validate.py BUNDLE [--strict] [--exclude GLOB]... [--json]
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

try:
    import validate as upstream
    from okf_io import RESERVED, iter_markdown, read_scalars, split_frontmatter
except ImportError:  # run as a script from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import validate as upstream
    from okf_io import RESERVED, iter_markdown, read_scalars, split_frontmatter

SUMMARY_RE = re.compile(r"^(\d+) file\(s\), (\d+) error\(s\)(?:, (\d+) lint\(s\))?$")


def _asha_lints(root: Path, files: list[Path]) -> list[tuple[str, str]]:
    from okf_common import extract_links, is_external, path_part, resolve_link
    from okf_log import order_problems
    lints: list[tuple[str, str]] = []
    by_resolved = {f.resolve(): f for f in files}
    root_index = root / "index.md"
    if root_index.is_file():
        seen = {root_index.resolve()}
        queue = [root_index]
        while queue:
            current = queue.pop()
            if current.name == "log.md":
                continue
            try:
                text = current.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for target in extract_links(text):
                if is_external(target) or target.startswith("#"):
                    continue
                part = path_part(target)
                if not part.endswith(".md"):
                    continue
                resolved = resolve_link(part, current, root).resolve()
                if resolved in by_resolved and resolved not in seen:
                    seen.add(resolved)
                    queue.append(by_resolved[resolved])
        for path in files:
            if path.name in RESERVED:
                continue
            if path.resolve() not in seen:
                lints.append((path.relative_to(root).as_posix(),
                              "not reachable from the root index.md (asha: index-first navigation)"))
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.name == "log.md":
            for problem in order_problems(text):
                lints.append((rel, problem + " (asha)"))
            continue
        if path.name in RESERVED:
            continue
        block, _ = split_frontmatter(text)
        if block is None:
            continue
        data = read_scalars(text)
        verified = data.get("verified")
        if isinstance(verified, list) and any(not isinstance(item, dict) for item in verified):
            lints.append((rel, "'verified' contains an entry that is not a mapping; "
                               "the upstream helper would drop it silently (asha)"))
    return lints


def run(bundle: str, strict: bool = False, excludes=(), allow_empty: bool = False) -> dict:
    root = Path(bundle)
    result = {"bundle": str(root), "strict": strict, "excludes": list(excludes),
              "files": None, "errors": None, "lints": None, "asha_lints": [], "rc": 2,
              "status": "validator unavailable", "output": ""}
    if upstream.yaml is None:
        result["output"] = "error: PyYAML is required to validate OKF bundles"
        return result
    if not root.is_dir():
        result["output"] = f"error: not a directory: {root}"
        return result
    expected = iter_markdown(root, excludes)
    upstream.iter_markdown_files = lambda _root, _files=expected: list(_files)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = upstream.main([str(root)] + (["--strict"] if strict else []))
    output = buffer.getvalue()
    result["output"] = output
    if rc == 2:
        return result
    summary = None
    for line in reversed(output.splitlines()):
        match = SUMMARY_RE.match(line.strip())
        if match:
            summary = match
            break
    if summary is None:
        result["output"] += "\nerror: validator printed no summary line"
        return result
    files, errors, lints = int(summary.group(1)), int(summary.group(2)), int(summary.group(3) or 0)
    result.update(files=files, errors=errors, lints=lints)
    problems = []
    if files != len(expected):
        problems.append(f"validator saw {files} file(s) but {len(expected)} were expected")
    if files == 0 and not allow_empty:
        problems.append("validator saw no files; an empty validation set is not a pass")
    asha = _asha_lints(root, expected) if strict else []
    result["asha_lints"] = [f"{rel}: {msg}" for rel, msg in asha]
    result["count_problems"] = problems
    failed = rc != 0 or problems or (strict and asha)
    result["rc"] = 1 if failed else 0
    result["status"] = "failed" if failed else "pass"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate an OKF bundle through the pinned upstream validator, with Asha's gate.")
    parser.add_argument("bundle")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--exclude", action="append", default=[], help="bundle-relative glob or dir/ prefix to skip")
    parser.add_argument("--allow-empty", action="store_true", help="accept a bundle with no markdown files")
    parser.add_argument("--json", action="store_true", help="print a JSON summary instead of text")
    args = parser.parse_args(argv)
    result = run(args.bundle, args.strict, args.exclude, args.allow_empty)
    if args.json:
        print(json.dumps(result, indent=2))
        return result["rc"]
    sys.stdout.write(result["output"].rstrip("\n") + "\n")
    for line in result.get("asha_lints", []):
        print(f"LINT   {line}")
    for line in result.get("count_problems", []):
        print(f"ERROR  .: {line}")
    if result["rc"] == 2:
        print("okf_validate: validator unavailable (exit 2); this is not a pass")
    else:
        total_lints = (result["lints"] or 0) + len(result["asha_lints"])
        print(f"okf_validate: {result['status']}; {result['files']} file(s) validated, "
              f"{result['errors']} error(s), {total_lints} lint(s)")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
