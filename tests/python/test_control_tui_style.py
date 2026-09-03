"""Tiers, the pipeline rail, coloured spans, and every degradation path."""

from __future__ import annotations

import unittest

from lib.control import tui
from lib.control.tui import TuiModel, render
from lib.control.orchestration.tui_model import InitiativesScreen
from lib.control.tui_style import (
    BAD, GOOD, INERT, MACHINE, TIER_PAIR, TIER_XTERM, WAITING, Line, LineBuilder,
    display_state, rail_text, rail_tiers, short_label, summary_counts, tier_for,
)
from lib.control.orchestration.model import (
    ATTEMPT_STATES, COORDINATOR_STATES, INITIATIVE_STATES, NODE_STATES,
)


def view(slug, state, nodes=(), **extra):
    payload = {
        "initiative": {"initiative_id": slug, "slug": slug, "state": state},
        "nodes": [dict({"node_id": f"n{i}"}, **node) for i, node in enumerate(nodes)],
        "attempts": [], "links": [], "approvals": [], "verifications": [],
        "coordinator": {"state": "active"}, "coordinator_live": True,
        "seals": [], "actions": [], "events": [],
    }
    payload.update(extra)
    return payload


class TierTests(unittest.TestCase):
    def test_every_record_state_resolves_to_exactly_one_tier(self) -> None:
        classes = (INITIATIVE_STATES, NODE_STATES, ATTEMPT_STATES, COORDINATOR_STATES)
        every = set().union(*(set(item) for item in classes))
        # Both numbers the guide quotes: 57 states across the four record
        # classes, 45 distinct words, because seven names (running, failed,
        # approved, cancelled, dispatching, needs-input, stale) are reused.
        self.assertEqual(sum(len(item) for item in classes), 57)
        self.assertEqual(len(every), 45)
        # Assert EXPLICIT membership, not just that tier_for returns something:
        # inert is the fallback, so a state nobody classified would pass a
        # weaker check while silently rendering as "not actionable". `ready`
        # and `partial` both reached a live screen that way.
        from lib.control.tui_style import _STATE_TIER

        unclassified = sorted(every - set(_STATE_TIER))
        self.assertEqual(unclassified, [], f"states with no explicit tier: {unclassified}")
        for state in every:
            self.assertIn(tier_for(state), {WAITING, MACHINE, GOOD, BAD, INERT}, state)
        self.assertEqual(tier_for("ready"), MACHINE)
        self.assertEqual(tier_for("partial"), BAD)
        # The three that mean the operator is the blocker are the loud tier.
        for state in ("awaiting-plan-approval", "needs-input", "ready-for-integration"):
            self.assertEqual(tier_for(state), WAITING, state)
        for state in ("failed", "abnormal-exit", "launch-failed"):
            self.assertEqual(tier_for(state), BAD, state)
        self.assertEqual(tier_for(None), INERT)
        self.assertEqual(tier_for("something-invented"), INERT)

    def test_short_labels_never_collapse_two_states_into_one_stub(self) -> None:
        every = set(INITIATIVE_STATES) | set(NODE_STATES) | set(ATTEMPT_STATES)
        labels = {}
        for state in every:
            label = short_label(state)
            self.assertLessEqual(len(label), 11, state)
            self.assertNotIn("…", label, f"{state} still truncates into ambiguity")
            labels.setdefault(label, []).append(state)
        # Clipping used to render these two as near-identical stubs.
        self.assertNotEqual(short_label("sealed-success"), short_label("success-seal-ready"))
        for label, states in labels.items():
            if len(states) > 1:
                self.assertLessEqual(len(states), 2, f"{label} is overloaded by {states}")


