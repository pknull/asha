#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Vendored from https://github.com/sniperunder123/okf-knowledge (MIT)
# commit cbfb00bc42be73c0756b243029c783ad31406f60 (2026-09-17), OKF v0.2 tooling.
# upstream-sha256: 1f6ded8a9b44806bbda6bcefe1108d2181c1bb6dbdbd46d42d0ba478f3c9a5fc
# Do not edit locally; re-vendor from upstream (plugins/okf/README.md).
# ---------------------------------------------------------------------------
"""Validate an Open Knowledge Format (OKF) bundle.

OKF v0.2 conformance (https://github.com/GoogleCloudPlatform/open-knowledge-format):
  1. Every non-reserved .md file begins with a parseable YAML frontmatter block.
  2. That frontmatter contains a non-empty `type` (a string).
  3. Reserved files follow their structure when present: index.md and log.md take
     no frontmatter; only the bundle-root index.md may carry frontmatter, and only
     to declare `okf_version`.

These three rules are unchanged from v0.1 and are the only hard spec requirements;
violating any of them is an ERROR. Under --strict the validator additionally reports
producer-quality LINTs:
  - broken links, links missing .md, orphan concepts, missing recommended fields
    (title/description) -- unchanged from v0.1;
  - malformed optional v0.2 families (`generated`/`verified`/`sources`/`status`/
    `stale_after`, and `runtime` on `type: Attested Computation`) when present;
  - migration nudges for the two v0.1 fields v0.2 superseded (`timestamp` -> the
    `generated` family, body `# Citations` -> frontmatter `sources`).
None of these are spec violations -- consumers MUST tolerate them, and a v0.1
bundle stays fully conformant -- but they still fail the --strict gate (exit 1)
so producers can keep a bundle tidy and current.

Usage:
    python validate.py <bundle-path>
    python validate.py <bundle-path> --strict

Exit code: 0 = conformant, 1 = errors (and, under --strict, any lints),
2 = bad invocation / PyYAML not installed.

Requires PyYAML for authoritative YAML parsing:  pip install pyyaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from okf_common import (
    FRONTMATTER_RE,
    extract_links,
    is_external,
    is_nonempty_string,
    normalize_verified,
    parse_frontmatter,
    parse_iso8601,
    path_part,
    resolve_link,
)

try:
    import yaml
except ImportError:
    yaml = None

RESERVED = {"index.md", "log.md"}
RECOMMENDED_FIELDS = ("title", "description")


def iter_markdown_files(root):
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def _check_iso(msgs, label, value):
    if value is not None and parse_iso8601(value) is None:
        msgs.append(f"'{label}' is not a valid ISO 8601 datetime: {value!r}")


def _has_citations_heading(text):
    return any(line.strip() == "# Citations" for line in text.splitlines())


def v02_lints(data, text):
    """Producer-quality nudges for the optional OKF v0.2 families (spec §5, §10).

    Never spec violations: these families are optional and their absence is a
    plain, fully conformant v0.1-shaped concept.
    """
    msgs = []

    if "timestamp" in data and "generated" not in data:
        msgs.append("legacy 'timestamp' field present without 'generated'; "
                     "consider migrating to generated: {by, at} (OKF v0.2 §13.1)")
    if _has_citations_heading(text):
        msgs.append("body has a '# Citations' heading; OKF v0.2 moves provenance to "
                     "frontmatter 'sources' + footnote attribution (§13.1)")

    generated = data.get("generated")
    if isinstance(generated, dict):
        if not is_nonempty_string(generated.get("by")):
            msgs.append("'generated' is missing required 'by' (actor)")
        _check_iso(msgs, "generated.at", generated.get("at"))
    elif generated is not None:
        msgs.append("'generated' should be a mapping with 'by' and 'at'")

    for v in normalize_verified(data):
        if not is_nonempty_string(v.get("by")):
            msgs.append("a 'verified' entry is missing 'by' (actor)")
        _check_iso(msgs, "verified[].at", v.get("at"))

    sources = data.get("sources")
    if isinstance(sources, list):
        for i, src in enumerate(sources):
            if isinstance(src, dict):
                if not is_nonempty_string(src.get("resource")):
                    msgs.append(f"sources[{i}] is missing required 'resource'")
                _check_iso(msgs, f"sources[{i}].last_modified", src.get("last_modified"))
            else:
                msgs.append(f"sources[{i}] should be a mapping")
    elif sources is not None:
        msgs.append("'sources' should be a list of source entries")

    usage_window = data.get("usage_window")
    if isinstance(usage_window, dict):
        _check_iso(msgs, "usage_window.from", usage_window.get("from"))
        _check_iso(msgs, "usage_window.to", usage_window.get("to"))

    _check_iso(msgs, "stale_after", data.get("stale_after"))

    status = data.get("status")
    if status is not None and status not in ("draft", "stable", "deprecated"):
        msgs.append(f"'status' should be one of draft|stable|deprecated (found: {status!r})")

    if data.get("type") == "Attested Computation" and not is_nonempty_string(data.get("runtime")):
        msgs.append("type 'Attested Computation' is missing required 'runtime'")

    return msgs


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate an Open Knowledge Format (OKF) bundle."
    )
    parser.add_argument("bundle", help="path to the OKF bundle directory")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also run producer-quality lint checks and fail (exit 1) on any lints",
    )
    args = parser.parse_args(argv)

    if yaml is None:
        print("error: PyYAML is required to validate OKF bundles. "
              "Install it with: pip install pyyaml", file=sys.stderr)
        return 2

    root = Path(args.bundle)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    errors = []
    lints = []
    files = iter_markdown_files(root)
    text_by_rel = {}
    link_destinations = set()  # resolved .md paths referenced by non-log files

    # Pass 1: conformance (rules 1-3) + collect link destinations.
    for md in files:
        rel = md.relative_to(root).as_posix()
        try:
            text = md.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append((rel, "file is not valid UTF-8"))
            continue
        text_by_rel[rel] = text

        is_reserved = md.name in RESERVED
        is_root_index = md.name == "index.md" and md.parent == root

        if not is_reserved:
            data, err = parse_frontmatter(text)
            if err:
                errors.append((rel, err))
            elif not is_nonempty_string(data.get("type")):
                errors.append((rel, "frontmatter is missing a non-empty 'type' string"))
            elif args.strict:
                for field in RECOMMENDED_FIELDS:
                    if not is_nonempty_string(data.get(field)):
                        lints.append((rel, f"missing recommended field '{field}'"))
                for msg in v02_lints(data, text):
                    lints.append((rel, msg))
        elif FRONTMATTER_RE.match(text):
            # Rule 3: reserved files carry no frontmatter, except the bundle-root
            # index.md, which may declare only okf_version. Default error.
            if md.name == "log.md":
                errors.append((rel, "log.md must not carry a frontmatter block"))
            elif not is_root_index:
                errors.append((rel, "only the bundle-root index.md may carry frontmatter"))
            else:
                data, err = parse_frontmatter(text)
                if err:
                    errors.append((rel, f"root index.md frontmatter is invalid: {err}"))
                else:
                    extra = [k for k in (data or {}) if k != "okf_version"]
                    if extra:
                        errors.append((rel, "root index.md frontmatter may only declare "
                                            f"okf_version (found: {', '.join(extra)})"))
                    elif args.strict and "okf_version" in (data or {}):
                        # P3.10: if declared, okf_version should be a non-empty string.
                        if not is_nonempty_string(data.get("okf_version")):
                            lints.append((rel, "root index.md 'okf_version' should be a "
                                               "non-empty string"))

        if md.name != "log.md":  # historical log links are expected to dangle
            for target in extract_links(text):
                if is_external(target) or target.startswith("#"):
                    continue
                part = path_part(target)
                if part.endswith(".md"):
                    link_destinations.add(resolve_link(part, md, root).resolve())

    # Pass 2 (--strict only): links + orphans (producer-quality lints).
    if args.strict:
        for md in files:
            rel = md.relative_to(root).as_posix()
            text = text_by_rel.get(rel)
            if text is None:
                continue

            if md.name != "log.md":
                for target in extract_links(text):
                    if is_external(target) or target.startswith("#"):
                        continue
                    part = path_part(target)
                    if part.endswith(".md"):
                        # P0.2: resolve the same way the orphan set does so both
                        # passes agree on case-insensitive filesystems / with '..'.
                        if not resolve_link(part, md, root).resolve().exists():
                            lints.append((rel, f"broken link to '{target}'"))
                    elif part and not part.endswith("/") and (
                        part.startswith("/") or part.startswith("./") or part.startswith("../")
                    ):
                        last = part.rsplit("/", 1)[-1]
                        if "." not in last:
                            lints.append(
                                (rel, f"intra-bundle link '{target}' is missing the .md extension")
                            )

            if md.name not in RESERVED and md.resolve() not in link_destinations:
                lints.append(
                    (rel, "orphan concept: not reachable from any index.md or linked by any concept")
                )

        if not (root / "index.md").exists():
            lints.append((".", "bundle has no root index.md (recommended for navigation)"))

    for rel, msg in errors:
        print(f"ERROR  {rel}: {msg}")
    if args.strict:
        if lints:
            print("note: LINTs below are producer-quality lints, NOT spec violations; "
                  "consumers MUST tolerate them.")
        for rel, msg in lints:
            print(f"LINT   {rel}: {msg}")

    summary = f"{len(files)} file(s), {len(errors)} error(s)"
    if args.strict:
        summary += f", {len(lints)} lint(s)"
    print(summary)

    if errors:
        return 1
    if args.strict and lints:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
