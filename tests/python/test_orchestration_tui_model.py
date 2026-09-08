from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest import mock

from lib.control import tui
from lib.control.orchestration import tui_model
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.coordinator import claim
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.tui_model import InitiativesScreen, TuiModel, attention_items
from lib.control.tui_style import INERT, WAITING, summary_counts
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.orchestration_increment3_fixtures import advance_node
from tests.python.test_orchestration_coordinator_claim import FakeTmux


class OrchestrationTuiModelTests(unittest.TestCase):
    def test_tree_rows_filter_sort_detail_and_bounded_events(self) -> None:
        initiative = {"initiative_id": "i", "slug": "demo", "label": "Demo", "state": "approved"}
        nodes = [
            {"node_id": "b", "state": "approved", "type": "review", "goal": "Review"},
            {"node_id": "a", "state": "proposed", "type": "work", "goal": "Build"},
            {"node_id": "old", "state": "superseded", "type": "work", "goal": "Old"},
        ]
        attempts = [{"attempt_id": "x", "node_id": "a", "state": "allocated", "ordinal": 1}]
        events = [{"sequence": number, "type": "plan-proposed"} for number in range(1, 8)]
        model = TuiModel(initiative, nodes, attempts, events, event_limit=3)
        self.assertEqual([row["id"] for row in model.rows()], ["i", "a", "x", "b"])
        self.assertEqual([row["id"] for row in model.superseded_rows()], ["old"])
        self.assertEqual(model.detail("old")["state"], "superseded")
        self.assertEqual([item["sequence"] for item in model.event_tail()], [5, 6, 7])
        self.assertEqual(model.detail("a")["goal"], "Build")
        self.assertEqual([row["id"] for row in model.rows(query="review")], ["i", "b"])



