"""facts_extract.py: the D1 extraction pipeline built on facts.py's pure core
-- the extractor system prompt, since-cursor material assembly, response
parsing, the pure normaliser, and extract_once's eligibility-to-store attempt.

The engine is ALWAYS stubbed at the consumer seam
(everthine.facts_extract.engine.try_run_once), never called for real, exactly
as tests/test_diary.py stubs everthine.diary.engine.try_run_once. Persona
loading runs against a real tmp persona folder, never a model. Conventions
follow tests/test_diary.py (the _cfg() helper, tz-aware fixtures, the engine
monkeypatch idiom) and tests/test_facts.py (the tmp-dir Config style)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from everthine import archive, facts, facts_extract
from everthine.config import load_config
from everthine.engine import EngineReply
from everthine.persona import reset_persona_cache

BASE_ENV = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
TODAY_STR = "2026-07-11"

ENGINE_SEAM = "everthine.facts_extract.engine.try_run_once"

# A well-formed extractor reply and the fact it should store.
GOOD_JSON = ('[{"date": "2026-07-11", "content": "started learning the piano", '
             '"category": "interest"}]')
STORED_TEXT = "started learning the piano"


def _cfg(td, **overrides):
    env = {**BASE_ENV, "DATA_DIR": str(td), **overrides}
    return load_config(env)


def _seed(cfg, speaker, text, *, minutes_ago):
    """Write one archive entry `minutes_ago` before NOW (tz-aware)."""
    archive.write_entry(cfg.archive_dir, speaker, text,
                        ts=NOW - timedelta(minutes=minutes_ago))


# ---------------------------------------------------------------------
# 1. Extractor system prompt
# ---------------------------------------------------------------------

class TestExtractorSystemPrompt(unittest.TestCase):
    def test_formats_both_placeholders_without_keyerror(self):
        out = facts_extract.EXTRACTOR_SYSTEM_PROMPT.format(
            partner_name="Wren", today="2026-07-11")
        self.assertIn("about Wren from the conversation", out)
        self.assertIn("Today's date: 2026-07-11", out)

    def test_doubled_braces_collapse_to_literal_json(self):
        out = facts_extract.EXTRACTOR_SYSTEM_PROMPT.format(
            partner_name="Wren", today="2026-07-11")
        # The doubled {{ }} in the source render as single literal braces...
        self.assertIn('{"date": "YYYY-MM-DD", "content": "concrete statement"', out)
        self.assertIn('[{"date": "2026-07-11", "content": "prefers matcha latte over coffee"', out)
        # ...and no unresolved doubled brace survives.
        self.assertNotIn("{{", out)
        self.assertNotIn("}}", out)

    def test_lists_the_six_categories(self):
        out = facts_extract.EXTRACTOR_SYSTEM_PROMPT.format(partner_name="W", today="T")
        self.assertIn("interest / mood / stress / follow_up / life_event / conflict", out)


# ---------------------------------------------------------------------
# 2. Material assembly
# ---------------------------------------------------------------------

class TestBuildMaterialRendering(unittest.TestCase):
    def test_renders_one_line_per_entry_in_archive_speaker_labels(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed(cfg, "user", "hi there", minutes_ago=40)
            _seed(cfg, "companion", "hey you", minutes_ago=39)
            material, _ = facts_extract.build_material(cfg, None)
        self.assertEqual(material, "user: hi there\ncompanion: hey you")

    def test_returned_ts_is_newest_included_entry(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed(cfg, "user", "older", minutes_ago=40)
            _seed(cfg, "companion", "newest", minutes_ago=38)
            _, ts = facts_extract.build_material(cfg, None)
        self.assertEqual(ts, (NOW - timedelta(minutes=38)).isoformat())

    def test_no_entries_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            self.assertIsNone(facts_extract.build_material(cfg, None))


class TestBuildMaterialSinceCursor(unittest.TestCase):
    def test_filters_to_entries_at_or_after_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed(cfg, "user", "before cursor", minutes_ago=60)
            _seed(cfg, "user", "at cursor", minutes_ago=50)
            _seed(cfg, "companion", "after cursor", minutes_ago=40)
            cursor = NOW - timedelta(minutes=50)  # iter_entries is inclusive of ==
            material, ts = facts_extract.build_material(cfg, cursor)
        self.assertIn("at cursor", material)          # boundary is inclusive
        self.assertIn("after cursor", material)
        self.assertNotIn("before cursor", material)   # strictly older is dropped
        self.assertEqual(ts, (NOW - timedelta(minutes=40)).isoformat())

    def test_cursor_past_everything_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed(cfg, "user", "old", minutes_ago=90)
            self.assertIsNone(facts_extract.build_material(cfg, NOW))


class TestBuildMaterialTruncation(unittest.TestCase):
    def test_tail_truncation_keeps_newest_whole_lines_and_ts(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            # 20 user turns, one per minute, oldest first.
            for i in range(20):
                _seed(cfg, "user", f"line {i:03d}", minutes_ago=40 - i)
            expected_lines = [f"user: line {i:03d}" for i in range(20)]
            orig = facts_extract.FACTS_MATERIAL_MAX_CHARS
            facts_extract.FACTS_MATERIAL_MAX_CHARS = 80
            self.addCleanup(setattr, facts_extract, "FACTS_MATERIAL_MAX_CHARS", orig)
            material, ts = facts_extract.build_material(cfg, None)
        kept = material.split("\n")
        self.assertGreater(len(kept), 0)
        self.assertLess(len(kept), 20)                                  # some were dropped
        self.assertTrue(all(line in expected_lines for line in kept))   # no mid-line fragment
        self.assertEqual(kept, expected_lines[len(expected_lines) - len(kept):])  # newest tail
        self.assertLessEqual(len(material), 80)                         # within the cap
        # Truncation drops the OLDEST; the newest entry (and its ts) survives.
        self.assertEqual(kept[-1], "user: line 019")
        self.assertEqual(ts, (NOW - timedelta(minutes=21)).isoformat())  # 40 - 19


# ---------------------------------------------------------------------
# 3. Parse
# ---------------------------------------------------------------------

class TestParseExtractorOutput(unittest.TestCase):
    def test_direct_array(self):
        out = facts_extract.parse_extractor_output(GOOD_JSON)
        self.assertEqual(out, [{"date": "2026-07-11", "content": STORED_TEXT,
                                "category": "interest"}])

    def test_valid_empty_array_is_empty_list_not_none(self):
        out = facts_extract.parse_extractor_output("[]")
        self.assertEqual(out, [])
        self.assertIsNotNone(out)  # genuine empty, NOT a parse failure

    def test_prose_wrapped_array_via_slice(self):
        raw = 'Sure, here are the facts:\n[{"content": "likes tea"}]\nHope that helps!'
        self.assertEqual(facts_extract.parse_extractor_output(raw),
                         [{"content": "likes tea"}])

    def test_markdown_fenced_array_via_slice(self):
        raw = '```json\n[{"content": "walks at dawn"}]\n```'
        self.assertEqual(facts_extract.parse_extractor_output(raw),
                         [{"content": "walks at dawn"}])

    def test_non_list_json_is_empty_list(self):
        # Valid JSON, wrong shape -> genuine empty (advance the cursor), NOT None.
        self.assertEqual(facts_extract.parse_extractor_output('{"content": "x"}'), [])

    def test_garbage_returns_none_with_warning(self):
        with self.assertLogs("everthine", level="WARNING") as cm:
            out = facts_extract.parse_extractor_output("just rambling, no json here")
        self.assertIsNone(out)
        self.assertTrue(any("could not parse extractor output" in line for line in cm.output))

    def test_unbalanced_brackets_return_none(self):
        with self.assertLogs("everthine", level="WARNING"):
            self.assertIsNone(facts_extract.parse_extractor_output("[not valid json at all"))


# ---------------------------------------------------------------------
# 4. Normalise (pure)
# ---------------------------------------------------------------------

class TestNormaliseFacts(unittest.TestCase):
    def test_content_becomes_text(self):
        out = facts_extract.normalise_facts(
            [{"content": "loves matcha", "category": "interest", "date": "2026-07-01"}],
            TODAY_STR)
        self.assertEqual(out, [{"text": "loves matcha", "category": "interest",
                                "date": "2026-07-01"}])

    def test_text_field_is_the_fallback_for_content(self):
        out = facts_extract.normalise_facts([{"text": "from text field"}], TODAY_STR)
        self.assertEqual(out[0]["text"], "from text field")

    def test_text_truncated_to_200(self):
        out = facts_extract.normalise_facts([{"content": "z" * 250}], TODAY_STR)
        self.assertEqual(len(out[0]["text"]), 200)

    def test_blank_and_missing_text_dropped(self):
        out = facts_extract.normalise_facts(
            [{"content": "   "}, {"category": "mood"}, {"content": "", "text": ""}],
            TODAY_STR)
        self.assertEqual(out, [])

    def test_category_defaults_to_life_event(self):
        out = facts_extract.normalise_facts(
            [{"content": "a"}, {"content": "b", "category": "  "}], TODAY_STR)
        self.assertEqual([f["category"] for f in out], ["life_event", "life_event"])

    def test_unknown_category_kept_as_is(self):
        out = facts_extract.normalise_facts(
            [{"content": "a", "category": "banana"}], TODAY_STR)
        self.assertEqual(out[0]["category"], "banana")

    def test_date_defaults_to_today(self):
        out = facts_extract.normalise_facts([{"content": "a"}], TODAY_STR)
        self.assertEqual(out[0]["date"], TODAY_STR)

    def test_non_dict_entries_dropped(self):
        out = facts_extract.normalise_facts(
            ["a string", 42, None, {"content": "kept"}], TODAY_STR)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "kept")


# ---------------------------------------------------------------------
# 5. newest_user_ts
# ---------------------------------------------------------------------

class TestNewestUserTs(unittest.TestCase):
    def test_none_when_no_user_entry(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed(cfg, "companion", "only me", minutes_ago=10)
            self.assertIsNone(facts_extract.newest_user_ts(cfg))

    def test_max_user_timestamp_ignoring_companion(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed(cfg, "user", "first", minutes_ago=40)
            _seed(cfg, "companion", "later but not user", minutes_ago=5)
            _seed(cfg, "user", "newest user", minutes_ago=20)
            got = facts_extract.newest_user_ts(cfg)
        self.assertEqual(got, NOW - timedelta(minutes=20))


# ---------------------------------------------------------------------
# 6. extract_once -- the execution line (engine ALWAYS stubbed)
# ---------------------------------------------------------------------

def _persona_folder(td):
    """Write a minimal valid folder-mode persona under td and return it."""
    folder = Path(td) / "persona"
    folder.mkdir()
    (folder / "identity.md").write_text("A steady, warm companion.", encoding="utf-8")
    (folder / "settings.yaml").write_text(
        "companion:\n  name: Alex\npartner:\n  name: Sam\n", encoding="utf-8")
    return folder


class TestExtractOnce(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def _folder_cfg(self, td, **overrides):
        return _cfg(td, PERSONA_PATH=str(_persona_folder(td)), **overrides)

    def _cursor(self, cfg):
        return facts.load_state(cfg.facts_state_path)["last_extracted_ts"]

    def _seed_idle_user(self, cfg, minutes_ago=40):
        """One user turn old enough to pass the 30-minute idle gate."""
        _seed(cfg, "user", "I started learning the piano last week", minutes_ago=minutes_ago)

    # --- happy path: stores, advances cursor to material ts (not now) ---------

    def test_success_stores_fact_and_advances_cursor_to_material_ts(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg, minutes_ago=40)
            good = EngineReply(GOOD_JSON, "s", ok=True)
            with mock.patch(ENGINE_SEAM, return_value=good) as run, \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            stored = facts.load_facts(cfg.facts_path)
            cursor = self._cursor(cfg)
        self.assertTrue(result)
        self.assertEqual([f["text"] for f in stored], [STORED_TEXT])
        # Cursor is the material's newest ts, NOT now.
        self.assertEqual(cursor, (NOW - timedelta(minutes=40)).isoformat())
        self.assertNotEqual(cursor, NOW.isoformat())
        self.assertTrue(any("facts: stored 1 fact(s)" in line for line in cm.output))
        # Engine kwargs pinned: fresh session, own timeout, extractor system prompt.
        kwargs = run.call_args.kwargs
        self.assertIsNone(kwargs["session_id"])
        self.assertEqual(kwargs["timeout_s"], facts_extract.FACTS_TIMEOUT_S)
        self.assertIn("memory extraction assistant", kwargs["system_prompt"])
        self.assertIn("about Sam", kwargs["system_prompt"])  # partner_name filled
        # User prompt carries the instruction line + the material.
        self.assertTrue(run.call_args.args[1].startswith(facts_extract.EXTRACT_INSTRUCTION))
        self.assertIn("I started learning the piano", run.call_args.args[1])

    # --- eligibility skips: no engine call, cursor untouched ------------------

    def test_disabled_skip(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td, FACTS_ENABLED="false")
            self._seed_idle_user(cfg)
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        run.assert_not_called()
        self.assertEqual(cursor, "")
        self.assertTrue(any("facts: skip (disabled)" in line for line in cm.output))

    def test_idle_not_reached_skip(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg, minutes_ago=5)  # spoke 5 min ago: still active
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = facts_extract.extract_once(cfg, NOW)
        self.assertFalse(result)
        run.assert_not_called()
        self.assertTrue(any("facts: skip (idle_not_reached)" in line for line in cm.output))

    def test_no_new_material_skip_when_cursor_past_last_user(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg, minutes_ago=40)
            # Cursor already at/after the newest user turn -> nothing new.
            facts.save_state(cfg.facts_state_path, {"last_extracted_ts": NOW.isoformat()})
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        run.assert_not_called()
        self.assertEqual(cursor, NOW.isoformat())  # unchanged
        self.assertTrue(any("facts: skip (no_new_material)" in line for line in cm.output))

    def test_file_mode_persona_skip_without_storing(self):
        with tempfile.TemporaryDirectory() as td:
            persona_file = Path(td) / "persona.md"
            persona_file.write_text("You are Testbot.", encoding="utf-8")
            cfg = _cfg(td, PERSONA_PATH=str(persona_file))
            self._seed_idle_user(cfg)
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        run.assert_not_called()  # bailed before the engine
        self.assertEqual(cursor, "")
        self.assertTrue(any("facts: skip (file_mode)" in line for line in cm.output))

    # --- engine outcomes that must NOT advance the cursor --------------------

    def test_engine_busy_no_advance(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg)
            with mock.patch(ENGINE_SEAM, return_value=None) as run, \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        run.assert_called_once()
        self.assertEqual(cursor, "")  # cursor never advanced
        self.assertTrue(any("facts: skip (engine_busy)" in line for line in cm.output))

    def test_engine_failed_no_advance(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg)
            failed = EngineReply("", None, ok=False, error_kind="timeout")
            with mock.patch(ENGINE_SEAM, return_value=failed), \
                    self.assertLogs("everthine", level="WARNING") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        self.assertEqual(cursor, "")
        self.assertTrue(any("facts: extraction failed (timeout)" in line for line in cm.output))

    def test_parse_failure_no_advance_and_stores_nothing(self):
        # The surfaced wrinkle: a parse failure returns [] from nothing -- it
        # must NOT advance the cursor (retry the same material), unlike a
        # genuine empty answer below.
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg)
            garbage = EngineReply("I couldn't find anything, sorry!", "s", ok=True)
            with mock.patch(ENGINE_SEAM, return_value=garbage), \
                    self.assertLogs("everthine", level="WARNING") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            stored = facts.load_facts(cfg.facts_path)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        self.assertEqual(stored, [])
        self.assertEqual(cursor, "")  # NOT advanced -- retry next tick
        self.assertTrue(any("could not parse extractor output" in line for line in cm.output))

    # --- outcomes that DO advance the cursor (material was examined) ----------

    def test_genuine_empty_answer_advances_cursor(self):
        # Valid JSON empty array: the model looked and kept nothing. The cursor
        # MUST advance, or the same silence is re-extracted every tick forever.
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg, minutes_ago=40)
            empty = EngineReply("[]", "s", ok=True)
            with mock.patch(ENGINE_SEAM, return_value=empty), \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            stored = facts.load_facts(cfg.facts_path)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        self.assertEqual(stored, [])
        self.assertEqual(cursor, (NOW - timedelta(minutes=40)).isoformat())
        self.assertTrue(any("facts: nothing worth keeping" in line for line in cm.output))

    def test_normalise_drops_everything_advances_cursor(self):
        # Valid array, but every entry is blank -> normalise yields nothing.
        # Still "examined to a conclusion" -> advance the cursor.
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg, minutes_ago=40)
            blank = EngineReply('[{"content": "   "}, {"category": "mood"}]', "s", ok=True)
            with mock.patch(ENGINE_SEAM, return_value=blank), \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            stored = facts.load_facts(cfg.facts_path)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        self.assertEqual(stored, [])
        self.assertEqual(cursor, (NOW - timedelta(minutes=40)).isoformat())
        self.assertTrue(any("facts: nothing worth keeping" in line for line in cm.output))

    def test_all_duplicates_advances_cursor_and_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg, minutes_ago=40)
            # Pre-seed the exact fact the engine will re-extract.
            facts.append_facts(cfg.facts_path,
                               [{"text": STORED_TEXT, "category": "interest",
                                 "date": "2026-07-10"}], cfg.facts_max)
            good = EngineReply(GOOD_JSON, "s", ok=True)
            with mock.patch(ENGINE_SEAM, return_value=good), \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = facts_extract.extract_once(cfg, NOW)
            stored = facts.load_facts(cfg.facts_path)
            cursor = self._cursor(cfg)
        self.assertFalse(result)
        self.assertEqual(len(stored), 1)  # nothing new appended
        self.assertEqual(cursor, (NOW - timedelta(minutes=40)).isoformat())
        self.assertTrue(any("facts: all duplicates" in line for line in cm.output))

    # --- the cursor rule: entries during the engine call stay beyond it ------

    def test_entries_arriving_during_engine_call_do_not_move_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            self._seed_idle_user(cfg, minutes_ago=40)  # material snapshot = NOW-40

            def _append_then_reply(*args, **kwargs):
                # A newer turn lands WHILE the engine is "running".
                _seed(cfg, "user", "a brand new thing she just said", minutes_ago=1)
                return EngineReply(GOOD_JSON, "s", ok=True)

            with mock.patch(ENGINE_SEAM, side_effect=_append_then_reply):
                result = facts_extract.extract_once(cfg, NOW)
            cursor = self._cursor(cfg)
        self.assertTrue(result)
        # Cursor is the material's snapshot ts (NOW-40), never re-scanned to the
        # NOW-1 entry that arrived mid-call -- that stays beyond it for next round.
        self.assertEqual(cursor, (NOW - timedelta(minutes=40)).isoformat())


if __name__ == "__main__":
    unittest.main()
