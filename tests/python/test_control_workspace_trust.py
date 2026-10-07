"""control.workspace_trust stays a validated configuration key.

The workspace-trust inheritance it configured left with the task substrate
(L-b); existing configuration files keep loading.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lib.control.config import ConfigError, load_config


class TrustConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = {
            "HOME": str(self.root / "home"), "ASHA_CONFIG": str(self.root / "config.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(mode=0o700)

    def write(self, control: dict) -> None:
        path = Path(self.env["ASHA_CONFIG"])
        path.write_text(json.dumps({"control": control}))
        path.chmod(0o600)

    def test_workspace_trust_defaults_to_inherit_and_rejects_unknown_modes(self) -> None:
        self.write({})
        self.assertEqual(load_config(self.env).workspace_trust, "inherit")
        self.write({"workspace_trust": "never"})
        self.assertEqual(load_config(self.env).workspace_trust, "never")
        self.write({"workspace_trust": "always"})
        with self.assertRaisesRegex(ConfigError, "workspace_trust must be one of"):
            load_config(self.env)


if __name__ == "__main__":
    unittest.main()
