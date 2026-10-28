"""Tests for the image-3d-craft skill (plugins/image/skills/3d-craft).

These tests guard the skill text. Set ASHA_TEST_GODOT to a Godot 4 executable
to import an asset and run the report.
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SKILL = Path(__file__).parents[2] / "plugins" / "image" / "skills" / "3d-craft"
SCRIPTS = SKILL / "scripts"


class SkillTextTests(unittest.TestCase):
    """The skill must stay in its layer, harness-neutral and machine-neutral."""

    MACHINE_SPECIFIC = ("/home/", "/Users/", "C:\\", "~/Applications", "~/Code", "RX 7800", "gfx1101")

    def setUp(self):
        text = (SKILL / "SKILL.md").read_text()
        _, front, self.body = text.split("---", 2)
        self.front = yaml.safe_load(front)

    def test_frontmatter_has_only_name_and_description(self):
        self.assertEqual(set(self.front), {"name", "description"})
        self.assertEqual(self.front["name"], "image-3d-craft")

    def test_description_does_not_trigger_on_blender(self):
        self.assertNotIn("blender", self.front["description"].lower())
        self.assertRegex(self.front["description"], r"(?:^|[.;!?]\s+)Use when\b")
        self.assertLessEqual(len(self.front["description"]), 1024)

    def test_points_to_the_tool_layer(self):
        self.assertIn("`image-blender`", self.body)

    def test_no_persona_or_authority_claims(self):
        for path in sorted(SKILL.rglob("*.md")):
            text = path.read_text().lower()
            for phrase in ("source of truth", "i'm a", "i am a", "years experience", "always consult"):
                self.assertNotIn(phrase, text, f"{path.relative_to(SKILL)} says {phrase!r}")

    def test_no_machine_specifics(self):
        for path in sorted(SKILL.rglob("*")):
            if path.suffix not in (".md", ".py", ".gd"):
                continue
            text = path.read_text()
            for needle in self.MACHINE_SPECIFIC:
                self.assertNotIn(needle, text, f"{path.relative_to(SKILL)} mentions {needle!r}")

    def test_every_reference_and_script_is_named_in_skill_md(self):
        for path in sorted((SKILL / "references").iterdir()) + sorted(SCRIPTS.glob("*.[pg]*")):
            self.assertIn(f"{path.parent.name}/{path.name}", self.body, path.name)

    def test_relative_links_and_anchors_resolve(self):
        def anchors(path: Path) -> set[str]:
            heads = re.findall(r"^#+ (.+)$", path.read_text(), re.M)
            return {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in heads}

        for path in sorted(SKILL.rglob("*.md")):
            for file, anchor in re.findall(r"\]\(([^)#\s]+\.md)(?:#([^)]+))?\)", path.read_text()):
                linked = (path.parent / file).resolve()
                self.assertTrue(linked.is_file(), f"{path.name} links to missing {file}")
                if anchor:
                    self.assertIn(anchor, anchors(linked), f"{path.name} links to missing #{anchor} in {file}")


def tiny_gltf(path: Path) -> None:
    """Write a one-triangle glTF whose node name asks Godot for collision only."""
    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1]}],
        "nodes": [{"name": "Panel", "mesh": 0}, {"name": "Floor-colonly", "mesh": 0, "translation": [3, 0, 0]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                       "min": [0, 0, 0], "max": [1, 1, 0]}],
        "bufferViews": [{"buffer": 0, "byteLength": len(positions)}],
        "buffers": [{"byteLength": len(positions),
                     "uri": "data:application/octet-stream;base64," + base64.b64encode(positions).decode()}],
    }
    path.write_text(json.dumps(document))


def result_line(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1, stdout[-2000:]
    return json.loads(lines[0][len("RESULT "):])


@unittest.skipUnless(os.environ.get("ASHA_TEST_GODOT"), "set ASHA_TEST_GODOT to a Godot 4 executable")
class LiveGodotReportTests(unittest.TestCase):
    def test_import_and_report(self):
        godot = os.environ["ASHA_TEST_GODOT"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            # Keep the editor's settings and per-project logs out of the user's home.
            env = {**os.environ, **{f"XDG_{k}_HOME": str(Path(tmp) / k.lower()) for k in ("CONFIG", "DATA", "CACHE")}}
            (root / "project.godot").write_text('config_version=5\n\n[application]\nconfig/name="report"\n')
            tiny_gltf(root / "tiny.gltf")
            subprocess.run([godot, "--headless", "--path", str(root), "--import"],
                           capture_output=True, text=True, timeout=300, env=env)
            run = subprocess.run([godot, "--headless", "--path", str(root), "--script",
                                  str(SCRIPTS / "godot_report.gd"), "--", "res://tiny.gltf", "res://missing.glb"],
                                 capture_output=True, text=True, timeout=120, env=env)
            result = result_line(run.stdout)
            self.assertTrue((Path(tmp) / "data" / "godot").is_dir(), "Godot ignored XDG_DATA_HOME")
        self.assertEqual(run.returncode, 1)
        self.assertFalse(result["assets"]["res://missing.glb"]["ok"])
        tiny = result["assets"]["res://tiny.gltf"]
        self.assertTrue(tiny["ok"])
        self.assertEqual([m["surfaces"][0]["triangles"] for m in tiny["meshes"]], [1])
        self.assertEqual([b["body"] for b in tiny["bodies"]], ["StaticBody3D"])


if __name__ == "__main__":
    unittest.main()