class RailTests(unittest.TestCase):
    def test_the_rail_reads_the_lifecycle_from_the_record(self) -> None:
        done = [{"type": "work", "state": "succeeded"}, {"type": "review", "state": "succeeded"},
                {"type": "verify", "state": "succeeded"}]
        plan = {"revision": 1}
        self.assertEqual(rail_text(view("a", "ready-for-integration", done, plan=plan)), "[✓✓✓✓✓!]")
        self.assertEqual(rail_text(view("a", "integrated", done, plan=plan)), "[✓✓✓✓✓✓]")
        recorded = view("a", "ready-for-integration", done, plan=plan, events=[{
            "type": "seal-integration-recorded",
            "payload": {"disposition": "integrated"},
        }])
        self.assertEqual(rail_text(recorded), "[✓✓✓✓✓✓]")
        self.assertEqual(
            rail_text(view("a", "archived", done, plan=plan)), "[✓✓✓✓✓·]",
        )
        self.assertEqual(rail_text(view("a", "awaiting-plan-approval", plan=plan)), "[✓!····]")
        self.assertEqual(rail_text(view("a", "draft")), "[······]")
        self.assertEqual(
            rail_text(view("a", "running", [{"type": "work", "state": "running"}], plan=plan)),
            "[✓✓●···]")
        self.assertEqual(
            rail_text(view("a", "running", [{"type": "work", "state": "needs-input"}], plan=plan)),
            "[✓✓!···]")

    def test_a_demand_outranks_a_failure_outranks_live_work(self) -> None:
        nodes = [{"type": "work", "state": "needs-input"}, {"type": "work", "state": "failed"},
                 {"type": "work", "state": "running"}]
        self.assertEqual(rail_tiers(view("a", "running", nodes))[2], WAITING)
        self.assertEqual(rail_tiers(view("a", "running", nodes[1:]))[2], BAD)
        self.assertEqual(rail_tiers(view("a", "running", nodes[2:]))[2], MACHINE)

    def test_the_ascii_rail_is_exact_width_where_glyphs_are_ambiguous(self) -> None:
        unicode_rail = rail_text(view("a", "awaiting-plan-approval"))
        ascii_rail = rail_text(view("a", "awaiting-plan-approval"), glyphs="ascii")
        self.assertEqual(len(unicode_rail), len(ascii_rail))
        self.assertTrue(all(ord(char) < 128 for char in ascii_rail), ascii_rail)


class RollUpTests(unittest.TestCase):
    def test_only_operator_demand_reaches_the_collapsed_parent(self) -> None:
        review_waiting = view(
            "a", "running", [{"type": "review", "state": "needs-input"}],
        )
        self.assertEqual(display_state(review_waiting), (MACHINE, "running"))
        self.assertEqual(rail_tiers(review_waiting)[3], WAITING)

        self.assertEqual(display_state(view("b", "needs-input")), (WAITING, "needs you"))
        requested = view("c", "running", approvals=[{"state": "requested"}])
        self.assertEqual(display_state(requested), (WAITING, "needs you"))

    def test_a_failing_child_is_not_rolled_up_because_retries_are_automatic(self) -> None:
        failing = view("a", "running", [{"type": "work", "state": "failed"}])
        tier, _label = display_state(failing)
        self.assertEqual(tier, MACHINE, "the initiative is still the machine's move")
        self.assertEqual(rail_tiers(failing)[2], BAD, "but the rail still shows the failure")

    def test_counts_equal_the_rows_they_describe(self) -> None:
        views = [
            view("a", "awaiting-plan-approval"),
            view("b", "running", [{"type": "work", "state": "needs-input"}]),
            view("c", "ready-for-integration"),
            view("d", "running", [{"type": "work", "state": "running"}]),
            view("e", "paused"), view("f", "failed"),
        ]
        model = TuiModel(height=40, width=140)
        model.initiatives = InitiativesScreen(views, height=40, width=140)
        counts = summary_counts(model.initiatives.rows())
        self.assertEqual(counts["waiting"], 2, "child demand stays in the rail")
        self.assertEqual(counts["initiatives"], len(views))
        self.assertEqual(
            sum(counts[key] for key in ("waiting", "running", "failed", "paused", "settled")),
            counts["initiatives"], "every initiative lands in exactly one bucket",
        )
        displayed_amber = [
            row for row in model.initiatives.rows()
            if row.kind == "initiative" and row.display[0] == WAITING
        ]
        self.assertEqual(
            len(displayed_amber), counts["waiting"],
            "rail-only child demand must not inflate the title count",
        )


