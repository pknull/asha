#!/usr/bin/env python3
"""Run the vendored OKF visualizer with its inline JSON made script-safe.

visualize.py inlines `json.dumps(...)` payloads into a `<script>` block. A
concept description containing `</script>` would end that block and run as
markup when the HTML opens (OKF frontmatter is agent-authored). This wrapper
escapes every `</` in the payloads as `<\\/`, which JSON parses identically,
before the template substitution. The vendored file is not edited.

Usage: okf_viz.py BUNDLE [--title TEXT]
"""
from __future__ import annotations

import json as _json
import sys
from pathlib import Path

try:
    import visualize as upstream
except ImportError:  # run as a script from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import visualize as upstream


class _ScriptSafeJson:
    """json module stand-in whose dumps never emits a `</` sequence."""

    def __getattr__(self, name):
        return getattr(_json, name)

    @staticmethod
    def dumps(obj, **kwargs):
        return _json.dumps(obj, **kwargs).replace("</", "<\\/")


def script_safe(text: str) -> str:
    return text.replace("</", "<\\/")


def main(argv=None) -> int:
    upstream.json = _ScriptSafeJson()
    return upstream.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
