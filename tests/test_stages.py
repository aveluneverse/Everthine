"""stages.py: state file round-trips, fail-soft parsing, advance/retreat
bounds, and the prompt block's exact shape."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from everthine import stages

NAMES = ("Settling in", "In rhythm", "Deep water")
TEXTS = ("calm text", "warmer text", "deep text")
NOW = datetime(2026, 7, 6, 21, 30)


class TestLoadState(unittest.TestCase):
    def test_missing_file_is_fresh_state(self):
        with tempfile.TemporaryDirectory() as td:
            state = stages.load_state(Path(td) / "stage.json")
        self.assertEqual(state, {"current": None, "history": []})

    def test_corrupt_file_degrades_and_keeps_corpse(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "stage.json"
            p.write_text("{not json", encoding="utf-8")
            state = stages.load_state(p)
            self.assertEqual(state, {"current": None, "history": []})
            corpses = list(Path(td).glob("stage.json.corrupt-*"))
            self.assertEqual(len(corpses), 1)  # her ring of years is never overwritten silently

    def test_wrong_shape_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "stage.json"
            p.write_text('["list", "not", "dict"]', encoding="utf-8")
            self.assertEqual(stages.load_state(p),
                             {"current": None, "history": []})


class TestResolveIndex(unittest.TestCase):
    def test_none_current_is_first_stage(self):
        self.assertEqual(stages.resolve_index({"current": None, "history": []}, NAMES), 0)

    def test_known_current_resolves(self):
        self.assertEqual(
            stages.resolve_index({"current": "In rhythm", "history": []}, NAMES), 1)

    def test_unknown_current_fails_soft_to_first(self):
        with self.assertLogs("everthine", level="WARNING"):
            idx = stages.resolve_index({"current": "Ghost", "history": []}, NAMES)
        self.assertEqual(idx, 0)


class TestAdvanceRetreat(unittest.TestCase):
    def test_advance_moves_and_persists_with_note(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "stage.json"
            new = stages.advance(p, NAMES, "first midnight talk", NOW)
            self.assertEqual(new, "In rhythm")
            state = stages.load_state(p)
            self.assertEqual(state["current"], "In rhythm")
            self.assertEqual(state["history"], [{
                "stage": "In rhythm", "date": "2026-07-06",
                "note": "first midnight talk"}])

    def test_advance_at_top_is_none_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "stage.json"
            stages.advance(p, NAMES, "", NOW)
            stages.advance(p, NAMES, "", NOW)
            self.assertIsNone(stages.advance(p, NAMES, "", NOW))
            self.assertEqual(stages.load_state(p)["current"], "Deep water")
            self.assertEqual(len(stages.load_state(p)["history"]), 2)

    def test_retreat_moves_back_and_records(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "stage.json"
            stages.advance(p, NAMES, "up", NOW)
            back = stages.retreat(p, NAMES, NOW)
            self.assertEqual(back, "Settling in")
            state = stages.load_state(p)
            self.assertEqual(state["current"], "Settling in")
            self.assertEqual(state["history"][-1],
                             {"stage": "Settling in", "date": "2026-07-06", "note": ""})

    def test_retreat_at_bottom_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "stage.json"
            self.assertIsNone(stages.retreat(p, NAMES, NOW))
            self.assertFalse(p.exists())  # no state file conjured from nothing


class TestStageBlock(unittest.TestCase):
    def test_first_stage_no_history(self):
        block = stages.stage_block(NAMES, TEXTS,
                                   {"current": None, "history": []}, "Wren")
        self.assertIn("# Where the two of you are", block)
        self.assertIn("pace belongs entirely to\nWren", block)
        self.assertIn("stage named: Settling in", block)
        self.assertIn("calm text", block)
        self.assertNotIn(stages.STAGE_ROAD_HEADER, block)
        self.assertNotIn("never0", block)  # no format residue

    def test_current_stage_text_swaps(self):
        block = stages.stage_block(
            NAMES, TEXTS, {"current": "Deep water", "history": []}, "Wren")
        self.assertIn("deep text", block)
        self.assertNotIn("calm text", block)

    def test_history_renders_in_order_with_and_without_note(self):
        state = {"current": "Deep water", "history": [
            {"stage": "In rhythm", "date": "2026-06-01", "note": "the rain talk"},
            {"stage": "Deep water", "date": "2026-07-01", "note": ""},
        ]}
        block = stages.stage_block(NAMES, TEXTS, state, "Wren")
        self.assertIn(stages.STAGE_ROAD_HEADER, block)
        self.assertIn('- 2026-06-01 - you entered "In rhythm" - marked: "the rain talk"',
                      block)
        self.assertIn('- 2026-07-01 - you entered "Deep water"', block)
        self.assertLess(block.index("2026-06-01"), block.index("2026-07-01"))