class LineTests(unittest.TestCase):
    def test_a_line_is_a_string_everywhere_and_a_span_carrier_only_for_the_painter(self) -> None:
        line = LineBuilder().add("ab", 4, WAITING).add("cd", 2, GOOD).build()
        self.assertIsInstance(line, str)
        self.assertEqual(line, "ab  cd")
        self.assertTrue(line.startswith("ab"))
        self.assertIn("cd", line)
        self.assertEqual(line.spans, ((0, 4, WAITING), (4, 6, GOOD)))
        self.assertEqual(getattr("plain string", "spans", ()), ())

    def test_clipping_trims_spans_with_the_text(self) -> None:
        line = LineBuilder().add("aaaa", 4, WAITING).add("bbbb", 4, GOOD).build()
        clipped = line.clipped(6)
        self.assertEqual(clipped, "aaaab…")
        self.assertEqual(len(clipped), 6, "a clipped line fills its width")
        self.assertTrue(all(stop <= len(clipped) for _s, stop, _t in clipped.spans))
        self.assertEqual(line.clipped(0), "")
        self.assertIs(line.clipped(99), line)


class PaintTests(unittest.TestCase):
    class Screen:
        def __init__(self, accepts_attribute=True):
            self.writes = []
            self._accepts = accepts_attribute

        def getmaxyx(self):
            return (10, 40)

        def erase(self):
            self.writes.clear()

        def refresh(self):
            pass

        def addnstr(self, y, x, value, limit, attribute=None):
            if attribute is not None and not self._accepts:
                raise TypeError("takes 5 positional arguments")
            self.writes.append((y, x, value[:limit], attribute or 0))

    class Curses:
        error = RuntimeError
        A_BOLD = 1 << 21
        COLORS = 256

        def __init__(self):
            self.pairs = {}

        def has_colors(self):
            return True

        def start_color(self):
            pass

        def use_default_colors(self):
            pass

        def init_pair(self, index, foreground, background):
            self.pairs[index] = (foreground, background)

        def color_pair(self, index):
            return index << 8

    def model(self):
        model = TuiModel(height=10, width=40)
        model.initiatives = InitiativesScreen(
            [view("alpha", "awaiting-plan-approval")], height=10, width=40)
        return model

    def test_colour_init_uses_the_true_xterm_indices_from_the_design(self) -> None:
        curses_module = self.Curses()
        self.assertTrue(tui.init_colours(curses_module))
        for tier, pair in TIER_PAIR.items():
            self.assertEqual(curses_module.pairs[pair][0], TIER_XTERM[tier], tier)

    def test_a_terminal_without_colour_still_paints_every_word(self) -> None:
        class Mono(PaintTests.Curses):
            def has_colors(self):
                return False

        self.assertFalse(tui.init_colours(Mono()))

        class Bare:
            error = RuntimeError

        self.assertFalse(tui.init_colours(Bare()), "a curses without the colour API is not an error")
        screen, model = self.Screen(), self.model()
        model.coloured = False
        tui._paint(screen, self.Curses(), model)
        painted = "".join(value for _y, _x, value, _a in screen.writes)
        self.assertIn("approve", painted, "the short label survives without colour")
        self.assertIn("alpha", painted)
        bold = self.Curses().A_BOLD
        self.assertTrue(any(attribute == bold for _y, _x, _v, attribute in screen.writes),
                        "the loud tier is still emphasised by bold alone")

    def test_each_span_is_painted_with_its_own_tier_attribute(self) -> None:
        screen, curses_module, model = self.Screen(), self.Curses(), self.model()
        model.coloured = True
        tui._paint(screen, curses_module, model)
        waiting = curses_module.color_pair(TIER_PAIR[WAITING]) | curses_module.A_BOLD
        self.assertTrue(any(attribute == waiting for _y, _x, _v, attribute in screen.writes))
        rebuilt = {}
        for y, x, value, _attribute in screen.writes:
            rebuilt.setdefault(y, {})[x] = value
        first = "".join(rebuilt[0][key] for key in sorted(rebuilt[0]))
        self.assertTrue(first.startswith("ASHA CONTROL"))

    def test_hostile_spans_paint_exactly_what_a_single_write_would(self) -> None:
        """Spans come from a builder today, but the painter must not trust them.

        Every shape below must paint the same text the previous single
        `addnstr(y, 0, line, width - 1)` produced: no lost text, no double
        write, no negative index, no write past the limit.
        """
        class Recorder:
            def __init__(self) -> None:
                self.cells: dict[int, str] = {}

            def addnstr(self, _y, x, value, limit, _attribute=0):
                assert limit >= 0, f"negative limit {limit}"
                assert x >= 0, f"negative column {x}"
                for offset, character in enumerate(value[:limit]):
                    self.cells[x + offset] = character

        hostile = {
            "ordered": [(0, 2, WAITING), (4, 6, GOOD)],
            "out of order": [(4, 6, GOOD), (0, 2, WAITING)],
            "overlapping": [(0, 5, WAITING), (3, 7, GOOD)],
            "stop beyond the text": [(2, 99, WAITING)],
            "start beyond the text": [(10, 12, WAITING)],
            "zero width": [(2, 2, WAITING)],
            "negative start": [(-3, 2, WAITING)],
        }
        for name, spans in hostile.items():
            for width in (1, 2, 5, 40):
                line = Line("abcdefgh", spans)
                recorder = Recorder()
                tui._paint_spans(recorder, self.Curses(), 0, line, line.spans, width, True)
                painted = "".join(
                    recorder.cells.get(index, "") for index in sorted(recorder.cells)
                )
                self.assertEqual(painted, str(line)[: max(0, width - 1)], f"{name} at {width}")
        empty = Line("", [(0, 3, WAITING)])
        recorder = Recorder()
        tui._paint_spans(recorder, self.Curses(), 0, empty, empty.spans, 10, True)
        self.assertEqual(recorder.cells, {})

    def test_a_screen_that_cannot_take_attributes_gets_plain_text(self) -> None:
        screen, model = self.Screen(accepts_attribute=False), self.model()
        model.coloured = True
        tui._paint(screen, self.Curses(), model)
        painted = "\n".join(value for _y, _x, value, _a in screen.writes)
        self.assertIn("alpha", painted, "text survives where attributes cannot")


