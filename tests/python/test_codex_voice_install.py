"""Installer and doctor coverage for the optional native Codex voice bridge."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CodexVoiceInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "lib").mkdir()
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root)}

    def install(self, source):
        if source is not None:
            (self.root / "lib/codex_voice.py").write_text(source)
        return subprocess.run(
            ["bash", "-c", '''
set -eu
asha_harness_home() { printf '%s/.codex' "$HOME"; }
resolve_path() { printf '%s' "$1"; }
die() { printf '%s\n' "$1" >&2; exit "${2:-1}"; }
MARKET_ROOT="$1"
source "$2/harnesses/codex.sh"
_codex_prepare_hooks() { echo HOOK_MUTATION_REACHED >&2; return 88; }
codex_install
''', "voice-install-test", str(self.root), str(ROOT)],
            env=self.env, text=True, capture_output=True, check=False,
        )

    def test_missing_bridge_fails_before_hook_mutation(self):
        result = self.install(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("HOOK_MUTATION_REACHED", result.stderr)
        self.assertIn("voice", result.stderr.lower())

    def test_invalid_bridge_fails_before_hook_mutation(self):
        result = self.install("def broken(:\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("HOOK_MUTATION_REACHED", result.stderr)

    def test_valid_bridge_is_checked_without_executing_or_compiling_to_disk(self):
        result = self.install("raise RuntimeError('must not execute during install')\n")
        self.assertEqual(result.returncode, 88, result.stderr)
        self.assertIn("HOOK_MUTATION_REACHED", result.stderr)
        self.assertFalse((self.root / "lib/__pycache__").exists())


class CodexVoiceDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "lib").mkdir()
        self.native = self.root / "codex"
        self.native.write_text("#!/bin/sh\nexit 0\n")
        self.native.chmod(0o700)
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root)}

    def doctor(self, payload=None, rc=0, target="codex", full=False):
        if payload is not None:
            (self.root / "lib/codex_voice.py").write_text(
                "import sys\n"
                "assert sys.argv[1:] == ['--check', " + repr(str(self.native)) + "]\n"
                "print(" + repr(payload) + ")\n"
                "sys.exit(" + str(rc) + ")\n"
            )
        (self.root / "bin").mkdir(exist_ok=True)
        (self.root / "bin/asha-drift-check.sh").write_text("exit 0\n")
        return subprocess.run(
            ["bash", "-c", '''
set -euo pipefail
source "$1/lib/doctor.sh"
MARKET_ROOT="$2"
asha_harness_executable() { printf '%s/codex' "$MARKET_ROOT"; }
if [[ "$4" == full ]]; then
  _asha_doctor_workspace_section() { :; }
  _asha_doctor_imported_skills_section() { :; }
  _asha_doctor_session_profile_section() { :; }
  _asha_doctor_codex_daemon_section() { :; }
  _asha_doctor_operational_budget_section() { :; }
  _asha_doctor_okf_bundle_section() { :; }
  asha_doctor_main "$3"
else
  _asha_doctor_codex_voice_section "$3"
fi
''', "voice-doctor-test", str(ROOT), str(self.root), target,
             "full" if full else "section"],
            env=self.env, text=True, capture_output=True, check=False,
        )

    def test_other_harness_is_untouched(self):
        result = self.doctor(target="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_supported_native_contract_passes(self):
        result = self.doctor(json.dumps({"supported": True, "reason": "native v3"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)
        self.assertIn("voice", result.stdout.lower())

    def test_unsupported_native_contract_warns_without_breaking_text(self):
        result = self.doctor(json.dumps({"supported": False, "reason": "no initialItems"}), 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARN", result.stdout)
        self.assertIn("no initialItems", result.stdout)
        self.assertIn("native launch", result.stdout)

    def test_missing_helper_is_failure(self):
        result = self.doctor()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("FAIL", result.stdout)

    def test_malformed_or_crashed_check_is_failure(self):
        for payload, rc in [("not json", 0), ('{"supported":true}', 0),
                            ('{"supported":true,"reason":"bad exit"}', 1),
                            ('{"supported":false,"reason":"crashed"}', 2)]:
            with self.subTest(payload=payload, rc=rc):
                result = self.doctor(payload, rc)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("FAIL", result.stdout)

    def test_main_propagates_voice_failure(self):
        result = self.doctor(full=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("voice", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
