#!/usr/bin/env python3
"""Discover, inspect, and explicitly import portable Agent Skills."""

from __future__ import annotations

from pathlib import Path
import sys


# Keep the executable usable both as a script and through importlib's file loader.
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from find_skills_cli import build_parser, main  # noqa: E402
from find_skills_common import (  # noqa: E402
    ApprovalError,
    CollisionError,
    FindSkillsError,
    HttpClient,
    ValidationError,
    parse_frontmatter,
)
from find_skills_inspect import (  # noqa: E402
    inspect_candidate,
    inspection_report,
    parse_candidate,
    parse_search_payload,
    search_skills,
    search_url,
)
from find_skills_store import (  # noqa: E402
    build_import_proposal,
    proposal_report,
    status_store,
    write_import as _write_import,
)


__all__ = [
    "ApprovalError",
    "CollisionError",
    "FindSkillsError",
    "HttpClient",
    "ValidationError",
    "_write_import",
    "build_import_proposal",
    "build_parser",
    "inspect_candidate",
    "inspection_report",
    "main",
    "parse_candidate",
    "parse_frontmatter",
    "parse_search_payload",
    "proposal_report",
    "search_skills",
    "search_url",
    "status_store",
]


if __name__ == "__main__":
    raise SystemExit(main())
