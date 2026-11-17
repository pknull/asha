"""post-edit-lint must report on Markdown, never rewrite it (#153)."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HOOK = Path(__file__).resolve().parents[2] / "plugins" / "code" / "hooks" / "post-edit-lint.sh"

# A stand-in prettier: --write rewrites the file, --check reports and exits 1
# the way the real one does for an unformatted file.
STUB_PRETTIER = """#!/usr/bin/env bash
mode="$1"; file="$2"
case "$mode" in
  --write) printf 'REWRITTEN\\n' > "$file"; echo "$file 1ms" ;;
  --check) echo "[warn] $file"; exit 1 ;;
  *) exit 2 ;;
esac
"""


class PostEditLintMarkdownTest(unittest.TestCase):
    def run_hook(self, file_path, bin_dir):
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        payload = json.dumps({"tool_input": {"file_path": str(file_path)}})
        return subprocess.run(
            ["bash", str(HOOK)], input=payload, text=True, capture_output=True, env=env, timeout=30
        )

    def test_markdown_is_reported_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "prettier"
            stub.write_text(STUB_PRETTIER)
            stub.chmod(0o755)
            note = tmp / "note.md"
            original = "# Note\n\n#131 fixed the close race\n"
            note.write_text(original)

            result = self.run_hook(note, bin_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "{}")
            self.assertEqual(note.read_text(), original)
            self.assertIn("note.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
