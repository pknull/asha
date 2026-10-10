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
        self.post_login_capability = None
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
        self.list_rows = list_rows if list_rows is not None else [
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

    def capability(self):
        self.calls.append(("capability",))
        advertised = self.post_login_capability
        if advertised is None:
            advertised = b" ".join(self.capabilities)
        return "OK", [advertised]

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

    def list(self, *args):
        self.calls.append(("list", *args))
        return "OK", self.list_rows

    def create(self, mailbox):
        self.calls.append(("create", mailbox))
        self.list_rows.append(f'() "/" {mailbox}'.encode("ascii"))
        return "OK", [b"created"]

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
    """Protocol-shaped fake for batch UID FETCH/STORE/FLAGS/MOVE operations."""

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
        self.move_status = "OK"
        self.move_response = b"[COPYUID 900 21:23 99:101] moved"
        self.list_rows.extend([
            b'() "/" "INBOX"',
            b'(\\Archive) "/" "Archive"',
            b'() "/" "Folders/X"',
        ])

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        command = command.upper()
        uid_set = args[0] if args else ""
        requested = proton_mail.expand_uid_set(uid_set)
        if command == "MOVE":
            return self.move_status, [self.move_response]
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

    def batch_operation(self, action="move-batch", mailbox="INBOX"):
        operation = {
            "refs": [
                {"mailbox": mailbox, "uidvalidity": 812, "uid": uid}
                for uid in (23, 21, 22)
            ]
        }
        if action == "move-batch":
            operation["destination"] = "Folders/X"
        return operation

    def test_new_batch_plans_validate_references(self):
        for action in ("move-batch", "delete-batch"):
            bad_refs = [
                [], [{"mailbox": "INBOX", "uidvalidity": 812, "uid": 0}],
                [{"mailbox": "INBOX", "uidvalidity": 812, "uid": uid}
                 for uid in range(1, 502)],
            ]
            for field, value in (
                ("uid", 23), ("uid", True), ("uid", "21"),
                ("mailbox", "Archive"), ("uidvalidity", 999),
            ):
                refs = self.batch_operation(action)["refs"]
                refs[1][field] = value
                bad_refs.append(refs)
            for refs in bad_refs:
                with self.subTest(action=action, refs=refs[:2]):
                    operation = self.batch_operation(action)
                    operation["refs"] = refs
                    with self.assertRaises(proton_mail.PlanError):
                        proton_mail.create_plan(action, operation)
            plan = proton_mail.create_plan(action, self.batch_operation(action))
            self.assertEqual(
                [ref["uid"] for ref in plan["operation"]["refs"]], [21, 22, 23]
            )
            plan["operation"]["refs"].reverse()
            plan["plan_hash"] = proton_mail.plan_digest({
                key: value for key, value in plan.items() if key != "plan_hash"
            })
            with self.assertRaisesRegex(proton_mail.PlanError, "canonical"):
                proton_mail.verify_plan(
                    plan, expected_action=action, supplied_hash=plan["plan_hash"]
                )

    def test_move_batch_destination_validation(self):
        for destination in ("", "  ", None, 123, "X\r", "\nX", "X\0", "X\t", "X\x7f"):
            with self.subTest(destination=destination):
                operation = self.batch_operation()
                operation["destination"] = destination
                with self.assertRaises(proton_mail.PlanError):
                    proton_mail.create_plan("move-batch", operation)
        operation = self.batch_operation()
        operation["destination"] = " Folders/X "
        self.assertEqual(
            proton_mail.create_plan("move-batch", operation)["operation"]["destination"],
            "Folders/X",
        )

    def test_move_batch_cli_round_trip_is_offline(self):
        stdout = io.StringIO()
        with mock.patch.object(proton_mail, "connect_imap") as imap, \
                mock.patch.object(proton_mail, "connect_smtp") as smtp, \
                mock.patch.object(proton_mail.BridgeConfig, "from_env") as env, \
                redirect_stdout(stdout):
            status = proton_mail.main([
                "plan-move-batch", "--mailbox", "INBOX", "--uidvalidity", "812",
                "--uid", "23", "--uid", "21", "--uid", "22",
                "--destination", "Folders/X",
            ])
        self.assertEqual(status, 0)
        imap.assert_not_called()
        smtp.assert_not_called()
        env.assert_not_called()
        plan = proton_mail.load_plan(stdout.getvalue())
        operation = proton_mail.verify_plan(
            plan, expected_action="move-batch", supplied_hash=plan["plan_hash"]
        )
        self.assertEqual([ref["uid"] for ref in operation["refs"]], [21, 22, 23])
        self.assertEqual(operation["destination"], "Folders/X")

    def test_batch_move_success_and_destination_policy(self):
        for destination, source in (
            ("Folders/X", "INBOX"), ("Archive", "INBOX"), ("INBOX", "Folders/X"),
        ):
            with self.subTest(destination=destination):
                imap = BatchImap()
                operation = self.batch_operation(mailbox=source)
                operation["destination"] = destination
                plan = proton_mail.create_plan("move-batch", operation)
                original_uid = imap.uid

                def checked_uid(command, *args):
                    if command == "MOVE":
                        self.assertIn(plan["nonce"], self.ledger.read_text())
                    else:
                        if self.ledger.exists():
                            self.assertNotIn(plan["nonce"], self.ledger.read_text())
                    return original_uid(command, *args)

                imap.uid = checked_uid
                result = proton_mail.apply_move_batch(
                    imap, plan, plan["plan_hash"], **self.apply_kwargs()
                )
                self.assertEqual(result, {
                    "ok": True, "status": "moved", "action": "move-batch",
                    "refs": plan["operation"]["refs"], "destination": destination,
                    "destination_uidvalidity": 900, "destination_uids": [99, 100, 101],
                })
                self.assertEqual(
                    [call for call in imap.calls if call[:2] == ("uid", "MOVE")],
                    [("uid", "MOVE", "21:23", destination)],
                )
                self.assertIn(("select", source, False), imap.calls)
                self.assertIn(("uid", "FETCH", "21:23", "(UID)"), imap.calls)

    def test_move_batch_policy_failures_leave_ledger_untouched(self):
        for destination, flags, exists in (
            ("Trash", r"\Trash", True), ("Labels/X", r"\Archive", True),
            ("Sent", r"\Sent", True), ("Folders/X", r"\Noselect", True),
            ("INBOX", "", True), ("inbox", "", True), ("Missing", "", False),
            ("Other", "", True),
            *(("Folders/X", flag, True) for flag in (
                r"\Trash", r"\Drafts", r"\Sent", r"\All", r"\Junk", r"\Flagged",
            )),
        ):
            with self.subTest(destination=destination, flags=flags):
                imap = BatchImap()
                imap.list_rows = (
                    [f'({flags}) "/" "{destination}"'.encode()] if exists else []
                )
                operation = self.batch_operation()
                operation["destination"] = destination
                plan = proton_mail.create_plan("move-batch", operation)
                with self.assertRaises(proton_mail.SafetyError):
                    proton_mail.apply_move_batch(
                        imap, plan, plan["plan_hash"], **self.apply_kwargs()
                    )
                self.assertFalse(self.ledger.exists())
                self.assertFalse(any(call[0] in ("select", "uid") for call in imap.calls))

    def test_batch_moves_reject_stale_refs_and_missing_capabilities_before_reserve(self):
        for action in ("move-batch", "delete-batch"):
            for failure in ("validity", "missing", "MOVE", "UIDPLUS"):
                with self.subTest(action=action, failure=failure):
                    imap = BatchImap(
                        uidvalidity=999 if failure == "validity" else 812,
                        uids=(21, 23) if failure == "missing" else (21, 22, 23),
                    )
                    if failure in ("MOVE", "UIDPLUS"):
                        imap.capabilities = tuple(
                            value for value in imap.capabilities if value != failure.encode()
                        )
                    plan = proton_mail.create_plan(action, self.batch_operation(action))
                    apply = getattr(proton_mail, "apply_" + action.replace("-", "_"))
                    error = (
                        proton_mail.SafetyError if failure in ("MOVE", "UIDPLUS")
                        else proton_mail.StaleMessageError
                    )
                    with self.assertRaises(error):
                        apply(imap, plan, plan["plan_hash"], **self.apply_kwargs())
                    self.assertFalse(self.ledger.exists())
                    self.assertFalse(any(call[:2] == ("uid", "MOVE") for call in imap.calls))

    def test_batch_move_ambiguous_results_consume_nonce(self):
        for action in ("move-batch", "delete-batch"):
            for response, status in (
                (b"[COPYUID 900 21:22 99:101]", "OK"),
                (b"[COPYUID 900 21:23 99:100]", "OK"),
                (b"[COPYUID 0 21:23 99:101]", "OK"),
                (b"moved", "OK"), (b"failed", "NO"), (b"failed", "BAD"),
                (b"[COPYUID 900 21:23 99:101]", "exception"),
            ):
                with self.subTest(action=action, response=response, status=status):
                    imap = BatchImap()
                    imap.move_response, imap.move_status = response, status
                    if status == "exception":
                        original_uid = imap.uid

                        def fail_move(command, *args):
                            if command == "MOVE":
                                raise OSError("connection lost")
                            return original_uid(command, *args)

                        imap.uid = fail_move
                    plan = proton_mail.create_plan(action, self.batch_operation(action))
                    apply = getattr(proton_mail, "apply_" + action.replace("-", "_"))
                    result = apply(imap, plan, plan["plan_hash"], **self.apply_kwargs())
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], "ambiguous")
                    self.assertEqual(result["action"], action)
                    self.assertEqual(result["refs"], plan["operation"]["refs"])
                    self.assertTrue(result["retry_prohibited"])
                    self.assertIn(plan["nonce"], self.ledger.read_text())
                    with self.assertRaisesRegex(proton_mail.PlanError, "used"):
                        apply(BatchImap(), plan, plan["plan_hash"], **self.apply_kwargs())

    def test_batch_copyuid_stdlib_payload_and_noncontiguous_sets(self):
        imap = BatchImap(uids=(21, 22, 24))
        imap.move_response = b"moved"
        imap.response = mock.Mock(return_value=("COPYUID", [b"901 21:22,24 99,101:102"]))
        operation = self.batch_operation()
        operation["refs"][0]["uid"] = 24
        plan = proton_mail.create_plan("move-batch", operation)
        result = proton_mail.apply_move_batch(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["destination_uids"], [99, 101, 102])
        self.assertEqual(result["destination_uidvalidity"], 901)
        self.assertIn(("uid", "MOVE", "21:22,24", "Folders/X"), imap.calls)
        imap.response.assert_called_once_with("COPYUID")

    def test_delete_batch_resolves_trash_without_expunge_or_fallback(self):
        imap = BatchImap()
        imap.list_rows = [b'(\\Trash) "/" "Deleted Items"']
        plan = proton_mail.create_plan("delete-batch", self.batch_operation("delete-batch"))
        result = proton_mail.apply_delete_batch(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["destination"], "Deleted Items")
        self.assertEqual(
            [call for call in imap.calls if call[:2] == ("uid", "MOVE")],
            [("uid", "MOVE", "21:23", '"Deleted Items"')],
        )
        self.assertFalse(any(
            str(value).upper() in {"EXPUNGE", "COPY", "STORE"}
            for call in imap.calls for value in call
        ))

    def test_delete_batch_rejects_trash_source_absent_or_ambiguous_trash(self):
        for source, rows in (
            ("Trash", [b'(\\Trash) "/" "Trash"']),
            ("INBOX", []),
            ("INBOX", [b'(\\Trash) "/" "Trash"', b'(\\Trash) "/" "Deleted"']),
            ("INBOX", [b'(\\Trash \\Noselect) "/" "Trash"']),
        ):
            with self.subTest(source=source, rows=rows):
                imap = BatchImap()
                imap.list_rows = rows
                plan = proton_mail.create_plan(
                    "delete-batch", self.batch_operation("delete-batch", source)
                )
                with self.assertRaises(proton_mail.SafetyError):
                    proton_mail.apply_delete_batch(
                        imap, plan, plan["plan_hash"], **self.apply_kwargs()
                    )
                self.assertFalse(self.ledger.exists())
                self.assertFalse(any(call[0] in ("select", "uid") for call in imap.calls))

    def test_create_folder_name_validation(self):
        for name in ("", " ", "a/b", "x" * 101, "\nx", "x\r", "x\0", "x\t", "x\x7f", None, 2):
            with self.subTest(name=name):
                with self.assertRaises(proton_mail.PlanError):
                    proton_mail.create_plan("create-folder", {"name": name})
        for name in ("x" * 100, "旅行 & Receipts"):
            plan = proton_mail.create_plan("create-folder", {"name": f" {name} "})
            self.assertEqual(plan["operation"], {"name": name})

    def test_create_folder_refuses_existing_before_reservation(self):
        imap = FakeImap(list_rows=[b'(\\Noselect) "/" "Folders/X"'])
        plan = proton_mail.create_plan("create-folder", {"name": "X"})
        with self.assertRaisesRegex(proton_mail.SafetyError, "folder already exists"):
            proton_mail.apply_create_folder(
                imap, plan, plan["plan_hash"], **self.apply_kwargs()
            )
        self.assertFalse(self.ledger.exists())
        self.assertEqual(imap.calls, [("list", '""', "*")])

    def test_create_folder_refuses_name_shared_with_label_before_reservation(self):
        # Proton rejects a folder whose name matches any label or folder
        # (409, code 2500), wherever it sits in the hierarchy.
        for row in (
            b'(\\Marked) "/" "Labels/Bills"',
            b'(\\Marked) "/" "Labels/bills"',
            b'(\\Unmarked) "/" "Folders/Parent/Bills"',
        ):
            with self.subTest(row=row):
                imap = FakeImap(list_rows=[row])
                plan = proton_mail.create_plan("create-folder", {"name": "Bills"})
                with self.assertRaisesRegex(
                    proton_mail.SafetyError, "label or folder named"
                ):
                    proton_mail.apply_create_folder(
                        imap, plan, plan["plan_hash"], **self.apply_kwargs()
                    )
                self.assertFalse(self.ledger.exists())
                self.assertEqual(imap.calls, [("list", '""', "*")])

    def test_create_folder_verifies_list_after_create(self):
        imap = FakeImap(list_rows=[])
        name = '旅行 & "Receipts"'
        plan = proton_mail.create_plan("create-folder", {"name": name})
        original_create = imap.create

        def checked_create(mailbox):
            self.assertIn(plan["nonce"], self.ledger.read_text())
            return original_create(mailbox)

        imap.create = checked_create
        result = proton_mail.apply_create_folder(
            imap, plan, plan["plan_hash"], **self.apply_kwargs()
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["action"], "create-folder")
        self.assertEqual(result["mailbox"], "Folders/" + name)
        self.assertEqual(imap.calls, [
            ("list", '""', "*"),
            ("create", proton_mail.imap_mailbox_arg("Folders/" + name)),
            ("list", '""', "*"),
        ])

    def test_create_folder_unverified_or_failed_create_is_ambiguous(self):
        for failure in ("absent", "noselect", "NO", "exception", "list-error"):
            with self.subTest(failure=failure):
                imap = FakeImap(list_rows=[])
                plan = proton_mail.create_plan("create-folder", {"name": "X"})

                def create(mailbox):
                    imap.calls.append(("create", mailbox))
                    if failure == "exception":
                        raise OSError("connection lost")
                    if failure == "noselect":
                        imap.list_rows = [b'(\\Noselect) "/" "Folders/X"']
                    if failure == "list-error":
                        imap.list = mock.Mock(side_effect=OSError("connection lost"))
                    return ("NO" if failure == "NO" else "OK"), [b"done"]

                imap.create = create
                result = proton_mail.apply_create_folder(
                    imap, plan, plan["plan_hash"], **self.apply_kwargs()
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "ambiguous")
                self.assertTrue(result["retry_prohibited"])
                self.assertIn(plan["nonce"], self.ledger.read_text())
                with self.assertRaisesRegex(proton_mail.PlanError, "used"):
                    proton_mail.apply_create_folder(
                        FakeImap(), plan, plan["plan_hash"], **self.apply_kwargs()
                    )

    def test_mailboxes_decodes_names_and_selectability_via_cli(self):
        name = "Folders/旅行 & Receipts"
        imap = FakeImap(list_rows=[
            b'(\\HasChildren \\Noselect) "/" "Folders"',
            f'(\\HasNoChildren) "/" "{proton_mail.encode_mailbox(name)}"'.encode(),
            b'() NIL INBOX',
        ])
        with mock.patch.object(proton_mail, "connect_imap", return_value=imap):
            result = proton_mail.execute(
                proton_mail.build_parser().parse_args(["mailboxes"]), config()
            )
        self.assertEqual(result, {"ok": True, "mailboxes": [
            {"name": "Folders", "flags": [r"\HasChildren", r"\Noselect"], "selectable": False},
            {"name": name, "flags": [r"\HasNoChildren"], "selectable": True},
            {"name": "INBOX", "flags": [], "selectable": True},
        ]})
        self.assertEqual(imap.calls, [("list", '""', "*"), ("logout",)])

    def test_new_actions_expire_before_network_or_reservation(self):
        for action in ("move-batch", "delete-batch", "create-folder"):
            with self.subTest(action=action):
                operation = (
                    {"name": "X"} if action == "create-folder" else self.batch_operation(action)
                )
                plan = proton_mail.create_plan(action, operation, now=100, ttl_seconds=10)
                imap = BatchImap()
                apply = getattr(proton_mail, "apply_" + action.replace("-", "_"))
                with self.assertRaisesRegex(proton_mail.PlanError, "expired"):
                    apply(imap, plan, plan["plan_hash"], **self.apply_kwargs())
                self.assertEqual(imap.calls, [])
                self.assertFalse(self.ledger.exists())

    def test_new_apply_cli_dispatches_and_exits_nonzero_on_ambiguity(self):
        for action in ("move-batch", "delete-batch", "create-folder"):
            with self.subTest(action=action):
                operation = (
                    {"name": "X"} if action == "create-folder" else self.batch_operation(action)
                )
                plan = proton_mail.create_plan(action, operation)
                imap = BatchImap()
                imap.move_status = "NO"
                if action == "create-folder":
                    imap.list_rows = []
                imap.create = mock.Mock(return_value=("NO", [b"failed"]))
                stdout = io.StringIO()
                with mock.patch.object(proton_mail, "connect_imap", return_value=imap), \
                        mock.patch.object(proton_mail.BridgeConfig, "from_env", return_value=config()), \
                        mock.patch.object(proton_mail, "default_ledger_path", return_value=self.ledger), \
                        redirect_stdout(stdout):
                    status = proton_mail.main([
                        "apply-" + action, "--plan", json.dumps(plan),
                        "--plan-hash", plan["plan_hash"],
                    ])
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(stdout.getvalue())["status"], "ambiguous")
                self.assertIn(("logout",), imap.calls)

    def test_delete_batch_and_create_folder_planning_cli_are_offline(self):
        for argv, action, expected in (
            (
                ["plan-delete-batch", "--mailbox", "INBOX", "--uidvalidity", "812",
                 "--uid", "22", "--uid", "21"],
                "delete-batch",
                {"refs": [
                    {"mailbox": "INBOX", "uidvalidity": 812, "uid": uid}
                    for uid in (21, 22)
                ]},
            ),
            (
                ["plan-create-folder", "--name", " Receipts "],
                "create-folder", {"name": "Receipts"},
            ),
        ):
            with self.subTest(action=action):
                with mock.patch.object(proton_mail, "connect_imap") as connect:
                    plan = proton_mail.execute(proton_mail.build_parser().parse_args(argv))
                connect.assert_not_called()
                self.assertEqual(plan["action"], action)
                self.assertEqual(plan["operation"], expected)

    def test_batch_move_boundary_sizes(self):
        for count in (1, 500):
            with self.subTest(count=count):
                uids = list(range(1, count + 1))
                operation = {
                    "refs": [
                        {"mailbox": "INBOX", "uidvalidity": 812, "uid": uid}
                        for uid in reversed(uids)
                    ],
                    "destination": "Folders/X",
                }
                plan = proton_mail.create_plan("move-batch", operation)
                imap = BatchImap(uids=uids)
                source = "1" if count == 1 else f"1:{count}"
                destination = "1001" if count == 1 else f"1001:{1000 + count}"
                imap.move_response = f"[COPYUID 900 {source} {destination}] moved".encode()
                result = proton_mail.apply_move_batch(
                    imap, plan, plan["plan_hash"], **self.apply_kwargs()
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["destination_uids"], list(range(1001, 1001 + count)))
                self.assertEqual(
                    [call for call in imap.calls if call[:2] == ("uid", "MOVE")],
                    [("uid", "MOVE", source, "Folders/X")],
                )

    def test_new_actions_reject_noncanonical_fields_and_rehashed_names(self):
        for action in ("move-batch", "delete-batch", "create-folder"):
            with self.subTest(action=action):
                operation = (
                    {"name": "X"} if action == "create-folder" else self.batch_operation(action)
                )
                with self.assertRaises(proton_mail.PlanError):
                    proton_mail.create_plan(action, {**operation, "extra": True})
                plan = proton_mail.create_plan(action, operation)
                if action == "create-folder":
                    plan["operation"]["name"] = " X "
                elif action == "move-batch":
                    plan["operation"]["destination"] = " Folders/X "
                else:
                    plan["operation"]["refs"][0]["extra"] = True
                plan["plan_hash"] = proton_mail.plan_digest({
                    key: value for key, value in plan.items() if key != "plan_hash"
                })
                imap = BatchImap()
                apply = getattr(proton_mail, "apply_" + action.replace("-", "_"))
                with self.assertRaisesRegex(proton_mail.PlanError, "canonical"):
                    apply(imap, plan, plan["plan_hash"], **self.apply_kwargs())
                self.assertEqual(imap.calls, [])
                self.assertFalse(self.ledger.exists())

    def test_mailboxes_literal_names_and_list_failure(self):
        imap = FakeImap(list_rows=[(b'() "/" {9}', b"Folders/X"), b""])
        self.assertEqual(proton_mail.list_mailboxes(imap), [
            {"name": "Folders/X", "flags": [], "selectable": True},
        ])
        imap.list = mock.Mock(return_value=("NO", [b"unavailable"]))
        with self.assertRaises(proton_mail.ProtocolError):
            proton_mail.list_mailboxes(imap)

    def test_preflight_list_failure_does_not_reserve_or_create(self):
        for rows in ([b"malformed LIST row"], None):
            with self.subTest(rows=rows):
                imap = FakeImap()
                imap.list = mock.Mock(return_value=("OK" if rows else "NO", rows))
                plan = proton_mail.create_plan("create-folder", {"name": "X"})
                with self.assertRaises(proton_mail.ProtocolError):
                    proton_mail.apply_create_folder(
                        imap, plan, plan["plan_hash"], **self.apply_kwargs()
                    )
                self.assertFalse(self.ledger.exists())
                self.assertFalse(imap.calls)

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

    def test_connect_refreshes_capabilities_advertised_after_login(self):
        # Bridge advertises MOVE and UIDPLUS only once authenticated; imaplib
        # keeps the pre-login list unless asked again.
        fake = FakeImap(capability=b"IMAP4rev1 STARTTLS AUTH=PLAIN")
        fake.post_login_capability = b"IMAP4rev1 MOVE STARTTLS UIDPLUS UNSELECT"
        with mock.patch.object(
            proton_mail.imaplib, "IMAP4", lambda **kwargs: fake
        ):
            client = proton_mail.connect_imap(config())

        self.assertEqual(
            [call[0] for call in fake.calls[:3]],
            ["starttls", "login", "capability"],
        )
        proton_mail.require_move(client)
        self.assertIn("UIDPLUS", proton_mail._capabilities(client))

    def test_connect_fails_closed_when_capability_refresh_fails(self):
        fake = FakeImap()
        fake.capability = lambda: ("NO", [b"unavailable"])
        with mock.patch.object(
            proton_mail.imaplib, "IMAP4", lambda **kwargs: fake
        ):
            with self.assertRaises(proton_mail.ProtonMailError):
                proton_mail.connect_imap(config())

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
            "mailboxes",
            "plan-move-batch",
            "apply-move-batch",
            "plan-delete-batch",
            "apply-delete-batch",
            "plan-create-folder",
            "apply-create-folder",
            "destination_uids",
            "Labels/",
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

    def _delete_plan(self):
        return proton_mail.create_plan(
            "delete",
            {"ref": {"mailbox": "INBOX", "uidvalidity": 812, "uid": 23}},
        )

    def test_existing_ledger_state_is_not_refused_for_its_modes(self):
        for broad in ("directory", "lock", "ledger"):
            with self.subTest(broad=broad):
                state = Path(self.tempdir.name) / f"state-{broad}"
                state.mkdir(mode=0o700)
                ledger = state / "replay-ledger.json"
                ledger.write_text('{"version": 1, "reservations": {}}')
                ledger.chmod(0o600)
                lock = state / "replay-ledger.json.lock"
                lock.write_text("")
                lock.chmod(0o600)
                {"directory": state, "lock": lock, "ledger": ledger}[broad].chmod(
                    0o755 if broad == "directory" else 0o644
                )
                plan = self._delete_plan()

                proton_mail.reserve_plan(plan, ledger_path=ledger)

                self.assertIn(plan["nonce"], ledger.read_text(encoding="utf-8"))
                with self.assertRaisesRegex(proton_mail.PlanError, "used"):
                    proton_mail.reserve_plan(plan, ledger_path=ledger)

    def test_new_ledger_state_is_created_private(self):
        ledger = Path(self.tempdir.name) / "fresh" / "replay-ledger.json"
        proton_mail.reserve_plan(self._delete_plan(), ledger_path=ledger)
        self.assertEqual(ledger.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        lock = ledger.with_name(ledger.name + ".lock")
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
