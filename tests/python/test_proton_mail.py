"""Acceptance tests for the Proton Mail Bridge helper."""

from __future__ import annotations

import importlib.util
import io
import hashlib
import json
import ssl
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = (
    Path(__file__).parents[2]
    / "plugins"
    / "admin"
    / "skills"
    / "proton-mail"
    / "scripts"
    / "proton_mail.py"
)
SPEC = importlib.util.spec_from_file_location("proton_mail", SCRIPT)
assert SPEC and SPEC.loader
proton_mail = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proton_mail)


class FakeImap:
    def __init__(
        self,
        *,
        capability=b"IMAP4rev1 MOVE UIDPLUS SPECIAL-USE ENABLE UTF8=ACCEPT",
        uidvalidity=812,
        existing=True,
        message=None,
        list_rows=None,
    ):
        self.capabilities = tuple(capability.split())
        self.calls = []
        self.untagged_responses = {}
        self.uidvalidity = uidvalidity
        self.existing = existing
        self.message = message or (
            b"From: sender@example.com\r\n"
            b"To: receiver@example.com\r\n"
            b"Subject: Test\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"message body"
        )
        self.list_rows = list_rows or [
            b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
            b'(\\HasNoChildren \\Trash) "/" "Trash"',
        ]
        self.move_response = b"[COPYUID 900 23 99] moved"

    def starttls(self, ssl_context=None):
        self.calls.append(("starttls", ssl_context))
        return "OK", [b"Begin TLS"]

    def login(self, username, password):
        self.calls.append(("login", username, password))
        return "OK", [b"authenticated"]

    def select(self, mailbox="INBOX", readonly=False):
        self.calls.append(("select", mailbox, readonly))
        self.untagged_responses["UIDVALIDITY"] = [
            str(self.uidvalidity).encode()
        ]
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command.upper() == "FETCH":
            if not self.existing:
                return "OK", [None]
            query = args[-1]
            if "RFC822.SIZE" in query and "BODY.PEEK" not in query:
                return "OK", [
                    (
                        f"1 (UID 23 RFC822.SIZE {len(self.message)})".encode(),
                        b"",
                    )
                ]
            if query == "(UID)":
                return "OK", [(b"1 (UID 23)", b"")]
            if "HEADER.FIELDS" in query:
                header = self.message.partition(b"\r\n\r\n")[0] + b"\r\n\r\n"
                header = header[: proton_mail.MAX_HEADER_BYTES]
                return "OK", [
                    (
                        f"1 (UID 23 BODY[HEADER.FIELDS] <0> {{{len(header)}}}".encode(),
                        header,
                    ),
                    b")",
                ]
            return "OK", [
                (
                    f"1 (UID 23 BODY[] {{{len(self.message)}}}".encode(),
                    self.message,
                ),
                b")",
            ]
        if command.upper() == "MOVE":
            return "OK", [self.move_response]
        return "OK", [b"done"]

    def enable(self, capability):
        self.calls.append(("enable", capability))
        return "OK", [b"UTF8=ACCEPT enabled"]

    def list(self):
        self.calls.append(("list",))
        return "OK", self.list_rows

    def append(self, mailbox, flags, date_time, message):
        self.calls.append(("append", mailbox, flags, date_time, message))
        return "OK", [b"APPENDUID 812 99"]

    def logout(self):
        self.calls.append(("logout",))


class FakeSmtp:
    def __init__(self, refused=None):
        self.calls = []
        self.refused = refused or {}

    def ehlo(self):
        self.calls.append(("ehlo",))
        return 250, b"localhost"

    def starttls(self, context=None):
        self.calls.append(("starttls", context))
        return 220, b"ready"

    def login(self, username, password):
        self.calls.append(("login", username, password))
        return 235, b"authenticated"

    def sendmail(self, sender, recipients, message):
        self.calls.append(("sendmail", sender, recipients, message))
        return self.refused

    def quit(self):
        self.calls.append(("quit",))


class BatchImap(FakeImap):
    """Protocol-shaped fake for batch UID FETCH/STORE/FLAGS operations."""

    def __init__(
        self,
        uids=(21, 22, 23),
        *,
        uidvalidity=812,
        store_status="OK",
        mark_after_store=None,
    ):
        super().__init__(uidvalidity=uidvalidity)
        self.uids = set(uids)
        self.flags = {uid: {r"\Flagged"} for uid in self.uids}
        self.store_status = store_status
        self.mark_after_store = (
            set(mark_after_store)
            if mark_after_store is not None
            else set(self.uids)
        )

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        command = command.upper()
        uid_set = args[0] if args else ""
        requested = proton_mail.expand_uid_set(uid_set)
        if command == "STORE":
            if self.store_status != "OK":
                return self.store_status, [b"STORE failed"]
            for uid in requested & self.mark_after_store:
                if uid in self.flags:
                    self.flags[uid].add(r"\Seen")
            return "OK", [b"STORE completed"]
        if command == "FETCH" and args[-1] == "(UID)":
            return "OK", [
                (f"{index} (UID {uid})".encode(), b"")
                for index, uid in enumerate(sorted(requested & self.uids), 1)
            ]
        if command == "FETCH" and args[-1] == "(UID FLAGS)":
            rows = []
            for index, uid in enumerate(sorted(requested & self.uids), 1):
                flags = " ".join(sorted(self.flags[uid]))
                rows.append(
                    (
                        f"{index} (UID {uid} FLAGS ({flags}))".encode(),
                        b"",
                    )
                )
            return "OK", rows
        return super().uid(command, *args)


