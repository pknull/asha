"""Read-only evidence for retired initiatives: list, show and export.

The legacy initiative engine is retired (Keeper, 2026-10-05, K1). Its records
stay in the Control database as evidence; these verbs only read them.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from lib.control import cli, initiative_evidence
from lib.control.config import load_config
from lib.control.database import DATABASE_NAME, ControlDatabase


INITIATIVE_A = "11111111-1111-4111-8111-111111111111"
INITIATIVE_B = "22222222-2222-4222-8222-222222222222"


def _raw(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


class InitiativeEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        self.path = self.config.asha_home / "state/control" / DATABASE_NAME

    def _seed(self, *, children: int = 1200) -> int:
        """Write initiatives, their child records and unrelated domains."""
        rows = []
        for initiative_id, slug, state in (
            (INITIATIVE_A, "first-smoke", "archived"),
            (INITIATIVE_B, "second-fix", "integrated"),
        ):
            rows.append(("initiatives", "registry", initiative_id, {
                "contract": "asha.orchestration-initiative.v2",
                "initiative_id": initiative_id, "slug": slug, "label": slug.replace("-", " "),
                "objective": "Evidence for " + slug, "state": state,
                "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-11T00:00:00Z",
            }))
        for index in range(children):
            scope = INITIATIVE_A if index % 3 else INITIATIVE_B
            domain = ("initiative.events", "initiative.actions", "initiative.evidence")[index % 3]
            # Event keys carry the schema's sequence-uuid shape (database v4).
            key = (f"{index + 1:06d}-{uuid.UUID(int=index)}.json"
                   if domain == "initiative.events" else f"{index:06d}.json")
            rows.append((domain, scope, key,
                         {"sequence": index, "state": "completed", "text": "é" * (index % 7)}))
        rows.append(("initiative.seals", INITIATIVE_B, "seal-1.json", {
            "seal_id": "seal-1", "initiative_id": INITIATIVE_B, "node_id": "implementation",
            "outcome": "success", "jj_commit_id": "91dc54368d0b759a393c64e77b29cc5f90ddd5de",
            "sealed_at": "2026-09-06T07:58:53Z", "state": "sealed",
        }))
        rows.append(("tasks", "registry", "task-1", {"state": "archived", "label": "orch worker"}))
        rows.append(("rooms", "registry", "room-1", {"state": "ended"}))
        rows.append(("authorities", "registry", "authority-1", {"state": "active", "label": "plane"}))
        with ControlDatabase(self.config, create=True) as db:
            with db.transaction(write=True) as c:
                for domain, scope, key, value in rows:
                    raw = _raw(value)
                    c.execute(
                        "INSERT INTO records(domain,scope,record_key,payload,digest,revision,state,updated_at,search_text)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (domain, scope, key, raw, hashlib.sha256(raw.encode()).hexdigest(), 1,
                         value.get("state", ""), value.get("updated_at", ""), raw),
                    )
        return len(rows)

    def _table(self) -> list[tuple]:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return connection.execute(
                "SELECT record_id,domain,scope,record_key,payload,digest,revision,state,updated_at"
                " FROM records ORDER BY record_id"
            ).fetchall()
        finally:
            connection.close()

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = initiative_evidence.main(list(args), env=self.env)
        return code, out.getvalue(), err.getvalue()

    def test_asha_initiative_export_reaches_the_reader(self):
        expected = self._seed(children=5)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["initiative", "export"], env=self.env)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(len(out.getvalue().splitlines()), expected)

    def test_export_returns_every_record_byte_for_byte(self):
        expected = self._seed()
        before = self._table()
        self.assertEqual(len(before), expected)

        code, out, err = self._run("export")

        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), expected)
        exported = [json.loads(line) for line in lines]
        self.assertEqual(
            [(r["record_id"], r["domain"], r["scope"], r["record_key"], r["payload"], r["digest"],
              r["revision"], r["state"], r["updated_at"]) for r in exported],
            [tuple(row) for row in before],
        )
        for record in exported:
            self.assertEqual(hashlib.sha256(record["payload"].encode("utf-8")).hexdigest(), record["digest"])
        self.assertIn(f"exported {expected} records", err)
        # Reading is all it does: the evidence is left exactly in place.
        self.assertEqual(self._table(), before)

    def test_export_opens_the_database_read_only(self):
        self._seed(children=3)
        opened = []
        real = ControlDatabase

        def spy(config, **options):
            opened.append(options)
            return real(config, **options)

        with patch("lib.control.initiative_evidence.ControlDatabase", side_effect=spy):
            for verb in (["export"], ["list"], ["show", INITIATIVE_A]):
                code, _out, err = self._run(*verb)
                self.assertEqual(code, 0, err)
        self.assertEqual(opened, [{"read_only": True}] * 3)

    def test_list_names_every_initiative_with_its_state(self):
        self._seed(children=3)
        code, out, err = self._run("list", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["contract"], "asha.initiative-evidence-list.v1")
        self.assertEqual(
            sorted((i["initiative_id"], i["slug"], i["state"]) for i in payload["initiatives"]),
            [(INITIATIVE_A, "first-smoke", "archived"), (INITIATIVE_B, "second-fix", "integrated")],
        )
        code, out, err = self._run("list")
        self.assertEqual(code, 0, err)
        self.assertIn("first-smoke", out)
        self.assertIn("integrated", out)

    def test_show_by_id_or_slug_counts_child_records_and_names_seal_commits(self):
        self._seed(children=30)
        code, out, err = self._run("show", "second-fix", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["contract"], "asha.initiative-evidence-show.v1")
        self.assertEqual(payload["initiative"]["initiative_id"], INITIATIVE_B)
        self.assertEqual(payload["records"], {"initiative.events": 10, "initiative.seals": 1})
        self.assertEqual(payload["seals"], [{
            "seal_id": "seal-1", "node_id": "implementation", "outcome": "success",
            "jj_commit_id": "91dc54368d0b759a393c64e77b29cc5f90ddd5de",
            "sealed_at": "2026-09-06T07:58:53Z",
        }])
        code, out, err = self._run("show", INITIATIVE_B)
        self.assertEqual(code, 0, err)
        self.assertIn("91dc54368d0b759a393c64e77b29cc5f90ddd5de", out)
        code, _out, err = self._run("show", "no-such-initiative")
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_a_home_without_a_database_has_nothing_to_list_or_export(self):
        code, out, err = self._run("export")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("exported 0 records", err)
        code, out, err = self._run("list")
        self.assertEqual(code, 0, err)
        self.assertIn("No initiatives", out)
        self.assertFalse(self.path.exists())

    def test_an_export_whose_reader_stops_early_ends_quietly(self):
        # `asha initiative export | head` is the common use: the reader closes
        # the pipe after a few lines and the export must stop without a trace.
        self._seed(children=6000)
        program = ("import sys\nfrom lib.control.initiative_evidence import main\n"
                   "raise SystemExit(main(['export']))\n")
        child = subprocess.Popen(
            [sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[2],
            env={**self.env, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertTrue(child.stdout.readline())
        child.stdout.close()
        stderr = child.stderr.read().decode()
        child.stderr.close()
        self.assertEqual(child.wait(timeout=30), 1)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("BrokenPipeError", stderr)

    def test_unknown_verbs_and_options_are_refused(self):
        self._seed(children=3)
        for args in (["approve", INITIATIVE_A], ["export", "--all"], ["list", "--bogus"], ["show"]):
            code, _out, err = self._run(*args)
            self.assertEqual(code, 2, args)
            self.assertTrue(err, args)


if __name__ == "__main__":
    unittest.main()