class DegradationTests(unittest.TestCase):
    def test_every_width_fills_exactly_and_keeps_the_demand_column(self) -> None:
        """No width may overflow the terminal, waste columns, or lose the demand."""
        for width in range(10, 400):
            columns = tui._tree_columns(width)
            names = [name for name, _size in columns]
            total = sum(size for _name, size in columns)
            self.assertLessEqual(total, width, f"width {width} would wrap: {columns}")
            self.assertEqual(total, width, f"width {width} wastes {width - total} columns")
            self.assertIn("waiting", names, f"width {width} dropped the demand column")
            self.assertIn("name", names, width)

    def test_columns_shed_worker_then_state_and_never_the_rail_or_the_demand(self) -> None:
        for width, expected in (
            (140, ["glyph", "state", "name", "rail", "worker", "age", "waiting"]),
            (100, ["glyph", "state", "name", "rail", "age", "waiting"]),
            (80, ["glyph", "rail", "name", "age", "waiting"]),
            (50, ["glyph", "rail", "name", "waiting"]),
            (40, ["glyph", "name", "waiting"]),
        ):
            self.assertEqual([name for name, _w in tui._tree_columns(width)], expected, width)
        for width in (140, 100, 80, 50, 46):
            names = [name for name, _w in tui._tree_columns(width)]
            self.assertIn("rail", names)
            self.assertIn("waiting", names)

    def test_rows_align_to_their_header_at_every_width(self) -> None:
        views = [view("alpha", "running", [{"type": "work", "state": "running"}]),
                 view("beta", "ready-for-integration")]
        for width in (140, 120, 100, 80, 72, 60):
            model = TuiModel(height=30, width=width)
            model.initiatives = InitiativesScreen(views, height=30, width=width)
            lines = [str(line) for line in render(model)]
            header = next(line for line in lines if "WAITING ON" in line)
            rows = [line for line in lines if line[:1] in {" ", ">"} and "alpha" in line or "beta" in line]
            for row in rows:
                self.assertEqual(
                    len(row.rstrip()) <= len(header.rstrip()) or len(row) == len(header), True,
                    f"width {width}: {row!r} against {header!r}",
                )
            self.assertTrue(all(len(line) <= width for line in lines), width)

    def test_a_cjk_locale_selects_the_exact_width_glyphs(self) -> None:
        self.assertEqual(tui._glyph_mode({}), "unicode")
        self.assertEqual(tui._glyph_mode({"LANG": "ja_JP.UTF-8"}), "ascii")
        self.assertEqual(tui._glyph_mode({"LC_ALL": "zh_CN.UTF-8"}), "ascii")
        self.assertEqual(tui._glyph_mode({"ASHA_CONTROL_GLYPHS": "ascii"}), "ascii")
        self.assertEqual(tui._glyph_mode({"ASHA_CONTROL_GLYPHS": "unicode", "LANG": "ko_KR"}), "unicode")

    def test_the_title_sheds_by_width_keeping_the_demand_last(self) -> None:
        views = [view("a", "awaiting-plan-approval"), view("b", "running",
                 [{"type": "work", "state": "running"}])]
        titles = {}
        for width in (140, 60, 40, 26):
            model = TuiModel(height=24, width=width)
            model.initiatives = InitiativesScreen(views, height=24, width=width)
            titles[width] = str(render(model)[0])
            self.assertLessEqual(len(titles[width]), width, width)
        self.assertIn("Scope: active", titles[140])
        self.assertNotIn("Scope: active", titles[40])
        self.assertIn("1 need you", titles[40], "the demand is the last thing to go")


