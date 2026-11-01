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
            concept(root / "brack]et.md", "Bracket")
            concept(root / "Founder's-Day.md", "Founders")
            concept(root / "plain.md", "Plain")
            # Standard grammar: names are written as they are (the upstream validator
            # decodes nothing), and the ones its grammar cannot address are named.
            self.assertEqual(["brack]et.md", "frag#ment.md", "with space.md"], [p.name for p in okf_index.unlinkable(root)])
            result = run(TOOLS / "okf_index.py", root)
            self.assertEqual(0, result.returncode)
            self.assertIn("UNLINKABLE  with space.md", result.stdout)
            self.assertIn("UNLINKABLE  frag#ment.md", result.stdout)
            index = (root / "index.md").read_text()
            self.assertIn("(./Founder's-Day.md)", index)
            self.assertIn("(./with space.md)", index)
            self.assertNotIn("%", index)
            # Obsidian grammar: encoded targets, only ']' is unaddressable.
            (root / ".okfconfig").write_text('{"links": "obsidian"}\n')
            okf_index.write_indexes(root)
            index = (root / "index.md").read_text()
            self.assertIn("(./with%20space.md)", index)
            self.assertIn("(./frag%23ment.md)", index)
            self.assertIn("(./Founder%27s-Day.md)", index)
            self.assertEqual(["brack]et.md"], [p.name for p in okf_index.unlinkable(root)])
            self.assertEqual(0, okf_validate.run(str(root), strict=True)["rc"])

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


def okf_io_iter(root: Path):
    import okf_io
    return okf_io.iter_markdown(root, okf_io.effective_excludes(root))


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