class ParkedWaitingWorkProjectionTests(ExecutionFixture, unittest.TestCase):
    """The production loader, tree, and attention assembler over real parked records.

    No projection is hand-built here: the views come from `_load_initiative_views`
    over records the store validated, the pause and resume are real operator
    actions, and every surface (`!` filter rows, head display, node demand,
    `attention_items`) is read from the same code the TUI and CLI run.
    """

    def views(self) -> list[dict]:
        return tui._load_initiative_views(self.env)

    def screen(self, views: list[dict], **kwargs) -> InitiativesScreen:
        screen = InitiativesScreen(views, height=28, width=122, **kwargs)
        screen.expanded.add(("initiative", self.initiative_id))
        return screen

    def head_and_node(self, rows):
        head = next(row for row in rows if row.kind == "initiative")
        node = next(row for row in rows if row.kind == "node" and row.id == "implementation-a")
        return head, node

    def wait_for_the_operator(self) -> None:
        initiative = self.initiative()
        waiting = copy.deepcopy(initiative)
        waiting.update({
            "state": "needs-input",
            "state_revision": initiative["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(waiting, expected_digest=record_digest(initiative))

    def act(self, action_class: str) -> dict:
        record = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), action_class, {}),
        )
        self.assertEqual(record["state"], "completed", record["outcome"])
        return record

    def test_parking_removes_idle_demand_everywhere_and_resume_restores_it(self) -> None:
        # A pending node decision (durable state) under an initiative that is
        # itself waiting on the operator; no worker is linked, so resume's live
        # reconciliation has nothing to conflict with.
        advance_node(self, "implementation-a", ["dispatching", "running", "needs-input"])
        self.wait_for_the_operator()

        waiting = self.views()
        head, node = self.head_and_node(self.screen(waiting).rows())
        self.assertEqual(
            (head.state, head.attention, head.display),
            ("needs-input", "needs input", (WAITING, "needs you")),
        )
        self.assertEqual((node.state, node.attention), ("needs-input", "needs input"))
        self.assertTrue(head.needs_human and node.needs_human)
        # The head waits on the operator with no question on record (this
        # needs-input came from the store, as the paused-seal path writes it),
        # so the verb lists the head generically beside the node decision.
        self.assertEqual(
            [(item["kind"], item.get("node_id"), item["detail"]) for item in attention_items(waiting)],
            [("operator-decision", None, "initiative waits on the operator"),
             ("needs-input", "implementation-a", "node implementation-a needs a decision")],
        )
        self.assertEqual(
            [row.id for row in self.screen(waiting, attention_only=True).rows()],
            [self.initiative_id, "implementation-a"],
        )
        # Collapsed, `!` still lists the node decision with its head.
        self.assertEqual(
            [row.id for row in InitiativesScreen(
                waiting, height=28, width=122, attention_only=True,
            ).rows()],
            [self.initiative_id, "implementation-a"],
        )

        self.act("pause")
        parked = self.views()
        self.assertEqual(parked[0]["initiative"]["state"], "paused")
        # Parked with nothing observed running, no coordinator and no demand,
        # the head is quiet: the Current view holds it back and says so in its
        # counts, and All retained still draws it exactly as before.
        current = self.screen(parked)
        self.assertEqual([row.kind for row in current.rows()], [])
        self.assertEqual(
            {key: current.retained_counts[key] for key in ("shown", "loaded", "hidden")},
            {"shown": 0, "loaded": 1, "hidden": 1},
        )
        head, node = self.head_and_node(self.screen(parked, view_scope="all").rows())
        # Readable in the normal tree, but nothing here waits on the operator.
        self.assertEqual(
            (head.state, head.attention, head.display), ("paused", "-", (INERT, "paused")),
        )
        self.assertEqual((node.state, node.attention), ("needs-input", "-"))
        self.assertFalse(head.needs_human or node.needs_human)
        self.assertEqual(self.screen(parked, attention_only=True).rows(), [])
        self.assertEqual(
            self.screen(parked, view_scope="all", attention_only=True).rows(), [],
            "the retained view widens what is drawn, never what counts as demand",
        )
        self.assertEqual(attention_items(parked), [])
        # The decision was parked, not answered.
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )

        self.act("resume")
        restored = self.views()
        # No operator question was ever journaled, so resume does not restore
        # a needs-input head: the parked node decision is the whole demand.
        self.assertEqual(restored[0]["initiative"]["state"], "running")
        head, node = self.head_and_node(self.screen(restored).rows())
        self.assertEqual((head.attention, node.attention), ("-", "needs input"))
        self.assertTrue(node.needs_human)
        self.assertEqual([item["kind"] for item in attention_items(restored)], ["needs-input"])
        self.assertEqual(
            [row.id for row in self.screen(restored, attention_only=True).rows()],
            [self.initiative_id, "implementation-a"],
        )
        self.assertEqual(
            [
                (event["payload"]["from"], event["payload"]["to"])
                for event in self.store.list_events_snapshot(self.initiative_id)
                if event["type"] == "initiative-state-changed"
            ],
            [("needs-input", "paused"), ("paused", "running")],
        )


