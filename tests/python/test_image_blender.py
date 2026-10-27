"""Tests for the image-blender skill scripts (plugins/image/skills/blender).

The scripts run inside Blender; these tests cover their pure logic without
Blender. Set ASHA_TEST_BLENDER to a Blender executable to also run the
end-to-end smoke test headless.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SKILL = Path(__file__).parents[2] / "plugins" / "image" / "skills" / "blender"
SCRIPTS = SKILL / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cycles_devices = load("cycles_devices")
sys.path.insert(0, str(SCRIPTS))
try:
    smoke = load("smoke")
finally:
    sys.path.remove(str(SCRIPTS))


class FakeDevice:
    def __init__(self, name: str, kind: str):
        self.name, self.type, self.use = name, kind, False


class FakePrefs:
    def __init__(self, devices: dict[str, list[FakeDevice]]):
        self.devices = devices
        self.compute_device_type = "NONE"

    def get_device_types(self, _context):
        return [("NONE", "None", "", 0)] + [(k, k, "", i) for i, k in enumerate(self.devices, 1)]

    def get_devices_for_type(self, kind):
        return self.devices[kind]


def fake_bpy(devices: dict[str, list[FakeDevice]], version=(5, 2, 3)):
    """A stand-in for the parts of bpy the scripts touch before rendering."""
    prefs = FakePrefs(devices)
    scenes = [SimpleNamespace(name="Scene", cycles=SimpleNamespace(device="CPU"))]
    bpy = SimpleNamespace(
        app=SimpleNamespace(version=version, version_string="5.2.3 LTS"),
        context=SimpleNamespace(preferences=SimpleNamespace(
            addons={"cycles": SimpleNamespace(preferences=prefs)})),
        data=SimpleNamespace(scenes=scenes),
    )
    return bpy, prefs, scenes


def run_main(module, argv: list[str], bpy) -> tuple[list[str], int]:
    """Run module.main() with argv after "--"; return (RESULT lines, exit code)."""
    out = io.StringIO()
    code = 0
    with mock.patch.dict(sys.modules, {"bpy": bpy}), \
            mock.patch.object(sys, "argv", ["blender", "-b", "--", *argv]), redirect_stdout(out):
        try:
            module.main()
        except SystemExit as exc:
            code = exc.code
    return [l for l in out.getvalue().splitlines() if l.startswith("RESULT ")], code


class ScriptArgsTests(unittest.TestCase):
    def test_takes_arguments_after_separator(self):
        argv = ["blender", "-b", "-P", "x.py", "--", "--backend", "HIP"]
        self.assertEqual(cycles_devices.script_args(argv), ["--backend", "HIP"])

    def test_no_separator_means_no_arguments(self):
        self.assertEqual(cycles_devices.script_args(["blender", "-b"]), [])

    def test_parse_ignores_arguments_meant_for_others(self):
        args = cycles_devices.parse(["--cycles-device", "HIP", "--out", "/x", "--backend", "hip"])
        self.assertEqual(args.backend, "HIP")
        self.assertFalse(args.require_gpu)

    def test_parse_never_matches_another_scripts_option_by_prefix(self):
        args = cycles_devices.parse(["--probe", "--req", "--with"])
        self.assertFalse(args.probe_only)
        self.assertFalse(args.require_gpu)
        self.assertFalse(args.with_cpu)

    def test_smoke_never_matches_another_scripts_option_by_prefix(self):
        args = smoke.parse(["--out", "/x", "--req", "--form", "obj"])
        self.assertFalse(args.require_gpu)
        self.assertEqual(args.formats, ",".join(smoke.FORMATS))

    def test_bad_option_raises_instead_of_exiting(self):
        with self.assertRaises(cycles_devices.ArgumentError):
            cycles_devices.parse(["--backend"])


class ChooseTests(unittest.TestCase):
    def test_auto_prefers_optix_over_cuda(self):
        self.assertEqual(
            cycles_devices.choose({"CUDA": ["gpu"], "OPTIX": ["gpu"]}, "auto"), ("OPTIX", None))

    def test_auto_skips_backends_without_gpus(self):
        available = {"CUDA": [], "OPTIX": [], "HIP": ["gpu"], "ONEAPI": []}
        self.assertEqual(cycles_devices.choose(available, "auto"), ("HIP", None))

    def test_auto_falls_back_to_cpu(self):
        self.assertEqual(cycles_devices.choose({"CUDA": [], "HIP": []}, "auto"), (None, None))

    def test_auto_accepts_an_unlisted_backend(self):
        self.assertEqual(cycles_devices.choose({"FUTURE": ["gpu"]}, "auto"), ("FUTURE", None))

    def test_cpu_request_uses_no_backend(self):
        self.assertEqual(cycles_devices.choose({"HIP": ["gpu"]}, "cpu"), (None, None))

    def test_named_backend_without_device_is_an_error(self):
        backend, error = cycles_devices.choose({"HIP": ["gpu"], "OPTIX": []}, "OPTIX")
        self.assertIsNone(backend)
        self.assertIn("OPTIX", error)


class ApplyTests(unittest.TestCase):
    def test_enables_gpus_only_and_points_scenes_at_gpu(self):
        gpu, cpu = FakeDevice("Card", "HIP"), FakeDevice("Chip", "CPU")
        prefs = FakePrefs({"HIP": [gpu, cpu]})
        scenes = [SimpleNamespace(cycles=SimpleNamespace(device="CPU"))]
        used = cycles_devices.apply(prefs, scenes, "HIP", with_cpu=False)
        self.assertEqual(prefs.compute_device_type, "HIP")
        self.assertTrue(gpu.use)
        self.assertFalse(cpu.use)
        self.assertEqual(scenes[0].cycles.device, "GPU")
        self.assertEqual(used, ["Card (HIP)"])

    def test_with_cpu_enables_the_cpu_entry(self):
        gpu, cpu = FakeDevice("Card", "HIP"), FakeDevice("Chip", "CPU")
        cycles_devices.apply(FakePrefs({"HIP": [gpu, cpu]}), [], "HIP", with_cpu=True)
        self.assertTrue(cpu.use)

    def test_no_backend_renders_on_cpu(self):
        prefs = FakePrefs({"HIP": [FakeDevice("Card", "HIP")]})
        prefs.compute_device_type = "HIP"
        scenes = [SimpleNamespace(cycles=SimpleNamespace(device="GPU"))]
        self.assertEqual(cycles_devices.apply(prefs, scenes, None, with_cpu=False), ["CPU"])
        self.assertEqual(prefs.compute_device_type, "NONE")
        self.assertEqual(scenes[0].cycles.device, "CPU")

    def test_probe_lists_gpu_names_per_backend(self):
        prefs = FakePrefs({"HIP": [FakeDevice("Card", "HIP"), FakeDevice("Chip", "CPU")], "CUDA": []})
        self.assertEqual(cycles_devices.probe(prefs, None), {"HIP": ["Card"], "CUDA": []})


class SelectTests(unittest.TestCase):
    def test_require_gpu_without_gpu_fails_and_changes_nothing(self):
        bpy, prefs, scenes = fake_bpy({"HIP": [FakeDevice("Chip", "CPU")]})
        with mock.patch.dict(sys.modules, {"bpy": bpy}):
            result = cycles_devices.select(require_gpu=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no GPU backend found")
        self.assertFalse(result["applied"])
        self.assertEqual((prefs.compute_device_type, scenes[0].cycles.device), ("NONE", "CPU"))

    def test_no_gpu_falls_back_to_cpu(self):
        bpy, _, scenes = fake_bpy({"HIP": [], "CUDA": []})
        with mock.patch.dict(sys.modules, {"bpy": bpy}):
            result = cycles_devices.select()
        self.assertTrue(result["ok"])
        self.assertEqual((result["backend"], result["devices"]), ("CPU", ["CPU"]))
        self.assertEqual(scenes[0].cycles.device, "CPU")

    def test_probe_only_reports_without_applying(self):
        bpy, prefs, scenes = fake_bpy({"HIP": [FakeDevice("Card", "HIP")]})
        with mock.patch.dict(sys.modules, {"bpy": bpy}):
            result = cycles_devices.select(probe_only=True)
        self.assertEqual((result["ok"], result["backend"], result["applied"]), (True, "HIP", False))
        self.assertEqual((prefs.compute_device_type, scenes[0].cycles.device), ("NONE", "CPU"))

    def test_gpu_found_is_applied(self):
        bpy, prefs, scenes = fake_bpy({"HIP": [FakeDevice("Card", "HIP")]})
        with mock.patch.dict(sys.modules, {"bpy": bpy}):
            result = cycles_devices.select()
        self.assertEqual((result["backend"], result["applied"], result["scenes"]), ("HIP", True, {"Scene": "GPU"}))
        self.assertEqual(prefs.compute_device_type, "HIP")


class MainTests(unittest.TestCase):
    def test_success_prints_one_result_line(self):
        lines, code = run_main(cycles_devices, ["--backend", "auto"], fake_bpy({"HIP": [FakeDevice("Card", "HIP")]})[0])
        self.assertEqual((len(lines), code), (1, 0))
        self.assertTrue(json.loads(lines[0][7:])["ok"])

    def test_bad_option_is_a_failed_result_line(self):
        lines, code = run_main(cycles_devices, ["--backend"], fake_bpy({})[0])
        self.assertEqual((len(lines), code), (1, 1))
        self.assertIn("bad option", json.loads(lines[0][7:])["error"])

    def test_missing_backend_is_a_failed_result_line(self):
        lines, code = run_main(cycles_devices, ["--backend", "OPTIX"], fake_bpy({"OPTIX": []})[0])
        self.assertEqual((len(lines), code), (1, 1))

    def test_smoke_without_out_is_a_failed_result_line(self):
        lines, code = run_main(smoke, ["--engine", "EEVEE"], fake_bpy({})[0])
        self.assertEqual((len(lines), code), (1, 1))
        self.assertIn("bad option", json.loads(lines[0][7:])["errors"][0])

    def test_smoke_refuses_bad_options_before_touching_the_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "notes.txt").write_text("keep me")
            cases = ((["--out", "relative/dir"], "must be an absolute path"),
                     (["--out", tmp], "did not write"),
                     (["--out", tmp + "/new", "--formats", "dae"], "unknown format"))
            for argv, expected in cases:
                bpy = fake_bpy({})[0]
                bpy.context.scene = None   # any scene access would raise
                lines, code = run_main(smoke, argv, bpy)
                self.assertEqual((len(lines), code), (1, 1), argv)
                self.assertEqual(len(json.loads(lines[0][7:])["errors"]), 1, argv)
                self.assertIn(expected, json.loads(lines[0][7:])["errors"][0], argv)
            self.assertEqual(sorted(os.listdir(tmp)), ["notes.txt"])

    def test_smoke_unreadable_out_is_a_failed_result_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            locked = Path(tmp, "locked")
            locked.mkdir(mode=0o000)
            try:
                if os.access(locked, os.R_OK):
                    self.skipTest("running with permission to read a mode-000 directory")
                lines, code = run_main(smoke, ["--out", str(locked)], fake_bpy({})[0])
            finally:
                locked.chmod(0o700)
            self.assertEqual((len(lines), code), (1, 1))
            self.assertIn("cannot be read", json.loads(lines[0][7:])["errors"][0])


class SmokeOptionTests(unittest.TestCase):
    def options(self, out, formats="glb"):
        return smoke.check_options(SimpleNamespace(out=out, formats=formats))

    def test_relative_out_and_unknown_format_are_refused(self):
        _, errors = self.options("relative/dir", "glb,dae")
        self.assertEqual(len(errors), 2)

    def test_out_with_foreign_files_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "notes.txt").write_text("keep me")
            _, errors = self.options(tmp)
            self.assertIn("did not write", errors[0])

    def test_out_holding_an_earlier_smoke_run_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "smoke.glb").write_bytes(b"x")
            Path(tmp, "textures").mkdir()
            self.assertEqual(self.options(tmp), (["glb"], []))

    def test_out_that_is_a_file_is_refused(self):
        with tempfile.NamedTemporaryFile() as handle:
            _, errors = self.options(handle.name)
            self.assertIn("not a directory", errors[0])

    def test_empty_format_list_renders_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.options(os.path.join(tmp, "new"), ""), ([], []))


class SmokeHelperTests(unittest.TestCase):
    def test_eevee_identifier_follows_the_version(self):
        self.assertEqual(smoke.engine_id(SimpleNamespace(app=SimpleNamespace(version=(5, 2, 3))), "eevee"),
                         "BLENDER_EEVEE")
        self.assertEqual(smoke.engine_id(SimpleNamespace(app=SimpleNamespace(version=(4, 5, 14))), "EEVEE"),
                         "BLENDER_EEVEE_NEXT")
        self.assertEqual(smoke.engine_id(SimpleNamespace(app=SimpleNamespace(version=(5, 2, 3))), "workbench"),
                         "BLENDER_WORKBENCH")

    def test_png_size_reads_ihdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.png")
            ihdr = struct.pack(">IIBBBBB", 7, 5, 8, 6, 0, 0, 0)
            chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)
            self.assertEqual(smoke.png_size(path), (7, 5))
            Path(path).write_bytes(b"not a png at all, just bytes")
            self.assertIsNone(smoke.png_size(path))


class SkillTextTests(unittest.TestCase):
    """The skill must work on any machine: no host paths or one machine's GPU."""

    MACHINE_SPECIFIC = ("/home/", "/Users/", "C:\\", "~/Applications", "RX 7800", "gfx1101", "i7-8700K")

    def test_no_machine_specifics_in_skill_files(self):
        for path in sorted(SKILL.rglob("*")):
            if path.suffix not in (".md", ".py"):
                continue
            text = path.read_text()
            for needle in self.MACHINE_SPECIFIC:
                self.assertNotIn(needle, text, f"{path.relative_to(SKILL)} mentions {needle!r}")


@unittest.skipUnless(os.environ.get("ASHA_TEST_BLENDER"), "set ASHA_TEST_BLENDER to a Blender executable")
class LiveSmokeTests(unittest.TestCase):
    def test_smoke_runs_headless(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run(
                [os.environ["ASHA_TEST_BLENDER"], "-b", "--factory-startup", "--python-exit-code", "1",
                 "-P", str(SCRIPTS / "smoke.py"), "--", "--out", tmp],
                capture_output=True, text=True, timeout=600)
            lines = [l for l in run.stdout.splitlines() if l.startswith("RESULT ")]
            self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-2000:])
            self.assertEqual(len(lines), 1)
            self.assertTrue(json.loads(lines[0][len("RESULT "):])["ok"])


if __name__ == "__main__":
    unittest.main()
