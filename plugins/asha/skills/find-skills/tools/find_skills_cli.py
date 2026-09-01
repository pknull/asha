"""Command-line interface for the find-skills tool."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import unicodedata

from find_skills_common import (
    FindSkillsError,
    HttpClient,
    default_asha_home,
    default_repo_root,
)
from find_skills_inspect import (
    inspect_candidate,
    inspection_report,
    parse_candidate,
    search_skills,
)
from find_skills_store import (
    build_import_proposal,
    proposal_report,
    status_store,
    write_import,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _terminal_safe(value: Any, *, multiline: bool = False) -> str:
    escapes = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}
    rendered: list[str] = []
    for character in str(value):
        if multiline and character == "\n":
            rendered.append(character)
            continue
        category = unicodedata.category(character)
        if category not in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            rendered.append(character)
            continue
        codepoint = ord(character)
        if character in escapes:
            rendered.append(escapes[character])
        elif codepoint <= 0xFF:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(f"\\U{codepoint:08x}")
    return "".join(rendered)


def _print_search(records: Sequence[Mapping[str, Any]]) -> None:
    if not records:
        print("No Skills.sh candidates found.")
        return
    for record in records:
        print(
            f"{_terminal_safe(record['id'])}\t{_terminal_safe(record['name'])}"
            f"\tinstalls={_terminal_safe(record.get('installs', 0))}"
        )


def _print_inspection(report: Mapping[str, Any]) -> None:
    print(f"candidate: {_terminal_safe(report['candidate'])}")
    print(f"revision: {_terminal_safe(report['revision'])} (immutable)")
    print(f"path: {_terminal_safe(report['upstream_path'])}")
    print(f"name: {_terminal_safe(report.get('name') or '<invalid>')}")
    print(f"files: {len(report['files'])}; tree sha256: {_terminal_safe(report['tree_digest'])}")
    license_data = report.get("license", {})
    declared_license = (
        license_data.get("declared") or license_data.get("spdx_id") or "not declared"
    )
    print(f"license: {_terminal_safe(declared_license)}")
    print(f"dependencies: {json.dumps(report.get('dependencies'), sort_keys=True)}")
    print(f"tools: {_terminal_safe(', '.join(report.get('tools', [])) or 'none declared')}")
    print(
        "permissions: "
        + _terminal_safe(', '.join(report.get('permissions', [])) or 'none declared')
    )
    findings = report.get("safety_findings", [])
    print(f"safety findings: {len(findings)}")
    for finding in findings:
        print(
            f"  - {_terminal_safe(finding['category'])}: "
            f"{_terminal_safe(finding['path'])} — {_terminal_safe(finding['evidence'])}"
        )
    if report.get("import_blockers"):
        print("importable: no")
        for blocker in report["import_blockers"]:
            print(f"  - {_terminal_safe(blocker)}")
    else:
        print("importable: yes (still requires Keeper review and explicit --approve)")
    print("\n--- pinned SKILL.md ---")
    print(_terminal_safe(str(report.get("skill_markdown", "")).rstrip(), multiline=True))


def _print_proposal(report: Mapping[str, Any]) -> None:
    print(
        f"dry-run: action={_terminal_safe(report['action'])} "
        f"destination={_terminal_safe(report['destination'])}"
    )
    if report.get("local_state"):
        print(f"local state: {_terminal_safe(report['local_state']['state'])}")
        for issue in report["local_state"].get("issues", []):
            print(
                f"  - {_terminal_safe(issue.get('kind'))}: "
                f"{_terminal_safe(issue.get('path', ''))}"
            )
    print("exact proposed writes:")
    if not report.get("writes"):
        print("  (none; pinned import is already clean)")
    for item in report.get("writes", []):
        mode = "executable" if item["executable"] else "regular"
        print(
            f"  {_terminal_safe(item['path'])}  "
            f"sha256={_terminal_safe(item['sha256'])} "
            f"bytes={_terminal_safe(item['bytes'])} mode={mode}"
        )
    print("proposal only; no files have been written yet")


def _add_candidate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("candidate", nargs="?", help="Skills.sh id: owner/repo/skillId")
    parser.add_argument("--source", help="upstream owner/repo (with --skill-id)")
    parser.add_argument("--skill-id", help="Skills.sh skillId (with --source)")
    parser.add_argument("--revision", "--ref", dest="ref", help="commit, tag, or branch to resolve")
    parser.add_argument("--skill-path", help="explicit repository directory containing SKILL.md")
    parser.add_argument("--json", action="store_true", help="emit machine-readable evidence")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search and explicitly import portable Agent Skills without executing them."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="query the public Skills.sh JSON route")
    search.add_argument("query")
    search.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect", help="fetch pinned bytes and print safety evidence")
    _add_candidate_args(inspect)
    dry = sub.add_parser("dry-run", aliases=["propose"], help="print exact proposed writes")
    _add_candidate_args(dry)
    dry.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    dry.add_argument("--asha-home", type=Path, help=argparse.SUPPRESS)
    importer = sub.add_parser("import", help="import only with an explicit approval flag")
    _add_candidate_args(importer)
    importer.add_argument("--approve", "--approved", action="store_true")
    importer.add_argument(
        "--replace", "--replace-drift", action="store_true",
        help="separate approval to replace an existing pinned or locally drifted import",
    )
    importer.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    importer.add_argument("--asha-home", type=Path, help=argparse.SUPPRESS)
    status = sub.add_parser("status", help="recompute imported hashes and report drift")
    status.add_argument("--name")
    status.add_argument("--json", action="store_true")
    status.add_argument("--asha-home", type=Path, help=argparse.SUPPRESS)
    return parser


def _inspect_from_args(
    args: argparse.Namespace, client: HttpClient | None = None
) -> dict[str, Any]:
    source, skill_id = parse_candidate(args.candidate, args.source, args.skill_id)
    return inspect_candidate(
        source, skill_id, ref=args.ref, skill_path=args.skill_path, client=client
    )


def _run_search(args: argparse.Namespace) -> int:
    records = search_skills(args.query)
    _print_json({"skills": records}) if args.json else _print_search(records)
    return 0


def _run_inspect(args: argparse.Namespace) -> int:
    report = inspection_report(_inspect_from_args(args))
    _print_json(report) if args.json else _print_inspection(report)
    return 0 if report["importable"] else 1


def _proposal_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return build_import_proposal(
        _inspect_from_args(args),
        (args.asha_home or default_asha_home()).expanduser().resolve(),
        (args.repo_root or default_repo_root()).expanduser().resolve(),
    )


def _run_proposal(args: argparse.Namespace) -> int:
    report = proposal_report(_proposal_from_args(args))
    _print_json(report) if args.json else _print_proposal(report)
    return 0


def _run_import(args: argparse.Namespace) -> int:
    proposal = _proposal_from_args(args)
    report = proposal_report(proposal)
    if args.json:
        print(json.dumps({"proposal": report}, indent=2, sort_keys=True), file=sys.stderr)
    else:
        _print_proposal(report)
    result = write_import(proposal, approve=args.approve, replace=args.replace)
    if args.json:
        _print_json(result)
    else:
        action = "wrote approved bytes" if result["written"] else "no write needed"
        print(f"import: {result['action']} ({action})")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    status = status_store(
        (args.asha_home or default_asha_home()).expanduser().resolve(), args.name
    )
    if args.json:
        _print_json(status)
    else:
        print(
            f"imported skills: {_terminal_safe(status['state'])} "
            f"({_terminal_safe(status['store'])})"
        )
        for item in status["skills"]:
            print(
                f"  {_terminal_safe(item['name'])}: "
                f"{_terminal_safe(item['state'])}"
            )
            for issue in item["issues"]:
                print(
                    f"    - {_terminal_safe(issue['kind'])}: "
                    f"{_terminal_safe(issue.get('path', ''))}"
                )
    return 0 if status["state"] == "clean" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runners = {
        "search": _run_search,
        "inspect": _run_inspect,
        "dry-run": _run_proposal,
        "propose": _run_proposal,
        "import": _run_import,
        "status": _run_status,
    }
    try:
        return runners[args.command](args)
    except FindSkillsError as exc:
        print(f"ERROR: {_terminal_safe(exc)}", file=sys.stderr)
        return 2