class ParkedOperatorQuestionProjectionTests(ExecutionFixture, unittest.TestCase):
    """U5 corrective: an initiative-only operator question survives parking as attention.

    The coordinator's `request-decision` is the only demand here: no node is in
    needs-input and no approval is requested. Every surface is read from the
    production loader, tree, header, and attention assembler over the real
    journal, across a real operator pause and resume.
    """

    QUESTION = "Which base should the retry use?"

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        record = claim(
            self.store, self.initiative(), env={**self.env, "TMUX_PANE": "%7"}, tmux=self.tmux,
        )
        asked = submit_action(self.store, self.initiative_id, build_action_document(
            self.initiative(), "request-decision",
            {"subject_id": "implementation-a", "question": self.QUESTION},
            actor_id=f"coordinator:{record['coordinator_id']}", coordinator=record,
        ))
        self.assertEqual(asked["state"], "completed", asked["outcome"])
        self.assertEqual(self.initiative()["state"], "needs-input")

    def views(self) -> list[dict]:
        return tui._load_initiative_views(self.env, tmux=self.tmux)

    def act(self, action_class: str) -> dict:
        record = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), action_class, {}),
        )
        self.assertEqual(record["state"], "completed", record["outcome"])
        return record

    def screen(self, views: list[dict], *, expanded: bool, **kwargs) -> InitiativesScreen:
        screen = InitiativesScreen(views, height=28, width=122, **kwargs)
        if expanded:
            screen.expanded.add(("initiative", self.initiative_id))
        return screen

    def title(self, screen: InitiativesScreen) -> str:
        model = tui.TuiModel(height=28, width=122)
        model.initiatives = screen
        return str(tui.render(model)[0])

    def question_event(self) -> dict:
        questions = [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "approval-requested"
            and event["payload"].get("kind") == "operator-decision"
        ]
        self.assertEqual(len(questions), 1)
        return questions[0]

    def transitions(self) -> list:
        return [
            (event["payload"]["from"], event["payload"]["to"])
            for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "initiative-state-changed"
        ]

    def assert_question_is_the_operators_move(self, views: list[dict]) -> None:
        for expanded in (False, True):
            screen = self.screen(views, expanded=expanded)
            rows = screen.rows()
            head = next(row for row in rows if row.kind == "initiative")
            self.assertEqual(
                (head.state, head.attention, head.display),
                ("needs-input", "needs input", (WAITING, "needs you")),
            )
            self.assertTrue(head.needs_human)
            self.assertEqual(
                [row.attention for row in rows if row.kind == "node"],
                ["-", "-", "-"] if expanded else [],
                "no node demands: the question is the whole ask",
            )
            filtered = self.screen(views, expanded=expanded, attention_only=True)
            self.assertEqual([row.id for row in filtered.rows()], [self.initiative_id])
            for shown in (screen, filtered):
                title = self.title(shown)
                self.assertLessEqual(len(title), 122)
                self.assertIn("1 need you", title)
                self.assertEqual(summary_counts(shown.rows())["waiting"], 1)
        items = attention_items(views)
        self.assertEqual(
            [(item["kind"], item.get("node_id"), item["detail"]) for item in items],
            [("operator-decision", None, f"operator decision: {self.QUESTION}")],
        )
        self.assertEqual(
            items[0]["resolution"], f"answer, then asha initiative resume {self.initiative_id}",
        )

    def assert_parked_and_silent(self, views: list[dict]) -> None:
        for expanded in (False, True):
            screen = self.screen(views, expanded=expanded)
            head = next(row for row in screen.rows() if row.kind == "initiative")
            self.assertEqual(
                (head.state, head.attention, head.display, head.needs_human),
                ("paused", "-", (INERT, "paused"), False),
            )
            self.assertEqual(
                self.screen(views, expanded=expanded, attention_only=True).rows(), [],
            )
            title = self.title(screen)
            self.assertNotIn("need you", title)
            self.assertIn("1 paused", title)
        self.assertEqual(attention_items(views), [])

    def test_a_parked_question_regains_attention_on_resume_and_is_answered_once(self) -> None:
        question = self.question_event()
        self.assert_question_is_the_operators_move(self.views())

        self.act("pause")
        parked = self.views()
        self.assertEqual(parked[0]["initiative"]["state"], "paused")
        self.assert_parked_and_silent(parked)
        self.assertEqual(self.question_event(), question)

        self.act("resume")
        restored = self.views()
        self.assertEqual(restored[0]["initiative"]["state"], "needs-input")
        self.assert_question_is_the_operators_move(restored)
        # Restored from the journal, not re-asked: the same event bytes, no
        # new question, no node or approval record created on the way.
        self.assertEqual(self.question_event(), question)
        self.assertEqual(
            [item for item in restored[0]["approvals"] if item["state"] == "requested"], [],
        )
        self.assertEqual(
            [node["state"] for node in restored[0]["nodes"]], ["ready", "blocked", "blocked"],
        )
        self.assertEqual(
            self.transitions(),
            [("running", "needs-input"), ("needs-input", "paused"), ("paused", "needs-input")],
        )

        # Answering is the same resume as before parking, and it does not
        # come back: parking and resuming running work leaves it running.
        self.act("resume")
        answered = self.views()
        self.assertEqual(answered[0]["initiative"]["state"], "running")
        head = next(row for row in self.screen(answered, expanded=True).rows() if row.kind == "initiative")
        self.assertEqual((head.attention, head.needs_human), ("-", False))
        self.assertEqual(attention_items(answered), [])
        self.assertEqual(self.screen(answered, expanded=True, attention_only=True).rows(), [])
        self.act("pause")
        self.act("resume")
        self.assertEqual(self.views()[0]["initiative"]["state"], "running")
        self.assertEqual(attention_items(self.views()), [])
        self.assertEqual(
            self.transitions()[3:],
            [("needs-input", "running"), ("running", "paused"), ("paused", "running")],
        )
        self.assertEqual(self.question_event(), question)


