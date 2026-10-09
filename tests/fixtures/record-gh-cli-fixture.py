#!/usr/bin/env python3
"""Record the local gh's flag and --json field surface for chosen command paths.

Regenerates tests/fixtures/gh-cli-<version>.json, which
tests/python/test_github_cli_skill.py uses to check every gh command the
code-github-cli skill documents. It only runs `gh <path> --help` and
`gh <path> --json` (which lists fields and exits); neither contacts GitHub.

    python3 tests/fixtures/record-gh-cli-fixture.py "auth status" "pr view" ... \
        > tests/fixtures/gh-cli-$(gh --version | awk 'NR==1 {print $3}').json
"""

import datetime
import json
import re
import subprocess
import sys

FLAG = re.compile(r"^\s+(?:(-[A-Za-z0-9]),\s+)?(--[a-z0-9][a-z0-9-]*)")


def flags(path: str) -> list[str]:
    text = subprocess.run(["gh", *path.split(), "--help"], capture_output=True,
                          text=True, check=True).stdout
    section, found = None, set()
    for line in text.splitlines():
        if line and not line.startswith(" "):
            section = line.strip()
            continue
        if section in ("FLAGS", "INHERITED FLAGS"):
            match = FLAG.match(line)
            if match:
                found.update(flag for flag in match.groups() if flag)
    return sorted(found)


def json_fields(path: str) -> list[str]:
    done = subprocess.run(["gh", *path.split(), "--json"], capture_output=True, text=True, cwd="/")
    text = done.stdout + done.stderr
    if "Specify one or more comma-separated fields" not in text:
        raise SystemExit(f"unexpected --json output for {path}: {text[:200]}")
    return sorted(line.strip() for line in text.splitlines()[1:] if line.strip())


def main() -> None:
    version = subprocess.run(["gh", "--version"], capture_output=True, text=True,
                             check=True).stdout.split()[2]
    commands = {}
    for path in sys.argv[1:]:
        entry = {"flags": flags(path)}
        if "--json" in entry["flags"]:
            entry["json_fields"] = json_fields(path)
        commands[path] = entry
    print(json.dumps({
        "gh_version": version, "recorded": datetime.date.today().isoformat(),
        "source": "local `gh <path> --help` (FLAGS and INHERITED FLAGS) and `gh <path> --json` field lists",
        "commands": commands,
    }, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
