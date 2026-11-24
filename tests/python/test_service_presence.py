"""Execute the service skills' local diagnostics with synthetic environments."""

from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SERVICES = {
    "admin/bookstack": ("BOOKSTACK_BASE_URL", "BOOKSTACK_API_TOKEN"),
    "admin/gemini": ("GEMINI_API_KEY",),
    "admin/homeassistant": ("HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"),
    "admin/todoist": ("TODOIST_API_TOKEN",),
    "admin/wolfram": ("WOLFRAM_APP_ID",),
    "devops/unifi": ("UNIFI_BASE_URL", "UNIFI_API_KEY"),
}
SENTINEL = "synthetic-secret-KEEP-PRIVATE\n$(printf injected)`printf injected`"


class ServicePresenceTests(unittest.TestCase):
    def snippet(self, service):
        plugin, skill = service.split("/")
        text = (ROOT / "plugins" / plugin / "skills" / skill / "SKILL.md").read_text()
        heading = "## Access" if skill == "unifi" else "## Setup"
        section = text.split(heading + "\n", 1)[1].split("\n## ", 1)[0]
        snippets = re.findall(r"^```sh\n(.*?)^```$", section, re.M | re.S)
        self.assertEqual(1, len(snippets), f"{service}: one local presence diagnostic")
        return snippets[0]

    def check(self, service, supplied, trace=False):
        snippet = self.snippet(service)
        names = SERVICES[service]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".asha").mkdir()
            # Sourcing even a synthetic store must fail the diagnostic contract.
            marker = home / "credential-store-read"
            (home / ".asha/secrets.env").write_text(
                f"printf touched > {shlex.quote(str(marker))}\n"
            )
            env = {"HOME": directory, "PATH": directory, "UNRELATED_SECRET": SENTINEL}
            env.update(supplied)
            result = subprocess.run(
                ["/bin/sh", "-eux" if trace else "-eu", "-s"],
                input=snippet, env=env, cwd=directory,
                text=True, capture_output=True, timeout=3,
            )
            self.assertFalse(marker.exists(), "diagnostic sourced a credential store")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        self.assertNotIn("synthetic-secret-KEEP-PRIVATE", result.stdout + result.stderr)
        self.assertNotIn("injected", result.stdout + result.stderr)
        self.assertEqual(
            "".join(f"{name}: {'present' if supplied.get(name) else 'missing'}\n"
                    for name in names), result.stdout,
        )
        if not trace:
            self.assertEqual("", result.stderr)

    def test_absent_variables_are_missing(self):
        for service in SERVICES:
            with self.subTest(service=service):
                self.check(service, {})

    def test_empty_variables_are_missing(self):
        for service, names in SERVICES.items():
            with self.subTest(service=service):
                self.check(service, dict.fromkeys(names, ""))

    def test_present_variables_never_expose_or_execute_values(self):
        for service, names in SERVICES.items():
            with self.subTest(service=service):
                self.check(service, dict.fromkeys(names, SENTINEL))

    def test_each_required_variable_is_checked_independently(self):
        for service, names in SERVICES.items():
            for present in names:
                with self.subTest(service=service, present=present):
                    self.check(service, {present: SENTINEL})

    def test_shell_tracing_does_not_expose_values(self):
        for service, names in SERVICES.items():
            with self.subTest(service=service):
                self.check(service, dict.fromkeys(names, SENTINEL), trace=True)


if __name__ == "__main__":
    unittest.main()