class EdgeLessAnsweredQuestionProjectionTests(ExecutionFixture, unittest.TestCase):
    """The operator surface reads the answer's own proof, not the journal alone.

    The recorded U5 variant leaves no `initiative-state-changed` edge that
    leaves or enters `running` after the question's own opening edge: the
    operator's answer died after its `running` head write and a paused seal
    took the head back to `needs-input` with no event.  The views come from
    the production `_load_initiative_views`, which now carries the same
    retained actions the CLI snapshot already carried, so the tree head, the
    `!` filter, the 122x28 header, and `attention_items` all classify that
    question the same way.
    """

    QUESTION = "Which base should the retry use?"

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        record = claim(
            self.store, self.initiative(),
            env={**self.env, "TMUX_PANE": "%7"}, tmux=self.tmux,
        )
        asked = submit_action(self.store, self.initiative_id, build_action_document(
            self.initiative(), "request-decision",
            {"subject_id": "implementation-a", "question": self.QUESTION},
            actor_id=f"coordinator:{record['coordinator_id']}", coordinator=record,
        ))
        self.assertEqual(asked["state"], "completed", asked["outcome"])
        self.question = self.question_event()

    def question_event(self) -> dict:
        questions = [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "approval-requested"
            and event["payload"].get("kind") == "operator-decision"
        ]
        self.assertEqual(len(questions), 1)
        return questions[0]

    def views(self) -> list[dict]:
        return tui._load_initiative_views(self.env, tmux=self.tmux)

    def act(self, action_class: str) -> dict:
        record = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), action_class, {}),
        )
        self.assertEqual(record["state"], "completed", record["outcome"])
        return record

    def screen(self, views: list[dict], *, expanded: bool, **kwargs) -> InitiativesScreen:
        screen = InitiativesScreen(views, height=28, width=122, **kwargs)
        if expanded:
            screen.expanded.add(("initiative", self.initiative_id))
        return screen

    def title(self, views: list[dict], **kwargs) -> str:
        model = tui.TuiModel(height=28, width=122)
        model.initiatives = self.screen(views, expanded=False, **kwargs)
        return str(tui.render(model)[0])

    def answer_dies_after_its_head_write(self) -> dict:
        """The operator's answer, killed between its head write and its edge."""
        document = build_action_document(self.initiative(), "resume", {})
        original = self.store.append_event

        def boundary(initiative_id, event):
            if (
                event["type"] == "initiative-state-changed"
                and event["payload"].get("to") == "running"
            ):
                raise KeyboardInterrupt("controller died before the answer edge")
            return original(initiative_id, event)

        with mock.patch.object(self.store, "append_event", side_effect=boundary):
            with self.assertRaises(KeyboardInterrupt):
                submit_action(self.store, self.initiative_id, document)
        self.assertEqual(self.initiative()["state"], "running")
        return document

    def paused_seal_head(self) -> None:
        """The labelled stand-in for a paused seal's head write: no event at all."""
        original = self.initiative()
        waiting = copy.deepcopy(original)
        waiting.update({
            "state": "needs-input",
            "state_revision": original["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(waiting, expected_digest=record_digest(original))

    def details(self, views: list[dict]) -> list[str]:
        return [item["detail"] for item in attention_items(views)]

    def test_the_answered_question_stops_being_quoted_and_resume_runs_the_work(self) -> None:
        views = self.views()
        self.assertEqual(self.details(views), [f"operator decision: {self.QUESTION}"])
        self.assertIn("actions", views[0], "the view carries the retained actions")

        answer = self.answer_dies_after_its_head_write()
        self.paused_seal_head()
        crashed = self.views()

        # The head still waits: the paused seal's own node decision is real
        # demand.  What is gone is the answered question behind it.
        self.assertEqual(crashed[0]["initiative"]["state"], "needs-input")
        self.assertEqual(self.details(crashed), ["initiative waits on the operator"])
        for expanded in (False, True):
            head = next(
                row for row in self.screen(crashed, expanded=expanded).rows()
                if row.kind == "initiative"
            )
            self.assertEqual(
                (head.state, head.attention, head.display, head.needs_human),
                ("needs-input", "needs input", (WAITING, "needs you"), True),
            )
            filtered = self.screen(crashed, expanded=expanded, attention_only=True)
            self.assertEqual([row.id for row in filtered.rows()], [self.initiative_id])
        title = self.title(crashed)
        self.assertLessEqual(len(title), 122)
        self.assertIn("1 need you", title)
        self.assertEqual(
            summary_counts(self.screen(crashed, expanded=True).rows())["waiting"], 1,
        )

        # Parking and resuming that wait must not bring the question back.
        self.act("pause")
        parked = self.views()
        self.assertEqual(parked[0]["initiative"]["state"], "paused")
        self.assertEqual(attention_items(parked), [])
        self.assertIn("1 paused", self.title(parked))

        self.act("resume")
        resumed = self.views()
        self.assertEqual(resumed[0]["initiative"]["state"], "running")
        self.assertEqual(attention_items(resumed), [])
        self.assertEqual(self.screen(resumed, expanded=True, attention_only=True).rows(), [])
        self.assertEqual(self.question_event(), self.question)
        self.assertEqual(
            self.store.read_action(self.initiative_id, answer["action_id"])["state"],
            "indeterminate",
            "no completion was manufactured for the interrupted answer",
        )

    def test_an_answer_that_never_wrote_keeps_the_question_on_every_surface(self) -> None:
        document = build_action_document(self.initiative(), "resume", {})
        original = self.store.save_initiative

        def boundary(record, **kwargs):
            if record["state"] == "running":
                raise KeyboardInterrupt("controller died before the answer head write")
            return original(record, **kwargs)

        with mock.patch.object(self.store, "save_initiative", side_effect=boundary):
            with self.assertRaises(KeyboardInterrupt):
                submit_action(self.store, self.initiative_id, document)

        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assertEqual(
            self.details(self.views()), [f"operator decision: {self.QUESTION}"],
        )
        self.act("pause")
        self.assertEqual(attention_items(self.views()), [])
        self.act("resume")
        restored = self.views()
        self.assertEqual(restored[0]["initiative"]["state"], "needs-input")
        self.assertEqual(
            self.details(restored), [f"operator decision: {self.QUESTION}"],
        )
        self.assertEqual(self.question_event(), self.question)

    def test_a_truncated_event_tail_never_discharges_the_question(self) -> None:
        """The loader keeps the last fifty events; a shorter tail proves less, not more."""
        self.answer_dies_after_its_head_write()
        self.paused_seal_head()
        views = self.views()
        self.assertEqual(self.details(views), ["initiative waits on the operator"])

        truncated = copy.deepcopy(views)
        truncated[0]["events"] = truncated[0]["events"][:-1]
        self.assertEqual(
            self.details(truncated), [f"operator decision: {self.QUESTION}"],
            "a tail that no longer reaches the head cannot prove the answer landed",
        )
        without_actions = copy.deepcopy(views)
        without_actions[0]["actions"] = []
        self.assertEqual(
            self.details(without_actions), [f"operator decision: {self.QUESTION}"],
        )


class RetainedDirectiveParityTests(ExecutionFixture, unittest.TestCase):
    """One retained pending directive, read against its own bound records.

    Nothing is hand-built: the coordinator generation, the dispatched attempt,
    its Control task link and the directive are records the production actions
    wrote, and every verdict below is read from the production loader, tree,
    node projection and attention assembler. The live case this generalises is
    a valid directive whose sole target later sealed paused while its worker
    exited: `delivery == "pending"` was the only thing the old selection read,
    so retained history was reported as a live ask forever.
    """

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        self.coordinator = claim(
            self.store, self.initiative(),
            env={**self.env, "TMUX_PANE": "%7"}, tmux=self.tmux,
        )
        self.attempt = self.dispatch()
        self.directive = self.submit_directive(self.attempt["attempt_id"])

    # -- real records -----------------------------------------------------

    def dispatch(self) -> dict:
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            self.control_task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed", action["outcome"])
        return self.store.list_attempts_snapshot(self.initiative_id)[0]

    def submit_directive(self, attempt_id: str) -> dict:
        record = submit_action(self.store, self.initiative_id, build_action_document(
            self.initiative(), "directive",
            {"node_id": "implementation-a", "attempt_id": attempt_id,
             "text": "prefer the smaller diff"},
            actor_id=f"coordinator:{self.coordinator['coordinator_id']}",
            coordinator=self.coordinator,
        ))
        self.assertEqual(record["state"], "completed", record["outcome"])
        self.assertIn('"delivery":"pending"', record["outcome"])
        return record

    def seal_attempt_paused(self) -> None:
        current = self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])
        for state in ("reported", "awaiting-exit", "paused-seal-ready", "sealing",
                      "sealed-paused"):
            changed = copy.deepcopy(current)
            changed.update({"state": state, "updated_at": now_text()})
            self.store.save_attempt(
                self.initiative_id, changed, expected_digest=record_digest(current),
            )
            current = changed

    def worker(self, state: str) -> tui.TuiRow:
        task = copy.deepcopy(self.control_task)
        return tui.TuiRow.from_records(task, {
            "contract": "asha.control-reconciliation.v1",
            "task_id": task["task_id"], "state": state, "blocker": None,
            "evidence": [], "runs": [],
        })

    def views(self) -> list[dict]:
        return tui._load_initiative_views(self.env, tmux=self.tmux)

    def screen(self, views, rows, **kwargs) -> InitiativesScreen:
        screen = InitiativesScreen(
            views, height=28, width=122, task_rows=rows, **kwargs,
        )
        screen.expanded.add(("initiative", self.initiative_id))
        return screen

    def node_row(self, screen: InitiativesScreen):
        return next(
            row for row in screen.rows()
            if row.kind == "node" and row.id == "implementation-a"
        )

    def records_digest(self) -> str:
        payload = json.dumps({
            "actions": self.store.list_actions_snapshot(self.initiative_id),
            "attempts": self.store.list_attempts_snapshot(self.initiative_id),
            "links": self.store.list_links_snapshot(self.initiative_id),
            "events": self.store.list_events_snapshot(self.initiative_id),
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    # -- behaviour --------------------------------------------------------

    def test_a_live_target_keeps_the_directive_in_the_tree_and_the_verb(self) -> None:
        rows = (self.worker("working"),)
        views = self.views()
        items = attention_items(views, rows)
        directive = next(item for item in items if item["kind"] == "directive-pending")
        self.assertEqual(directive["certainty"], "live")
        self.assertIn(self.directive["action_id"], directive["detail"])
        self.assertEqual(directive["node_id"], "implementation-a")
        # The tree carries the same ask on the node row it binds to, which is
        # where the old code showed nothing at all.
        node = self.node_row(self.screen(views, rows))
        self.assertEqual(node.attention, "directive pending")
        self.assertTrue(node.needs_human)
        self.assertEqual(
            [row.id for row in self.screen(views, rows, attention_only=True).rows()],
            [self.initiative_id, "implementation-a"],
        )

    def test_a_sealed_ended_target_defers_the_directive_without_touching_it(self) -> None:
        self.seal_attempt_paused()
        before = self.records_digest()
        rows = (self.worker("exited"),)
        views = self.views()

        self.assertEqual(
            [item["kind"] for item in attention_items(views, rows)], [],
            "a directive whose only target sealed and exited is history, not demand",
        )
        node = self.node_row(self.screen(views, rows))
        self.assertEqual(node.attention, "-")
        # The record itself is untouched and still readable as retained history.
        retained = tui_model._pending_directives(
            views[0], *self._maps(views, rows),
        )
        self.assertEqual(
            [(item["action_id"], item["classification"]) for item in retained],
            [(self.directive["action_id"], "deferred")],
        )
        self.assertEqual(self.records_digest(), before, "classification writes nothing")
        self.assertEqual(
            self.store.read_action(self.initiative_id, self.directive["action_id"])["state"],
            "completed",
        )
        self.assertNotIn(
            "directive-delivered",
            [event["type"] for event in self.store.list_events_snapshot(self.initiative_id)],
        )

    def test_a_sealed_target_with_a_live_worker_stays_honestly_unknown(self) -> None:
        self.seal_attempt_paused()
        rows = (self.worker("working"),)
        views = self.views()
        directive = next(
            item for item in attention_items(views, rows)
            if item["kind"] == "directive-pending"
        )
        self.assertEqual(directive["certainty"], "unknown")
        self.assertIn("sealed while its worker still reads live", directive["detail"])
        self.assertIn(self.directive["action_id"], directive["detail"])
        self.assertEqual(self.node_row(self.screen(views, rows)).attention, "directive pending")

    def test_a_foreign_or_malformed_binding_is_uncertainty_never_authority(self) -> None:
        views = self.views()
        rows = (self.worker("working"),)
        for outcome, expected in (
            ({"delivery": "pending"}, "no node binding is recorded"),
            ({"delivery": "pending", "node_id": "implementation-a"},
             "no attempt binding is recorded"),
            ({"delivery": "pending", "node_id": "implementation-a",
              "attempt_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
             "its attempt record is not among the loaded records"),
            ({"delivery": "pending", "node_id": "review-a",
              "attempt_id": self.attempt["attempt_id"]},
             "its node and attempt bindings disagree"),
        ):
            with self.subTest(outcome=sorted(outcome)):
                view = copy.deepcopy(views[0])
                for action in view["actions"]:
                    if action["action_class"] == "directive":
                        action["outcome"] = json.dumps(outcome)
                item = next(
                    entry for entry in attention_items([view], rows)
                    if entry["kind"] == "directive-pending"
                )
                self.assertEqual(item["certainty"], "unknown")
                self.assertIn(expected, item["detail"])
                self.assertIn(
                    "nothing here delivers it", item["resolution"],
                    "an unresolved binding never reads as permission to deliver",
                )
        # A malformed record is uncertainty too, never an excuse to drop it.
        for broken, expected in (
            ("{not json", "its recorded outcome cannot be read"),
            (None, "its recorded outcome cannot be read"),
        ):
            with self.subTest(outcome=broken):
                view = copy.deepcopy(views[0])
                for action in view["actions"]:
                    if action["action_class"] == "directive":
                        action["outcome"] = broken
                items = [
                    item for item in attention_items([view], rows)
                    if item["kind"] == "directive-pending"
                ]
                if broken is None:
                    # A directive with no recorded outcome carries no pending
                    # delivery to read; nothing is invented from its absence.
                    self.assertEqual(items, [])
                else:
                    self.assertEqual(len(items), 1)
                    self.assertIn(expected, items[0]["detail"])
                    self.assertEqual(items[0]["certainty"], "unknown")
        # A state this controller cannot read is uncertainty, not a live ask.
        view = copy.deepcopy(views[0])
        view["attempts"][0]["state"] = "teleported"
        item = next(
            entry for entry in attention_items([view], rows)
            if entry["kind"] == "directive-pending"
        )
        self.assertEqual(item["certainty"], "unknown")
        self.assertIn("cannot read", item["detail"])

    def test_the_head_carries_a_directive_no_loaded_node_can(self) -> None:
        views = self.views()
        view = copy.deepcopy(views[0])
        view["nodes"] = []
        for action in view["actions"]:
            if action["action_class"] == "directive":
                action["outcome"] = json.dumps({
                    "delivery": "pending", "node_id": "implementation-a",
                    "attempt_id": self.attempt["attempt_id"],
                })
        item = next(
            entry for entry in attention_items([view], ())
            if entry["kind"] == "directive-pending"
        )
        self.assertIsNone(item["node_id"])
        screen = InitiativesScreen([view], height=28, width=122)
        head = next(row for row in screen.rows() if row.kind == "initiative")
        self.assertEqual(head.attention, "directive pending")

    def test_a_directive_bound_to_a_superseded_plan_is_unknown_not_authority(self) -> None:
        """The accepted finding: the stale active-plan branch had no behaviour test.

        The directive's own record names the plan digest it was written
        against. When the head has since activated a different plan that
        binding no longer addresses anything the controller can act on: it is
        neither a live ask nor proved history, so it stays listed as
        uncertainty and never confers permission to deliver.
        """
        views = self.views()
        rows = (self.worker("running"),)
        live = next(
            item for item in attention_items(views, rows)
            if item["kind"] == "directive-pending"
        )
        self.assertEqual(live["certainty"], "live")
        binding = views[0]["initiative"]["active_plan"]
        self.assertIsInstance(binding, dict)
        self.assertIsInstance(binding["digest"], str)
        directive = next(
            action for action in views[0]["actions"]
            if action["action_class"] == "directive"
        )
        # The record was written against the plan that is active right now.
        self.assertEqual(directive["active_plan_digest"], binding["digest"])
        stale = copy.deepcopy(views[0])
        for action in stale["actions"]:
            if action["action_class"] == "directive":
                action["active_plan_digest"] = "0" * 64
        item = next(
            entry for entry in attention_items([stale], rows)
            if entry["kind"] == "directive-pending"
        )
        self.assertEqual(item["certainty"], "unknown")
        self.assertIn("bound to a plan that is no longer active", item["detail"])
        self.assertIn("nothing here delivers it", item["resolution"])
        # The same verdict reaches the tree, on the node row and by identity.
        screen = self.screen([stale], rows)
        self.assertEqual(self.node_row(screen).attention, "directive pending")
        # An activated plan the directive does match is still live, so the
        # branch tests the binding and not merely the presence of a digest.
        matching = copy.deepcopy(views[0])
        for action in matching["actions"]:
            if action["action_class"] == "directive":
                action["active_plan_digest"] = binding["digest"]
        self.assertEqual(
            next(
                entry for entry in attention_items([matching], rows)
                if entry["kind"] == "directive-pending"
            )["certainty"],
            "live",
        )

    def test_a_nonobject_outcome_is_unknown_and_never_silently_dropped(self) -> None:
        """The accepted finding: well-formed JSON of the wrong shape was dropped.

        `validate_action` constrains `outcome` only as optional text, so a
        foreign or corrupted record holding `[]`, `"pending"` or `7` is
        storable and validates. Reading the missing `delivery` field as "not
        pending" discharged a directive whose binding was never resolved.
        """
        views = self.views()
        rows = (self.worker("running"),)
        for outcome in ("[]", '"pending"', "7", "null", '"{}"', "true"):
            with self.subTest(outcome=outcome):
                view = copy.deepcopy(views[0])
                for action in view["actions"]:
                    if action["action_class"] == "directive":
                        action["outcome"] = outcome
                items = [
                    item for item in attention_items([view], rows)
                    if item["kind"] == "directive-pending"
                ]
                self.assertEqual(len(items), 1, "the ask is never dropped")
                self.assertEqual(items[0]["certainty"], "unknown")
                self.assertIn(
                    "is not a directive record", items[0]["detail"],
                )
                self.assertIn("nothing here delivers it", items[0]["resolution"])
                # And the head still carries it where no node row can.
                screen = InitiativesScreen([view], height=28, width=122)
                head = next(
                    row for row in screen.rows() if row.kind == "initiative"
                )
                self.assertEqual(head.attention, "directive pending")
        # A well-formed record that simply is not pending is still not demand.
        delivered = copy.deepcopy(views[0])
        for action in delivered["actions"]:
            if action["action_class"] == "directive":
                action["outcome"] = json.dumps({"delivery": "relayed"})
        self.assertEqual(
            [item for item in attention_items([delivered], rows)
             if item["kind"] == "directive-pending"],
            [],
        )

    def _maps(self, views, rows):
        bound, by_attempt = tui_model._link_maps(views)
        del bound
        return by_attempt, {row.task["task_id"]: row for row in rows}


if __name__ == "__main__":
    unittest.main()
