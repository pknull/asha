from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lib.control.config import load_config as load_control_config
from lib.control.orchestration.config import (
    ORCHESTRATION_CONFIG_CONTRACT,
    OrchestrationConfigError,
    load_config,
)


class OrchestrationConfigTests(unittest.TestCase):
    def environment(self, root: Path) -> dict[str, str]:
        home = root / "home"
        state = root / "state"
        data = root / "data"
        runtime = root / "runtime"
        for directory in (home, state, data, runtime):
            directory.mkdir(mode=0o700)
        return {
            "HOME": str(home),
            "ASHA_CONFIG": str(root / "config.json"),
            "XDG_STATE_HOME": str(state),
            "XDG_DATA_HOME": str(data),
            "XDG_RUNTIME_DIR": str(runtime),
        }

    @staticmethod
    def write(path: Path, value: object) -> None:
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def test_defaults_and_initiatives_root_follow_control(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = self.environment(root)
            config = load_config(env)
            self.assertEqual(config.contract, ORCHESTRATION_CONFIG_CONTRACT)
            self.assertEqual(config.default_coordinator_harness, "claude")
            self.assertEqual(config.max_parallel_tasks, 3)
            self.assertEqual(config.max_total_tasks, 12)
            self.assertEqual(config.max_attempts_per_node, 2)
            self.assertEqual(config.max_repair_cycles, 2)
            self.assertEqual(config.max_retained_bytes_before_pause, 10737418240)
            self.assertEqual(config.max_retained_inodes_before_pause, 200000)
            self.assertEqual(config.coordinator_wait_seconds, 120)
            self.assertEqual(config.result_grace_seconds, 120)
            self.assertEqual(config.max_consecutive_failures, 3)
            self.assertEqual(
                config.initiatives_dir, root / "state/asha/control/initiatives"
            )

    def test_exact_document_parses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = self.environment(root)
            value = {
                "contract": ORCHESTRATION_CONFIG_CONTRACT,
                "default_coordinator_harness": "codex",
                "max_parallel_tasks": 4,
                "max_total_tasks": 20,
                "max_attempts_per_node": 3,
                "max_repair_cycles": 4,
                "max_retained_bytes_before_pause": 1024,
                "max_retained_inodes_before_pause": 100,
                "coordinator_wait_seconds": 30,
                "result_grace_seconds": 45,
                "max_consecutive_failures": 5,
            }
            self.write(Path(env["ASHA_CONFIG"]), {"orchestration": value})
            config = load_config(env)
            for key, expected in value.items():
                self.assertEqual(getattr(config, key), expected)

    def test_unknown_wrong_nonpositive_and_future_values_refuse(self) -> None:
        bad_values = [
            {"unknown": 1},
            {"contract": "asha.orchestration-config.v2"},
            {"default_coordinator_harness": "unsupported"},
            {"max_total_tasks": True},
            {"max_total_tasks": "12"},
            {"max_total_tasks": 0},
            {"max_total_tasks": -1},
        ]
        for bad in bad_values:
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                env = self.environment(root)
                self.write(Path(env["ASHA_CONFIG"]), {"orchestration": bad})
                with self.assertRaises(OrchestrationConfigError):
                    load_config(env)

    def test_corrupt_orchestration_key_does_not_affect_control_parser(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = self.environment(root)
            self.write(
                Path(env["ASHA_CONFIG"]),
                {
                    "control": {"default_harness": "codex"},
                    "orchestration": {
                        "contract": "asha.orchestration-config.v99",
                        "bad": "document",
                    },
                },
            )
            self.assertEqual(load_control_config(env).default_harness, "codex")
            with self.assertRaises(OrchestrationConfigError):
                load_config(env)


if __name__ == "__main__":
    unittest.main()
