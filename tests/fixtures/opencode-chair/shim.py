#!/usr/bin/python3
"""Fixture-only command client. This script never calls production Control."""

import json
from pathlib import Path
import socket
import sys

SOCKET = "/fixture/control.sock"


def main():
    argv = [Path(sys.argv[0]).name, *sys.argv[1:]]
    drafts = {}
    if argv[0] == "save_none.py":
        for flag, limit in (("--active-file", 4096), ("--decisions-file", 65536)):
            if flag not in argv or argv.index(flag) + 1 >= len(argv):
                continue
            name = argv[argv.index(flag) + 1]
            try:
                with Path(name).open("rb") as source:
                    data = source.read(limit + 1)
                if len(data) <= limit:
                    drafts[name] = data.decode("utf-8")
            except (OSError, UnicodeError):
                # Always deliver argv. Server-side argument validation must run
                # before file errors, and every publish attempt must be logged.
                continue
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(45)
        connection.connect(SOCKET)
        connection.sendall(json.dumps({"argv": argv, "drafts": drafts}, ensure_ascii=False).encode() + b"\n")
        reader = connection.makefile("rb")
        result = json.loads(reader.readline(131073))
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    return result["exit_code"]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(2)
