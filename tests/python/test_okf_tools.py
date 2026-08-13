"""Canonical workspace OKF tools retain their shared parser."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "plugins/session/tools"


class OkfToolTests(unittest.TestCase):
    def test_validate_and_visualize_load_with_rehomed_parser(self):
        for tool in ("validate.py", "visualize.py"):
            result = subprocess.run(
                [sys.executable, str(TOOLS / tool), "--help"],
                capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
