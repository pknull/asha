"""Acceptance tests for the image-fal helper (plugins/image/skills/fal)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).parents[2]
    / "plugins"
    / "image"
    / "skills"
    / "fal"
    / "scripts"
    / "fal_api.py"
)
SPEC = importlib.util.spec_from_file_location("fal_api", SCRIPT)
assert SPEC and SPEC.loader
fal_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fal_api)

TEST_KEY = "test-key-id:test-key-secret-0123456789"
ENDPOINT = "fal-ai/example-model"
REQUEST_ID = "6b6a1a9e-0000-4000-8000-000000000001"
PRICE = {
    "prices": [
        {
            "endpoint_id": ENDPOINT,
            "unit_price": 0.04,
            "unit": "image",
            "currency": "USD",
        }
    ],
    "next_cursor": None,
    "has_more": False,
}


def submit_reply(**overrides):
    reply = {
        "request_id": REQUEST_ID,
        "status_url": f"https://queue.fal.run/{ENDPOINT}/requests/{REQUEST_ID}/status",
        "response_url": f"https://queue.fal.run/{ENDPOINT}/requests/{REQUEST_ID}",
        "cancel_url": f"https://queue.fal.run/{ENDPOINT}/requests/{REQUEST_ID}/cancel",
        "queue_position": 0,
    }
    reply.update(overrides)
    return reply


class FakeTransport:
    """Records every outgoing request; answers by (method, URL prefix)."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []
        self.downloads = []

    @staticmethod
    def _record(request):
        headers = {k.lower(): v for k, v in request.header_items()}
        return {
            "method": request.get_method(),
            "url": request.full_url,
            "headers": headers,
            "redirectable_headers": {k.lower() for k in request.headers},
            "body": request.data,
        }

    def send(self, request, *, timeout, limit, opener=None):
        record = self._record(request)
        record["opener"] = opener
        self.requests.append(record)
        for method, prefix, answer in self.routes:
            if record["method"] == method and record["url"].startswith(prefix):
                if isinstance(answer, BaseException):
                    raise answer
                if callable(answer):
                    answer = answer(record)
                if isinstance(answer, (bytes, bytearray)):
                    return bytes(answer)
                return json.dumps(answer).encode()
        raise AssertionError(f"unexpected request {record['method']} {record['url']}")

    def stream(self, request, handle, *, timeout, limit):
        record = self._record(request)
        self.downloads.append(record)
        handle.write(b"generated-bytes")
        return len(b"generated-bytes")

    def urls(self):
        return [r["url"] for r in self.requests]


def http_error(url, code, body=b"{}"):
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(body))


class HelperCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "Work" / "fal" / "generations.jsonl"
        self.out = self.root / "Work" / "fal" / "outputs"
        env = mock.patch.dict(os.environ, {"FAL_KEY": TEST_KEY})
        env.start()
        self.addCleanup(env.stop)
        sleep = mock.patch.object(fal_api, "_sleep", lambda _s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, transport):
        for name, value in (("_send", transport.send), ("_stream_to", transport.stream)):
            patcher = mock.patch.object(fal_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def happy_routes(self, *, submit=None, result=None, extra=()):
        return [
            *extra,
            ("GET", "https://api.fal.ai/v1/models/pricing", PRICE),
            ("POST", f"https://queue.fal.run/{ENDPOINT}", submit or submit_reply()),
            ("GET", f"https://queue.fal.run/{ENDPOINT}/requests/{REQUEST_ID}/status",
             {"status": "COMPLETED", "request_id": REQUEST_ID}),
            ("GET", f"https://queue.fal.run/{ENDPOINT}/requests/{REQUEST_ID}",
             result or {"images": [{"url": "https://v3.fal.media/files/a/out.png",
                                    "content_type": "image/png"}], "seed": 7}),
        ]

    def main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = fal_api.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def run_cli(self, *extra, input_json='{"prompt": "a drone"}', approved="0.10"):
        return self.main(
            "run", ENDPOINT,
            "--input", input_json,
            "--approved-cost", approved,
            "--log", str(self.log),
            "--out", str(self.out),
            "--name", "drone",
            *extra,
        )

    def submitted_body(self, transport):
        posts = [r for r in transport.requests
                 if r["method"] == "POST" and r["url"] == f"https://queue.fal.run/{ENDPOINT}"]
        self.assertEqual(len(posts), 1, transport.urls())
        return json.loads(posts[0]["body"])

    def log_records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class HostAllowlistTests(HelperCase):
    def test_documented_fal_api_hosts_carry_the_key(self):
        for url in (
            f"https://queue.fal.run/{ENDPOINT}",
            "https://api.fal.ai/v1/models/pricing?endpoint_id=x",
            "https://rest.fal.ai/storage/upload/initiate?storage_type=gcs",
            "https://QUEUE.FAL.RUN/fal-ai/x/requests/1/status",
            "https://queue.fal.run:443/fal-ai/x",
        ):
            with self.subTest(url=url):
                self.assertTrue(fal_api.key_host_allowed(url))

    def test_lookalike_foreign_and_downgraded_hosts_never_carry_the_key(self):
        for url in (
            "http://queue.fal.run/fal-ai/x",
            "https://queue.fal.run.evil.example/fal-ai/x",
            "https://evilqueue.fal.run/fal-ai/x",
            "https://fal.run.evil.example/",
            "https://evil.example/queue.fal.run",
            "https://user@queue.fal.run/fal-ai/x",
            "https://queue.fal.run@evil.example/fal-ai/x",
            "https://evil.example\\@queue.fal.run/fal-ai/x",
            "https://queue.fal.run:8443/fal-ai/x",
            "https://queue.fal.run./fal-ai/x",
            "https://v3.fal.media/files/a/out.png",
            "https://storage.googleapis.com/bucket/x",
            "//queue.fal.run/fal-ai/x",
            "queue.fal.run/fal-ai/x",
            "",
            None,
        ):
            with self.subTest(url=url):
                self.assertFalse(fal_api.key_host_allowed(url))

    def test_authenticated_request_to_foreign_host_never_reaches_the_network(self):
        transport = FakeTransport([])
        self.install(transport)
        with self.assertRaises(fal_api.FalError) as caught:
            fal_api.api_request("GET", "https://queue.fal.run.evil.example/status")
        self.assertEqual(transport.requests, [])
        self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_key_is_unredirected_and_redirects_are_refused(self):
        transport = FakeTransport([("GET", "https://api.fal.ai/", PRICE)])
        self.install(transport)
        fal_api.api_request("GET", "https://api.fal.ai/v1/models/pricing?endpoint_id=x")
        (record,) = transport.requests
        self.assertEqual(record["headers"]["authorization"], f"Key {TEST_KEY}")
        self.assertNotIn("authorization", record["redirectable_headers"])
        self.assertIs(record["opener"], fal_api.NO_REDIRECT_OPENER)
        handler = fal_api._RefuseRedirect()
        request = urllib.request.Request("https://api.fal.ai/v1/x")
        self.assertIsNone(
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/")
        )

    def test_server_supplied_status_url_on_foreign_host_is_refused(self):
        evil = "https://queue.fal.run.evil.example/steal"
        transport = FakeTransport(self.happy_routes(
            submit=submit_reply(status_url=evil + "/status", response_url=evil),
        ))
        self.install(transport)
        code, _out, err = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertFalse(any("evil.example" in url for url in transport.urls()))
        self.assertIn("refusing", err)
        submitted = [r for r in self.log_records() if r["event"] == "submitted"]
        self.assertEqual([r["request_id"] for r in submitted], [REQUEST_ID])

    def test_logged_response_url_on_foreign_host_is_refused_by_result(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text(json.dumps({
            "event": "submitted", "endpoint": ENDPOINT, "request_id": REQUEST_ID,
            "response_url": "https://evil.example/result",
        }) + "\n")
        transport = FakeTransport([])
        self.install(transport)
        code, _out, err = self.main("result", REQUEST_ID, "--log", str(self.log),
                                    "--out", str(self.out))
        self.assertNotEqual(code, 0)
        self.assertEqual(transport.requests, [])
        self.assertIn("refusing", err)

    def test_presigned_upload_and_output_downloads_never_carry_the_key(self):
        source = self.root / "concept.png"
        source.write_bytes(b"png")
        transport = FakeTransport(self.happy_routes(extra=[
            ("POST", "https://rest.fal.ai/storage/upload/initiate",
             {"upload_url": "https://storage.googleapis.com/bucket/signed?sig=1",
              "file_url": "https://v3.fal.media/files/b/concept.png"}),
            ("PUT", "https://storage.googleapis.com/bucket/signed", b""),
        ]))
        self.install(transport)
        code, _out, err = self.run_cli("--file", f"image_url={source}")
        self.assertEqual(code, 0, err)
        put = [r for r in transport.requests if r["method"] == "PUT"]
        self.assertEqual(len(put), 1)
        self.assertNotIn("authorization", put[0]["headers"])
        self.assertEqual(len(transport.downloads), 1)
        for record in transport.downloads:
            self.assertNotIn("authorization", record["headers"])
        for record in transport.requests:
            if "authorization" in record["headers"]:
                self.assertTrue(fal_api.key_host_allowed(record["url"]), record["url"])

    def test_http_error_text_is_redacted(self):
        transport = FakeTransport([
            ("GET", "https://api.fal.ai/",
             http_error("https://api.fal.ai/v1/x", 401, f"bad key {TEST_KEY}".encode())),
        ])
        self.install(transport)
        with self.assertRaises(fal_api.FalError) as caught:
            fal_api.api_request("GET", "https://api.fal.ai/v1/models/pricing?endpoint_id=x")
        self.assertNotIn(TEST_KEY, str(caught.exception))


class SchemaTests(HelperCase):
    def test_schema_reads_the_public_openapi_without_the_key(self):
        # Shape of fal's live queue OpenAPI document (fal-ai/trellis-2, 2026-10-05).
        document = {
            "openapi": "3.0.4",
            "paths": {
                f"/{ENDPOINT}": {"post": {"requestBody": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/ExampleInput"}}}}}},
                f"/{ENDPOINT}/requests/{{request_id}}": {"get": {"responses": {"200": {
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/ExampleOutput"}}}}}}},
            },
            "components": {"schemas": {
                "ExampleInput": {"required": ["image_url"], "properties": {
                    "image_url": {"type": "string", "description": "Input image"},
                    "texture_size": {"type": "integer", "default": 2048, "enum": [1024, 2048]},
                }},
                "ExampleOutput": {"properties": {"model_glb": {"$ref": "#/components/schemas/File"}}},
            }},
        }
        transport = FakeTransport([
            ("GET", "https://fal.ai/api/openapi/queue/openapi.json", document),
        ])
        self.install(transport)
        code, out, err = self.main("schema", ENDPOINT)
        self.assertEqual(code, 0, err)
        (record,) = transport.requests
        self.assertEqual(
            record["url"],
            f"https://fal.ai/api/openapi/queue/openapi.json?endpoint_id={ENDPOINT}")
        self.assertNotIn("authorization", record["headers"])
        reply = json.loads(out)
        self.assertEqual(reply["required"], ["image_url"])
        self.assertEqual(reply["input"]["texture_size"]["default"], 2048)
        self.assertEqual(reply["output"]["model_glb"]["type"], "File")


class ExplicitUploadTests(HelperCase):
    def test_existing_local_path_in_input_is_sent_verbatim_and_not_uploaded(self):
        private = self.root / "private.png"
        private.write_bytes(b"secret pixels")
        transport = FakeTransport(self.happy_routes())
        self.install(transport)
        code, _out, err = self.run_cli(
            input_json=json.dumps({"prompt": "x", "image_url": str(private),
                                   "image_urls": [str(private), "@" + str(private)]}),
        )
        self.assertEqual(code, 0, err)
        self.assertFalse(any("rest.fal.ai" in url for url in transport.urls()))
        self.assertFalse(any(r["method"] == "PUT" for r in transport.requests))
        body = self.submitted_body(transport)
        self.assertEqual(body["image_url"], str(private))
        self.assertEqual(body["image_urls"], [str(private), "@" + str(private)])

    def test_named_file_is_uploaded_into_its_field(self):
        first = self.root / "a.png"
        second = self.root / "b.png"
        for path in (first, second):
            path.write_bytes(b"png")
        uploaded = []

        def initiate(record):
            name = json.loads(record["body"])["file_name"]
            uploaded.append(name)
            return {"upload_url": f"https://storage.googleapis.com/b/{name}?sig=1",
                    "file_url": f"https://v3.fal.media/files/x/{name}"}

        transport = FakeTransport(self.happy_routes(extra=[
            ("POST", "https://rest.fal.ai/storage/upload/initiate", initiate),
            ("PUT", "https://storage.googleapis.com/b/", b""),
        ]))
        self.install(transport)
        code, _out, err = self.run_cli(
            "--file", f"image_url={first}",
            "--file", f"image_urls={first}",
            "--file", f"image_urls={second}",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(uploaded, ["a.png", "a.png", "b.png"])
        body = self.submitted_body(transport)
        self.assertEqual(body["image_url"], "https://v3.fal.media/files/x/a.png")
        self.assertEqual(body["image_urls"], ["https://v3.fal.media/files/x/a.png",
                                              "https://v3.fal.media/files/x/b.png"])

    def test_missing_named_file_fails_before_any_request(self):
        transport = FakeTransport(self.happy_routes())
        self.install(transport)
        code, _out, err = self.run_cli("--file", f"image_url={self.root / 'absent.png'}")
        self.assertNotEqual(code, 0)
        self.assertIn("absent.png", err)
        self.assertEqual(transport.requests, [])

    def test_named_file_cannot_override_a_field_from_the_input(self):
        source = self.root / "a.png"
        source.write_bytes(b"png")
        transport = FakeTransport(self.happy_routes())
        self.install(transport)
        code, _out, _err = self.run_cli(
            "--file", f"image_url={source}",
            input_json=json.dumps({"image_url": "https://example.com/x.png"}),
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(transport.requests, [])


class CostGateTests(HelperCase):
    def test_estimate_above_approval_refuses_before_upload_or_submit(self):
        source = self.root / "a.png"
        source.write_bytes(b"png")
        transport = FakeTransport(self.happy_routes())
        self.install(transport)
        code, _out, err = self.run_cli("--file", f"image_url={source}", "--units", "4",
                                       approved="0.10")
        self.assertNotEqual(code, 0)
        self.assertIn("0.16", err)
        self.assertEqual(transport.urls(),
                         [f"https://api.fal.ai/v1/models/pricing?endpoint_id={ENDPOINT}"])
        self.assertFalse(self.log.exists())

    def test_unlisted_or_zero_price_refuses(self):
        for prices in ([], [{"endpoint_id": ENDPOINT, "unit_price": 0, "unit": "image",
                             "currency": "USD"}]):
            with self.subTest(prices=prices):
                transport = FakeTransport([
                    ("GET", "https://api.fal.ai/v1/models/pricing",
                     {"prices": prices, "next_cursor": None, "has_more": False}),
                ])
                self.install(transport)
                code, _out, err = self.run_cli()
                self.assertNotEqual(code, 0)
                self.assertIn("price", err.lower())
                self.assertEqual(len(transport.requests), 1)

    def test_paid_submission_is_never_retried(self):
        transport = FakeTransport([
            ("GET", "https://api.fal.ai/v1/models/pricing", PRICE),
            ("POST", f"https://queue.fal.run/{ENDPOINT}",
             http_error(f"https://queue.fal.run/{ENDPOINT}", 503)),
        ])
        self.install(transport)
        code, _out, _err = self.run_cli()
        self.assertNotEqual(code, 0)
        posts = [r for r in transport.requests if r["method"] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_calls_estimate_uses_fal_history_and_gates_the_submission(self):
        def estimate(record):
            self.assertEqual(json.loads(record["body"]), {
                "estimate_type": "historical_api_price",
                "endpoints": {ENDPOINT: {"call_quantity": 3}},
            })
            return {"estimate_type": "historical_api_price", "total_cost": 0.45,
                    "currency": "USD"}

        transport = FakeTransport(self.happy_routes(extra=[
            ("POST", "https://api.fal.ai/v1/models/pricing/estimate", estimate),
        ]))
        self.install(transport)
        code, _out, err = self.run_cli("--calls", "3", approved="0.40")
        self.assertNotEqual(code, 0)
        self.assertIn("0.45", err)
        self.assertFalse(any(r["url"].startswith("https://queue.fal.run")
                             for r in transport.requests))

        code, _out, err = self.run_cli("--calls", "3", approved="0.50")
        self.assertEqual(code, 0, err)
        (submitted,) = [r for r in self.log_records() if r["event"] == "submitted"]
        self.assertEqual(submitted["estimate_basis"], "historical_api_price")
        self.assertEqual(submitted["calls"], 3)
        self.assertAlmostEqual(submitted["estimated_cost"], 0.45)

    def test_zero_historical_estimate_means_no_history_and_refuses(self):
        # Live: bytedance/seedance-2.5/image-to-video answered total_cost 0.0
        # on 2026-10-05, which would pass any approval ceiling.
        transport = FakeTransport(self.happy_routes(extra=[
            ("POST", "https://api.fal.ai/v1/models/pricing/estimate",
             {"estimate_type": "historical_api_price", "total_cost": 0.0, "currency": "USD"}),
        ]))
        self.install(transport)
        code, _out, err = self.run_cli("--calls", "1", approved="5")
        self.assertNotEqual(code, 0)
        self.assertIn("history", err)
        self.assertFalse(any(r["url"].startswith("https://queue.fal.run")
                             for r in transport.requests))

    def test_units_and_calls_are_exclusive(self):
        transport = FakeTransport(self.happy_routes())
        self.install(transport)
        code, _out, _err = self.run_cli("--calls", "1", "--units", "2")
        self.assertNotEqual(code, 0)
        self.assertEqual(transport.requests, [])

    def test_endpoint_id_cannot_escape_the_queue_path(self):
        transport = FakeTransport([])
        self.install(transport)
        for endpoint in ("../admin", "fal-ai/../x", "fal-ai/x?y=1", "fal-ai", "fal-ai/x#y"):
            with self.subTest(endpoint=endpoint):
                code, _out, _err = self.main(
                    "run", endpoint, "--input", "{}", "--approved-cost", "1",
                    "--log", str(self.log), "--out", str(self.out))
                self.assertNotEqual(code, 0)
        self.assertEqual(transport.requests, [])


class GenerationLogTests(HelperCase):
    def test_run_logs_model_request_price_and_outputs_and_spend_totals_them(self):
        transport = FakeTransport(self.happy_routes())
        self.install(transport)
        code, out, err = self.run_cli("--units", "2")
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertEqual(summary["request_id"], REQUEST_ID)
        records = self.log_records()
        self.assertEqual([r["event"] for r in records], ["submitted", "completed"])
        submitted, completed = records
        self.assertEqual(submitted["endpoint"], ENDPOINT)
        self.assertEqual(submitted["request_id"], REQUEST_ID)
        self.assertEqual(submitted["unit_price"], 0.04)
        self.assertEqual(submitted["currency"], "USD")
        self.assertAlmostEqual(submitted["estimated_cost"], 0.08)
        self.assertEqual(submitted["approved_cost"], 0.10)
        self.assertEqual(completed["request_id"], REQUEST_ID)
        self.assertEqual(len(completed["files"]), 1)
        written = Path(completed["files"][0])
        self.assertTrue(written.is_file())
        self.assertEqual(written.parent.resolve(), self.out.resolve())
        self.assertNotIn(TEST_KEY, self.log.read_text())

        code, out, err = self.main("spend", "--log", str(self.log))
        self.assertEqual(code, 0, err)
        spend = json.loads(out)
        self.assertEqual(spend["submissions"], 1)
        self.assertAlmostEqual(spend["estimated_total"]["USD"], 0.08)

    def test_output_names_stay_inside_the_output_directory(self):
        transport = FakeTransport(self.happy_routes(result={
            "../../escape": {"url": "https://v3.fal.media/files/a/..%2F..%2Fx.png"},
            "images": [{"url": "https://v3.fal.media/files/a/out.sh/../../evil"},
                       {"url": "https://v3.fal.media/files/a/two.webp"}],
            "local": {"url": "http://v3.fal.media/files/a/plain.png"},
        }))
        self.install(transport)
        code, out, err = self.main(
            "run", ENDPOINT, "--input", "{}", "--approved-cost", "0.10",
            "--log", str(self.log), "--out", str(self.out), "--name", "../../up")
        self.assertEqual(code, 0, err)
        files = json.loads(out)["files"]
        self.assertEqual(len(files), 3)
        for name in files:
            self.assertEqual(Path(name).resolve().parent, self.out.resolve())
        self.assertFalse(any(r["url"].startswith("http:") for r in transport.downloads))

    def test_key_must_come_from_the_environment(self):
        transport = FakeTransport(self.happy_routes())
        self.install(transport)
        env_file = self.root / ".env"
        env_file.write_text(f"FAL_KEY={TEST_KEY}\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            cwd = os.getcwd()
            os.chdir(self.root)
            try:
                code, _out, err = self.run_cli()
            finally:
                os.chdir(cwd)
        self.assertNotEqual(code, 0)
        self.assertIn("FAL_KEY", err)
        self.assertNotIn(TEST_KEY, err)
        self.assertFalse(any("queue.fal.run" in url for url in transport.urls()))


if __name__ == "__main__":
    unittest.main()
