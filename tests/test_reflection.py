"""reflection.py: the gate pure function, state-file round-trips, prompt
assembly from a persona, engine-output parsing, and jsonl append/prune.
Conventions follow tests/test_diary.py (corpse-file assertions, the _cfg()
Config-building helper, the _persona() fixture builder)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from everthine import reflection
from everthine.config import load_config
from everthine.persona import Persona, PersonaSettings

BASE_ENV = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}

TODAY = "2026-07-06"
YESTERDAY = "2026-07-05"
NOW = datetime(2026, 7, 6, 21, 30, tzinfo=timezone.utc)
FRESH_STATE = {"count_date": None, "count_today": 0}


def _cfg(td, **overrides):
    env = {**BASE_ENV, "DATA_DIR": str(td), **overrides}
    return load_config(env)


IDENTITY_TEXT = ("Ledger-keeper by day, storyteller by night, always half a "
                 "page ahead in the book on the nightstand.")
VOICE_TEXT = "Short sentences. Warm and a little wry, never flowery."


def _persona(*, identity_text=IDENTITY_TEXT, voice_text="", boundaries_text="",
             companion_name="Alex", partner_name="Sam", living="together"):
    settings = PersonaSettings(
        companion_name=companion_name, partner_name=partner_name, living=living)
    return Persona(mode="folder", identity_text=identity_text, voice_text=voice_text,
                   boundaries_text=boundaries_text, settings=settings)


# ---------------------------------------------------------------------
# should_reflect: pure gate, no I/O, no clock of its own
# ---------------------------------------------------------------------

class TestShouldReflect(unittest.TestCase):
    def test_disabled_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, REFLECTION_ENABLED="false")
            reason = reflection.should_reflect(cfg, "a message long enough to pass", FRESH_STATE, NOW)
        self.assertEqual(reason, "disabled")

    def test_nineteen_chars_after_strip_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            msg = "a" * 19
            self.assertEqual(len(msg.strip()), 19)
            reason = reflection.should_reflect(cfg, msg, FRESH_STATE, NOW)
        self.assertEqual(reason, "too_short")

    def test_twenty_chars_after_strip_passes(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            msg = "  " + ("a" * 20) + "  "  # padding whitespace either side
            self.assertEqual(len(msg.strip()), 20)
            reason = reflection.should_reflect(cfg, msg, FRESH_STATE, NOW)
        self.assertIsNone(reason)

    def test_cap_reached_today_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, REFLECTION_DAILY_CAP="12")
            state = {"count_date": TODAY, "count_today": 12}
            reason = reflection.should_reflect(cfg, "a message long enough to pass", state, NOW)
        self.assertEqual(reason, "cap")

    def test_stale_count_date_yesterday_full_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, REFLECTION_DAILY_CAP="12")
            state = {"count_date": YESTERDAY, "count_today": 12}
            reason = reflection.should_reflect(cfg, "a message long enough to pass", state, NOW)
        self.assertIsNone(reason)

    def test_all_clear_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            reason = reflection.should_reflect(cfg, "a message long enough to pass", FRESH_STATE, NOW)
        self.assertIsNone(reason)


# ---------------------------------------------------------------------
# State file: cfg.reflection_state_path (data/reflection_state.json)
# ---------------------------------------------------------------------

class TestLoadState(unittest.TestCase):
    def test_missing_file_is_fresh_state(self):
        with tempfile.TemporaryDirectory() as td:
            state = reflection.load_state(Path(td) / "reflection_state.json")
        self.assertEqual(state, FRESH_STATE)

    def test_corrupt_json_degrades_and_keeps_corpse(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "reflection_state.json"
            p.write_text("{not json", encoding="utf-8")
            state = reflection.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            corpses = list(Path(td).glob("reflection_state.json.corrupt-*"))
            self.assertEqual(len(corpses), 1)

    def test_wrong_shape_list_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "reflection_state.json"
            p.write_text('["not", "a", "dict"]', encoding="utf-8")
            state = reflection.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("reflection_state.json.corrupt-*"))), 1)

    def test_count_today_as_bool_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "reflection_state.json"
            p.write_text('{"count_date": null, "count_today": true}', encoding="utf-8")
            state = reflection.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("reflection_state.json.corrupt-*"))), 1)


class TestRecordWritten(unittest.TestCase):
    def test_first_write_sets_count_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "reflection_state.json"
            reflection.record_written(p, NOW)
            state = reflection.load_state(p)
        self.assertEqual(state["count_today"], 1)
        self.assertEqual(state["count_date"], TODAY)

    def test_second_write_same_day_accumulates(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "reflection_state.json"
            reflection.record_written(p, NOW)
            reflection.record_written(p, NOW)
            state = reflection.load_state(p)
        self.assertEqual(state["count_today"], 2)
        self.assertEqual(state["count_date"], TODAY)

    def test_write_next_day_rolls_over_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "reflection_state.json"
            yesterday = NOW - timedelta(days=1)
            reflection.record_written(p, yesterday)
            reflection.record_written(p, yesterday)
            reflection.record_written(p, NOW)
            state = reflection.load_state(p)
        self.assertEqual(state["count_today"], 1)
        self.assertEqual(state["count_date"], TODAY)


# ---------------------------------------------------------------------
# build_reflection_prompt: pure, folder-mode only
# ---------------------------------------------------------------------

class TestBuildReflectionPrompt(unittest.TestCase):
    def test_five_block_order_with_voice(self):
        system_prompt, _ = reflection.build_reflection_prompt(
            _persona(voice_text=VOICE_TEXT), "how was your day", "it was quiet, mostly")
        self.assertTrue(system_prompt.startswith("# Who you are"))
        self.assertTrue(system_prompt.endswith(reflection.REFLECTION_FORMAT_LINE))
        self.assertLess(system_prompt.index("# Who you are"), system_prompt.index(IDENTITY_TEXT))
        self.assertLess(system_prompt.index(IDENTITY_TEXT), system_prompt.index(VOICE_TEXT))
        self.assertLess(system_prompt.index(VOICE_TEXT),
                         system_prompt.index("# A moment in your own head"))
        self.assertLess(system_prompt.index("# A moment in your own head"),
                         system_prompt.index(reflection.REFLECTION_FORMAT_LINE))

    def test_empty_voice_no_stray_blank(self):
        system_prompt, _ = reflection.build_reflection_prompt(
            _persona(voice_text=""), "how was your day", "it was quiet, mostly")
        self.assertNotIn(VOICE_TEXT, system_prompt)
        self.assertNotIn("\n\n\n", system_prompt)
        self.assertIn(IDENTITY_TEXT, system_prompt)
        self.assertTrue(system_prompt.endswith(reflection.REFLECTION_FORMAT_LINE))

    def test_subject_reminder_pin(self):
        system_prompt, _ = reflection.build_reflection_prompt(
            _persona(), "how was your day", "it was quiet, mostly")
        self.assertIn("don't put what's yours under their name", system_prompt)

    def test_excludes_dna_boundaries_stage(self):
        system_prompt, _ = reflection.build_reflection_prompt(
            _persona(voice_text=VOICE_TEXT, boundaries_text="Never mention the storm."),
            "how was your day", "it was quiet, mostly")
        self.assertNotIn("# The ground rules", system_prompt)
        self.assertNotIn("## Their boundaries, in their own words", system_prompt)
        self.assertNotIn("Never mention the storm.", system_prompt)

    def test_user_prompt_exact(self):
        _, user_prompt = reflection.build_reflection_prompt(
            _persona(partner_name="Wren"), "how was your day", "it was quiet, mostly")
        self.assertEqual(user_prompt, "Wren: how was your day\nYou: it was quiet, mostly")

    def test_file_mode_persona_raises(self):
        with self.assertRaises(ValueError):
            reflection.build_reflection_prompt(
                Persona(mode="file", raw_text="You are Testbot."), "hi there, how are you", "hello")

    def test_format_line_literal_braces_intact(self):
        system_prompt, _ = reflection.build_reflection_prompt(
            _persona(), "how was your day", "it was quiet, mostly")
        self.assertIn('{"text": "one or two sentences"}', system_prompt)
        with self.assertRaises(KeyError):
            reflection.REFLECTION_FORMAT_LINE.format()


# ---------------------------------------------------------------------
# parse_output: two parse strategies + shape validation
# ---------------------------------------------------------------------

class TestParseOutput(unittest.TestCase):
    def test_direct_json(self):
        raw = '{"text": "a quiet ripple, nothing more"}'
        self.assertEqual(reflection.parse_output(raw), "a quiet ripple, nothing more")

    def test_fenced_json_caught_by_bare_object_regex(self):
        raw = '```json\n{"text": "fenced thought"}\n```'
        self.assertEqual(reflection.parse_output(raw), "fenced thought")

    def test_bare_object_with_surrounding_prose(self):
        raw = ('Here is what drifted through:\n'
               '{"text": "quiet thoughts"}\n'
               'Hope that captures it.')
        self.assertEqual(reflection.parse_output(raw), "quiet thoughts")

    def test_empty_text_is_none(self):
        self.assertIsNone(reflection.parse_output('{"text": ""}'))

    def test_whitespace_only_text_is_none(self):
        self.assertIsNone(reflection.parse_output('{"text": "   "}'))

    def test_non_string_text_is_none(self):
        self.assertIsNone(reflection.parse_output('{"text": 3}'))

    def test_json_list_is_none(self):
        self.assertIsNone(reflection.parse_output('["x"]'))

    def test_garbage_text_is_none(self):
        self.assertIsNone(reflection.parse_output("just rambling, no structure here at all"))


# ---------------------------------------------------------------------
# append_entry: cfg.reflections_path (data/reflections.jsonl)
# ---------------------------------------------------------------------

class TestAppendEntry(unittest.TestCase):
    def test_two_appends_two_valid_lines_with_id_and_offset(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            reflection.append_entry(cfg, "a quiet thought", NOW)
            reflection.append_entry(cfg, "another passing one", NOW)
            lines = cfg.reflections_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        texts = []
        for line in lines:
            entry = json.loads(line)  # each line must be legal JSON on its own
            texts.append(entry["text"])
            self.assertRegex(entry["id"], r"^[0-9a-f]{8}$")
            created = datetime.fromisoformat(entry["created_at"])
            self.assertIsNotNone(created.utcoffset())
        self.assertEqual(texts, ["a quiet thought", "another passing one"])


# ---------------------------------------------------------------------
# prune: retention + malformed-line sweep, rewrite only when something drops
# ---------------------------------------------------------------------

def _write_raw_lines(path, dict_lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(d) for d in dict_lines) + "\n", encoding="utf-8")


class TestPrune(unittest.TestCase):
    def test_expired_removed_recent_kept_and_logged(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            old = NOW - timedelta(days=61)
            recent = NOW - timedelta(days=1)
            _write_raw_lines(cfg.reflections_path, [
                {"id": "aaaaaaaa", "created_at": old.isoformat(), "text": "old one"},
                {"id": "bbbbbbbb", "created_at": recent.isoformat(), "text": "recent one"},
            ])
            with self.assertLogs("everthine", level="INFO"):
                reflection.prune(cfg, NOW)
            lines = cfg.reflections_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "recent one")

    def test_malformed_line_removed_with_log(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            recent = NOW - timedelta(days=1)
            cfg.reflections_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.reflections_path.write_text(
                "{not json\n" + json.dumps(
                    {"id": "bbbbbbbb", "created_at": recent.isoformat(), "text": "good one"}) + "\n",
                encoding="utf-8")
            with self.assertLogs("everthine", level="INFO"):
                reflection.prune(cfg, NOW)
            lines = cfg.reflections_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "good one")

    def test_all_fresh_file_byte_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            recent = NOW - timedelta(days=1)
            _write_raw_lines(cfg.reflections_path, [
                {"id": "aaaaaaaa", "created_at": recent.isoformat(), "text": "fresh"},
            ])
            before = cfg.reflections_path.read_bytes()
            reflection.prune(cfg, NOW)
            after = cfg.reflections_path.read_bytes()
        self.assertEqual(before, after)

    def test_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            reflection.prune(cfg, NOW)  # must not raise; nothing to prune

    def test_naive_created_at_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            recent_naive = (NOW - timedelta(days=1)).replace(tzinfo=None)
            _write_raw_lines(cfg.reflections_path, [
                {"id": "aaaaaaaa", "created_at": recent_naive.isoformat(), "text": "naive"},
            ])
            reflection.prune(cfg, NOW)  # must not raise; naive compared as local
            lines = cfg.reflections_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