class VaultLinkTests(unittest.TestCase):
    """A vault like AAS: space-named notes, wikilinks, an .okfconfig selecting Obsidian's grammar."""

    def make_vault(self, tmp: Path) -> Path:
        vault = Path(tmp) / "Lore"
        concept(vault / "World/Places/Hot Springs Island.md", "Hot Springs Island", "See [[Swordfish Islands]] and [[Places/Swordfish Islands|the archipelago]].")
        concept(vault / "World/Places/Swordfish Islands.md", "Swordfish Islands", "Back to [[Hot Springs Island#Hexes]] and ![[map.png]] and [[Missing Note]].")
        concept(vault / "World/Magic/Wizardry.md", "Wizardry", "Same-dir twin resolves locally, as in Obsidian: [[Notes]].")
        concept(vault / "World/Magic/Notes.md", "Notes", "")
        concept(vault / "World/Places/Notes.md", "Notes", "")
        concept(vault / "Docs/Authority Map.md", "Authority Map", "From a third directory [[Notes]] is ambiguous; [[spellbook.pdf]] and [[Courts.canvas]] are attachments.")
        concept(vault / "Books/The Hush/01 Departure/Chapter 1.md", "Chapter 1", "Standard encoded link: [island](../../../World/Places/Hot%20Springs%20Island.md).")
        (vault / ".okfconfig").write_text('{"links": "obsidian"}\n')
        return vault

    def test_an_aliased_file_is_reachable_through_either_name(self):
        # ~/.asha/keeper-voice.md -> reference/keeper-voice.md: two admitted paths, one file.
        import okf_links
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            concept(home / "reference/keeper-voice.md", "Keeper voice")
            (home / "keeper-voice.md").symlink_to(home / "reference/keeper-voice.md")
            concept(home / "soul.md", "Soul")
            okf_index.write_indexes(home, title="Asha", okf_version="0.2")
            result = okf_validate.run(str(home), strict=True)
            self.assertEqual(0, result["rc"], result["output"] + "\n".join(result["asha_lints"]))
            files = [p for p in okf_io_iter(home)]
            graph = okf_links.analyze(home, files, "standard", files)
            self.assertEqual([], graph["unreachable"]); self.assertEqual([], graph["orphans"])
            (home / ".okfconfig").write_text('{"links": "obsidian"}\n')
            okf_index.write_indexes(home)
            result = okf_validate.run(str(home), strict=True)
            self.assertEqual(0, result["rc"], result["output"] + "\n".join(result["asha_lints"]))

    def test_dotted_note_names_are_notes_when_a_note_has_that_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            concept(vault / "World/v1.2.md", "Version")
            concept(vault / "World/Magic/Wizardry.md", "Wizardry", "See [[v1.2]] and [[workbench.archive]] and [[diagram.svg]].")
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            joined = "\n".join(okf_validate.run(str(vault), strict=True)["asha_lints"])
            self.assertNotIn("v1.2", joined, "a note with a dotted stem is linked, not an attachment")
            self.assertIn("broken wikilink to '[[workbench.archive]]'", joined, "no such note: broken, not silently skipped")
            self.assertNotIn("diagram.svg", joined)

    def test_aliases_are_one_match_and_their_links_union(self):
        import okf_links
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            concept(home / "a-dir/kv.md", "KV", "see [sib](./sibling.md)")
            concept(home / "a-dir/sibling.md", "Sibling")
            (home / "zz-kv.md").symlink_to(home / "a-dir/kv.md")  # alias sorting after the real file
            concept(home / "learnings/x.md", "X", "[[kv]] and [[zz-kv]]")
            (home / ".okfconfig").write_text('{"links": "obsidian"}\n')
            okf_index.write_indexes(home, title="Home", okf_version="0.2")
            idx = home / "a-dir/index.md"  # sibling reachable only through kv's own link
            idx.write_text("\n".join(l for l in idx.read_text().splitlines() if "sibling" not in l) + "\n")
            result = okf_validate.run(str(home), strict=True)
            joined = "\n".join(result["asha_lints"])
            self.assertNotIn("ambiguous wikilink '[[kv]]'", joined, "two paths to one file are one match")
            self.assertNotIn("sibling.md: not reachable", joined, "an alias must not erase the real file's outgoing links")

    def test_a_note_whose_stem_ends_in_an_attachment_suffix_is_still_a_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            concept(vault / "World/Courts.canvas.md", "Courts canvas note")
            concept(vault / "World/Magic/Wizardry.md", "Wizardry", "Note, not attachment: [[Courts.canvas]]; attachment: [[other.canvas]].")
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            idx = vault / "World/index.md"
            idx.write_text("\n".join(l for l in idx.read_text().splitlines() if "Courts" not in l) + "\n")
            joined = "\n".join(okf_validate.run(str(vault), strict=True)["asha_lints"])
            self.assertNotIn("Courts.canvas.md: orphan", joined, "the wikilink credited the note")
            self.assertNotIn("other.canvas", joined)

    def test_a_source_relative_wikilink_beats_the_basename_twin(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            concept(vault / "World/Magic/Wizardry.md", "Wizardry", "Mine: [[./Notes]].")
            (vault / "World/Places/Notes.md").unlink()
            concept(vault / "Books/Notes.md", "Notes")  # a twin elsewhere
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            joined = "\n".join(okf_validate.run(str(vault), strict=True)["asha_lints"])
            self.assertNotIn("Wizardry.md: ambiguous", joined, "./Notes names the directory, so no twin applies")

    def test_excluded_twins_never_make_an_admitted_target_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            concept(vault / "Private/Notes.md", "Notes")  # excluded below
            (vault / ".okfignore").write_text("Private/\n")
            (vault / "Docs/Authority Map.md").write_text("---\ntype: Reference\ntitle: Authority Map\ndescription: d\n---\n# Map\n[[Magic/Notes]] and [[Places/Notes]] and [[Private/Notes]] and [[WIZARDRY]] and [[world/magic/wizardry]].\n")
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            joined = "\n".join(okf_validate.run(str(vault), strict=True)["asha_lints"])
            self.assertNotIn("Private/Notes", joined, "a path-qualified link into an excluded note resolves silently")
            self.assertNotIn("WIZARDRY", joined); self.assertNotIn("world/magic", joined)
            concept(vault / "Books/The Hush/01 Departure/Chapter 1.md", "Chapter 1", "Twin outside: [[Notes]].")
            joined = "\n".join(okf_validate.run(str(vault), strict=True)["asha_lints"])
            self.assertIn("ambiguous wikilink '[[Notes]]' matches World/Magic/Notes.md, World/Places/Notes.md", joined,
                          "only admitted twins are reported; the excluded one does not join the list")

    def test_orphans_log_exemption_and_source_relative_links_in_obsidian_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            concept(vault / "World/Lonely.md", "Lonely")
            concept(vault / "World/Places/Nearby.md", "Nearby", "Relative: [[./Swordfish Islands]] and [[../Magic/Wizardry]]; comment %% [[Nope]] %% and <!-- [[Nope2]] -->; bare [./Hot Springs Island](./Hot%20Springs%20Island) lacks .md.")
            (vault / "log.md").write_text("# Log\n\n## 2026-10-31\n\n- links in the log may dangle: [[Gone]]\n")
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            index = vault / "World/index.md"  # drop Lonely's own entry, by hand
            index.write_text("\n".join(l for l in index.read_text().splitlines() if "Lonely" not in l) + "\n")
            result = okf_validate.run(str(vault), strict=True)
            joined = "\n".join(result["asha_lints"])
            self.assertIn("World/Lonely.md: orphan concept", joined)
            self.assertIn("World/Lonely.md: not reachable", joined)
            self.assertNotIn("Gone", joined, "log.md links are exempt")
            self.assertNotIn("Nope", joined, "comments are not links")
            self.assertNotIn("Nearby.md: broken", joined, "source-relative wikilinks resolve")
            self.assertNotIn("Wizardry.md: not reachable", joined)
            self.assertIn("is missing the .md extension", joined)
            (vault / ".okfconfig").write_text('{"links": "obsidian", "waive": ["description"]}\n')
            (vault / "World/Lonely.md").write_text("---\ntype: Reference\ntitle: Lonely\n---\n# Lonely\n")
            okf_index.write_indexes(vault)
            joined = "\n".join(okf_validate.run(str(vault), strict=True)["asha_lints"])
            self.assertNotIn("missing recommended field 'description'", joined)

    def test_indexes_encode_space_names_and_the_links_resolve(self):
        import okf_links
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            places = (vault / "World/Places/index.md").read_text()
            self.assertIn("- [Hot Springs Island](./Hot%20Springs%20Island.md): about Hot Springs Island", places)
            self.assertIn("- [The Hush/](./The%20Hush/index.md)", (vault / "Books/index.md").read_text())
            self.assertIn("- [01 Departure/](./01%20Departure/index.md)", (vault / "Books/The Hush/index.md").read_text())
            self.assertEqual("./Hot Springs Island.md", okf_links.decode_target("./Hot%20Springs%20Island.md"))
            # Reachability through the encoded entries, from the root index.
            files = [p for p in vault.rglob("*.md")]
            graph = okf_links.analyze(vault, files, "standard")
            self.assertEqual([], [p.name for p in graph["unreachable"]])

    def test_obsidian_mode_reads_wikilinks_and_reports_missing_and_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            result = okf_validate.run(str(vault), strict=True)
            self.assertEqual("obsidian", result["links"])
            joined = "\n".join(result["asha_lints"])
            self.assertIn("Swordfish Islands.md: broken wikilink to '[[Missing Note]]'", joined)
            self.assertIn("Authority Map.md: ambiguous wikilink '[[Notes]]' matches World/Magic/Notes.md, World/Places/Notes.md", joined)
            self.assertNotIn("Wizardry.md", joined, "a same-directory twin resolves locally")
            self.assertNotIn("spellbook.pdf", joined, "attachments are not concepts")
            self.assertNotIn("Courts.canvas", joined)
            self.assertNotIn("map.png", joined)
            self.assertNotIn("Hot Springs Island", joined, "encoded, partial-path and wikilinked targets resolve")
            self.assertNotIn("orphan", joined)
            self.assertEqual(1, result["rc"])
            # Fix the two problems and the vault passes strict.
            sf = vault / "World/Places/Swordfish Islands.md"
            sf.write_text(sf.read_text().replace(" and [[Missing Note]]", ""))
            am = vault / "Docs/Authority Map.md"
            am.write_text(am.read_text().replace("[[Notes]]", "[[Magic/Notes]]"))
            result = okf_validate.run(str(vault), strict=True)
            self.assertEqual(0, result["rc"], result["output"] + "\n".join(result["asha_lints"]))
            printed = run(TOOLS / "okf_validate.py", vault, "--strict").stdout
            self.assertIn("obsidian link grammar", printed)
            self.assertIn("okf_validate: pass;", printed)

    def test_standard_mode_cannot_pass_a_space_named_vault_but_asha_mode_can(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            (vault / ".okfconfig").unlink()
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            standard = okf_validate.run(str(vault), strict=True)
            self.assertEqual("standard", standard["links"])
            self.assertEqual(1, standard["rc"])
            self.assertIn("orphan concept", standard["output"])  # upstream cannot parse the encoded entries
            # Indexes written under the standard grammar are not encoded, so even the
            # obsidian validator cannot follow space-named entries: declare the grammar
            # before indexing, as the skill says.
            (vault / ".okfconfig").write_text('{"links": "obsidian"}\n')
            okf_index.write_indexes(vault)
            forced = okf_validate.run(str(vault), strict=True)
            joined = "\n".join(forced["asha_lints"])
            self.assertNotIn("orphan", joined); self.assertNotIn("missing the .md extension", joined)
            self.assertEqual(["Docs/Authority Map.md: ambiguous wikilink '[[Notes]]' matches World/Magic/Notes.md, World/Places/Notes.md",
                              "World/Places/Swordfish Islands.md: broken wikilink to '[[Missing Note]]'"], forced["asha_lints"])

    def test_a_link_into_an_excluded_note_is_not_broken_and_the_note_is_not_a_concept(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            concept(vault / "TTRPG/Notes/Casey/Diary.md", "Diary", "Private: [[Hot Springs Island]].")
            concept(vault / "TTRPG/Sessions/1895-09-05.md", "Session", "See [[Diary]] and [[TTRPG/Notes/Casey/Diary]].")
            (vault / ".okfignore").write_text("TTRPG/Notes/*/\n")
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            self.assertFalse((vault / "TTRPG/Notes/Casey/index.md").exists())
            result = okf_validate.run(str(vault), strict=True)
            joined = "\n".join(result["asha_lints"])
            self.assertNotIn("Diary", joined, "a link into an excluded note is neither broken nor an orphan")
            self.assertNotIn("TTRPG/Notes/Casey", result["output"])
            self.assertIn("Swordfish Islands.md: broken wikilink to '[[Missing Note]]'", joined)  # still reported

    def test_wikilinks_inside_code_are_documentation_not_links(self):
        import okf_links
        text = "Real [[Alpha]].\n\n```yaml\nscope: \"[[<Campaign>]]\"\n```\nInline `[[Beta]]` too."
        self.assertEqual([("Alpha", False)], okf_links.wikilinks(text))

    def test_a_bundle_may_waive_a_field_lint_its_taxonomy_owns(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            (vault / "World/Magic/Notes.md").write_text("---\ntype: Reference\ntitle: Notes\ndescription: d\nstatus: wip\n---\n# Notes\n")
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            result = okf_validate.run(str(vault), strict=True)
            self.assertIn("Notes.md: 'status' should be one of draft|stable|deprecated (found: 'wip')", "\n".join(result["asha_lints"]))
            (vault / ".okfconfig").write_text('{"links": "obsidian", "waive": ["status"]}\n')
            result = okf_validate.run(str(vault), strict=True)
            self.assertEqual(["status"], result["waivers"])
            self.assertNotIn("'status'", "\n".join(result["asha_lints"]))
            printed = run(TOOLS / "okf_validate.py", vault, "--strict").stdout
            self.assertIn("waived field lints for status", printed)
            # waivers never reach the standard grammar or the conformance errors
            self.assertEqual([], okf_validate.run(str(vault), strict=True, links="standard")["waivers"])

    def test_field_lints_still_run_in_obsidian_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.make_vault(Path(tmp))
            (vault / "World/Magic/Notes.md").write_text("---\ntype: Reference\ntitle: Notes\nstatus: bogus\n---\n# Notes\n")
            okf_index.write_indexes(vault, title="Lore", okf_version="0.2")
            result = okf_validate.run(str(vault), strict=True)
            joined = "\n".join(result["asha_lints"])
            self.assertIn("Notes.md: missing recommended field 'description'", joined)
            self.assertIn("'status' should be one of draft|stable|deprecated", joined)
            (vault / "World/Magic/Notes.md").write_text("---\ntitle: no type\n---\n# Notes\n")
            result = okf_validate.run(str(vault), strict=True)
            self.assertEqual(1, result["errors"])
            self.assertIn("missing a non-empty 'type'", result["output"])


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
