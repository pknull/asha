"""Offline acceptance tests for the DriveThruRPG helper."""

from __future__ import annotations

import base64
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[2] / "plugins/rpg/skills/dtrpg/scripts/dtrpg.py"
SPEC = importlib.util.spec_from_file_location("dtrpg", SCRIPT)
assert SPEC and SPEC.loader
dtrpg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dtrpg)


def product(files=None, **kwargs):
    return {
        "product_id": 10, "order_product_id": 100, "name": "The Game",
        "publisher": "Publisher", "file_last_modified": "2026-07-02T00:00:00Z",
        "file_last_downloaded": "2026-07-01T00:00:00Z", "updated_since_download": True,
        "files": files or [], **kwargs,
    }


QP_MESSAGE = (
    b"From: no-reply-comp-copies@drivethrurpg.com\r\n"
    b"Subject: Your complimentary copies\r\n"
    b"Date: Fri, 09 Oct 2026 12:00:00 +0000\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/alternative; boundary="offer"\r\n\r\n'
    b"--offer\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    b'Your copy of "Should not be duplicated"\r\n'
    b"--offer\r\nContent-Type: text/html; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
    b'<a href=3D"https://www.drivethrurpg.com/browse.php?discount=3D91f7abcd">'
    b"  Hero=E2=80=99s <b>Guide</b> =E2=80=94 =E2=80=9CDeluxe=E2=80=9D </a>"
    b'<a href=3D"https://www.drivethrurpg.com/en/browse?discountId=3Dnewcode">'
    b"Unclaimed &amp; New</a>\r\n--offer--\r\n"
)


class DtrpgTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "library"
        self.root.mkdir()
        self.staging = self.base / "staging"
        self.state = self.base / "state"
        self.state.mkdir()
        self.env = mock.patch.dict(os.environ, {"DTRPG_STATE_DIR": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def save(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = dtrpg.main(list(args))
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_iso_comparison_offsets_never_downloaded_and_bad_dates(self):
        cases = [
            ("2026-07-01T01:00:00+02:00", "2026-07-01T00:00:00Z", False),
            ("2026-07-01T02:00:00+02:00", "2026-07-01T00:00:00Z", False),
            ("2026-07-01T00:00:00.000001Z", "2026-07-01T00:00:00+00:00", True),
            ("2026-07-02T00:00:00", "2026-07-01T00:00:00Z", True),
            (None, None, True), ("bad", None, True),
            (None, "2026-07-01", False), ("bad", "bad", False),
        ]
        for modified, downloaded, expected in cases:
            with self.subTest(modified=modified, downloaded=downloaded):
                self.assertEqual(dtrpg.updated_since_download(modified, downloaded), expected)

    def test_api_normalization_handles_null_fields(self):
        value = dtrpg.normalize_product({"productId": 1, "publisher": None, "files": None})
        self.assertEqual(value["product_id"], 1)
        self.assertIsNone(value["publisher"])
        self.assertEqual(value["files"], [])
        self.assertTrue(value["updated_since_download"])
        value = dtrpg.normalize_product({"publisher": {"name": "P"}, "files": [{}]})
        self.assertEqual(value["publisher"], "P")
        self.assertEqual(value["files"], [{"index": None, "filename": None, "size": None}])

    def test_library_json_is_offline_and_recomputes_update_flag(self):
        saved = self.save("library.json", {"products": [product(updated_since_download=False)], "count": 99})
        with mock.patch.object(dtrpg, "authenticate", side_effect=AssertionError("network")):
            code, result, _ = self.cli("library", "--library-json", saved)
        self.assertEqual(code, 0)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["products"][0]["updated_since_download"])

    def test_candidate_names_strip_only_leading_numeric_prefix(self):
        names = dtrpg.candidate_names("123-Rule_Book.PDF")
        self.assertIn("rule book.pdf", names)
        self.assertIn("123-rule book.pdf", names)
        self.assertEqual(dtrpg.normalized_filename("Rule Book.pdf"), "rule book.pdf")
        self.assertNotIn("book.pdf", dtrpg.candidate_names("Rule-123-Book.pdf"))

    def test_tsv_comments_blanks_notes_missing_and_malformed(self):
        (self.state / "placements.tsv").write_text("# comment\n\n10\tGame/Core\tmy note\n  # indented\n20\tOther\n")
        (self.state / "ignore.tsv").write_text("\n# comment\n10\t*.zip\talready extracted\n20\t*\tother language\n")
        placements, ignores = dtrpg.load_state()
        self.assertEqual(placements, {"10": "Game/Core", "20": "Other"})
        self.assertEqual(ignores[1], ["20", "*", "other language"])
        self.assertEqual(dtrpg.read_tsv(self.state / "absent.tsv", 3), [])
        (self.state / "ignore.tsv").write_text("bad row\n")
        with self.assertRaisesRegex(ValueError, "Malformed TSV row 1"):
            dtrpg.load_state()

    def test_audit_flags_never_downloaded_stale_for_comparison(self):
        (self.root / "book.pdf").write_bytes(b"pledge manager copy")
        files = [{"index": 0, "filename": "book.pdf", "size": 999}]
        never = dtrpg.audit({"products": [product(files, file_last_downloaded=None)]}, self.root)
        self.assertEqual(never["files"][0]["status"], "stale")
        self.assertIn("never downloaded", never["files"][0]["reason"])
        updated = dtrpg.audit({"products": [product(files)]}, self.root)
        self.assertEqual(updated["files"][0]["status"], "stale")
        self.assertIsNone(updated["files"][0]["reason"])
        identical = [{"index": 0, "filename": "book.pdf", "size": 19}]
        same = dtrpg.audit({"products": [product(identical, file_last_downloaded=None)]}, self.root)
        self.assertEqual(same["files"][0]["status"], "present")

    def test_default_http_get_sends_browser_headers(self):
        # watermark.drivethrurpg.com answers httpx's default User-Agent with
        # 503 "Blocked for not following robot.txt rules, Bad bot".
        calls = []
        fake_httpx = SimpleNamespace(stream=lambda *a, **k: calls.append((a, k)))
        dtrpg.make_http_get(fake_httpx)("https://example.invalid/file")
        args, kwargs = calls[0]
        self.assertEqual(args, ("GET", "https://example.invalid/file"))
        self.assertEqual(kwargs["headers"]["User-Agent"], "Mozilla/5.0")
        self.assertTrue(kwargs["follow_redirects"])

    def test_audit_exact_size_match_overrides_product_level_update_date(self):
        # The update date is per product; a file whose bytes still match the
        # library size is current even when a sibling file changed.
        (self.root / "same.pdf").write_bytes(b"12345")
        (self.root / "changed.pdf").write_bytes(b"123")
        files = [{"index": 0, "filename": "same.pdf", "size": 5},
                 {"index": 1, "filename": "changed.pdf", "size": 9},
                 {"index": 2, "filename": "unknown.pdf", "size": None}]
        (self.root / "unknown.pdf").write_bytes(b"x")
        result = dtrpg.audit({"products": [product(files)]}, self.root)
        self.assertEqual([f["status"] for f in result["files"]], ["present", "stale", "stale"])

    def test_audit_all_rules_and_filtered_summary_with_fake_library_json(self):
        for name, content in {"rules book.PDF": b"name match", "ignored.zip": b"x", "renamed.pdf": b"1234567"}.items():
            (self.root / name).write_bytes(content)
        for excluded in ("node_modules", ".git", "__MACOSX"):
            nested = self.root / "nested" / excluded
            nested.mkdir(parents=True)
            (nested / "missing.pdf").write_bytes(b"00000000000")
        (self.state / "placements.tsv").write_text("10\tGame\n")
        (self.state / "ignore.tsv").write_text("10\t*.zip\textracted\n20\t*\tlanguage\n")
        files = [
            {"index": 0, "filename": "ignored.zip", "size": 1},
            {"index": 1, "filename": "99-Rules_Book.pdf", "size": 99},
            {"index": 2, "filename": "renamed-online.pdf", "size": 7},
            {"index": 3, "filename": "missing.pdf", "size": 11},
            {"index": 4, "filename": "zero.pdf", "size": 0},
        ]
        library = {"products": [product(files), product([files[0]], product_id=20),
                                 product([files[1]], product_id=30, file_last_downloaded="2026-07-03T00:00:00Z")]}
        saved = self.save("library.json", library)
        code, result, _ = self.cli("audit", "--root", str(self.root), "--library-json", saved)
        self.assertEqual(code, 0)
        self.assertEqual([f["status"] for f in result["files"]],
                         ["ignored", "stale", "present_by_size", "missing", "missing", "ignored", "present"])
        self.assertEqual(result["files"][0]["reason"], "extracted")
        self.assertEqual(result["files"][1]["local_paths"], ["rules book.PDF"])
        self.assertEqual(result["files"][2]["local_paths"], ["renamed.pdf"])
        self.assertEqual(result["files"][1]["suggested_dest"], "Game")
        self.assertIsNone(result["files"][-1]["suggested_dest"])
        _, filtered, _ = self.cli("audit", "--root", str(self.root), "--library-json", saved, "--status", "missing,stale")
        self.assertEqual(filtered["summary"], result["summary"])
        self.assertEqual(len(filtered["files"]), 3)

    def test_staging_inside_root_and_symlink_rejected(self):
        for path in (self.root, self.root / "nested"):
            with self.assertRaisesRegex(ValueError, "outside"):
                dtrpg.staging_path(self.root, path)
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            dtrpg.staging_path(self.root, alias / "nested")
        self.assertEqual(dtrpg.staging_path(self.root, self.staging), self.staging)

    def test_filename_sanitization(self):
        for value, expected in [("../../book.pdf", "book.pdf"), (r"C:\bad\book.pdf", "book.pdf"),
                                ("some..book.pdf", "some_book.pdf"), ("bo\x00ok.pdf", "bo_ok.pdf")]:
            self.assertEqual(dtrpg.safe_filename(value), expected)
        for value in (None, "", "..", "/", "."):
            with self.assertRaises(ValueError):
                dtrpg.safe_filename(value)

    def test_fetch_retries_503_reprepares_and_skips_existing(self):
        api = mock.Mock()
        api.prepare_download_url.side_effect = [{"url": "url1"}, {"url": "url2"}]
        urls, sleeps = [], []

        @contextmanager
        def getter(url):
            urls.append(url)
            yield SimpleNamespace(status_code=503 if len(urls) == 1 else 200,
                                  iter_bytes=lambda: iter([b"PDF", b"data"]))

        item = {"order_product_id": 100, "index": 2, "filename": "../book.pdf"}
        result = dtrpg.fetch([item, item], self.root, self.staging, delay=3,
                             api=api, http_get=getter, sleep=sleeps.append)
        self.assertEqual(urls, ["url1", "url2"])
        self.assertEqual(api.prepare_download_url.call_args_list, [mock.call(100, 2), mock.call(100, 2)])
        self.assertEqual(sleeps, [2, 3])
        self.assertEqual([r["status"] for r in result["results"]], ["downloaded", "skipped_exists"])
        self.assertEqual(result["results"][0]["bytes"], 7)
        self.assertEqual((self.staging / "book.pdf").read_bytes(), b"PDFdata")
        self.assertFalse((self.staging / "book.pdf.part").exists())

    def test_fetch_transport_exhaustion_has_four_retries_and_no_partial(self):
        class TransportError(Exception):
            pass

        api, sleeps = mock.Mock(), []
        api.prepare_download_url.return_value = {"url": "signed-secret-url"}

        @contextmanager
        def getter(url):
            raise TransportError("signed-secret-url")
            yield

        result = dtrpg.fetch([{"order_product_id": 1, "index": 0, "filename": "f.pdf"}],
                             self.root, self.staging, api=api, http_get=getter,
                             transport_errors=(TransportError,), sleep=sleeps.append)
        self.assertEqual(sleeps, [2, 4, 8, 16])
        self.assertEqual(api.prepare_download_url.call_count, 5)
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertNotIn("signed-secret-url", json.dumps(result))
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_fetch_truncated_stream_restarts_from_zero(self):
        class TransportError(Exception):
            pass

        api = mock.Mock()
        api.prepare_download_url.return_value = {"url": "url"}
        calls = []

        def chunks():
            yield b"old partial" if len(calls) == 1 else b"new"
            if len(calls) == 1:
                raise TransportError()

        @contextmanager
        def getter(url):
            calls.append(url)
            yield SimpleNamespace(status_code=200, iter_bytes=chunks)

        result = dtrpg.fetch([{"order_product_id": 1, "index": 0, "filename": "f.pdf"}],
                             self.root, self.staging, api=api, http_get=getter,
                             transport_errors=(TransportError,), sleep=lambda _: None)
        self.assertEqual(result["results"][0]["bytes"], 3)
        self.assertEqual((self.staging / "f.pdf").read_bytes(), b"new")

    def test_fetch_preserves_existing_part(self):
        self.staging.mkdir()
        partial = self.staging / "f.pdf.part"
        partial.write_bytes(b"keep")
        api = mock.Mock()
        result = dtrpg.download_item({"order_product_id": 1, "index": 0, "filename": "f.pdf"},
                                     self.staging, api, mock.Mock())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(partial.read_bytes(), b"keep")
        api.prepare_download_url.assert_not_called()

    def test_fetch_dry_run_from_audit_needs_no_token_or_dependencies(self):
        saved = self.save("audit.json", {"files": [
            {"order_product_id": 100, "index": 1, "filename": "book.pdf", "status": "missing"},
            {"order_product_id": 100, "index": 2, "filename": "skip.pdf", "status": "ignored"},
        ]})
        with mock.patch.object(dtrpg, "authenticate", side_effect=AssertionError("network")):
            code, result, _ = self.cli("fetch", "--from-audit", saved, "--dry-run", "--root", str(self.root), "--staging", str(self.staging))
        self.assertEqual(code, 0)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["status"], "would_download")
        self.assertFalse(self.staging.exists())
        with mock.patch.object(dtrpg, "authenticate", side_effect=AssertionError("network")):
            code, result, _ = self.cli("fetch", "--from-audit", saved, "--item", "100:2", "--dry-run",
                                       "--root", str(self.root), "--staging", str(self.staging))
        self.assertEqual(code, 0)
        self.assertEqual([r["index"] for r in result["results"]], [1, 2])

    def test_fetch_does_not_retry_permanent_http_errors(self):
        api, sleeps = mock.Mock(), []
        api.prepare_download_url.return_value = {"url": "url"}

        @contextmanager
        def getter(url):
            yield SimpleNamespace(status_code=404)

        result = dtrpg.fetch([{"order_product_id": 1, "index": 0, "filename": "f.pdf"}],
                             self.root, self.staging, api=api, http_get=getter, sleep=sleeps.append)
        self.assertEqual(result["results"][0]["error"], "HTTP 404")
        self.assertEqual(api.prepare_download_url.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_import_does_not_load_network_dependencies(self):
        import builtins
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"httpx", "drpg"}:
                raise AssertionError("Network dependency imported")
            return original_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=guarded_import):
            module = importlib.util.module_from_spec(SPEC)
            SPEC.loader.exec_module(module)
            self.assertTrue(module.updated_since_download(None, None))

    def test_fetch_selection_union_and_deduplication(self):
        saved = self.save("audit.json", {"files": [{"order_product_id": 100, "index": 1,
                                                   "filename": "one.pdf", "status": "missing"}]})
        library = {"products": [product([{"index": 2, "filename": "two.pdf"}])]}
        items = dtrpg.select_items(["100:1", "100:2", "100:2"], saved, library=library)
        self.assertEqual([i["index"] for i in items], [1, 2])
        with self.assertRaises(dtrpg.CLIError):
            dtrpg.select_items(["bad"], library=library)

    def test_fetch_exit_status_partial_and_total_failure(self):
        for statuses, expected in [(["failed"], 1), (["failed", "downloaded"], 0), (["skipped_exists"], 0)]:
            with mock.patch.object(dtrpg, "execute", return_value={"results": [{"status": s} for s in statuses]}):
                code, _, _ = self.cli("fetch")
            self.assertEqual(code, expected)

    def test_place_plan_apply_permissions_and_no_overwrite(self):
        self.staging.mkdir()
        for filename in ("book.pdf", "bundle.zip", "unknown.pdf", "skip.pdf"):
            (self.staging / filename).write_bytes(b"staged")
        fetch_data = {"staging": str(self.staging), "results": [
            {"order_product_id": order, "status": status, "path": str(self.staging / filename)}
            for filename, order, status in [("book.pdf", 100, "downloaded"), ("bundle.zip", 100, "downloaded"),
                                            ("unknown.pdf", 200, "downloaded"), ("gone.pdf", 100, "downloaded"),
                                            ("skip.pdf", 100, "skipped_exists")]
        ]}
        library_path = self.save("library.json", {"products": [product()]})
        fetch_path = self.save("fetch.json", fetch_data)
        (self.state / "placements.tsv").write_text("10\tSystem/Core\n")
        args = ["place", "--root", str(self.root), "--from-fetch", fetch_path, "--library-json", library_path]
        code, plan, _ = self.cli(*args)
        self.assertEqual(code, 0)
        self.assertEqual(len(plan["moves"]), 2)
        self.assertEqual(len(plan["unplaced"]), 1)
        self.assertFalse((self.root / "System").exists())
        target = self.root / "System/Core/book.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"curated")
        code, applied, _ = self.cli(*args, "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(len(applied["moves"]), 1)
        self.assertEqual(len(applied["conflicts"]), 1)
        self.assertEqual(target.read_bytes(), b"curated")
        self.assertEqual((self.staging / "book.pdf").read_bytes(), b"staged")
        zip_path = target.with_name("bundle.zip")
        self.assertEqual(zip_path.read_bytes(), b"staged")
        self.assertEqual(zip_path.stat().st_mode & 0o777, 0o664)
        self.assertFalse((self.staging / "bundle.zip").exists())
        self.assertTrue((self.staging / "skip.pdf").exists())

    def test_place_rejects_escape_paths(self):
        self.staging.mkdir()
        src = self.staging / "book.pdf"
        src.write_bytes(b"data")
        data = {"results": [{"order_product_id": 100, "status": "downloaded", "path": str(src)}]}
        library = {"products": [product()]}
        for folder in ("../outside", str(self.base / "outside")):
            with self.assertRaisesRegex(ValueError, "outside"):
                dtrpg.place(data, library, self.root, self.staging, {"10": folder}, apply=True)
        data["results"][0]["path"] = str(self.base / "outside.pdf")
        with self.assertRaisesRegex(ValueError, "outside staging"):
            dtrpg.place(data, library, self.root, self.staging, {"10": "Game"})

    def test_claims_raw_qp_and_base64url_deduplicate_and_match_variants(self):
        messages = self.base / "mail"
        messages.mkdir()
        (messages / "1.eml").write_bytes(QP_MESSAGE)
        (messages / "2.raw").write_bytes(base64.urlsafe_b64encode(QP_MESSAGE).rstrip(b"="))
        library = {"products": [product(name='Hero\'s Guide - "Deluxe"')]}
        result = dtrpg.claims(messages, library)
        self.assertEqual(result["summary"], {"claimed": 1, "unclaimed": 1})
        first, second = result["offers"]
        self.assertEqual(first["code"], "91f7abcd")
        self.assertEqual(first["claim_url"], "https://www.drivethrurpg.com/en/browse?discountId=91f7abcd")
        self.assertEqual(first["matched_product"], 'Hero\'s Guide - "Deluxe"')
        self.assertEqual(first["subject"], "Your complimentary copies")
        self.assertIn("2026", first["message_date"])
        self.assertEqual(second["title"], "Unclaimed & New")
        self.assertFalse(second["claimed"])
        self.assertIsNone(second["matched_product"])
        # Verify the encoded variant independently, not just through deduplication.
        (messages / "1.eml").unlink()
        encoded = dtrpg.claims(messages, library)
        self.assertEqual([o["code"] for o in encoded["offers"]], ["91f7abcd", "newcode"])

    def test_claims_plain_text_fallback_and_no_html_fallback(self):
        messages = self.base / "mail"
        messages.mkdir()
        message = messages / "plain.eml"
        message.write_bytes(b'From: no-reply-comp-copies@drivethrurpg.com\nContent-Type: text/plain\n\nYou have a free copy of "The Game".\n')
        result = dtrpg.claims(messages, {"products": [product()]})
        self.assertEqual(result["summary"], {"claimed": 1, "unclaimed": 0})
        self.assertIsNone(result["offers"][0]["code"])
        self.assertIsNone(result["offers"][0]["claim_url"])
        message.write_bytes(QP_MESSAGE.replace(b"discount=3D", b"other=3D").replace(b"discountId=3D", b"other=3D"))
        self.assertEqual(dtrpg.claims(messages, {"products": []})["offers"], [])

    def test_missing_token_argparse_errors_and_redaction(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, error, _ = self.cli("library")
        self.assertEqual(code, 2)
        self.assertTrue(error["error"].startswith("DRPG_TOKEN not set"))
        code, error, stderr = self.cli("place")
        self.assertEqual(code, 2)
        self.assertIn("--from-fetch", error["error"])
        self.assertEqual(stderr, "")
        with mock.patch.dict(os.environ, {"DRPG_TOKEN": 'secret"token'}), mock.patch.object(
                dtrpg, "execute", side_effect=ValueError('failure: secret"token')):
            _, error, _ = self.cli("library")
        self.assertNotIn("secret", json.dumps(error))


if __name__ == "__main__":
    unittest.main()
