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

Link grammar. In `standard` mode (the default) the upstream strict lints run as
shipped and Asha adds its own on top. In `obsidian` mode, selected by the
bundle's `.okfconfig` ({"links": "obsidian"}) or `--links obsidian`, the
upstream run checks only the three conformance errors, and Asha runs the whole
strict set itself (recommended fields and the v0.2 field lints through the
upstream functions; links, orphans and reachability through okf_links, which
reads `[[wikilinks]]` like Obsidian and decodes percent-encoded targets). A
vault whose note names carry spaces cannot pass the upstream link grammar,
whose targets stop at whitespace, and this mode is how it is gated instead.

Exit codes: 0 pass; 1 errors, lints or an unexpected file count; 2 the validator
could not run (PyYAML absent, bad path, no summary). Exit 2 is reported as
"validator unavailable" and is never a pass.

Usage:
    okf_validate.py BUNDLE [--strict] [--exclude GLOB]... [--links standard|obsidian] [--json]
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
    from okf_io import RESERVED, effective_excludes, iter_markdown, link_grammar, lint_waivers, read_scalars, split_frontmatter
    import okf_links
except ImportError:  # run as a script from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import validate as upstream
    from okf_io import RESERVED, effective_excludes, iter_markdown, link_grammar, lint_waivers, read_scalars, split_frontmatter
    import okf_links

SUMMARY_RE = re.compile(r"^(\d+) file\(s\), (\d+) error\(s\)(?:, (\d+) lint\(s\))?$")


def _field_lints(root: Path, files: list[Path], waivers=()) -> list[tuple[str, str]]:
    """The upstream strict field lints, run by Asha when upstream runs non-strict.

    A waived field's lints (messages that name the field first, e.g. `'status'
    should be one of ...`) are dropped; the verdict names the waiver.
    """
    lints: list[tuple[str, str]] = []
    def waived(msg: str) -> bool:
        return any(msg.startswith(f"'{w}'") or msg.startswith(f"missing recommended field '{w}'") for w in waivers)
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.name in RESERVED:
            if path.name == "index.md" and path.parent == root:
                data = read_scalars(text)
                if "okf_version" in data and not upstream.is_nonempty_string(data.get("okf_version")):
                    lints.append((rel, "root index.md 'okf_version' should be a non-empty string"))
            continue
        data, err = upstream.parse_frontmatter(text)
        if err or not upstream.is_nonempty_string((data or {}).get("type")):
            continue  # already an ERROR from the upstream run
        for field in upstream.RECOMMENDED_FIELDS:
            if not upstream.is_nonempty_string(data.get(field)):
                msg = f"missing recommended field '{field}'"
                if not waived(msg):
                    lints.append((rel, msg))
        for msg in upstream.v02_lints(data, text):
            if not waived(msg):
                lints.append((rel, msg))
    return lints


def _asha_lints(root: Path, files: list[Path], grammar: str) -> list[tuple[str, str]]:
    """Links, orphans, reachability (through okf_links), log order, malformed verified."""
    from okf_log import order_problems
    lints: list[tuple[str, str]] = []
    graph = okf_links.analyze(root, files, grammar, iter_markdown(root))
    if grammar == "obsidian":
        # Asha owns the link and orphan lints here; upstream ran non-strict.
        lints.extend(graph["problems"])
        for path in graph["orphans"]:
            lints.append((path.relative_to(root).as_posix(),
                          "orphan concept: not reachable from any index.md or linked by any concept"))
        if not graph["has_root_index"]:
            lints.append((".", "bundle has no root index.md (recommended for navigation)"))
    for path in graph["unreachable"]:
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
        verified = read_scalars(text).get("verified")
        if isinstance(verified, list) and any(not isinstance(item, dict) for item in verified):
            lints.append((rel, "'verified' contains an entry that is not a mapping; "
                               "the upstream helper would drop it silently (asha)"))
    return lints


def run(bundle: str, strict: bool = False, excludes=(), allow_empty: bool = False, use_ignore: bool = True,
        links: str | None = None) -> dict:
    root = Path(bundle)
    grammar = "standard"
    if root.is_dir():
        excludes = effective_excludes(root, excludes, use_ignore)
        grammar = link_grammar(root, links)
    waivers = lint_waivers(root) if root.is_dir() and grammar == "obsidian" else []
    result = {"bundle": str(root), "strict": strict, "excludes": list(excludes), "links": grammar, "waivers": waivers,
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
    upstream_strict = strict and grammar == "standard"
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = upstream.main([str(root)] + (["--strict"] if upstream_strict else []))
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
    asha = []
    if strict:
        if grammar == "obsidian":
            asha.extend(_field_lints(root, expected, waivers))
        asha.extend(_asha_lints(root, expected, grammar))
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
    parser.add_argument("--no-ignore", action="store_true", help="do not read the bundle's .okfignore")
    parser.add_argument("--links", choices=okf_links.GRAMMARS, default=None,
                        help="link grammar (default: .okfconfig's \"links\", else standard)")
    parser.add_argument("--json", action="store_true", help="print a JSON summary instead of text")
    args = parser.parse_args(argv)
    result = run(args.bundle, args.strict, args.exclude, args.allow_empty, not args.no_ignore, args.links)
    if args.json:
        print(json.dumps(result, indent=2))
        return result["rc"]
    sys.stdout.write(result["output"].rstrip("\n") + "\n")
    for line in result.get("asha_lints", []):
        print(f"LINT   {line}")
    for line in result.get("count_problems", []):
        print(f"ERROR  .: {line}")
    if result["excludes"]:
        print(f"okf_validate: excluded {', '.join(result['excludes'])}; the verdict covers the admitted set")
    if result.get("links") == "obsidian":
        print("okf_validate: obsidian link grammar; Asha ran the strict lints, upstream the conformance errors")
    if result.get("waivers"):
        print(f"okf_validate: waived field lints for {', '.join(result['waivers'])} (.okfconfig); the bundle's own vocabulary governs them")
    if result["rc"] == 2:
        print("okf_validate: validator unavailable (exit 2); this is not a pass")
    else:
        total_lints = (result["lints"] or 0) + len(result["asha_lints"])
        print(f"okf_validate: {result['status']}; {result['files']} file(s) validated, "
              f"{result['errors']} error(s), {total_lints} lint(s)")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
