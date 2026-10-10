"""Offline safety and portability checks for the DevOps skill collection."""

import importlib.util
import io
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "plugins/devops/skills"


def load_helper(skill, name):
    path = SKILLS / skill / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location("devops_test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def host(**overrides):
    value = {
        "id": "node-a", "owned": True, "kind": "host",
        "expected_hostname": "node-a", "ssh": {"target": "ops@192.0.2.10"},
    }
    value.update(overrides)
    return value


def process_result(payload, **overrides):
    value = {"status": "ok", "returncode": 0,
             "stdout": json.dumps(payload), "stderr": ""}
    value.update(overrides)
    return value


class SkillPortabilityTests(unittest.TestCase):
    # Removing convention names makes either harness discover different skills.
    def test_all_three_skills_have_portable_namespace_names(self):
        for name in ("inspect", "unifi", "change"):
            with self.subTest(skill=name):
                text = (SKILLS / name / "SKILL.md").read_text()
                self.assertRegex(text, rf"(?m)^name: ['\"]?devops-{name}['\"]?$")
                self.assertNotIn("/home/pknull/", text)
                self.assertNotIn("~/.claude/", text)
                self.assertNotIn("~/.codex/", text)

    # A moved/deleted linked reference must not survive the portability check.
    def test_bundled_markdown_references_resolve_from_skill_directory(self):
        for name in ("inspect", "unifi", "change"):
            root = SKILLS / name
            text = (root / "SKILL.md").read_text()
            references = re.findall(r"\]\(((?:references|scripts|assets)/[^)#\s]+)(?:#[^)]*)?\)", text)
            if name != "change":
                self.assertTrue(references, name + " must link its operational resources")
            for reference in references:
                with self.subTest(skill=name, resource=reference):
                    self.assertTrue((root / reference).is_file())


class FleetInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = load_helper("inspect", "fleet")

    # Rejecting hosts without SSH would confuse unavailable coverage with omission.
    def test_inventory_retains_host_without_ssh_for_coverage(self):
        record = host(ssh=None)
        records = self.fleet.validate_inventory({"version": 1, "hosts": [record]})
        self.assertEqual([record], records)

    # Coercion of untrusted ownership/structure into valid inventory is forbidden.
    def test_inventory_rejects_invalid_structure_and_ownership(self):
        bad = [None, [], {}, {"version": 2, "hosts": []},
               {"version": True, "hosts": [host()]}, {"version": 1, "hosts": "node-a"}]
        for ownership in ("true", 1, None):
            bad.append({"version": 1, "hosts": [host(owned=ownership)]})
        for record in ({}, "node-a", host(id="../node"), host(kind="unknown"),
                       host(expected_hostname="node-a; id"), host(ssh="node-a"),
                       host(ssh={"target": "node-a", "identity_file": ""}),
                       host(ssh={"target": "node-a", "identity_file": "relative/key"})):
            bad.append({"version": 1, "hosts": [record]})
        for inventory in bad:
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                self.fleet.validate_inventory(inventory)

    # Dropping either uniqueness or the fleet bound silently broadens contact scope.
    def test_inventory_rejects_duplicate_ids_and_excess_hosts(self):
        for records in ([host(), host()], [host(id=f"node-{i}") for i in range(65)]):
            with self.subTest(count=len(records)), self.assertRaises(ValueError):
                self.fleet.validate_inventory({"version": 1, "hosts": records})

    # Treating an SSH target as an option, command, or URL enables injected behavior.
    def test_ssh_targets_reject_options_shell_syntax_and_control_characters(self):
        for target in ("-oProxyCommand=id", "node;id", "node\nid", "node id",
                       "$(id)", "node`id`", "ssh://node", "user@@node", ""):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.fleet.ssh_command(host(ssh={"target": target}))

    # Removing any hardening option permits configured SSH side effects or trust bypass.
    def test_ssh_uses_strict_bounded_noninteractive_connection_without_forwarding(self):
        argv = self.fleet.ssh_command(host())
        self.assertEqual("ssh", argv[0])
        options = [argv[index + 1].lower() for index, item in enumerate(argv[:-1]) if item == "-o"]
        for option in ("stricthostkeychecking=yes", "updatehostkeys=no", "batchmode=yes",
                       "connecttimeout=8", "connectionattempts=1", "serveraliveinterval=5",
                       "serveralivecountmax=1", "clearallforwardings=yes", "forwardagent=no",
                       "forwardx11=no", "permitlocalcommand=no", "controlmaster=no",
                       "controlpath=none", "sendenv=-*"):
            self.assertIn(option, options)
        self.assertIn("ops@192.0.2.10", argv)
        self.assertNotIn("StrictHostKeyChecking=no", argv)
        self.assertIn("node-a", " ".join(argv))

    # Loading PYTHONSTARTUP/sitecustomize would execute unrelated code before identity checks.
    def test_remote_python_disables_environment_and_site_startup(self):
        argv = self.fleet.ssh_command(host())
        self.assertEqual("python3 -I -S - node-a", argv[-1])

    # A configured interactive command or no-session mode must not replace the probe.
    def test_ssh_config_remote_command_and_no_session_cannot_override_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "ssh_config"
            config.write_text("Host *\n    RemoteCommand exec custom-shell\n    SessionType none\n")
            argv = self.fleet.ssh_command(host())
            result = subprocess.run(
                [argv[0], "-G", "-F", str(config), *argv[1:]],
                capture_output=True, text=True, timeout=3)
        self.assertEqual(0, result.returncode, result.stderr)
        effective = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
        self.assertEqual("none", effective.get("remotecommand", "none"))
        self.assertEqual("default", effective.get("sessiontype"))

    # Contacting aliases/controller records creates invented owned-host coverage.
    def test_nonhost_and_unowned_records_never_start_transport(self):
        records = [host(kind=kind) for kind in ("alias", "template", "controller-record")]
        records.append(host(owned=False))
        for record in records:
            with self.subTest(record=record), mock.patch.object(self.fleet, "run_bounded") as run:
                try:
                    result = self.fleet.probe_host(record)
                except ValueError:
                    pass
                else:
                    self.assertNotEqual("ok", result["status"])
                run.assert_not_called()

    # A missing SSH capability must be reported without guessing a destination.
    def test_host_without_ssh_is_unavailable_not_contacted(self):
        with mock.patch.object(self.fleet, "run_bounded") as run:
            result = self.fleet.probe_host(host(ssh=None))
        self.assertEqual("unavailable", result["status"])
        run.assert_not_called()

    # Attributing checks before comparing the remote hostname invents host evidence.
    def test_hostname_mismatch_discards_any_health_payload(self):
        reply = {"status": "ok", "hostname": "different-machine", "checks": {"memory": "secret-data"}}
        with mock.patch.object(self.fleet, "run_bounded", return_value=process_result(reply)):
            result = self.fleet.probe_host(host())
        self.assertEqual("identity_mismatch", result["status"])
        self.assertNotIn("checks", result)
        self.assertNotIn("secret-data", json.dumps(result))

    # Splitting identity and metrics into separate connections weakens attribution.
    def test_matching_identity_returns_checks_from_one_remote_invocation(self):
        reply = {"status": "ok", "hostname": "node-a", "checks": {"memory": {"status": "ok"}}}
        with mock.patch.object(self.fleet, "run_bounded", return_value=process_result(reply)) as run:
            result = self.fleet.probe_host(host())
        self.assertEqual("ok", result["status"])
        self.assertEqual(reply["checks"], result["checks"])
        self.assertEqual("node-a", result["hostname"])
        run.assert_called_once()
        self.assertIsInstance(run.call_args.kwargs["input_data"], str)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 240)

    # Transport failures must not be converted into a down/healthy host diagnosis.
    def test_transport_failures_are_categorized_without_echoing_stderr(self):
        cases = [("timeout", "", "timeout"), ("output_limit", "", "output_limit"),
                 ("error", "Host key verification failed", "host_key_failure"),
                 ("error", "REMOTE HOST IDENTIFICATION HAS CHANGED", "host_key_failure"),
                 ("error", "Permission denied (publickey)", "authentication_failed"),
                 ("error", "connection refused", "ssh_failed")]
        for status, stderr, expected in cases:
            with self.subTest(status=status, stderr=stderr):
                reply = process_result({}, status=status, returncode=255, stderr=stderr + " secret-diagnostic")
                with mock.patch.object(self.fleet, "run_bounded", return_value=reply):
                    result = self.fleet.probe_host(host())
                self.assertEqual(expected, result["status"])
                self.assertNotIn("secret-diagnostic", json.dumps(result))
                self.assertNotIn("checks", result)

    # Treating malformed output as valid gives false evidence from shell banners/errors.
    def test_malformed_remote_protocol_is_unverified(self):
        for payload in ([], None, {"hostname": "node-a", "status": "ok", "checks": []},
                        {"hostname": "node-a", "status": "error", "checks": {}}):
            with self.subTest(payload=payload), mock.patch.object(
                    self.fleet, "run_bounded", return_value=process_result(payload)):
                self.assertEqual("invalid_response", self.fleet.probe_host(host())["status"])

    # Moving identity verification below collection executes reads on the wrong host.
    def test_remote_identity_gate_precedes_every_health_command(self):
        with mock.patch("socket.gethostname", return_value="unexpected"), mock.patch.object(
                self.fleet, "run_bounded", side_effect=AssertionError("health command before hostname verification")) as run:
            result = self.fleet.remote_baseline("node-a")
        self.assertEqual("identity_mismatch", result["status"])
        self.assertFalse(result["checks"])
        run.assert_not_called()


class FleetProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = load_helper("inspect", "fleet")

    # Returning before draining both pipes loses diagnostic coverage or deadlocks.
    def test_bounded_process_collects_stdout_stderr_and_exit_status(self):
        result = self.fleet.run_bounded(
            [sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr)"],
            timeout=2, limit=1024)
        self.assertEqual("ok", result["status"])
        self.assertEqual(0, result["returncode"])
        self.assertEqual("out\n", result["stdout"])
        self.assertEqual("err\n", result["stderr"])

    # A nonzero process exit must never be reported as a successful health check.
    def test_nonzero_exit_is_error(self):
        result = self.fleet.run_bounded([sys.executable, "-c", "raise SystemExit(7)"], timeout=2, limit=1024)
        self.assertEqual("error", result["status"])
        self.assertEqual(7, result["returncode"])

    # Omitting wall-clock termination allows hung remote connections indefinitely.
    def test_process_timeout_terminates_promptly(self):
        start = time.monotonic()
        result = self.fleet.run_bounded([sys.executable, "-c", "import time; time.sleep(10)"],
                                       timeout=0.1, limit=1024)
        self.assertEqual("timeout", result["status"])
        self.assertLess(time.monotonic() - start, 3)

    # Applying the cap to stdout alone permits unbounded stderr growth.
    def test_combined_output_cap_stops_noisy_process(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                result = self.fleet.run_bounded(
                    [sys.executable, "-c", f"import sys; sys.{stream}.write('x'*1000000)"],
                    timeout=2, limit=512)
                self.assertEqual("output_limit", result["status"])
                self.assertLessEqual(len(result["stdout"].encode()) + len(result["stderr"].encode()), 512)

    # Failure to pipe the probe source makes remote Python wait or run no checks.
    def test_stdin_payload_reaches_process_without_shell(self):
        result = self.fleet.run_bounded(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            input_data="safe input", timeout=2, limit=1024)
        self.assertEqual("ok", result["status"])
        self.assertEqual("safe input\n", result["stdout"])


class UniFiTests(unittest.TestCase):
    BASE = "https://192.0.2.1/proxy/network/api/s/default"
    TOKEN = "fixture-not-a-real-credential"

    @classmethod
    def setUpClass(cls):
        cls.unifi = load_helper("unifi", "unifi_read")

    def response(self, payload):
        result = io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        result.status = 200
        return result

    def fetch(self, payload, resource="clients"):
        opener = mock.Mock()
        opener.open.return_value = self.response(payload)
        with mock.patch("urllib.request.build_opener", return_value=opener) as build:
            result = self.unifi.fetch(resource, self.BASE, self.TOKEN)
        return result, opener, build

    # Incorrect resource paths can turn an inspection into an unrelated API request.
    def test_allowlisted_resources_use_get_and_api_key_header(self):
        for resource, suffix in (("clients", "stat/sta"), ("devices", "stat/device"),
                                 ("health", "stat/health"), ("remembered", "rest/user")):
            with self.subTest(resource=resource):
                result, opener, _ = self.fetch({"meta": {"rc": "ok"}, "data": []}, resource)
                self.assertEqual("ok", result["status"])
                request = opener.open.call_args.args[0]
                self.assertEqual(self.BASE + "/" + suffix, request.full_url)
                self.assertEqual("GET", request.get_method())
                self.assertIsNone(request.data)
                self.assertEqual(self.TOKEN, dict((k.lower(), v) for k, v in request.header_items())["x-api-key"])
                self.assertNotIn(self.TOKEN, request.full_url)
                self.assertLessEqual(opener.open.call_args.kwargs["timeout"], 30)

    # Configured proxy or redirect defaults may leak the token to another destination.
    def test_transport_disables_proxy_and_redirects_and_verifies_tls(self):
        _, _, build = self.fetch({"meta": {"rc": "ok"}, "data": []})
        handlers = build.call_args.args
        proxies = [handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler)]
        self.assertEqual([{}], [handler.proxies for handler in proxies])
        redirects = [handler for handler in handlers if isinstance(handler, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(1, len(redirects))
        request = urllib.request.Request(self.BASE)
        try:
            redirected = redirects[0].redirect_request(request, None, 302, "redirect", {}, "https://example.invalid/")
        except urllib.error.HTTPError:
            redirected = None
        self.assertIsNone(redirected)
        https = [handler for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler)]
        self.assertEqual(1, len(https))
        self.assertTrue(https[0]._context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, https[0]._context.verify_mode)

    # Ignoring the supplied CA prevents normal verification of private certificates.
    def test_private_ca_is_loaded_into_normal_verified_context(self):
        context = ssl.create_default_context()
        opener = mock.Mock()
        opener.open.return_value = self.response({"meta": {"rc": "ok"}, "data": []})
        with mock.patch("ssl.create_default_context", return_value=context) as create, mock.patch(
                "urllib.request.build_opener", return_value=opener):
            result = self.unifi.fetch("clients", self.BASE, self.TOKEN, ca_file="/tmp/fixture-ca.pem")
        self.assertEqual("ok", result["status"])
        create.assert_called_once_with(cafile="/tmp/fixture-ca.pem")
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)

    # Unvalidated base URLs allow plaintext, credential injection, or path escape.
    def test_invalid_configuration_is_rejected_before_transport(self):
        cases = [("../cmd", self.BASE, self.TOKEN)]
        for base in ("http://192.0.2.1/proxy/network/api/s/default",
                     "https://user:secret@192.0.2.1/proxy/network/api/s/default",
                     self.BASE + "?token=x", self.BASE + "#fragment", self.BASE + "/../../cmd",
                     "https://192.0.2.1/other", "https://192.0.2.1/proxy/network/api/s/%2e%2e"):
            cases.append(("clients", base, self.TOKEN))
        cases.extend(("clients", self.BASE, token) for token in ("", "key\nX-Injected: yes", None))
        for resource, base, token in cases:
            with self.subTest(resource=resource, base=base), mock.patch("urllib.request.build_opener") as build:
                try:
                    result = self.unifi.fetch(resource, base, token)
                except ValueError:
                    pass
                else:
                    self.assertNotEqual("ok", result["status"])
                build.assert_not_called()

    # Raw or recursively copied API fields expose credentials inside nested objects.
    def test_allowlist_filters_unknown_and_nested_secret_fields(self):
        fixture = {"meta": {"rc": "ok"}, "data": [{
            "name": "sample-device", "model": "EXAMPLE", "password": "never-print-this",
            "api_key": self.TOKEN, "config": {"token": self.TOKEN},
            "sys_stats": {"loadavg_1": "0.2", "mem_total": 4096, "password": self.TOKEN},
        }]}
        result, _, _ = self.fetch(fixture, "devices")
        self.assertEqual("ok", result["status"])
        self.assertEqual("sample-device", result["data"][0]["name"])
        rendered = json.dumps(result)
        self.assertNotIn(self.TOKEN, rendered)
        self.assertNotIn("never-print-this", rendered)
        self.assertNotIn("password", rendered)
        self.assertNotIn("config", result["data"][0])

    # An allowed field containing the auth value still must not expose credentials.
    def test_reflected_token_in_allowed_field_is_not_returned(self):
        result, _, _ = self.fetch({"meta": {"rc": "ok"}, "data": [{"name": self.TOKEN}]})
        self.assertNotEqual("ok", result["status"])
        self.assertNotIn(self.TOKEN, json.dumps(result))

    # JSON escaping must not hide a reflected credential from the redaction gate.
    def test_escaped_reflected_credentials_reject_the_entire_response(self):
        for token in ('fixture-"quoted"-secret', r'fixture-\backslash-secret'):
            for record in ({"name": "prefix-" + token + "-suffix"},
                           {"sys_stats": {"loadavg_1": "prefix-" + token + "-suffix"}}):
                with self.subTest(token=token, record=record):
                    opener = mock.Mock()
                    opener.open.return_value = self.response({"meta": {"rc": "ok"}, "data": [record]})
                    with mock.patch("urllib.request.build_opener", return_value=opener):
                        result = self.unifi.fetch("devices", self.BASE, token)
                    self.assertEqual("error", result["status"])
                    self.assertNotIn("data", result)
                    pending = [result]
                    while pending:
                        value = pending.pop()
                        if isinstance(value, dict):
                            pending.extend(value.keys())
                            pending.extend(value.values())
                        elif isinstance(value, list):
                            pending.extend(value)
                        elif isinstance(value, str):
                            self.assertNotIn(token, value)

    # DEL in a URL must be rejected before HTTP construction or network transport.
    def test_del_in_url_is_rejected_before_transport(self):
        with mock.patch("urllib.request.build_opener", side_effect=AssertionError("DEL URL reached transport construction")) as build:
            try:
                result = self.unifi.fetch("clients", self.BASE.replace("192.0.2.1", "192.0.2.1\x7f"), self.TOKEN)
            except ValueError:
                pass
            else:
                self.assertEqual("error", result["status"])
            build.assert_not_called()

    # Native HTTP parsing errors must not escape as tracebacks containing diagnostics.
    def test_http_protocol_exception_is_safe_structured_failure(self):
        opener = mock.Mock()
        opener.open.side_effect = http.client.HTTPException(self.TOKEN)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            result = self.unifi.fetch("clients", self.BASE, self.TOKEN)
        self.assertEqual("error", result["status"])
        self.assertNotIn(self.TOKEN, repr(result))

    # Ignoring meta.rc or accepting non-list data presents API failure as healthy inventory.
    def test_api_errors_and_malformed_payloads_are_not_success(self):
        for payload in ({"meta": {"rc": "error", "msg": self.TOKEN}, "data": []},
                        {"data": []}, {"meta": {"rc": "ok"}, "data": {}},
                        {"meta": {"rc": "ok"}, "data": [self.TOKEN]},
                        b"not-json-secret-body", b"x" * (2 * 1024 * 1024 + 1)):
            with self.subTest(payload_type=type(payload).__name__):
                result, _, _ = self.fetch(payload)
                self.assertNotEqual("ok", result["status"])
                self.assertNotIn(self.TOKEN, json.dumps(result))
                self.assertNotIn("not-json-secret-body", json.dumps(result))

    # Echoing exception text or HTTP bodies discloses upstream secret-bearing diagnostics.
    def test_http_failures_do_not_echo_body_or_secret(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            self.BASE, 401, self.TOKEN, {}, io.BytesIO(self.TOKEN.encode()))
        with mock.patch("urllib.request.build_opener", return_value=opener):
            result = self.unifi.fetch("clients", self.BASE, self.TOKEN)
        self.assertNotEqual("ok", result["status"])
        self.assertNotIn(self.TOKEN, json.dumps(result))

    # A missing credential must stop locally rather than contact an anonymous API.
    def test_cli_missing_credentials_fails_with_safe_structured_diagnostic(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("UNIFI_")}
        env["UNIFI_BASE_URL"] = self.BASE
        result = subprocess.run(
            [sys.executable, str(SKILLS / "unifi/scripts/unifi_read.py"), "clients"],
            env=env, capture_output=True, text=True, timeout=3)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("error", json.loads(result.stdout)["status"])
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
