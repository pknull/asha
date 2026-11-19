"""Tests for tests/check-private-names.py: asha is public, so no tracked file may name a private project.

The guard reads its names from a local, untracked config; nothing in the repository lists them.
"""
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tests/check-private-names.py"


class PrivateNamesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.config = self.base / "private-names.conf"
        self.words = self.base / "words"
        self.words.write_text("keeper\nfrost\nlibrary\ngranary\n", encoding="utf-8")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_check(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), "--config", str(self.config),
             "--dict", str(self.words), *extra],
            capture_output=True, text=True)

    def test_no_config_skips_and_passes(self):
        self.write("a.md", "Wendeline was here.\n")
        env = {k: v for k, v in os.environ.items() if k not in ("ASHA_HOME", "ASHA_PRIVATE_NAMES")}
        env["HOME"] = str(self.base)
        got = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--home-only"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("skipped", got.stdout)

    def test_a_listed_name_fails_with_its_file_and_line(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.write("docs/a.md", "first line\nsee Wendeline's notes\n")
        got = self.run_check()
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("docs/a.md:2: Wendeline", got.stdout)

    def test_a_name_is_matched_whole(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.write("a.md", "Wendelines and XWendeline and wendelinesque\n")
        self.assertEqual(self.run_check().returncode, 0)

    def test_a_name_is_found_in_identifiers_paths_and_encodings(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        for text in ("myWendeline = 1", "wendelineState", "0Wendeline", "Wendeline2", "[x](./Notes%20Wendeline.md)"):
            with self.subTest(text=text):
                self.write("a.md", text + "\n")
                self.assertEqual(self.run_check().returncode, 1, text)

    def test_a_file_path_is_checked_as_well_as_its_text(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.write("fixtures/Wendeline.md", "nothing here\n")
        got = self.run_check()
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("fixtures/Wendeline.md:0: Wendeline", got.stdout)

    def test_term_matches_as_written_or_in_capitals_only(self):
        self.config.write_text("term: Gorse\n", encoding="utf-8")
        self.write("a.md", "a gorse hedge\n")
        self.assertEqual(self.run_check().returncode, 0)
        for text in ("Gorse nods.", "GORSE: here"):
            with self.subTest(text=text):
                self.write("a.md", text + "\n")
                self.assertEqual(self.run_check().returncode, 1, text)

    def test_locations_prints_the_place_not_the_matched_text(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.write("dir with space/a.md", "x\nWendeline\n")
        got = self.run_check("--locations")
        self.assertEqual(got.returncode, 1)
        self.assertIn("dir with space/a.md:2", got.stdout)
        self.assertNotIn("Wendeline", got.stdout + got.stderr)

    def test_a_missing_explicit_config_or_a_bad_config_is_an_error(self):
        got = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--config",
                              str(self.base / "absent.conf"), "--dict", str(self.words)], capture_output=True, text=True)
        self.assertEqual(got.returncode, 2, got.stdout)
        self.config.write_bytes(b"name: \xff\xfe\n")
        self.assertEqual(self.run_check().returncode, 2)

    def test_a_name_is_matched_in_any_case_and_inside_markdown_emphasis(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        for text in ("wendeline", "WENDELINE: here", "_Wendeline_ grins", "a `wendeline_state` key"):
            with self.subTest(text=text):
                self.write("a.md", text + "\n")
                self.assertEqual(self.run_check().returncode, 1, text)

    def test_a_name_after_a_literal_newline_escape_is_found(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.write("t.py", 'TEXT = "intro\\n\\nWendeline nods."\n')
        got = self.run_check()
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("t.py:1: Wendeline", got.stdout)

    def test_stems_take_note_names_but_skip_dictionary_words_and_short_names(self):
        vault = self.base / "vault"
        for rel in ("World/Characters/Wendeline Marsh.md", "World/Frost.md", "World/The Granary.md",
                    "World/Ox.md", "World/.hidden/Secretname.md", "World/index.md"):
            p = vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n", encoding="utf-8")
        self.config.write_text(f"stems: {vault / 'World'}\n", encoding="utf-8")
        self.write("a.md", "Frost and the Granary, Ox, Secretname and index.\n")
        self.assertEqual(self.run_check().returncode, 0, self.run_check().stdout)
        self.write("b.md", "the wendeline marsh road, a common phrase\n")  # a stem in lowercase is a word
        self.assertEqual(self.run_check().returncode, 0, self.run_check().stdout)
        self.write("b.md", "the Wendeline Marsh road\n")
        got = self.run_check()
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("b.md:1: Wendeline Marsh", got.stdout)
        self.write("b.md", "WENDELINE MARSH: here\n")
        self.assertEqual(self.run_check().returncode, 1)

    def test_allow_drops_a_name(self):
        self.config.write_text("name: Wendeline\nname: Corvane\nallow: Wendeline\n", encoding="utf-8")
        self.write("a.md", "Wendeline\n")
        self.assertEqual(self.run_check().returncode, 0)
        self.write("a.md", "Corvane\n")
        self.assertEqual(self.run_check().returncode, 1)

    def test_binary_files_are_skipped(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        (self.root / "blob.bin").write_bytes(b"\x00\xffWendeline\x00")
        self.assertEqual(self.run_check().returncode, 0)

    def test_a_missing_stems_dir_fails_rather_than_checking_less(self):
        self.config.write_text(f"name: Wendeline\nstems: {self.base / 'nowhere'}\n", encoding="utf-8")
        got = self.run_check()
        self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
        self.assertIn("missing", got.stderr)

    def vault(self, *rels):
        vault = self.base / "vault"
        for rel in rels:
            p = vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n", encoding="utf-8")
        self.config.write_text(f"stems: {vault}\n", encoding="utf-8")
        return vault

    def test_a_the_name_is_found_mid_sentence_and_without_its_article(self):
        self.vault("Places/The Pewter Gull.md")
        for text in ("we met at the Pewter Gull", "Pewter Gull, upstairs", "THE PEWTER GULL"):
            with self.subTest(text=text):
                self.write("a.md", text + "\n")
                self.assertEqual(self.run_check().returncode, 1, text)

    def test_a_multiword_name_is_found_joined_by_separators(self):
        self.vault("Places/Wendeline Marsh.md")
        for text in ("Wendeline-Marsh", "wendeline-marsh.md", "Wendeline_Marsh", "wendeline_marsh",
                     "Wendeline%20Marsh.md", "WendelineMarsh"):
            with self.subTest(text=text):
                self.write("a.md", text + "\n")
                self.assertEqual(self.run_check().returncode, 1, text)

    def test_folder_names_are_names_too(self):
        self.vault("Places/Evergrove/Inn.md")
        self.write("a.md", "the Evergrove road\n")
        self.assertEqual(self.run_check().returncode, 1)

    def test_without_a_dictionary_stems_are_an_error_not_a_smaller_check(self):
        self.vault("Places/Setting.md", "Places/Wendeline Marsh.md")
        self.words.unlink()
        self.write("a.md", "the Setting is generic\n")
        got = self.run_check()
        self.assertEqual(got.returncode, 2, got.stdout)
        self.assertIn("dictionary", got.stderr)
        self.config.write_text("name: Wendeline\n", encoding="utf-8")  # names alone need no dictionary
        self.assertEqual(self.run_check().returncode, 0)

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(self.root),
                        *args], check=True, capture_output=True)

    def test_rev_checks_the_commit_not_the_working_tree(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "Wendeline\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "one")
        self.write("a.md", "nobody\n")
        self.assertEqual(self.run_check().returncode, 0)
        got = self.run_check("--rev", "HEAD")
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("a.md:1: Wendeline", got.stdout)
        self.git("commit", "-q", "-am", "two")
        self.assertEqual(self.run_check("--rev", "HEAD").returncode, 0)

    def test_pre_push_reads_the_pushed_commits_from_stdin(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "Wendeline\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "one")
        sha = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        line = f"refs/heads/master {sha} refs/heads/master {'0' * 40}\n"
        got = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--config", str(self.config),
                              "--dict", str(self.words), "--pre-push"], input=line, capture_output=True, text=True)
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        deleted = f"(delete) {'0' * 40} refs/heads/old {sha}\n"
        got = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--config", str(self.config),
                              "--dict", str(self.words), "--pre-push"], input=deleted, capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

    def pre_push(self, local, remote="0" * 40):
        line = f"refs/heads/master {local} refs/heads/master {remote}\n"
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--config", str(self.config),
                               "--dict", str(self.words), "--pre-push"], input=line, capture_output=True, text=True)

    def head(self):
        return subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()

    def test_pre_push_checks_every_pushed_commit_and_its_message(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        base = self.head()
        self.write("a.md", "Wendeline\n")
        self.git("commit", "-q", "-am", "add")
        self.write("a.md", "clean again\n")
        self.git("commit", "-q", "-am", "remove")
        got = self.pre_push(self.head(), base)  # the tip is clean; the middle commit is not
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("a.md", got.stdout)
        self.git("commit", "-q", "--allow-empty", "-m", "about wendeline")
        got = self.pre_push(self.head(), base)
        self.assertIn("message", got.stdout)
        clean = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD~2"], capture_output=True,
                               text=True, check=True).stdout.strip()
        self.assertEqual(self.pre_push(self.head(), clean).returncode, 1)  # the message alone fails
        self.write("b.md", "++ Wendeline in a line that starts with two pluses\n")
        self.git("add", "b.md")
        self.git("commit", "-q", "-m", "pluses")
        got = self.pre_push(self.head(), subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD~1"], capture_output=True, text=True,
            check=True).stdout.strip())
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("b.md", got.stdout)
        self.git("rm", "-q", "b.md")
        self.git("commit", "-q", "-m", "drop")
        self.git("commit", "-q", "--allow-empty", "-m", "fine")
        self.assertEqual(self.pre_push(self.head(), subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD~1"], capture_output=True, text=True,
            check=True).stdout.strip()).returncode, 0)

    def test_slug_forms_of_a_term_are_found_but_not_the_word(self):
        self.config.write_text("term: Gorse\n", encoding="utf-8")
        for text in ("  - gorse: NOT spawned (GM voice)", "subagent_type: character:gorse", "see gorse.md",
                     "Work/rp/gorse/log/day-0001.md"):
            with self.subTest(text=text):
                self.write("a.md", text + "\n")
                self.assertEqual(self.run_check().returncode, 1, text)
        self.write("a.md", "a gorse hedge: thick, low\n")
        self.assertEqual(self.run_check().returncode, 0)

    def load_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("check_private_names", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_config_lookup_order(self):
        mod = self.load_module()
        env = {"ASHA_PRIVATE_NAMES": str(self.config)}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(mod.config_path(None), (str(self.config), True))
        asha_home = self.base / "ah"
        asha_home.mkdir()
        (asha_home / "private-names.conf").write_text("name: X\n", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"ASHA_HOME": str(asha_home), "HOME": str(self.base)}):
            os.environ.pop("ASHA_PRIVATE_NAMES", None)
            self.assertEqual(mod.config_path(None)[0], str(asha_home / "private-names.conf"))
        account = self.base / "account"
        (account / ".asha").mkdir(parents=True)
        (account / ".asha" / "private-names.conf").write_text("name: X\n", encoding="utf-8")
        fake = unittest.mock.Mock(pw_dir=str(account))
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.base / "sandbox")}), \
                unittest.mock.patch.object(mod.pwd, "getpwuid", return_value=fake):
            os.environ.pop("ASHA_PRIVATE_NAMES", None)
            os.environ.pop("ASHA_HOME", None)
            self.assertEqual(mod.config_path(None)[0], str(account / ".asha" / "private-names.conf"))
            self.assertNotEqual(mod.config_path(None, home_only=True)[0], str(account / ".asha" / "private-names.conf"))

    def test_pre_push_checks_renamed_binary_and_empty_files_by_path(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("clean.md", "clean\n")
        self.git("add", "clean.md")
        self.git("commit", "-q", "-m", "base")
        base = self.head()
        for step in ("rename", "binary", "empty"):
            with self.subTest(step=step):
                if step == "rename":
                    self.git("mv", "clean.md", "Wendeline.md")
                elif step == "binary":
                    (self.root / "Wendeline.bin").write_bytes(b"\x00\x01\x02")
                    self.git("add", "Wendeline.bin")
                else:
                    (self.root / "wendeline-notes.txt").write_bytes(b"")
                    self.git("add", "wendeline-notes.txt")
                self.git("commit", "-q", "-m", step)
                got = self.pre_push(self.head(), base)
                self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
                base = self.head()

    def test_day_files_and_letterless_note_names_are_not_names(self):
        self.vault("Log/day-0012.md", "Notes/1-11-26.md", "Notes/1895-10-14.md")
        self.write("a.md", "see day-0012.md, logged 2021-11-26 and 1895-10-14\n")
        self.assertEqual(self.run_check().returncode, 0, self.run_check().stdout)

    def test_pre_push_skips_commits_already_on_a_remote(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "Wendeline\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "old leak")
        self.write("a.md", "clean\n")
        self.git("commit", "-q", "-am", "clean")
        self.git("update-ref", "refs/remotes/origin/master", self.head())
        self.git("commit", "-q", "--allow-empty", "-m", "new")
        self.assertEqual(self.pre_push(self.head(), "0" * 40).returncode, 0)  # a new branch: only "new" is new
        self.assertEqual(self.pre_push(self.head(), "1" * 40).returncode, 0)  # an old sha this clone lacks

    def test_pre_push_reads_a_message_without_a_trailing_newline(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "Wendeline\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "add")
        base = self.head()
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        (self.base / "msg").write_bytes(b"remove it")  # no trailing newline
        self.git("commit", "-q", "--cleanup=verbatim", "-F", str(self.base / "msg"))
        got = self.pre_push(self.head(), base)  # the removed line is not added text
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

    def test_pre_push_finds_a_name_after_an_unusual_line_break(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        base = self.head()
        (self.root / "a.md").write_bytes(b"clean\ntext\x0cWendeline\n")
        self.git("commit", "-q", "-am", "feed")
        self.assertEqual(self.pre_push(self.head(), base).returncode, 1)

    def test_the_guard_runs_from_python_c_as_the_hook_runs_it(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.write("a.md", "Wendeline\n")
        got = subprocess.run([sys.executable, "-I", "-c", SCRIPT.read_text(encoding="utf-8"), "--root", str(self.root),
                              "--config", str(self.config), "--dict", str(self.words)], capture_output=True, text=True)
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)

    def test_config_precedence(self):
        mod = self.load_module()
        other = self.base / "other.conf"
        other.write_text("name: X\n", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"ASHA_PRIVATE_NAMES": str(other)}):
            self.assertEqual(mod.config_path(str(self.config)), (str(self.config), True))
        ah, home = self.base / "ah2", self.base / "home2"
        for d in (ah, home / ".asha"):
            d.mkdir(parents=True)
            (d / "private-names.conf").write_text("name: X\n", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"ASHA_HOME": str(ah), "HOME": str(home)}):
            os.environ.pop("ASHA_PRIVATE_NAMES", None)
            self.assertEqual(mod.config_path(None)[0], str(ah / "private-names.conf"))

    def bare(self, name):
        path = self.base / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True, capture_output=True)
        self.git("remote", "add", name, str(path))
        return path

    def push_check(self, local, remote_sha, remote, local_ref="refs/heads/master", remote_ref="refs/heads/master",
                   url=None):
        line = f"{local_ref} {local} {remote_ref} {remote_sha}\n"
        extra = ["--url", url] if url else []
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--config", str(self.config),
                               "--dict", str(self.words), "--pre-push", "--remote", remote, *extra], input=line,
                              capture_output=True, text=True)

    def leak_repo(self):
        """base pushed to pub; a leak commit; a clean commit on top. Returns (base, leak)."""
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master")
        base = self.head()
        self.write("a.md", "Wendeline\n")
        self.git("commit", "-q", "-am", "leak")
        leak = self.head()
        self.write("a.md", "clean again\n")
        self.git("commit", "-q", "-am", "clean")
        return base, leak

    def test_the_push_url_not_the_fetch_url_is_the_destination(self):
        base, leak = self.leak_repo()
        self.bare("priv")
        self.git("push", "-q", "--no-verify", "priv", f"{leak}:refs/heads/master")
        self.git("remote", "add", "mirror", str(self.base / "priv.git"))
        self.git("remote", "set-url", "--push", "mirror", str(self.base / "pub.git"))
        got = self.push_check(self.head(), "0" * 40, "mirror", url=str(self.base / "pub.git"))
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)

    def test_what_only_the_live_destination_holds_is_not_checked_again(self):
        base, leak = self.leak_repo()
        self.git("push", "-q", "--no-verify", "pub", f"{leak}:refs/heads/old")
        self.git("update-ref", "-d", "refs/remotes/pub/old")  # only ls-remote knows pub holds the leak
        got = self.push_check(self.head(), base, "pub")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

    def test_a_destination_that_moved_ahead_unfetched_is_no_error(self):
        base, leak = self.leak_repo()
        other = self.base / "other"
        subprocess.run(["git", "clone", "-q", str(self.base / "pub.git"), str(other)], check=True, capture_output=True)
        (other / "b.md").write_text("theirs\n", encoding="utf-8")
        for args in (["add", "b.md"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m",
                                       "theirs"], ["push", "-q", "origin", "HEAD:refs/heads/master"]):
            subprocess.run(["git", "-C", str(other), *args], check=True, capture_output=True)
        got = self.push_check(self.head(), base, "pub", remote_ref="refs/heads/feature")
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)  # the leak is new to pub: checked, not an error

    def test_textconv_cannot_hide_pushed_content(self):
        base, _ = self.leak_repo()
        self.write(".gitattributes", "*.md diff=hide\n")
        self.git("config", "diff.hide.textconv", "sed s/Wendeline/x/")
        self.assertEqual(self.push_check(self.head(), base, "pub").returncode, 1)

    def test_binary_content_is_not_scanned_as_text(self):
        self.config.write_text("name: Odd\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master")
        base = self.head()
        (self.root / "blob.bin").write_bytes(b"\x00\x01 odd \x00\xff\n\x00odd\n")
        self.git("add", "blob.bin")
        self.git("commit", "-q", "-m", "blob")
        self.assertEqual(self.push_check(self.head(), base, "pub").returncode, 0)

    def test_a_lowercase_term_in_a_pushed_ref_name_is_found(self):
        self.config.write_text("term: Gorse\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        head = self.head()
        for ref in ("refs/heads/gorse", "refs/heads/fix-gorse", "refs/heads/rp/gorse"):
            with self.subTest(ref=ref):
                self.assertEqual(self.push_check(head, "0" * 40, "pub", remote_ref=ref).returncode, 1, ref)

    def test_deleting_a_named_branch_and_a_local_ref_name_are_allowed(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master")
        head = self.head()
        line = f"(delete) {'0' * 40} refs/heads/wendeline-notes {head}\n"
        got = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--config", str(self.config),
                              "--dict", str(self.words), "--pre-push", "--remote", "pub"], input=line,
                             capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stdout)
        got = self.push_check(head, head, "pub", local_ref="refs/heads/wendeline")  # the remote never sees it
        self.assertEqual(got.returncode, 0, got.stdout)

    def test_user_log_settings_do_not_hide_content(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "log.showRoot", "false")
        self.git("config", "log.diffMerges", "off")
        self.write("a.md", "Wendeline\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "root")
        self.bare("pub")
        got = self.push_check(self.head(), "0" * 40, "pub")
        self.assertIn(":a.md: Wendeline", got.stdout)  # the content hit (a path hit reads a.md:0)

    def test_a_non_utf8_tag_message_is_checked_not_a_crash(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        (self.base / "tagmsg").write_bytes(b"caf\xe9 Wendeline\n")
        self.git("tag", "-a", "v1", "-F", str(self.base / "tagmsg"))
        tag = subprocess.run(["git", "-C", str(self.root), "rev-parse", "v1"], capture_output=True, text=True,
                             check=True).stdout.strip()
        got = self.push_check(tag, "0" * 40, "pub", "refs/tags/v1", "refs/tags/v1")
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)

    def test_pre_push_checks_commits_that_only_another_remote_holds(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        self.bare("priv")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master")
        base = self.head()
        self.write("a.md", "Wendeline\n")
        self.git("commit", "-q", "-am", "leak")
        self.git("push", "-q", "--no-verify", "priv", "HEAD:refs/heads/master")
        self.write("a.md", "clean again\n")
        self.git("commit", "-q", "-am", "clean")
        got = self.push_check(self.head(), base, "pub")
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)  # priv holds the leak; pub does not
        self.assertEqual(self.push_check(self.head(), self.head(), "priv").returncode, 0)

    def test_pre_push_checks_ref_names_and_tag_messages(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master")
        head = self.head()
        got = self.push_check(head, "0" * 40, "pub", "refs/heads/x", "refs/heads/wendeline-notes")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.git("tag", "-a", "v1", "-m", "for wendeline")
        tag = subprocess.run(["git", "-C", str(self.root), "rev-parse", "v1"], capture_output=True, text=True,
                             check=True).stdout.strip()
        got = self.push_check(tag, "0" * 40, "pub", "refs/tags/v1", "refs/tags/v1")
        self.assertEqual(got.returncode, 1, got.stdout)

    def test_pre_push_checks_a_root_commit_and_a_merge_by_path(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        (self.root / "Wendeline.md").write_bytes(b"")
        self.git("add", "Wendeline.md")
        self.git("commit", "-q", "-m", "root")
        self.bare("pub")
        self.assertEqual(self.push_check(self.head(), "0" * 40, "pub").returncode, 1)  # the root commit's path
        self.git("rm", "-q", "Wendeline.md")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "clean")
        self.git("branch", "side")
        self.write("b.md", "main side\n")
        self.git("add", "b.md")
        self.git("commit", "-q", "-m", "main")
        self.git("checkout", "-q", "side")
        self.write("c.md", "side\n")
        self.git("add", "c.md")
        self.git("commit", "-q", "-m", "side")
        self.git("checkout", "-q", "-")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master", "side:refs/heads/side")
        before = self.head()
        self.git("merge", "-q", "--no-commit", "--no-ff", "side")
        (self.root / "wendeline-log.txt").write_bytes(b"")
        self.git("add", "wendeline-log.txt")
        self.git("commit", "-q", "-m", "merge")
        got = self.push_check(self.head(), before, "pub")
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)  # only the merge commit adds the path

    def test_a_text_attribute_does_not_hide_pushed_content(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write(".gitattributes", "*.md -diff\n")
        self.write("a.md", "clean\n")
        self.git("add", ".gitattributes", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master")
        base = self.head()
        self.write("a.md", "Wendeline\n")
        self.git("commit", "-q", "-am", "leak")
        got = self.push_check(self.head(), base, "pub")
        self.assertIn("a.md:", got.stdout)
        self.assertNotIn("a.md:0", got.stdout)  # a content hit, not only the path

    def test_a_git_failure_is_an_error_not_a_pass(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.assertEqual(self.pre_push("f" * 40).returncode, 2)  # a commit this clone does not have

    def run_hook(self, stdin):
        env = {k: v for k, v in os.environ.items() if k not in ("ASHA_HOME",)}
        env["ASHA_PRIVATE_NAMES"] = str(self.config)
        return subprocess.run(["sh", str(REPO / "tests/hooks/pre-push"), "pub", str(self.base / "pub.git")],
                              cwd=self.root, input=stdin, capture_output=True, text=True, env=env)

    def test_the_hook_runs_a_trusted_guard_or_skips(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.bare("pub")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master")
        base = self.head()
        self.write("a.md", "Wendeline\n")
        self.git("commit", "-q", "-am", "leak")
        self.bare("priv")
        self.git("push", "-q", "--no-verify", "priv", "HEAD:refs/heads/master")  # another remote holds the leak
        line = f"refs/heads/master {self.head()} refs/heads/master {base}\n"
        got = self.run_hook(line)  # no guard anywhere: a loud skip
        self.assertEqual(got.returncode, 0)
        self.assertIn("skipped", got.stderr)
        guard = self.root / "tests/check-private-names.py"
        guard.parent.mkdir()
        guard.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        got = self.run_hook(line)
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)  # the working copy's guard
        guard.unlink()
        guard.parent.rmdir()
        self.git("checkout", "-q", "-b", "trusted", base)  # origin/master: the guard, and no leak
        guard.parent.mkdir()
        guard.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        self.git("add", "tests/check-private-names.py")
        self.git("commit", "-q", "-m", "guard")
        self.git("update-ref", "refs/remotes/origin/master", "trusted")
        self.git("checkout", "-q", "master")
        self.assertFalse(guard.exists())
        got = self.run_hook(line)
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)  # origin/master's guard

    def test_a_public_leak_under_an_unfetched_tip_is_not_rechecked(self):
        base, leak = self.leak_repo()
        self.git("push", "-q", "--no-verify", "pub", f"{leak}:refs/heads/master")  # the leak is public already
        other = self.base / "other2"
        subprocess.run(["git", "clone", "-q", str(self.base / "pub.git"), str(other)], check=True, capture_output=True)
        (other / "b.md").write_text("theirs\n", encoding="utf-8")
        for args in (["add", "b.md"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m",
                                       "theirs"], ["push", "-q", "origin", "HEAD:refs/heads/master"]):
            subprocess.run(["git", "-C", str(other), *args], check=True, capture_output=True)
        got = self.push_check(self.head(), "0" * 40, "pub", remote_ref="refs/heads/feature")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)  # pub's tip is unknown here; its tracking ref is not

    def test_a_line_added_only_by_a_merge_is_found_whatever_log_diffmerges_says(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "log.diffMerges", "off")
        self.write("a.md", "clean\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "side")
        self.write("b.md", "main\n")
        self.git("add", "b.md")
        self.git("commit", "-q", "-m", "main")
        self.git("checkout", "-q", "side")
        self.write("c.md", "side\n")
        self.git("add", "c.md")
        self.git("commit", "-q", "-m", "side")
        self.git("checkout", "-q", "-")
        self.bare("pub")
        self.git("push", "-q", "pub", "HEAD:refs/heads/master", "side:refs/heads/side")
        before = self.head()
        self.git("merge", "-q", "--no-commit", "--no-ff", "side")
        self.write("a.md", "clean\nWendeline\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "merge")
        got = self.push_check(self.head(), before, "pub")
        self.assertIn(":a.md: Wendeline", got.stdout, got.stdout + got.stderr)

    def test_a_clean_tree_reports_what_it_checked(self):
        self.config.write_text("name: Wendeline\n", encoding="utf-8")
        self.write("a.md", "nothing private\n")
        got = self.run_check()
        self.assertEqual(got.returncode, 0)
        self.assertIn("clean: 1 names, 2 lines", got.stdout)


if __name__ == "__main__":
    unittest.main()
