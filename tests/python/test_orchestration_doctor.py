from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control.orchestration.config import load_config
from lib.control.orchestration.doctor import _contracts_probe, run_orchestration_doctor


class OrchestrationDoctorTests(unittest.TestCase):
    def test_contract_source_inspection_failure_is_unavailable(self) -> None:
        with mock.patch(
            "lib.control.orchestration.doctor.inspect.getsource",
            side_effect=OSError("source unavailable"),
        ):
            probe = _contracts_probe()
        self.assertEqual(probe.outcome, "unavailable")
        self.assertIn("source unavailable", probe.detail)

    def test_absent_initiatives_root_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            with mock.patch("lib.control.orchestration.doctor.run_control_doctor", return_value={"ok": True}):
                payload = run_orchestration_doctor(config)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["contract"], "asha.orchestration-doctor.v1")
            self.assertEqual([probe["name"] for probe in payload["probes"]], [
                "orchestration-config", "initiatives-root", "control-contracts",
                "create-by-id", "control-doctor", "coordinator-seam",
            ])

    def test_coordinator_seam_probe_is_advisory_and_never_blocks_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            for path in (config.initiatives_dir.parent, config.initiatives_dir.parent.parent):
                path.chmod(0o700)
            with mock.patch("lib.control.orchestration.doctor.run_control_doctor", return_value={"ok": True}), \
                 mock.patch("lib.control.orchestration.doctor.shutil.which", return_value=None):
                payload = run_orchestration_doctor(config)
            seam = next(probe for probe in payload["probes"] if probe["name"] == "coordinator-seam")
            self.assertEqual(seam["outcome"], "unavailable")
            self.assertIn("tmux", seam["detail"])
            self.assertTrue(payload["ok"])
            self.assertIn(seam["detail"], payload["limitations"])
            with mock.patch("lib.control.orchestration.doctor.run_control_doctor", return_value={"ok": True}), \
                 mock.patch("lib.control.orchestration.doctor.shutil.which", return_value="/usr/bin/tmux"):
                payload = run_orchestration_doctor(config)
            seam = next(probe for probe in payload["probes"] if probe["name"] == "coordinator-seam")
            self.assertEqual(seam["outcome"], "match")

    def test_private_existing_root_and_control_ok_make_doctor_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            for path in (
                config.initiatives_dir.parent,
                config.initiatives_dir.parent.parent,
            ):
                path.chmod(0o700)
            with mock.patch("lib.control.orchestration.doctor.run_control_doctor", return_value={"ok": True}):
                payload = run_orchestration_doctor(config)
            self.assertTrue(payload["ok"])

    def test_namespace_safety_is_rechecked_at_probe_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            unsafe = config.initiatives_dir.parent.parent
            unsafe.chmod(0o770)
            with mock.patch(
                "lib.control.orchestration.doctor.run_control_doctor",
                return_value={"ok": True},
            ):
                payload = run_orchestration_doctor(config)
            root_probe = next(
                probe for probe in payload["probes"]
                if probe["name"] == "initiatives-root"
            )
            self.assertEqual(root_probe["outcome"], "mismatch")


if __name__ == "__main__":
    unittest.main()
