"""revision-pass step 7: the documented old-value proof covers a Work/novel manuscript."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugins/write/skills/revision-pass/SKILL.md"
PHRASE = "brass-bound lantern"


def documented_proof() -> str:
    """The bash block of step 7, as the skill documents it."""
    text = SKILL.read_text(encoding="utf-8")
    step = text.split("7. **Prove the old value is gone.**", 1)[1].split("\n8. ", 1)[0]
    block = step.split("```bash\n", 1)[1].split("```", 1)[0]
    return "\n".join(line.strip() for line in block.splitlines())


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class RevisionPassProofTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "manuscript"
        self.repo.mkdir()
        self.command = documented_proof()
        self.assertIn('"OLD"', self.command)

    def _proof(self) -> subprocess.CompletedProcess:
        command = self.command.replace('"OLD"', f'"{PHRASE}"')
        return subprocess.run(["bash", "-c", command], cwd=self.repo, capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, timeout=60)

    def _hits(self, result: subprocess.CompletedProcess) -> set[str]:
        return {line.split(":", 1)[0].removeprefix("./") for line in result.stdout.splitlines()}

    def _plant_notes(self):
        # Step 5 records the retired value in DECISIONS.md on purpose; notes and VCS stores quote it too.
        write(self.repo / "DECISIONS.md", f"- Retired the {PHRASE}; the lamp is iron now.\n")
        write(self.repo / "Work/novel/DECISIONS.md", f"- The {PHRASE} goes.\n")
        write(self.repo / "Work/reports/review.md", f"Reviewer: chapter 2 still has the {PHRASE}.\n")
        write(self.repo / "Work/review-config.md", f"watch: {PHRASE}\n")
        write(self.repo / ".git/COMMIT_EDITMSG", f"drop the {PHRASE}\n")
        write(self.repo / ".jj/repo/op", f"{PHRASE}\n")

    def test_finds_the_old_value_in_a_work_novel_manuscript_and_ignores_notes(self):
        self._plant_notes()
        write(self.repo / "Work/novel/story/manuscript.md", f"She raised the {PHRASE}.\n")
        write(self.repo / "Work/novel/state/ch02/draft.md", f"The {PHRASE} swung.\n")
        write(self.repo / "Work/novel/bible/world/lamps.md", f"Every ferry carries a {PHRASE}.\n")
        write(self.repo / "chapters/ch03.md", f"No {PHRASE} here, she lied.\n")
        result = self._proof()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self._hits(result), {
            "Work/novel/story/manuscript.md",
            "Work/novel/state/ch02/draft.md",
            "Work/novel/bible/world/lamps.md",
            "chapters/ch03.md",
        })

    def test_a_clean_manuscript_gives_empty_output(self):
        self._plant_notes()
        write(self.repo / "Work/novel/story/manuscript.md", "She raised the iron lamp.\n")
        result = self._proof()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_a_repository_without_work_novel_still_runs(self):
        self._plant_notes()
        write(self.repo / "chapters/ch01.md", f"The {PHRASE} hung by the door.\n")
        result = self._proof()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self._hits(result), {"chapters/ch01.md"})

    def test_the_contract_strings_the_install_test_checks_survive(self):
        text = SKILL.read_text(encoding="utf-8")
        for needle in ("exactly one read-only review agent to each act", "DECISIONS.md",
                       "Record the exact command and its empty output"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