class ReviewFindingTests(unittest.TestCase):
    """Each of these failed before the fix it names."""

    def test_a_terminal_state_alone_never_ticks_a_stage_that_never_ran(self) -> None:
        # draft -> cancelled is a legal transition; the rail used to read the
        # terminal state alone and claim a plan and an approval that never were.
        never_planned = view("a", "cancelled")
        self.assertEqual(rail_text(never_planned), "[·····✗]")
        self.assertEqual(rail_tiers(never_planned)[:2], ["", ""])
        planned = view("b", "cancelled", plan={"revision": 1})
        self.assertEqual(rail_tiers(planned)[:2], [GOOD, GOOD])
        self.assertEqual(rail_tiers(view("c", "draft"))[:2], ["", ""])
        # An awaiting-approval initiative always has a plan, but assert the
        # gate rather than trusting it.
        self.assertEqual(
            rail_tiers(view("d", "awaiting-plan-approval", plan={"revision": 1}))[1], WAITING)

    def test_a_filter_cannot_make_the_title_advertise_hidden_rows(self) -> None:
        views = [view("alpha", "awaiting-plan-approval", plan={"revision": 1}),
                 view("beta", "running", [{"type": "work", "state": "running"}], plan={"revision": 1}),
                 view("gamma", "failed", plan={"revision": 1})]
        for narrowing, expected in (
            ({}, {"waiting": 1, "running": 1, "failed": 1, "initiatives": 3}),
            ({"filter_string": "beta"}, {"waiting": 0, "running": 1, "failed": 0, "initiatives": 1}),
            ({"attention_only": True}, {"waiting": 1, "running": 0, "failed": 0, "initiatives": 1}),
        ):
            model = TuiModel(height=30, width=140)
            model.initiatives = InitiativesScreen(views, height=30, width=140)
            for key, value in narrowing.items():
                setattr(model.initiatives, key, value)
            lines = [str(line) for line in render(model)]
            counts = summary_counts(model.initiatives.rows())
            for key, value in expected.items():
                self.assertEqual(counts[key], value, f"{narrowing} {key}")
            shown = [line for line in lines
                     if line[:1] in " >" and any(slug in line for slug in ("alpha", "beta", "gamma"))]
            self.assertEqual(len(shown), expected["initiatives"], narrowing)
            if expected["waiting"] == 0:
                self.assertNotIn("need you", lines[0], narrowing)

    def test_every_cell_is_sanitised_on_its_way_to_the_terminal(self) -> None:
        # The retired renderer sanitised through _clip; the builder must too,
        # so a control code or bidi override in any record cannot move the
        # cursor or reorder the line.
        hostile = "slug\u202ereversed\u0007\u200b\tend"
        line = LineBuilder().add(hostile, 40).build()
        self.assertNotIn("\u202e", line)
        self.assertNotIn("\u0007", line)
        self.assertNotIn("\t", line)
        model = TuiModel(height=20, width=140)
        model.initiatives = InitiativesScreen(
            [view(hostile, "running", [{"type": "work", "state": "running"}], plan={"revision": 1})],
            height=20, width=140)
        for line in render(model):
            for character in str(line):
                self.assertTrue(
                    character.isprintable(), f"{character!r} reached the terminal")

    def test_a_full_width_value_never_touches_the_next_column(self) -> None:
        model = TuiModel(height=20, width=140)
        model.initiatives = InitiativesScreen(
            [view("A" * 90, "running", [{"type": "work", "state": "running"}], plan={"revision": 1})],
            height=20, width=140)
        row = next(str(line) for line in render(model) if "AAAA" in str(line))
        self.assertIn(" [", row, "the label ran into the pipeline bracket")
        header = next(str(line) for line in render(model) if "WAITING ON" in str(line))
        self.assertEqual(len(row), len(header))

    def test_the_rail_survives_down_to_its_stated_floor(self) -> None:
        for width in range(46, 200):
            self.assertIn("rail", [name for name, _s in tui._tree_columns(width)], width)
        for width in range(10, 46):
            names = [name for name, _s in tui._tree_columns(width)]
            self.assertNotIn("rail", names, width)
            self.assertIn("waiting", names, f"the demand outlives the rail at {width}")

    def test_the_visible_terms_sum_to_the_visible_total(self) -> None:
        """A count line the operator cannot add up is not auditable."""
        views = [view("a", "awaiting-plan-approval", plan={"revision": 1}),
                 view("b", "running", [{"type": "work", "state": "running"}], plan={"revision": 1}),
                 view("c", "failed", plan={"revision": 1}),
                 view("d", "paused", plan={"revision": 1}),
                 view("e", "cancelled")]
        model = TuiModel(height=30, width=160)
        model.initiatives = InitiativesScreen(views, height=30, width=160)
        title = str(render(model)[0])
        import re

        terms = {label: int(number) for number, label in
                 re.findall(r"(\d+) (need you|running|failed|paused|settled)", title)}
        total = int(re.search(r"(\d+) initiatives", title).group(1))
        self.assertEqual(sum(terms.values()), total, title)
        self.assertEqual(total, len(views))

    def test_a_negative_column_width_cannot_eat_the_text(self) -> None:
        self.assertEqual(LineBuilder().add("abcdefgh", -3).build(), "")


if __name__ == "__main__":
    unittest.main()
