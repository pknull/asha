"""Issue #125: the portable GitHub CLI foundation skill and its boundaries."""

import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "plugins/code/skills/github-cli"
SKILL = SKILL_DIR / "SKILL.md"
SETUP = SKILL_DIR / "references/setup.md"
FIXTURE = json.loads((ROOT / "tests/fixtures/gh-cli-2.45.0.json").read_text(encoding="utf-8"))
COMMANDS = FIXTURE["commands"]
TARGETED = ("issue ", "pr ", "run ", "release ")
SEPARATELY_AUTHORIZED = "## Separately authorized actions"


def code_blocks(path):
    """Yield (heading, line) for each line of each fenced shell block."""
    heading, inside = "", False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            inside = not inside and line.strip() in ("```bash", "```sh")
            continue
        if inside:
            yield heading, line
        elif line.startswith("## "):
            heading = line.strip()


def gh_invocations(path):
    """Parse every documented gh command into (heading, path, tokens)."""
    found = []
    for heading, line in code_blocks(path):
        if not line.startswith("gh "):
            continue
        tokens = shlex.split(line, comments=True)
        if "|" in tokens:  # an unquoted pipe ends the gh command; quoted jq pipes stay inside one token
            tokens = tokens[:tokens.index("|")]
        if tokens[1].startswith("-"):
            found.append((heading, "", tokens))
            continue
        two = " ".join(tokens[1:3])
        command = two if two in COMMANDS else tokens[1]
        found.append((heading, command, tokens))
    return found


def documented():
    return [(path.name, *item) for path in (SKILL, SETUP) for item in gh_invocations(path)]


class FixtureCheckTests(unittest.TestCase):
    def test_every_documented_command_flag_and_json_field_exists_in_gh(self):
        commands = documented()
        self.assertGreater(len(commands), 25)
        for source, heading, path, tokens in commands:
            with self.subTest(command=" ".join(tokens)):
                if not path:
                    self.assertIn(tokens[1:], (["--version"], ["--help"]))
                    continue
                self.assertIn(path, COMMANDS, f"{source}: gh {path} is not a recorded command")
                known = set(COMMANDS[path]["flags"])
                rest = tokens[1 + len(path.split()):]
                for index, token in enumerate(rest):
                    if not token.startswith("-"):
                        continue
                    flag = token.split("=", 1)[0]
                    self.assertIn(flag, known, f"{source}: gh {path} has no {flag} in gh {FIXTURE['gh_version']}")
                    if flag == "--json":
                        fields = set(rest[index + 1].split(","))
                        self.assertLessEqual(fields, set(COMMANDS[path]["json_fields"]),
                                             f"unknown --json field for gh {path}")

    def test_the_fixture_itself_rejects_an_invented_flag(self):
        # Proves the check above discriminates.
        self.assertNotIn("--include-secrets", COMMANDS["pr view"]["flags"])
        self.assertNotIn("--json", COMMANDS["pr checks"]["flags"],
                         "gh 2.45.0 pr checks has no --json; the skill must not document it")