def config():
    return proton_mail.BridgeConfig(
        username="bridge-user",
        password="bridge-secret",
        host="localhost",
        imap_port=1143,
        smtp_port=1025,
    )


class ProtonMailAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tempdir.name) / "replay-ledger.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def apply_kwargs(self):
        return {"ledger_path": self.ledger}

    def test_bridge_rejects_non_loopback_hosts(self):
        for host in ["example.com", "127.0.0.2", "0.0.0.0"]:
            with self.subTest(host=host):
                with self.assertRaisesRegex(proton_mail.SafetyError, "localhost"):
                    proton_mail.BridgeConfig("u", "p", host=host)

    def test_imap_authentication_occurs_only_after_verified_starttls(self):
        fake = FakeImap()
        with mock.patch.object(
            proton_mail.imaplib, "IMAP4", lambda **kwargs: fake
        ):
            client = proton_mail.connect_imap(config())

        self.assertIs(client, fake)
        self.assertEqual(
            [call[0] for call in fake.calls[:2]], ["starttls", "login"]
        )
        self.assertIsInstance(fake.calls[0][1], ssl.SSLContext)
        self.assertEqual(fake.calls[0][1].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(fake.calls[0][1].check_hostname)

    def test_smtp_authentication_occurs_only_after_verified_starttls(self):
        fake = FakeSmtp()
        with mock.patch.object(
            proton_mail.smtplib, "SMTP", lambda **kwargs: fake
        ):
            client = proton_mail.connect_smtp(config())

        self.assertIs(client, fake)
        self.assertEqual(
            [call[0] for call in fake.calls[:4]],
            ["ehlo", "starttls", "ehlo", "login"],
        )
        context = fake.calls[1][1]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_stdlib_imap_api_uses_select_readonly_not_nonexistent_examine(self):
        imap = mock.create_autospec(proton_mail.imaplib.IMAP4, instance=True)
        imap.select.return_value = ("OK", [b"1"])
        imap.untagged_responses = {"UIDVALIDITY": [b"812"]}

        value = proton_mail.select_mailbox(imap, "INBOX", readonly=True)

        self.assertEqual(value, 812)
        imap.select.assert_called_once_with("INBOX", readonly=True)

    def test_read_sizes_before_body_peek_and_selects_readonly(self):
        imap = FakeImap()
        result = proton_mail.read_message(
            imap, proton_mail.MessageRef("INBOX", 812, 23)
        )
        self.assertEqual(
            result["ref"],
            {"mailbox": "INBOX", "uidvalidity": 812, "uid": 23},
        )
        self.assertEqual(
            imap.calls[:3],
            [
                ("select", "INBOX", True),
                ("uid", "FETCH", "23", "(UID RFC822.SIZE)"),
                ("uid", "FETCH", "23", "(UID BODY.PEEK[])"),
            ],
        )
        self.assertEqual(result["body"]["text"], "message body")
        self.assertEqual(result["content_trust"], "untrusted_email_data")

    def test_read_rejects_oversize_before_fetching_body(self):
        imap = FakeImap(message=b"x" * 101)

        with self.assertRaisesRegex(proton_mail.LimitError, "message"):
            proton_mail.read_message(
                imap,
                proton_mail.MessageRef("INBOX", 812, 23),
                max_message_bytes=100,
            )

        self.assertFalse(
            any(
                call[:2] == ("uid", "FETCH") and "BODY.PEEK" in call[-1]
                for call in imap.calls
            )
        )

    def test_header_summary_uses_partial_fetch_not_total_message_limit(self):
        huge_body = b"Subject: Huge\r\nFrom: sender@example.com\r\n\r\n" + (
            b"x" * (proton_mail.DEFAULT_MAX_MESSAGE_BYTES + 1)
        )
        imap = FakeImap(message=huge_body)

        summary = proton_mail.message_summary(imap, "INBOX", 812, 23)

        self.assertEqual(summary["ref"]["uid"], 23)
        self.assertIn(summary["summary_status"], {"available", "truncated"})
        fetches = [call for call in imap.calls if call[:2] == ("uid", "FETCH")]
        self.assertEqual(len(fetches), 1)
        self.assertIn("<0.", fetches[0][-1])
        self.assertNotIn("RFC822.SIZE", fetches[0][-1])

    def test_bad_header_summary_is_isolated_without_aborting_result_set(self):
        imap = FakeImap()
        original = proton_mail.message_summary

        def summarize(client, mailbox, uidvalidity, uid):
            if uid == 22:
                raise proton_mail.LimitError("header too large")
            return original(client, mailbox, uidvalidity, uid)

        with mock.patch.object(proton_mail, "message_summary", side_effect=summarize):
            summaries = proton_mail.summarize_uids(
                imap, "INBOX", 812, [22, 23]
            )

        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0]["ref"]["uid"], 22)
        self.assertEqual(summaries[0]["summary_status"], "unavailable")
        self.assertEqual(summaries[1]["ref"]["uid"], 23)

    def test_summary_transport_and_protocol_failures_propagate(self):
        imap = FakeImap()
        for failure in (
            proton_mail.imaplib.IMAP4.abort("connection reset"),
            proton_mail.ProtocolError("malformed server response"),
        ):
            with self.subTest(failure=failure):
                with mock.patch.object(
                    proton_mail, "message_summary", side_effect=failure
                ):
                    with self.assertRaises(type(failure)):
                        proton_mail.summarize_uids(
                            imap, "INBOX", 812, [23]
                        )

    def test_read_aborts_if_uidvalidity_changed_before_fetch(self):
        imap = FakeImap()
        with self.assertRaises(proton_mail.StaleMessageError):
            proton_mail.read_message(
                imap, proton_mail.MessageRef("INBOX", 999, 23)
            )
        self.assertFalse(any(call[0] == "uid" for call in imap.calls))

    def test_structured_search_quotes_values_and_has_no_raw_escape_hatch(self):
        criteria = proton_mail.build_search_criteria(
            from_address='person"@example.com',
            subject="alpha beta",
            unread=True,
            since="2026-07-01",
        )
        self.assertEqual(
            criteria,
            [
                "FROM",
                '"person\\"@example.com"',
                "SUBJECT",
                '"alpha beta"',
                "UNSEEN",
                "SINCE",
                "01-Jul-2026",
            ],
        )
        self.assertNotIn("--raw", proton_mail.build_parser().format_help())

    def test_mime_parsing_is_bounded(self):
        with self.assertRaisesRegex(proton_mail.LimitError, "message"):
            proton_mail.parse_message(b"x" * 101, max_message_bytes=100)
        raw = (
            b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
            b"--x\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b"--x\r\nContent-Disposition: attachment; filename=a.bin\r\n\r\n"
            b"01234567890\r\n--x--\r\n"
        )
        with self.assertRaisesRegex(proton_mail.LimitError, "attachment"):
            proton_mail.parse_message(raw, max_attachment_bytes=10)

    def test_plan_is_canonical_hash_bound_and_tampering_is_rejected(self):
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )
        hash_value = plan["plan_hash"]
        proton_mail.verify_plan(
            plan, expected_action="move", supplied_hash=hash_value
        )
        plan["operation"]["destination"] = "Trash"
        with self.assertRaisesRegex(proton_mail.PlanError, "hash"):
            proton_mail.verify_plan(
                plan, expected_action="move", supplied_hash=hash_value
            )

    def test_noncanonical_plan_is_rejected_even_with_recomputed_hash(self):
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )
        plan["operation"]["destination"] = " Archive "
        base = {key: value for key, value in plan.items() if key != "plan_hash"}
        plan["plan_hash"] = proton_mail.plan_digest(base)

        with self.assertRaisesRegex(proton_mail.PlanError, "canonical"):
            proton_mail.verify_plan(
                plan,
                expected_action="move",
                supplied_hash=plan["plan_hash"],
            )

    def test_single_ref_plans_reject_bool_and_string_integer_type_confusion(self):
        specimens = (
            ("uid", True),
            ("uid", False),
            ("uid", "23"),
            ("uidvalidity", True),
            ("uidvalidity", False),
            ("uidvalidity", "812"),
        )
        for field, value in specimens:
            with self.subTest(field=field, value=value):
                plan = proton_mail.create_plan(
                    "move",
                    {
                        "ref": {
                            "mailbox": "INBOX",
                            "uidvalidity": 812,
                            "uid": 23,
                        },
                        "destination": "Archive",
                    },
                )
                plan["operation"]["ref"][field] = value
                base = {
                    key: item
                    for key, item in plan.items()
                    if key != "plan_hash"
                }
                plan["plan_hash"] = proton_mail.plan_digest(base)
                imap = FakeImap()
                ledger = (
                    Path(self.tempdir.name)
                    / f"single-{field}-{value!s}.json"
                )

                with self.assertRaisesRegex(
                    proton_mail.PlanError, "integer"
                ):
                    proton_mail.apply_move(
                        imap,
                        plan,
                        plan["plan_hash"],
                        ledger_path=ledger,
                    )
                self.assertEqual(imap.calls, [])
                self.assertFalse(ledger.exists())

    def test_batch_refs_reject_bool_and_string_integer_type_confusion(self):
        specimens = (
            ("uid", True),
            ("uid", False),
            ("uid", "21"),
            ("uidvalidity", True),
            ("uidvalidity", False),
            ("uidvalidity", "812"),
        )
        for field, value in specimens:
            with self.subTest(field=field, value=value):
                plan = proton_mail.create_plan(
                    "mark-read",
                    {
                        "refs": [
                            {
                                "mailbox": "INBOX",
                                "uidvalidity": 812,
                                "uid": 21,
                            },
                            {
                                "mailbox": "INBOX",
                                "uidvalidity": 812,
                                "uid": 22,
                            },
                        ]
                    },
                )
                plan["operation"]["refs"][0][field] = value
                base = {
                    key: item
                    for key, item in plan.items()
                    if key != "plan_hash"
                }
                plan["plan_hash"] = proton_mail.plan_digest(base)
                imap = BatchImap()
                ledger = (
                    Path(self.tempdir.name)
                    / f"batch-{field}-{value!s}.json"
                )

                with self.assertRaisesRegex(
                    proton_mail.PlanError, "integer"
                ):
                    proton_mail.apply_mark_read(
                        imap,
                        plan,
                        plan["plan_hash"],
                        ledger_path=ledger,
                    )
                self.assertEqual(imap.calls, [])
                self.assertFalse(ledger.exists())

    def test_plan_mark_read_parser_canonicalizes_repeated_uids_without_network(self):
        args = proton_mail.build_parser().parse_args(
            [
                "plan-mark-read",
                "--mailbox",
                "INBOX",
                "--uidvalidity",
                "812",
                "--uid",
                "23",
                "--uid",
                "21",
                "--uid",
                "22",
            ]
        )
        with mock.patch.object(proton_mail, "connect_imap") as connect_imap:
            plan = proton_mail.execute(args, None)

        self.assertEqual(
            plan["operation"]["refs"],
            [
                {"mailbox": "INBOX", "uidvalidity": 812, "uid": 21},
                {"mailbox": "INBOX", "uidvalidity": 812, "uid": 22},
                {"mailbox": "INBOX", "uidvalidity": 812, "uid": 23},
            ],
        )
        connect_imap.assert_not_called()

    def test_mark_read_plan_rejects_invalid_duplicate_or_excess_uids(self):
        for refs in (
            [
                {"mailbox": "INBOX", "uidvalidity": 812, "uid": 23},
                {"mailbox": "INBOX", "uidvalidity": 812, "uid": 23},
            ],
            [{"mailbox": "INBOX", "uidvalidity": 812, "uid": 0}],
            [
                {"mailbox": "INBOX", "uidvalidity": 812, "uid": uid}
                for uid in range(1, 502)
            ],
        ):
            with self.subTest(count=len(refs)):
                with self.assertRaises(proton_mail.PlanError):
                    proton_mail.create_plan("mark-read", {"refs": refs})

    def test_mark_read_plan_rejects_noncanonical_ref_order(self):
        plan = proton_mail.create_plan(
            "mark-read",
            {
                "refs": [
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": 21},
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": 22},
                ]
            },
        )
        plan["operation"]["refs"].reverse()
        base = {key: value for key, value in plan.items() if key != "plan_hash"}
        plan["plan_hash"] = proton_mail.plan_digest(base)

        with self.assertRaisesRegex(proton_mail.PlanError, "canonical"):
            proton_mail.verify_plan(
                plan,
                expected_action="mark-read",
                supplied_hash=plan["plan_hash"],
            )

    def test_compact_uid_set_is_deterministic_and_round_trips(self):
        compact = proton_mail.compact_uid_set([1, 2, 3, 5, 7, 8])
        self.assertEqual(compact, "1:3,5,7:8")
        self.assertEqual(
            proton_mail.expand_uid_set(compact),
            {1, 2, 3, 5, 7, 8},
        )

    def test_apply_mark_read_uses_one_silent_store_and_verifies_seen(self):
        imap = BatchImap()
        plan = proton_mail.create_plan(
            "mark-read",
            {
                "refs": [
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": 23},
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": 21},
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": 22},
                ]
            },
        )

        result = proton_mail.apply_mark_read(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "mark-read")
        self.assertEqual(imap.calls[0], ("select", "INBOX", False))
        self.assertEqual(
            imap.calls[1], ("uid", "FETCH", "21:23", "(UID)")
        )
        stores = [call for call in imap.calls if call[:2] == ("uid", "STORE")]
        self.assertEqual(
            stores,
            [
                (
                    "uid",
                    "STORE",
                    "21:23",
                    "+FLAGS.SILENT",
                    r"(\Seen)",
                )
            ],
        )
        self.assertEqual(
            imap.calls[-1],
            ("uid", "FETCH", "21:23", "(UID FLAGS)"),
        )
        self.assertTrue(
            all(r"\Flagged" in imap.flags[uid] for uid in (21, 22, 23))
        )

    def test_mark_read_rejects_stale_identity_or_missing_uid_before_reserve(self):
        cases = (
            (BatchImap(uidvalidity=999), proton_mail.StaleMessageError),
            (BatchImap(uids=(21, 23)), proton_mail.StaleMessageError),
        )
        for index, (imap, error_type) in enumerate(cases):
            with self.subTest(index=index):
                plan = proton_mail.create_plan(
                    "mark-read",
                    {
                        "refs": [
                            {
                                "mailbox": "INBOX",
                                "uidvalidity": 812,
                                "uid": uid,
                            }
                            for uid in (21, 22, 23)
                        ]
                    },
                )
                ledger = Path(self.tempdir.name) / f"preflight-{index}.json"
                with self.assertRaises(error_type):
                    proton_mail.apply_mark_read(
                        imap,
                        plan,
                        plan["plan_hash"],
                        ledger_path=ledger,
                    )
                self.assertFalse(
                    any(call[:2] == ("uid", "STORE") for call in imap.calls)
                )
                self.assertFalse(ledger.exists())

    def test_mark_read_partial_verification_is_ambiguous_and_consumed(self):
        imap = BatchImap(mark_after_store=(21, 23))
        plan = proton_mail.create_plan(
            "mark-read",
            {
                "refs": [
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": uid}
                    for uid in (21, 22, 23)
                ]
            },
        )

        result = proton_mail.apply_mark_read(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "ambiguous")
        self.assertTrue(result["retry_prohibited"])
        self.assertEqual(result["unverified_uids"], [22])
        with self.assertRaisesRegex(proton_mail.PlanError, "used"):
            proton_mail.apply_mark_read(
                BatchImap(),
                plan,
                plan["plan_hash"],
                **self.apply_kwargs(),
            )

    def test_mark_read_store_failure_is_ambiguous_and_cli_nonzero(self):
        imap = BatchImap(store_status="NO")
        plan = proton_mail.create_plan(
            "mark-read",
            {
                "refs": [
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": 21}
                ]
            },
        )
        result = proton_mail.apply_mark_read(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "ambiguous")
        self.assertTrue(result["retry_prohibited"])

        stdout = io.StringIO()
        with mock.patch.object(
            proton_mail, "build_parser"
        ) as parser_factory, mock.patch.object(
            proton_mail.BridgeConfig, "from_env", return_value=config()
        ), mock.patch.object(
            proton_mail, "execute", return_value=result
        ):
            parser_factory.return_value.parse_args.return_value = (
                SimpleNamespace(command="apply-mark-read")
            )
            with redirect_stdout(stdout):
                status = proton_mail.main(["apply-mark-read"])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_mark_read_expiry_fails_before_select_and_replay_fails(self):
        expired = proton_mail.create_plan(
            "mark-read",
            {
                "refs": [
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": 21}
                ]
            },
            now=100,
            ttl_seconds=10,
        )
        imap = BatchImap()
        with self.assertRaisesRegex(proton_mail.PlanError, "expired"):
            proton_mail.apply_mark_read(
                imap, expired, expired["plan_hash"], **self.apply_kwargs()
            )
        self.assertEqual(imap.calls, [])

        plan = proton_mail.create_plan(
            "mark-read",
            {
                "refs": [
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": 21}
                ]
            },
        )
        proton_mail.apply_mark_read(
            BatchImap(), plan, plan["plan_hash"], **self.apply_kwargs()
        )
        with self.assertRaisesRegex(proton_mail.PlanError, "used"):
            proton_mail.apply_mark_read(
                BatchImap(), plan, plan["plan_hash"], **self.apply_kwargs()
            )

    def test_apply_move_selects_readwrite_and_verifies_uid_before_move(self):
        imap = FakeImap()
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )
        result = proton_mail.apply_move(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )
        self.assertEqual(result["action"], "move")
        self.assertEqual(imap.calls[0], ("select", "INBOX", False))
        self.assertEqual(imap.calls[1], ("uid", "FETCH", "23", "(UID)"))
        self.assertIn(("uid", "MOVE", "23", "Archive"), imap.calls)
        self.assertEqual(result["destination_uidvalidity"], 900)
        self.assertEqual(result["destination_uid"], 99)
        self.assertFalse(any(call[:2] == ("uid", "COPY") for call in imap.calls))
        self.assertFalse(any(call[:2] == ("uid", "STORE") for call in imap.calls))
        with self.assertRaisesRegex(proton_mail.SafetyError, "MOVE"):
            proton_mail.apply_move(
                FakeImap(capability=b"IMAP4rev1"),
                plan,
                plan["plan_hash"],
                **self.apply_kwargs(),
            )

    def test_move_requires_uidplus_and_copyuid_completion_mapping(self):
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )
        without_uidplus = FakeImap(capability=b"IMAP4rev1 MOVE")
        with self.assertRaisesRegex(proton_mail.SafetyError, "UIDPLUS"):
            proton_mail.apply_move(
                without_uidplus,
                plan,
                plan["plan_hash"],
                ledger_path=Path(self.tempdir.name) / "uidplus.json",
            )

        for response in (
            b"moved without mapping",
            b"[COPYUID 900 24 99] wrong source",
            b"[COPYUID 900 23 99:100] wrong destination",
        ):
            with self.subTest(response=response):
                imap = FakeImap()
                imap.move_response = response
                ledger = Path(self.tempdir.name) / (
                    hashlib.sha256(response).hexdigest() + ".json"
                )
                with self.assertRaisesRegex(
                    proton_mail.ProtocolError, "ambiguous"
                ):
                    proton_mail.apply_move(
                        imap,
                        plan,
                        plan["plan_hash"],
                        ledger_path=ledger,
                    )

    def test_move_accepts_stdlib_bare_copyuid_response_payload(self):
        class StdlibResponseImap(FakeImap):
            def response(self, code):
                self.calls.append(("response", code))
                return "COPYUID", [b"900 23 99"]

        imap = StdlibResponseImap()
        imap.move_response = b"moved"
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )

        result = proton_mail.apply_move(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )

        self.assertEqual(result["destination_uidvalidity"], 900)
        self.assertEqual(result["destination_uid"], 99)
        self.assertIn(("response", "COPYUID"), imap.calls)

    def test_disappearance_between_check_and_move_is_ambiguous_not_success(self):
        imap = FakeImap()
        imap.move_response = b"message vanished"
        plan = proton_mail.create_plan(
            "delete",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                }
            },
        )

        with self.assertRaisesRegex(proton_mail.ProtocolError, "ambiguous"):
            proton_mail.apply_delete(
                imap, plan, plan["plan_hash"], **self.apply_kwargs()
            )

    def test_move_rejects_missing_uid_without_reporting_success(self):
        imap = FakeImap(existing=False)
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )

        with self.assertRaisesRegex(proton_mail.StaleMessageError, "UID"):
            proton_mail.apply_move(
                imap, plan, plan["plan_hash"], **self.apply_kwargs()
            )
        self.assertFalse(any(call[:2] == ("uid", "MOVE") for call in imap.calls))

    def test_move_rechecks_stale_uidvalidity_in_readwrite_selection(self):
        imap = FakeImap(uidvalidity=999)
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )

        with self.assertRaises(proton_mail.StaleMessageError):
            proton_mail.apply_move(
                imap, plan, plan["plan_hash"], **self.apply_kwargs()
            )
        self.assertFalse(any(call[:2] == ("uid", "MOVE") for call in imap.calls))

    def test_delete_is_only_a_native_move_to_special_use_trash(self):
        imap = FakeImap()
        plan = proton_mail.create_plan(
            "delete",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                }
            },
        )
        proton_mail.apply_delete(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )
        self.assertIn(("uid", "MOVE", "23", "Trash"), imap.calls)
        self.assertFalse(
            any(
                any(str(value).upper() == "EXPUNGE" for value in call)
                for call in imap.calls
            )
        )

    def test_draft_is_appended_with_draft_flag_to_special_use_mailbox(self):
        imap = FakeImap()
        plan = proton_mail.create_plan(
            "save-draft",
            {
                "from": "sender@example.com",
                "to": ["receiver@example.com"],
                "subject": "Subject",
                "body": "Body",
            },
        )
        proton_mail.apply_save_draft(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )
        append = next(call for call in imap.calls if call[0] == "append")
        self.assertEqual(append[1:4], ("Drafts", r"(\Draft)", None))

    def test_send_keeps_bcc_in_envelope_only_and_rejects_header_injection(self):
        smtp = FakeSmtp()
        plan = proton_mail.create_plan(
            "send",
            {
                "from": "sender@example.com",
                "to": ["receiver@example.com"],
                "cc": [],
                "bcc": ["hidden@example.com"],
                "subject": "Subject",
                "body": "Body",
            },
        )
        proton_mail.apply_send(
            smtp, plan, plan["plan_hash"], **self.apply_kwargs()
        )
        sendmail = next(call for call in smtp.calls if call[0] == "sendmail")
        self.assertEqual(sendmail[1], "sender@example.com")
        self.assertEqual(
            sendmail[2],
            ["receiver@example.com", "hidden@example.com"],
        )
        parsed = BytesParser(policy=policy.default).parsebytes(sendmail[3])
        self.assertIsNone(parsed["Bcc"])
        with self.assertRaisesRegex(proton_mail.SafetyError, "header"):
            proton_mail.create_plan(
                "send",
                {
                    "from": "sender@example.com",
                    "to": ["receiver@example.com"],
                    "subject": "safe\r\nBcc: attacker@example.com",
                    "body": "Body",
                },
            )

    def test_partial_smtp_delivery_is_explicit_and_must_not_be_retried(self):
        smtp = FakeSmtp(
            refused={"bad@example.com": (550, b"mailbox unavailable")}
        )
        plan = proton_mail.create_plan(
            "send",
            {
                "from": "sender@example.com",
                "to": ["ok@example.com", "bad@example.com"],
                "subject": "Subject",
                "body": "Body",
            },
        )

        result = proton_mail.apply_send(
            smtp, plan, plan["plan_hash"], **self.apply_kwargs()
        )

        self.assertEqual(result["status"], "partial_delivery")
        self.assertEqual(result["accepted_recipients"], ["ok@example.com"])
        self.assertEqual(result["refused_recipients"], ["bad@example.com"])
        self.assertTrue(result["retry_prohibited"])

    def test_smtp_recipients_refused_exception_is_structured(self):
        smtp = FakeSmtp()
        smtp.sendmail = mock.Mock(
            side_effect=proton_mail.smtplib.SMTPRecipientsRefused(
                {"bad@example.com": (550, b"rejected")}
            )
        )
        plan = proton_mail.create_plan(
            "send",
            {
                "from": "sender@example.com",
                "to": ["bad@example.com"],
                "subject": "Subject",
                "body": "Body",
            },
        )

        result = proton_mail.apply_send(
            smtp, plan, plan["plan_hash"], **self.apply_kwargs()
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "all_recipients_refused")
        self.assertEqual(result["refused_recipients"], ["bad@example.com"])
        self.assertTrue(result["retry_prohibited"])

    def test_cli_returns_nonzero_for_structured_ok_false_result(self):
        fake_args = SimpleNamespace(command="status")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            proton_mail, "build_parser"
        ) as parser_factory, mock.patch.object(
            proton_mail.BridgeConfig, "from_env", return_value=config()
        ), mock.patch.object(
            proton_mail,
            "execute",
            return_value={
                "ok": False,
                "status": "all_recipients_refused",
            },
        ):
            parser_factory.return_value.parse_args.return_value = fake_args
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = proton_mail.main(["status"])

        self.assertEqual(status, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_plan_nonce_is_fresh_expiring_and_single_use(self):
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )
        self.assertRegex(plan["nonce"], r"^[A-Za-z0-9_-]{32,}$")
        self.assertGreater(plan["expires_at"], plan["created_at"])
        proton_mail.apply_move(
            FakeImap(), plan, plan["plan_hash"], **self.apply_kwargs()
        )

        with self.assertRaisesRegex(proton_mail.PlanError, "used"):
            proton_mail.apply_move(
                FakeImap(), plan, plan["plan_hash"], **self.apply_kwargs()
            )

        ledger = json.loads(self.ledger.read_text())
        serialized = json.dumps(ledger)
        self.assertNotIn("Archive", serialized)
        self.assertNotIn("recipient", serialized)
        self.assertNotIn("Body", serialized)
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)

    def test_ambiguous_move_failure_consumes_nonce_fail_closed(self):
        imap = FakeImap()
        original_uid = imap.uid

        def fail_move(command, *args):
            if command.upper() == "MOVE":
                raise proton_mail.ProtocolError("connection lost")
            return original_uid(command, *args)

        imap.uid = fail_move
        plan = proton_mail.create_plan(
            "move",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                },
                "destination": "Archive",
            },
        )

        with self.assertRaises(proton_mail.ProtocolError):
            proton_mail.apply_move(
                imap, plan, plan["plan_hash"], **self.apply_kwargs()
            )
        with self.assertRaisesRegex(proton_mail.PlanError, "used"):
            proton_mail.apply_move(
                FakeImap(), plan, plan["plan_hash"], **self.apply_kwargs()
            )

    def test_expired_plan_is_rejected(self):
        plan = proton_mail.create_plan(
            "delete",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                }
            },
            now=100,
            ttl_seconds=10,
        )
        with self.assertRaisesRegex(proton_mail.PlanError, "expired"):
            proton_mail.verify_plan(
                plan,
                expected_action="delete",
                supplied_hash=plan["plan_hash"],
                now=111,
            )

    def test_reservation_rechecks_expiry_inside_lock(self):
        plan = proton_mail.create_plan(
            "delete",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                }
            },
            now=100,
            ttl_seconds=10,
        )

        with self.assertRaisesRegex(proton_mail.PlanError, "expired"):
            proton_mail.reserve_plan(
                plan, ledger_path=self.ledger, now=111
            )
        if self.ledger.exists():
            self.assertNotIn(
                plan["nonce"], self.ledger.read_text(encoding="utf-8")
            )

    def test_concurrent_reservation_allows_exactly_one_consumer(self):
        plan = proton_mail.create_plan(
            "delete",
            {
                "ref": {
                    "mailbox": "INBOX",
                    "uidvalidity": 812,
                    "uid": 23,
                }
            },
        )
        barrier = threading.Barrier(2)
        outcomes = []

        def reserve():
            barrier.wait()
            try:
                proton_mail.reserve_plan(plan, ledger_path=self.ledger)
                outcomes.append("reserved")
            except proton_mail.PlanError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(sorted(outcomes), ["rejected", "reserved"])

    def test_message_and_plan_body_limits_apply_before_file_read(self):
        with self.assertRaisesRegex(proton_mail.LimitError, "body"):
            proton_mail.create_plan(
                "send",
                {
                    "from": "sender@example.com",
                    "to": ["receiver@example.com"],
                    "subject": "Subject",
                    "body": "x" * (proton_mail.MAX_BODY_BYTES + 1),
                },
            )
        body = Path(self.tempdir.name) / "body.txt"
        body.write_bytes(b"x" * (proton_mail.MAX_BODY_BYTES + 1))
        args = SimpleNamespace(
            body_file=str(body),
            body=None,
            sender="sender@example.com",
            to=["receiver@example.com"],
            cc=[],
            bcc=[],
            subject="Subject",
        )
        with self.assertRaisesRegex(proton_mail.LimitError, "body"):
            proton_mail._message_operation(args)
        with self.assertRaisesRegex(proton_mail.LimitError, "plan"):
            proton_mail.load_plan("x" * (proton_mail.MAX_PLAN_BYTES + 1))

    def test_argparse_failure_emits_exactly_one_json_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = proton_mail.main(["read"])

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        error = json.loads(lines[0])
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"], "CLIError")

    def test_status_closes_imap_when_smtp_connection_fails(self):
        imap = FakeImap()
        args = SimpleNamespace(command="status")
        with mock.patch.object(
            proton_mail, "connect_imap", return_value=imap
        ), mock.patch.object(
            proton_mail,
            "connect_smtp",
            side_effect=proton_mail.ProtocolError("SMTP unavailable"),
        ):
            with self.assertRaises(proton_mail.ProtocolError):
                proton_mail.execute(args, config())
        self.assertIn(("logout",), imap.calls)

    def test_special_use_mailbox_ambiguity_is_rejected(self):
        imap = FakeImap(
            list_rows=[
                b'(\\Trash) "/" "Trash"',
                b'(\\Trash) "/" "Deleted Items"',
            ]
        )
        with self.assertRaisesRegex(proton_mail.SafetyError, "ambiguous"):
            proton_mail.special_use_mailboxes(imap)

    def test_modified_utf7_mailbox_round_trip(self):
        encoded = proton_mail.encode_mailbox("旅行 & Receipts")
        self.assertTrue(encoded.isascii())
        self.assertEqual(
            proton_mail.decode_mailbox(encoded.encode()),
            "旅行 & Receipts",
        )

    def test_nonascii_structured_search_enables_utf8_accept(self):
        for kwargs in (
            {"from_address": "josé@example.com"},
            {"subject": "réunion"},
            {"text": "café"},
        ):
            with self.subTest(kwargs=kwargs):
                imap = FakeImap()
                criteria = proton_mail.build_search_criteria(**kwargs)
                proton_mail.search_messages(imap, "INBOX", criteria)
                self.assertIn(("enable", "UTF8=ACCEPT"), imap.calls)
                search_call = next(
                    call
                    for call in imap.calls
                    if call[:2] == ("uid", "SEARCH")
                )
                self.assertTrue(any(not value.isascii() for value in search_call[3:]))

    def test_nonascii_search_without_utf8_accept_fails_clearly(self):
        imap = FakeImap(capability=b"IMAP4rev1")
        criteria = proton_mail.build_search_criteria(subject="réunion")

        with self.assertRaisesRegex(
            proton_mail.ProtocolError, "UTF8=ACCEPT"
        ):
            proton_mail.search_messages(imap, "INBOX", criteria)

    def test_certificate_failure_never_attempts_login(self):
        fake = FakeImap()

        def reject_certificate(ssl_context=None):
            fake.calls.append(("starttls", ssl_context))
            raise ssl.SSLCertVerificationError("hostname mismatch")

        fake.starttls = reject_certificate
        with mock.patch.object(
            proton_mail.imaplib, "IMAP4", lambda **kwargs: fake
        ):
            with self.assertRaises(ssl.SSLCertVerificationError):
                proton_mail.connect_imap(config())
        self.assertFalse(any(call[0] == "login" for call in fake.calls))

    def test_skill_documents_untrusted_boundary_and_private_temp_files(self):
        skill = (
            SCRIPT.parent.parent / "SKILL.md"
        ).read_text(encoding="utf-8")
        for required in (
            "untrusted",
            "inert data",
            "fresh direct user message",
            "umask 077",
            "mktemp",
            "trap",
            "0600",
            "partial delivery",
            "Do not retry",
            "plan-mark-read",
            "+FLAGS.SILENT",
            "1 to 500",
        ):
            self.assertIn(required, skill)
        self.assertNotIn("/tmp/proton-plan.json", skill)
        self.assertNotIn("/tmp/proton-body.txt", skill)

    def test_credentials_are_redacted_from_errors_and_json(self):
        cfg = config()
        result = proton_mail.error_result(
            RuntimeError("login failed for bridge-user with bridge-secret"),
            cfg,
        )
        encoded = json.dumps(result)
        self.assertNotIn("bridge-secret", encoded)
        self.assertNotIn("bridge-user", encoded)
        self.assertIn("[REDACTED]", encoded)
