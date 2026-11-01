"""The okf plugin: pinned vendored tooling and Asha's wrappers over it."""

import hashlib
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "plugins/okf/tools"
FIXTURES = ROOT / "tests/fixtures/okf"
sys.path.insert(0, str(TOOLS))

import okf_index  # noqa: E402
import okf_log  # noqa: E402
import okf_validate  # noqa: E402

VENDORED = ("okf_common.py", "validate.py", "visualize.py")
PIN = "cbfb00bc42be73c0756b243029c783ad31406f60"


def run(*args, env=None):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def concept(path: Path, title: str, body: str = "", **fields):
    lines = ["---", "type: Reference", f"title: {title}", f"description: about {title}"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines += ["---", f"# {title}", body]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class VendoredPinTests(unittest.TestCase):
    def test_vendored_bodies_match_the_digests_in_their_headers(self):
        for name in VENDORED:
            lines = (TOOLS / name).read_text(encoding="utf-8").split("\n")
            self.assertTrue(lines[0].startswith("#!"), name)
            self.assertTrue(lines[1].startswith("# ----"), name)
            end = next(i for i in range(2, 12) if lines[i].startswith("# ----"))
            header = "\n".join(lines[1:end + 1])
            self.assertIn(PIN, header, name)
            digest = re.search(r"upstream-sha256: ([0-9a-f]{64})", header).group(1)
            body = "\n".join([lines[0], *lines[end + 1:]]).encode("utf-8")
            self.assertEqual(digest, hashlib.sha256(body).hexdigest(), f"{name} body differs from its pinned digest")

    def test_vendored_tools_load_with_their_shared_parser(self):
        for tool in ("validate.py", "visualize.py", "okf_validate.py", "okf_index.py", "okf_log.py", "okf_viz.py"):
            result = run(TOOLS / tool, "--help")
            self.assertEqual(0, result.returncode, f"{tool}: {result.stderr}")
            self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_pinned_validator_accepts_a_draft_concept_past_its_stale_after(self):
        # Decision 3 (stale candidates wait for a ruling) and decision 4 (strict
        # everywhere) coexist only while the pinned lint set stays like this.
        result = okf_validate.run(str(FIXTURES / "good"), strict=True)
        self.assertEqual(0, result["rc"], result["output"])
        self.assertEqual(5, result["files"])

    def test_vendored_validator_has_no_sinks(self):
        for name in VENDORED:
            text = (TOOLS / name).read_text(encoding="utf-8")
            for sink in ("urllib", "requests", "socket", "subprocess", "os.system", "eval(", "exec(", "pickle", "yaml.load("):
                self.assertNotIn(sink, text, f"{name} contains {sink}")


class ValidatorGateTests(unittest.TestCase):
    def test_good_twin_passes_with_the_expected_file_count(self):
        result = run(TOOLS / "okf_validate.py", FIXTURES / "good", "--strict")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("5 file(s), 0 error(s), 0 lint(s)", result.stdout)
        self.assertIn("okf_validate: pass; 5 file(s) validated", result.stdout)

    def test_broken_twin_fails_naming_the_error_and_the_orphan(self):
        result = run(TOOLS / "okf_validate.py", FIXTURES / "broken", "--strict")
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("missing a non-empty 'type'", result.stdout)
        self.assertIn("orphan concept", result.stdout)
        self.assertIn("not reachable from the root index.md", result.stdout)

    def test_exit_two_when_pyyaml_is_absent_is_reported_not_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "stubs/yaml"
            stub.mkdir(parents=True)
            (stub / "__init__.py").write_text("raise ImportError('planted: PyYAML absent')\n")
            result = run(TOOLS / "okf_validate.py", FIXTURES / "good", "--strict",
                         env={"PYTHONPATH": str(stub.parent)})
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("validator unavailable", result.stdout)
            self.assertNotIn("pass;", result.stdout)
            result = run(TOOLS / "okf_validate.py", FIXTURES / "good", "--strict", "--json",
                         env={"PYTHONPATH": str(stub.parent)})
            self.assertEqual(2, result.returncode)
            self.assertIn('"status": "validator unavailable"', result.stdout)

    def test_an_empty_validation_set_never_passes(self):
        result = run(TOOLS / "okf_validate.py", FIXTURES / "good", "--strict", "--exclude", "*")
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("0 file(s)", result.stdout)
        self.assertIn("empty validation set is not a pass", result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            result = run(TOOLS / "okf_validate.py", tmp, "--strict")
            self.assertEqual(1, result.returncode, result.stdout)

    def test_exclude_list_is_honoured_and_the_count_follows_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept(root / "keep/one.md", "One")
            concept(root / "state/control.md", "Control")
            (root / "skills/x").mkdir(parents=True)
            (root / "skills/x/SKILL.md").write_text("---\nname: x\n---\n")  # harness-owned, no type
            okf_index.write_indexes(root, excludes=["state/", "skills/*/SKILL.md"], title="T")
            result = okf_validate.run(str(root), strict=True, excludes=["state/", "skills/*/SKILL.md"])
            self.assertEqual(0, result["rc"], result["output"])
            self.assertEqual(3, result["files"])  # root index, keep/index.md, keep/one.md
            unfiltered = okf_validate.run(str(root), strict=True)
            self.assertEqual(1, unfiltered["rc"])
            self.assertIn("SKILL.md", unfiltered["output"])

    def test_a_validator_that_misreports_its_file_count_is_refused(self):
        from unittest import mock
        original = okf_validate.upstream.main

        def lying_main(argv):
            rc = original(argv)
            print("1 file(s), 0 error(s), 0 lint(s)")
            return rc
        with mock.patch.object(okf_validate.upstream, "main", lying_main):
            result = okf_validate.run(str(FIXTURES / "good"), strict=True)
        self.assertEqual(1, result["rc"])
        self.assertTrue(any("were expected" in p for p in result["count_problems"]), result)

    def test_asha_lints_cover_what_upstream_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept(root / "linked.md", "Linked")
            # A cycle nobody indexes: upstream's orphan lint accepts it.
            concept(root / "cyc-a.md", "Cyc A", "see [B](./cyc-b.md)")
            concept(root / "cyc-b.md", "Cyc B", "see [A](./cyc-a.md)")
            (root / "index.md").write_text("# T\n\n## Concepts\n\n- [Linked](./linked.md): about Linked\n")
            (root / "log.md").write_text("# Log\n\n## 2026-01-01\n\n- older first\n\n## 2026-02-01\n\n- newer last\n")
            concept(root / "bad-verified.md", "Bad", "", verified="[\"not-a-mapping\"]")
            (root / "index.md").write_text((root / "index.md").read_text() + "- [Bad](./bad-verified.md): about Bad\n")
            result = okf_validate.run(str(root), strict=True)
        joined = "\n".join(result["asha_lints"])
        self.assertIn("cyc-a.md: not reachable from the root index.md", joined)
        self.assertIn("cyc-b.md: not reachable from the root index.md", joined)
        self.assertIn("log.md: log.md date heading 2026-02-01 is not newer-first", joined)
        self.assertIn("bad-verified.md: 'verified' contains an entry that is not a mapping", joined)
        self.assertEqual(1, result["rc"])


class IndexerTests(unittest.TestCase):
    def test_generated_indexes_reach_every_concept_and_are_idempotent(self):
        rng = random.Random(20261031)
        for trial in range(25):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                names = ["alpha", "beta-2", "Gamma3", "delta.4", "eps_5"]
                dirs = ["", "a", "a/b", "c", "c/d/e"]
                written = []
                for i in range(rng.randint(1, 12)):
                    directory = rng.choice(dirs)
                    name = f"{rng.choice(names)}-{i}"
                    path = root / directory / f"{name}.md"
                    concept(path, f"Title {i}", description=f"d{i}")
                    written.append(path)
                if rng.random() < 0.5:
                    (root / "index.md").write_text('---\nokf_version: "0.2"\n---\n# old\n')
                okf_index.write_indexes(root, title="Bundle")
                first = {p: p.read_bytes() for p in root.rglob("*.md")}
                okf_index.write_indexes(root, title="Bundle")
                second = {p: p.read_bytes() for p in root.rglob("*.md")}
                self.assertEqual(first, second, f"trial {trial}: second run changed files")
                self.assertEqual([], okf_index.check_indexes(root, title="Bundle"))
                # BFS from the root index reaches every concept through ./ links.
                seen, queue = set(), [root / "index.md"]
                while queue:
                    current = queue.pop()
                    for target in okf_index.index_links(current.read_text()):
                        resolved = (current.parent / target).resolve()
                        if resolved not in seen:
                            seen.add(resolved)
                            queue.append(resolved)
                for path in written:
                    self.assertIn(path.resolve(), seen, f"trial {trial}: {path} unreachable")
                    self.assertEqual(concept_bytes(path), path.read_bytes())
                result = okf_validate.run(str(root), strict=True)
                self.assertEqual(0, result["rc"], f"trial {trial}: {result['output']}")
                if (root / "index.md").read_text().startswith("---"):
                    self.assertIn('okf_version: "0.2"', (root / "index.md").read_text())

    def test_check_reports_a_stale_index_and_the_orphan_control_lints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept(root / "one.md", "One")
            okf_index.write_indexes(root)
            concept(root / "two.md", "Two")
            self.assertEqual([root / "index.md"], okf_index.check_indexes(root))
            result = okf_validate.run(str(root), strict=True)
            self.assertEqual(1, result["rc"])
            self.assertIn("two.md: orphan concept", result["output"])
            self.assertEqual(1, run(TOOLS / "okf_index.py", root, "--check").returncode)
            self.assertEqual(0, run(TOOLS / "okf_index.py", root).returncode)
            self.assertEqual(0, okf_validate.run(str(root), strict=True)["rc"])

    def test_names_the_link_grammar_cannot_address_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept(root / "with space.md", "Space")
            concept(root / "frag#ment.md", "Frag")
            concept(root / "plain.md", "Plain")
            self.assertEqual(["frag#ment.md", "with space.md"],
                             [p.name for p in okf_index.unlinkable(root)])
            result = run(TOOLS / "okf_index.py", root)
            self.assertEqual(0, result.returncode)
            self.assertIn("UNLINKABLE  with space.md", result.stdout)
            self.assertIn("UNLINKABLE  frag#ment.md", result.stdout)

    def test_a_directory_that_had_an_index_keeps_one_when_emptied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept(root / "sub/one.md", "One")
            okf_index.write_indexes(root)
            (root / "sub/one.md").unlink()
            okf_index.write_indexes(root)
            self.assertEqual("# sub\n", (root / "sub/index.md").read_text())
            self.assertEqual(0, okf_validate.run(str(root), strict=True, allow_empty=False)["rc"])
            self.assertEqual([], okf_index.check_indexes(root))

    def test_non_root_indexes_never_carry_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept(root / "sub/one.md", "One")
            okf_index.write_indexes(root)
            self.assertFalse((root / "sub/index.md").read_text().startswith("---"))
            self.assertFalse((root / "index.md").read_text().startswith("---"))


def concept_bytes(path: Path) -> bytes:
    return path.read_bytes()


class BundleScopeTests(unittest.TestCase):
    """A bundle root like ~/.asha: directories reached through symlinks, scoped by .okfignore."""

    def make_home(self, tmp: Path) -> Path:
        dotfiles = tmp / "dotfiles"
        home = tmp / "home"
        home.mkdir()
        concept(dotfiles / "soul.md", "Soul")
        concept(dotfiles / "reference/deep.md", "Deep")
        concept(dotfiles / "cycle/loop.md", "Loop")
        (dotfiles / "cycle/back").symlink_to(dotfiles / "cycle", target_is_directory=True)  # a true cycle
        concept(home / "state/control/record.md", "State")  # runtime, not knowledge
        concept(home / "chat/notes.md", "Chat")
        (home / "soul.md").symlink_to(dotfiles / "soul.md")
        (home / "reference").symlink_to(dotfiles / "reference", target_is_directory=True)
        (home / "cycle").symlink_to(dotfiles / "cycle", target_is_directory=True)
        (home / "self").symlink_to(home, target_is_directory=True)  # a link back to the root
        (home / ".hidden").mkdir()
        concept(home / ".hidden/secret.md", "Hidden")
        (home / ".okfignore").write_text("# scope\nstate/\nchat/\nlearnings.pre-okf-*/\n")
        concept(home / "learnings.pre-okf-v02-20261031/old.md", "Backup")
        return home

    def test_walker_follows_directory_symlinks_once_and_honours_the_ignore_file(self):
        import okf_io
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(Path(tmp))
            rels = [p.relative_to(home).as_posix() for p in okf_io.iter_markdown(home, okf_io.effective_excludes(home))]
            self.assertEqual(["cycle/loop.md", "reference/deep.md", "soul.md"], rels)
            unscoped = [p.relative_to(home).as_posix() for p in okf_io.iter_markdown(home)]
            self.assertIn("state/control/record.md", unscoped)
            self.assertIn("learnings.pre-okf-v02-20261031/old.md", unscoped)
            self.assertNotIn(".hidden/secret.md", unscoped)
            self.assertFalse(any(r.startswith("cycle/back/") for r in unscoped), unscoped)
            self.assertFalse(any(r.startswith("self/") for r in unscoped), unscoped)

    def test_indexer_and_gate_scope_the_bundle_the_same_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(Path(tmp))
            written = okf_index.write_indexes(home, title="Asha", okf_version="0.2")
            self.assertEqual({home / "index.md", home / "reference/index.md", home / "cycle/index.md"}, set(written))
            self.assertFalse((home / "state/index.md").exists())
            self.assertFalse((home / "learnings.pre-okf-v02-20261031/index.md").exists())
            root = (home / "index.md").read_text()
            self.assertTrue(root.startswith('---\nokf_version: "0.2"\n---\n# Asha'), root)
            self.assertIn("- [reference/](./reference/index.md)", root)
            self.assertNotIn("self/", root)
            result = okf_validate.run(str(home), strict=True)
            self.assertEqual(0, result["rc"], result["output"])
            self.assertEqual(6, result["files"])
            self.assertEqual(["state/", "chat/", "learnings.pre-okf-*/"], result["excludes"])
            printed = run(TOOLS / "okf_validate.py", home, "--strict").stdout
            self.assertIn("excluded state/, chat/, learnings.pre-okf-*/; the verdict covers the admitted set", printed)
            unscoped = okf_validate.run(str(home), strict=True, use_ignore=False)
            self.assertEqual(1, unscoped["rc"])
            self.assertIn("state/control/record.md", unscoped["output"])

    def test_root_title_and_okf_version_survive_a_bare_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(Path(tmp))
            okf_index.write_indexes(home, title="Asha", okf_version="0.2")
            first = (home / "index.md").read_text()
            self.assertEqual([], okf_index.write_indexes(home))
            self.assertEqual(first, (home / "index.md").read_text())
            self.assertEqual([], okf_index.check_indexes(home))
            self.assertEqual(0, run(TOOLS / "okf_index.py", home, "--check").returncode)
            okf_index.write_indexes(home, title="Renamed")
            self.assertIn("# Renamed", (home / "index.md").read_text())
            self.assertIn('okf_version: "0.2"', (home / "index.md").read_text())

    def test_a_symlinked_index_is_written_through_not_replaced(self):
        import okf_io
        with tempfile.TemporaryDirectory() as tmp:
            dotfiles = Path(tmp) / "dotfiles"
            home = Path(tmp) / "home"
            dotfiles.mkdir(); home.mkdir()
            concept(home / "one.md", "One")
            (dotfiles / "index.md").write_text("# old\n")
            (home / "index.md").symlink_to(dotfiles / "index.md")
            okf_index.write_indexes(home, title="Home")
            self.assertTrue((home / "index.md").is_symlink(), "the stow link must survive a regeneration")
            self.assertIn("# Home", (dotfiles / "index.md").read_text())
            okf_io.atomic_write(home / "index.md", "# again\n")
            self.assertTrue((home / "index.md").is_symlink())
            self.assertEqual("# again\n", (dotfiles / "index.md").read_text())

    def test_root_frontmatter_keeps_only_okf_version_and_the_flag_replaces_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept(root / "one.md", "One")
            (root / "index.md").write_text('---\nokf_version: "0.1"\ntitle: hand\n---\n```\n# fenced\n```\n# Real heading\n')
            okf_index.write_indexes(root)
            text = (root / "index.md").read_text()
            self.assertTrue(text.startswith('---\nokf_version: "0.1"\n---\n# Real heading\n'), text)
            self.assertNotIn("title: hand", text)
            self.assertEqual(0, okf_validate.run(str(root), strict=True)["rc"])
            okf_index.write_indexes(root, okf_version="0.2")
            self.assertTrue((root / "index.md").read_text().startswith('---\nokf_version: "0.2"\n---\n# Real heading\n'))
            (root / "index.md").write_text("---\nnote: okf_version_history\n---\n# Hand\n")
            okf_index.write_indexes(root, okf_version="0.2")
            self.assertTrue((root / "index.md").read_text().startswith('---\nokf_version: "0.2"\n---\n# Hand\n'))

    def test_trailing_slash_patterns_may_carry_a_glob(self):
        import okf_io
        self.assertTrue(okf_io.is_excluded("learnings.pre-okf-v02-20261031/active/x.md", ["learnings.pre-okf-*/"]))
        self.assertTrue(okf_io.is_excluded("learnings.pre-okf-v02-20261031", ["learnings.pre-okf-*/"]))
        self.assertFalse(okf_io.is_excluded("learnings/active/x.md", ["learnings.pre-okf-*/"]))
        self.assertTrue(okf_io.is_excluded("state/control/a.md", ["state/"]))
        self.assertFalse(okf_io.is_excluded("statement.md", ["state/"]))


class LogTests(unittest.TestCase):
    def test_entries_are_grouped_newest_first_and_the_lint_sees_disorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            okf_log.append_entry(root, "**Creation** first", actor="asha/3.0.0", day="2026-10-01")
            okf_log.append_entry(root, "**Update** second", day="2026-10-02")
            okf_log.append_entry(root, "**Update** third the same day", day="2026-10-02")
            text = (root / "log.md").read_text()
            self.assertEqual(["## 2026-10-02", "## 2026-10-01"], re.findall(r"^## .*$", text, re.M))
            body = text.split("## 2026-10-02\n")[1]
            self.assertLess(body.index("third"), body.index("second"))
            self.assertIn("(asha/3.0.0)", text)
            self.assertFalse(text.startswith("---"))
            self.assertEqual([], okf_log.order_problems(text))
            self.assertTrue(okf_log.order_problems("# Log\n\n## 2026-01-01\n\n- a\n\n## 2026-02-01\n\n- b\n"))
            with self.assertRaises(ValueError):
                okf_log.append_entry(root, "   ")


class VizTests(unittest.TestCase):
    def test_inline_json_cannot_close_the_script_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = "</script><img src=x onerror=alert(1)>"
            (root / "evil.md").write_text(f"---\ntype: Reference\ntitle: Evil\ndescription: \"{payload}\"\n---\n# Evil\n")
            okf_index.write_indexes(root)
            result = run(TOOLS / "okf_viz.py", root)
            self.assertEqual(0, result.returncode, result.stderr)
            html = (root / "viz.html").read_text()
            self.assertNotIn(payload, html)
            self.assertIn("<\\/script><img", html)


if __name__ == "__main__":
    unittest.main()