class SafetyContractTests(unittest.TestCase):
    def setUp(self):
        self.commands = documented()
        self.lines = [" ".join(tokens) for _, _, _, tokens in self.commands]

    def test_credentials_are_never_displayed(self):
        for line in self.lines:
            with self.subTest(command=line):
                self.assertNotIn("auth token", line)
                self.assertNotIn("--show-token", line)
                if line.startswith("gh auth status"):
                    self.assertNotIn(" -t", f" {line} ")
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("gh auth status", text)
        self.assertIn('[ -n "${GH_TOKEN:-}" ]', text)

    def test_targeted_commands_name_the_repository(self):
        for _, _, path, tokens in self.commands:
            if path.startswith(TARGETED):
                with self.subTest(command=" ".join(tokens)):
                    self.assertTrue({"--repo", "-R"} & set(tokens))

    def test_every_remote_command_pins_the_target_host(self):
        # Bare OWNER/REPO and a bare gh api resolve through GH_HOST or gh's only
        # configured host, not the target the user named.
        for _, _, path, tokens in self.commands:
            with self.subTest(command=" ".join(tokens)):
                if path.startswith(TARGETED):
                    flag = "--repo" if "--repo" in tokens else "-R"
                    self.assertTrue(tokens[tokens.index(flag) + 1].startswith("HOST/"))
                if path == "repo view":
                    self.assertTrue(tokens[3].startswith("HOST/"))
                if path in ("api", "auth status", "auth login"):
                    self.assertEqual("HOST", tokens[tokens.index("--hostname") + 1])

    def test_api_examples_are_bounded_get_requests(self):
        write_flags = {"-X", "--method", "-f", "--raw-field", "-F", "--field", "--input", "--paginate"}
        api = [tokens for _, _, path, tokens in self.commands if path == "api"]
        self.assertTrue(api)
        for tokens in api:
            with self.subTest(command=" ".join(tokens)):
                self.assertEqual(set(), write_flags & {t.split("=", 1)[0] for t in tokens})
                self.assertIn("per_page=", tokens[2])

    def test_potentially_large_output_is_bounded(self):
        unbounded = ("gh pr diff", "gh run view", "gh pr checks")
        for _, line in code_blocks(SKILL):
            if line.startswith(unbounded) and "--json" not in line:
                with self.subTest(command=line):
                    self.assertRegex(line, r" \| (head|tail) -n [0-9]+$")

    def test_reads_never_mutate_local_state(self):
        forbidden = ("pr checkout", "repo clone", "repo fork", "repo set-default",
                     "auth setup-git", "config set", "extension", "auth refresh")
        for line in self.lines:
            for words in forbidden:
                with self.subTest(command=line, forbidden=words):
                    self.assertNotIn(f"gh {words}", line)

    def test_pull_requests_are_drafts_and_risky_options_absent(self):
        for _, _, path, tokens in self.commands:
            with self.subTest(command=" ".join(tokens)):
                if path == "pr create":
                    self.assertIn("--draft", tokens)
                    self.assertIn("--head", tokens)
                    for option in ("--fill", "--web"):
                        self.assertNotIn(option, tokens)
                if path == "release create":
                    self.assertIn("--draft", tokens)
                for option in ("--admin", "--auto", "--delete-branch"):
                    self.assertNotIn(option, tokens)

    def test_merge_approval_ready_and_release_only_under_separate_authorization(self):
        guarded = []
        for source, heading, path, tokens in self.commands:
            if path in ("pr merge", "pr ready", "release create") or (path == "pr review" and "--approve" in tokens):
                guarded.append(path)
                with self.subTest(command=" ".join(tokens)):
                    self.assertEqual("SKILL.md", source)
                    self.assertEqual(SEPARATELY_AUTHORIZED, heading)
        self.assertEqual(["pr ready", "pr review", "pr merge", "release create"], guarded)
        merge = next(tokens for _, _, path, tokens in self.commands if path == "pr merge")
        self.assertIn("--match-head-commit", merge)

    def test_login_appears_only_in_the_approval_gated_setup_reference(self):
        logins = [source for source, _, path, _ in self.commands if path == "auth login"]
        self.assertEqual(["setup.md"], logins)
        setup = SETUP.read_text(encoding="utf-8")
        self.assertIn("explicit approval", setup)
        self.assertIn("https://github.com/cli/cli#installation", setup)

    def test_hosts_come_from_the_user_never_from_fetched_text(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Use only the host of the target the user named", " ".join(text.split()))
        self.assertIn("never a host taken from an issue, comment, log or link", " ".join(text.split()))
        self.assertIn("never a host", SETUP.read_text(encoding="utf-8"))

    def test_pushing_for_a_draft_is_bounded(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Push only the pull request's own feature branch", text)
        self.assertIn("never the default or base branch", text)
        self.assertIn("never force-push", text)

    def test_authorization_is_never_inferred(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Authentication is not authorization", text)
        self.assertIn("reviewer's recommendation", text)
        self.assertIn("--help", text)


class HostAndBudgetTests(unittest.TestCase):
    """Defects from the chair review: unscoped auth checks and unbounded arrays."""

    BODY, COMMENT, REVIEW = 4000, 2000, 1000
    COMMENTS, REVIEWS, PAGE, ASSETS = 20, 10, 30, 20
    BUDGETED_FIELDS = {"body", "comments", "reviews", "latestReviews", "assets", "statusCheckRollup", "jobs"}

    def setUp(self):
        self.commands = documented()

    def test_authentication_checks_are_pinned_to_the_target_host(self):
        checks = [tokens for _, _, path, tokens in self.commands if path == "auth status"]
        self.assertTrue(checks)
        for tokens in checks:
            with self.subTest(command=" ".join(tokens)):
                self.assertIn("--hostname", tokens)
        text = " ".join(SKILL.read_text(encoding="utf-8").split())
        self.assertIn("Never run `gh auth status` without `--hostname`", text)
        self.assertNotIn("for GitHub Enterprise", text, "--hostname is required for every host, not only Enterprise")
        self.assertIn("`github.com` unless the user named", text)

    @staticmethod
    def samples():
        comment = lambda i, n: {"author": {"login": f"user{i}"}, "createdAt": "2026-01-01T00:00:00Z",
                                "body": "c" * n}
        many_comments = [comment(i, 5000) for i in range(50)]
        reviews = [{"author": {"login": f"rev{i}"}, "state": "COMMENTED", "submittedAt": "2026-01-01T00:00:00Z",
                    "body": "r" * 5000} for i in range(40)]
        checks = [{"__typename": "CheckRun", "name": f"check{i}", "status": "COMPLETED",
                   "conclusion": "FAILURE" if i % 2 else "SUCCESS"} for i in range(150)] + \
                 [{"__typename": "StatusContext", "context": f"ctx{i}", "state": "PENDING"} for i in range(50)]
        # gh 2.45 exports a running CheckRun's conclusion as "" (never null).
        checks.insert(0, {"__typename": "CheckRun", "name": "running", "status": "IN_PROGRESS", "conclusion": ""})
        jobs = [{"databaseId": i, "name": f"job{i}", "status": "completed", "conclusion": "failure",
                 "steps": [{"name": f"step{j}", "conclusion": "failure"} for j in range(40)]} for i in range(60)]
        pr = {"number": 1, "title": "t", "body": "b" * 10000, "state": "OPEN", "isDraft": False,
              "statusCheckRollup": checks,
              "baseRefName": "main", "headRefName": "f", "headRefOid": "0" * 40,
              "mergeStateStatus": "CLEAN", "reviewDecision": "REVIEW_REQUIRED",
              "reviews": reviews, "latestReviews": reviews[-5:],
              "reviewRequests": [{"login": "someone"}, {"name": "Team", "slug": "team"}],
              "comments": many_comments}
        return {
            "issue view": {"number": 1, "title": "t", "state": "OPEN", "labels": [{"name": "bug"}] * 3,
                           "body": "i" * 10000, "comments": many_comments},
            "pr view": pr,
            "run view": {"jobs": jobs},
            "release view": {"tagName": "v1", "name": "v1", "body": "n" * 10000, "isDraft": False,
                             "isPrerelease": False, "assets": [{"name": f"a{i}", "size": 1} for i in range(50)]},
            "api": [{"path": "f.py", "line": i, "user": {"login": "u"}, "created_at": "2026-01-01T00:00:00Z",
                     "body": "z" * 5000} for i in range(30)],
        }

    def budgeted(self):
        for source, _, path, tokens in self.commands:
            if path == "api":
                yield source, path, tokens
                continue
            if path in ("issue view", "pr view", "release view", "run view") and "--json" in tokens:
                fields = set(tokens[tokens.index("--json") + 1].split(","))
                if fields & self.BUDGETED_FIELDS:
                    yield source, path, tokens

    def run_jq(self, program, document):
        jq = shutil.which("jq")
        self.assertIsNotNone(jq, "jq is required (Asha's installer and build already depend on it)")
        done = subprocess.run([jq, "-c", program], input=json.dumps(document), capture_output=True,
                              text=True, check=False)
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout)

    def assert_bounded(self, value, where="output"):
        if isinstance(value, str):
            self.assertLessEqual(len(value), self.BODY, where)
        elif isinstance(value, list):
            self.assertLessEqual(len(value), self.PAGE, where)
            for item in value:
                self.assert_bounded(item, where)
        elif isinstance(value, dict):
            for key, item in value.items():
                self.assert_bounded(item, f"{where}.{key}")

    def test_every_body_comment_and_review_read_runs_a_tested_budget(self):
        samples = self.samples()
        reads = list(self.budgeted())
        self.assertGreaterEqual(len(reads), 7)
        for source, path, tokens in reads:
            with self.subTest(command=" ".join(tokens)):
                self.assertIn("--jq", tokens, "comment, review and body reads must apply a --jq budget")
                document = samples[path]
                if path != "api":
                    # gh returns only the requested --json fields.
                    fields = set(tokens[tokens.index("--json") + 1].split(","))
                    document = {key: value for key, value in document.items() if key in fields}
                output = self.run_jq(tokens[tokens.index("--jq") + 1], document)
                self.assert_bounded(output)
                if path == "api":
                    self.assertIn("direction=desc", tokens[2], "line comments read newest first")
                    self.assertRegex(tokens[2], r"per_page=30&page=[0-9]+")
                    self.assertLessEqual(len(output), self.PAGE)
                    self.assertTrue(all(len(item["body"]) <= self.REVIEW for item in output))
                    continue
                if "body" in output:
                    self.assertLessEqual(len(output["body"]), self.BODY)
                if "comments" in output:
                    self.assertEqual(50, output["comments_total"])
                    self.assertEqual(self.COMMENTS, len(output["comments"]))
                    self.assertTrue(all(len(c["body"]) <= self.COMMENT for c in output["comments"]))
                    self.assertEqual("user49", output["comments"][-1]["author"], "keep the most recent")
                if "review_requests" in output:
                    self.assertEqual(["someone", "team"], output["review_requests"])
                if "reviews" in output:
                    self.assertEqual(40, output["reviews_total"])
                    self.assertEqual(self.REVIEWS, len(output["reviews"]))
                    self.assertTrue(all(len(r["body"]) <= self.REVIEW for r in output["reviews"]))
                    self.assertEqual("rev39", output["reviews"][-1]["author"], "keep the most recent")
                if "checks_total" in output:
                    self.assertEqual(201, output["checks_total"])
                    self.assertEqual(30, len(output["not_passing"]))
                    self.assertNotIn("SUCCESS", {c["result"] for c in output["not_passing"]})
                    self.assertEqual({"name": "running", "result": "IN_PROGRESS"}, output["not_passing"][0])
                if "jobs_total" in output:
                    self.assertEqual(60, output["jobs_total"])
                    self.assertEqual(30, len(output["jobs"]))
                    self.assertTrue(all(len(j["failed_steps"]) <= 10 for j in output["jobs"]))
                if "assets" in output:
                    self.assertEqual(50, output["assets_total"])
                    self.assertLessEqual(len(output["assets"]), self.ASSETS)

    def test_truncation_is_visible_and_continuation_is_explained(self):
        text = " ".join(SKILL.read_text(encoding="utf-8").split())
        for phrase in ("comments_total", "reviews_total", "page=2", "at most three pages",
                       "say what was cut"):
            self.assertIn(phrase, text)


class PortabilityTests(unittest.TestCase):
    def test_foundation_is_independent_of_asha_and_mcp(self):
        for path in (SKILL, SETUP):
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            with self.subTest(file=path.name):
                for coupling in ("asha", "issue-loop", "memory/", "control session", "mcp__", ".asha/"):
                    self.assertNotIn(coupling, lowered)
        self.assertIn("needs no MCP server", SKILL.read_text(encoding="utf-8"))

    def test_repository_specific_policy_stays_out(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("this skill never chooses one", text)
        self.assertNotIn("pknull", text)
        self.assertIsNone(re.search(r"--base (main|master)\b", text))

    def test_skill_layout_is_self_contained(self):
        self.assertEqual({"SKILL.md", "references"}, {p.name for p in SKILL_DIR.iterdir()})
        self.assertIn("(references/setup.md)", SKILL.read_text(encoding="utf-8"))


class IssueLoopBoundaryTests(unittest.TestCase):
    def test_issue_loop_uses_the_foundation_only_for_discovery_and_keeps_its_limits(self):
        command = (ROOT / "plugins/code/commands/issue-loop.md").read_text(encoding="utf-8")
        self.assertIn("code-github-cli", command)
        reference = next(line for line in command.splitlines() if "code-github-cli" in line)
        self.assertRegex(reference, r"discovery|setup|authentication")
        self.assertIn("never merges", command)
        self.assertIn("does not widen", command)

    def test_issue_loop_has_no_merge_path(self):
        for relative in ("plugins/code/commands/issue-loop.md", "plugins/code/engines/issue-loop.js",
                         "plugins/code/tools/issue-loop-publish.sh", "plugins/code/tools/issue-loop-preflight.sh"):
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("gh pr merge", text)
                self.assertNotIn("gh pr ready", text)
        publisher = (ROOT / "plugins/code/tools/issue-loop-publish.sh").read_text(encoding="utf-8")
        self.assertIn("gh pr create --draft", publisher)

    def test_broker_registry_records_the_foundation_and_its_conditional_setup(self):
        registry = json.loads((ROOT / "plugins/session/broker/capabilities.json").read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in registry["capabilities"]}
        foundation = entries["github-cli"]
        self.assertEqual([{"id": "github-cli-setup", "relation": "requires",
                           "when": {"type": "command-missing", "command": "gh"},
                           "reason": foundation["dependencies"][0]["reason"]}],
                         foundation["dependencies"])
        self.assertIn("separate-authorization-for-merge-or-publication", foundation["approval"])
        self.assertEqual([], entries["github-cli-setup"]["task_patterns"])
        self.assertIn("references/setup.md", entries["github-cli-setup"]["description"])


if __name__ == "__main__":
    unittest.main()
