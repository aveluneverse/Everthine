"""diary.py: state-file round-trips, fail-soft parsing, eligibility's
pure decision table, engine-output parsing (three strategies + the
decline sentinel), sensitive-data redaction, and save/read of entries.
Conventions follow tests/test_stages.py (corpse-file assertions) and
tests/test_album.py (the _cfg() Config-building helper below mirrors)."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from everthine import album, archive, diary
from everthine.config import load_config
from everthine.persona import Persona, PersonaSettings

BASE_ENV = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}

TODAY = "2026-07-06"
YESTERDAY = "2026-07-05"
NOW = datetime(2026, 7, 6, 21, 30, tzinfo=timezone.utc)
FRESH_STATE = {"count_date": None, "count_today": 0, "declined_date": None}


def _cfg(td, **overrides):
    env = {**BASE_ENV, "DATA_DIR": str(td), **overrides}
    return load_config(env)


def _aware(hour, minute=0, day=6):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


class TestLoadState(unittest.TestCase):
    def test_missing_file_is_fresh_state(self):
        with tempfile.TemporaryDirectory() as td:
            state = diary.load_state(Path(td) / "diary_state.json")
        self.assertEqual(state, FRESH_STATE)

    def test_corrupt_json_degrades_and_keeps_corpse(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diary_state.json"
            p.write_text("{not json", encoding="utf-8")
            state = diary.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            corpses = list(Path(td).glob("diary_state.json.corrupt-*"))
            self.assertEqual(len(corpses), 1)

    def test_wrong_shape_list_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diary_state.json"
            p.write_text('["not", "a", "dict"]', encoding="utf-8")
            state = diary.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("diary_state.json.corrupt-*"))), 1)

    def test_count_today_as_string_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diary_state.json"
            p.write_text(
                '{"count_date": "2026-07-06", "count_today": "3", "declined_date": null}',
                encoding="utf-8")
            state = diary.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("diary_state.json.corrupt-*"))), 1)

    def test_count_today_as_bool_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diary_state.json"
            p.write_text(
                '{"count_date": null, "count_today": true, "declined_date": null}',
                encoding="utf-8")
            state = diary.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("diary_state.json.corrupt-*"))), 1)


class TestRecordWritten(unittest.TestCase):
    def test_first_write_sets_count_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diary_state.json"
            diary.record_written(p, NOW)
            state = diary.load_state(p)
        self.assertEqual(state["count_today"], 1)
        self.assertEqual(state["count_date"], TODAY)

    def test_second_write_same_day_accumulates(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diary_state.json"
            diary.record_written(p, NOW)
            diary.record_written(p, NOW)
            state = diary.load_state(p)
        self.assertEqual(state["count_today"], 2)
        self.assertEqual(state["count_date"], TODAY)

    def test_write_next_day_rolls_over_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diary_state.json"
            yesterday = NOW - timedelta(days=1)
            diary.record_written(p, yesterday)
            diary.record_written(p, yesterday)
            diary.record_written(p, NOW)
            state = diary.load_state(p)
        self.assertEqual(state["count_today"], 1)
        self.assertEqual(state["count_date"], TODAY)


class TestRecordDeclined(unittest.TestCase):
    def test_sets_declined_date_leaves_count_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diary_state.json"
            diary.record_written(p, NOW)
            diary.record_declined(p, NOW)
            state = diary.load_state(p)
        self.assertEqual(state["declined_date"], TODAY)
        self.assertEqual(state["count_today"], 1)
        self.assertEqual(state["count_date"], TODAY)


class TestEligibility(unittest.TestCase):
    def test_disabled_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, DIARY_ENABLED="false")
            now = _aware(22)
            reason = diary.eligibility(cfg, now, now - timedelta(minutes=40),
                                        FRESH_STATE, 100.0)
        self.assertEqual(reason, "disabled")

    def test_window_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            cases = (
                (7, 59, None),        # inside the window (early-morning edge)
                (8, 0, "window"),     # window closes at 08:00
                (20, 59, "window"),   # not open yet
                (21, 0, None),        # window opens at 21:00
            )
            for hour, minute, expected in cases:
                with self.subTest(hour=hour, minute=minute):
                    now = _aware(hour, minute)
                    last_contact = now - timedelta(minutes=31)
                    reason = diary.eligibility(cfg, now, last_contact, FRESH_STATE, 100.0)
                    self.assertEqual(reason, expected)

    def test_already_written_today_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, DIARY_MAX_DAILY="1")
            now = _aware(22)
            state = {"count_date": TODAY, "count_today": 1, "declined_date": None}
            reason = diary.eligibility(cfg, now, now - timedelta(minutes=40), state, 100.0)
        self.assertEqual(reason, "already_written")

    def test_already_written_yesterday_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, DIARY_MAX_DAILY="1")
            now = _aware(22)
            state = {"count_date": YESTERDAY, "count_today": 5, "declined_date": None}
            reason = diary.eligibility(cfg, now, now - timedelta(minutes=40), state, 100.0)
        self.assertIsNone(reason)

    def test_declined_today_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(22)
            state = {"count_date": None, "count_today": 0, "declined_date": TODAY}
            reason = diary.eligibility(cfg, now, now - timedelta(minutes=40), state, 100.0)
        self.assertEqual(reason, "declined")

    def test_declined_yesterday_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(22)
            state = {"count_date": None, "count_today": 0, "declined_date": YESTERDAY}
            reason = diary.eligibility(cfg, now, now - timedelta(minutes=40), state, 100.0)
        self.assertIsNone(reason)

    def test_too_soon(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(22)
            reason = diary.eligibility(cfg, now, now - timedelta(minutes=40), FRESH_STATE, 3.9)
        self.assertEqual(reason, "too_soon")

    def test_no_last_contact(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(22)
            reason = diary.eligibility(cfg, now, None, FRESH_STATE, 100.0)
        self.assertEqual(reason, "no_last_contact")

    def test_not_idle(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(22)
            reason = diary.eligibility(cfg, now, now - timedelta(minutes=29), FRESH_STATE, 100.0)
        self.assertEqual(reason, "not_idle")

    def test_all_clear_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(22)
            reason = diary.eligibility(cfg, now, now - timedelta(minutes=31), FRESH_STATE, 100.0)
        self.assertIsNone(reason)


class TestHoursSinceLastDiary(unittest.TestCase):
    def test_missing_directory_is_inf(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope"
            self.assertEqual(diary.hours_since_last_diary(missing), float("inf"))

    def test_empty_directory_is_inf(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(diary.hours_since_last_diary(Path(td)), float("inf"))

    def test_non_diary_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "notes.json").write_text("{}", encoding="utf-8")
            self.assertEqual(diary.hours_since_last_diary(Path(td)), float("inf"))

    def test_two_files_uses_newest_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            older = Path(td) / "2026-07-01_120000.json"
            newer = Path(td) / "2026-07-05_090000.json"
            older.write_text("{}", encoding="utf-8")
            newer.write_text("{}", encoding="utf-8")
            now = time.time()
            os.utime(older, (now - 10 * 3600, now - 10 * 3600))
            os.utime(newer, (now - 2 * 3600, now - 2 * 3600))
            hours = diary.hours_since_last_diary(Path(td))
        self.assertAlmostEqual(hours, 2.0, delta=0.05)


class TestParseOutput(unittest.TestCase):
    def test_direct_json(self):
        raw = '{"content": "a quiet night", "mood": "content", "keywords": ["rain"]}'
        entry = diary.parse_output(raw)
        self.assertEqual(entry["content"], "a quiet night")
        self.assertEqual(entry["mood"], "content")
        self.assertEqual(entry["keywords"], ["rain"])

    def test_fenced_json(self):
        raw = '```json\n{"content": "fenced entry", "want_to_write": true}\n```'
        entry = diary.parse_output(raw)
        self.assertEqual(entry["content"], "fenced entry")

    def test_bare_object_with_surrounding_prose(self):
        raw = ('Here is what I wrote tonight:\n'
               '{"content": "quiet thoughts", "mood": "calm"}\n'
               'Hope that captures it.')
        entry = diary.parse_output(raw)
        self.assertEqual(entry["content"], "quiet thoughts")

    def test_decline_sentinel_no_content(self):
        entry = diary.parse_output('{"want_to_write": false}')
        self.assertIsNotNone(entry)
        self.assertTrue(diary.is_decline(entry))

    def test_content_present_with_false_want_is_valid_and_not_decline(self):
        entry = diary.parse_output('{"want_to_write": false, "content": "wrote anyway"}')
        self.assertIsNotNone(entry)
        self.assertEqual(entry["content"], "wrote anyway")
        self.assertFalse(diary.is_decline(entry))

    def test_garbage_text_is_none(self):
        self.assertIsNone(diary.parse_output("just rambling, no structure here at all"))

    def test_valid_json_list_is_none(self):
        self.assertIsNone(diary.parse_output('["not", "an", "object"]'))

    def test_content_wrong_type_is_none(self):
        self.assertIsNone(diary.parse_output('{"content": 123}'))

    def test_no_content_and_no_want_to_write_is_none(self):
        self.assertIsNone(diary.parse_output('{"mood": "quiet"}'))


class TestFilterSensitive(unittest.TestCase):
    def test_env_style_assignment_redacted(self):
        text = "config had DATABASE_URL=postgres://user:pass@host/db in it"
        result = diary.filter_sensitive(text)
        self.assertNotIn("postgres://", result)
        self.assertIn("[REDACTED]", result)

    def test_userpass_at_ipv4_redacted(self):
        text = "he pasted admin:hunter2@192.168.0.1 by mistake"
        result = diary.filter_sensitive(text)
        self.assertNotIn("hunter2", result)
        self.assertIn("[REDACTED]", result)

    def test_password_colon_redacted(self):
        text = "she typed password: hunter2 into the form"
        result = diary.filter_sensitive(text)
        self.assertNotIn("hunter2", result)
        self.assertIn("[REDACTED]", result)

    def test_api_key_redacted(self):
        text = "left api_key=abc123 in the note"
        result = diary.filter_sensitive(text)
        self.assertNotIn("abc123", result)
        self.assertIn("[REDACTED]", result)

    def test_ordinary_narrative_untouched(self):
        text = "we met at 10:30 and watched a movie"
        self.assertEqual(diary.filter_sensitive(text), text)

    def test_capitalized_word_without_assignment_untouched(self):
        text = "I said the WORD out loud"
        self.assertEqual(diary.filter_sensitive(text), text)


class TestSaveEntry(unittest.TestCase):
    def test_filename_matches_expected_pattern(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = diary.save_entry(cfg, {"content": "hello"}, NOW)
        self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2}_\d{6}\.json$")

    def test_date_and_shared_always_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = diary.save_entry(
                cfg, {"content": "hello", "date": "1999-01-01", "shared": True}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["date"], TODAY)
        self.assertFalse(data["shared"])

    def test_want_to_write_not_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = diary.save_entry(cfg, {"content": "hello", "want_to_write": True}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("want_to_write", data)

    def test_missing_optional_fields_default(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = diary.save_entry(cfg, {"content": "hello"}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["mood"], "")
        self.assertEqual(data["reflection"], "")
        self.assertEqual(data["keywords"], [])

    def test_content_sensitive_data_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = diary.save_entry(cfg, {"content": "note: api_key=abc123 done"}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("abc123", data["content"])
        self.assertIn("[REDACTED]", data["content"])

    def test_file_is_valid_json_utf8_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = diary.save_entry(cfg, {"content": "hello"}, NOW)
            raw = path.read_bytes().decode("utf-8")
        self.assertTrue(raw.endswith("\n"))
        json.loads(raw)  # must not raise


class TestRecentEntries(unittest.TestCase):
    def test_takes_newest_two_oldest_to_newest(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            cfg.diary_dir.mkdir(parents=True, exist_ok=True)
            for name, content in (
                ("2026-07-01_090000.json", {"content": "first"}),
                ("2026-07-03_090000.json", {"content": "second"}),
                ("2026-07-05_090000.json", {"content": "third"}),
            ):
                (cfg.diary_dir / name).write_text(json.dumps(content), encoding="utf-8")
            entries = diary.recent_entries(cfg, 2)
        self.assertEqual([e["content"] for e in entries], ["second", "third"])

    def test_corrupt_entry_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            cfg.diary_dir.mkdir(parents=True, exist_ok=True)
            (cfg.diary_dir / "2026-07-01_090000.json").write_text("{not json", encoding="utf-8")
            (cfg.diary_dir / "2026-07-02_090000.json").write_text(
                json.dumps({"content": "good one"}), encoding="utf-8")
            with self.assertLogs("everthine", level="WARNING"):
                entries = diary.recent_entries(cfg, 5)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["content"], "good one")


# ---------------------------------------------------------------------
# M5 T3: material assembly (build_material) + diary system prompt.
# Persona fixtures mirror tests/test_persona_assembly.py; archive/album
# fixtures follow tests/test_album.py conventions (tz-aware timestamps).
# ---------------------------------------------------------------------

IDENTITY_TEXT = ("Ledger-keeper by day, storyteller by night, always half a "
                 "page ahead in the book on the nightstand.")
VOICE_TEXT = "Short sentences. Warm and a little wry, never flowery."


def _persona(*, identity_text=IDENTITY_TEXT, voice_text="", boundaries_text="",
             companion_name="Alex", partner_name="Sam", living="together"):
    settings = PersonaSettings(
        companion_name=companion_name, partner_name=partner_name, living=living)
    return Persona(mode="folder", identity_text=identity_text, voice_text=voice_text,
                   boundaries_text=boundaries_text, settings=settings)


def _seed_convo(cfg, now, pairs=(("user", "hi"), ("companion", "hey"))):
    """Write conversation entries into the archive an hour back, so they land
    inside the 24h diary lookback and keep the order they are given."""
    for i, (speaker, text) in enumerate(pairs):
        archive.write_entry(cfg.archive_dir, speaker, text,
                            ts=now - timedelta(hours=1) + timedelta(minutes=i))


class TestBuildMaterialRecordRequired(unittest.TestCase):
    def test_no_conversation_returns_none_even_with_keepsake(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            album.add_partner_flag(cfg, "his words", 1, NOW)  # today's keepsake exists
            # The conversation record is the required block: no record, no page,
            # even though the album holds a moment from today.
            self.assertIsNone(diary.build_material(cfg, NOW, None, "Wren"))


class TestBuildMaterialShape(unittest.TestCase):
    def test_record_mapping_order_and_hard_rules_last(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            material = diary.build_material(cfg, NOW, None, "Wren")
        self.assertIn(
            diary.DIARY_RECORD_HEADER.format(
                partner_name="Wren", hours=diary.DIARY_LOOKBACK_HOURS),
            material)
        self.assertIn("(the full record of the last 24 hours)", material)  # hours=24 pin
        self.assertIn("Wren: hi", material)   # user -> partner_name
        self.assertIn("You: hey", material)   # companion -> You
        self.assertLess(material.index("Wren: hi"), material.index("You: hey"))
        self.assertTrue(material.endswith(
            diary.DIARY_HARD_RULES.format(partner_name="Wren")))  # always last
        self.assertIn("must be traceable to the conversation", material)  # transcription pin
        # optional blocks absent when nothing seeds them
        self.assertNotIn(diary.DIARY_KEEPSAKE_HEADER, material)
        self.assertNotIn(diary.DIARY_RECENT_HEADER, material)
        self.assertNotIn("since you last heard from", material)


class TestBuildMaterialTruncation(unittest.TestCase):
    def test_tail_truncation_keeps_whole_newest_lines(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW, pairs=[("user", f"line {i:03d}") for i in range(20)])
            original = [f"Wren: line {i:03d}" for i in range(20)]
            orig_cap = diary.DIARY_CONTEXT_MAX_CHARS
            diary.DIARY_CONTEXT_MAX_CHARS = 120
            self.addCleanup(setattr, diary, "DIARY_CONTEXT_MAX_CHARS", orig_cap)
            material = diary.build_material(cfg, NOW, None, "Wren")
        record_block = material.split("\n\n")[0]
        block_lines = record_block.split("\n")
        self.assertEqual(
            block_lines[0],
            diary.DIARY_RECORD_HEADER.format(
                partner_name="Wren", hours=diary.DIARY_LOOKBACK_HOURS))
        self.assertEqual(block_lines[1], diary.DIARY_OMISSION_LINE)   # elision flagged at top
        kept = block_lines[2:]
        self.assertGreater(len(kept), 0)
        self.assertTrue(all(line in original for line in kept))       # no mid-line fragment
        self.assertEqual(kept, original[len(original) - len(kept):])  # newest tail, in order
        self.assertLessEqual(len("\n".join(kept)), 120)               # within the cap


class TestBuildMaterialKeepsake(unittest.TestCase):
    def test_who_mapping_verbatim_text_and_close_line(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            long_text = ("the sea was the exact grey of your eyes, " * 5) + "and I remember it"
            album.add_partner_flag(cfg, long_text, 1, NOW)           # she kept his words
            album.add_companion_flag(cfg, "her small joke", 2, NOW)  # he kept her words
            material = diary.build_material(cfg, NOW, None, "Wren")
        self.assertIn(diary.DIARY_KEEPSAKE_HEADER, material)
        self.assertIn(f"- [Wren kept this] {long_text}", material)    # partner_flagged, untruncated
        self.assertIn("- [You kept this] her small joke", material)   # companion_flagged
        self.assertIn(diary.DIARY_KEEPSAKE_CLOSE, material)
        self.assertLess(material.index("[Wren kept this]"),
                        material.index("[You kept this]"))             # storage order preserved

    def test_album_disabled_hides_block_even_with_data(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            album.add_partner_flag(cfg, "his kept words", 1, NOW)   # data written under enabled cfg
            disabled = _cfg(td, ALBUM_ENABLED="false")
            material = diary.build_material(disabled, NOW, None, "Wren")
        self.assertNotIn(diary.DIARY_KEEPSAKE_HEADER, material)
        self.assertNotIn("his kept words", material)

    def test_enabled_but_no_keepsake_today_hides_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            material = diary.build_material(cfg, NOW, None, "Wren")
        self.assertNotIn(diary.DIARY_KEEPSAKE_HEADER, material)

    def test_yesterday_keepsake_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            album.add_partner_flag(cfg, "old kept moment", 1, NOW - timedelta(days=1))
            material = diary.build_material(cfg, NOW, None, "Wren")
        self.assertNotIn(diary.DIARY_KEEPSAKE_HEADER, material)
        self.assertNotIn("old kept moment", material)


class TestBuildMaterialRecent(unittest.TestCase):
    def test_recent_snippet_capped_at_200(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            diary.save_entry(cfg, {"content": "a" * 250}, NOW - timedelta(days=1))
            material = diary.build_material(cfg, NOW, None, "Wren")
        self.assertIn(diary.DIARY_RECENT_HEADER, material)
        self.assertIn("a" * 200, material)
        self.assertNotIn("a" * 201, material)  # snippet is exactly content[:200]

    def test_no_diary_hides_recent_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            material = diary.build_material(cfg, NOW, None, "Wren")
        self.assertNotIn(diary.DIARY_RECENT_HEADER, material)


class TestBuildMaterialAbsence(unittest.TestCase):
    def test_gap_under_24h_no_absence(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            material = diary.build_material(cfg, NOW, NOW - timedelta(hours=23), "Wren")
        self.assertNotIn("since you last heard from", material)

    def test_gap_over_24h_absence_present(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            material = diary.build_material(cfg, NOW, NOW - timedelta(hours=25), "Wren")
        self.assertIn(
            diary.DIARY_ABSENCE_LINE.format(hours=25, partner_name="Wren"), material)
        self.assertIn("about 25 hours", material)

    def test_none_last_contact_no_absence(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _seed_convo(cfg, NOW)
            material = diary.build_material(cfg, NOW, None, "Wren")
        self.assertNotIn("since you last heard from", material)


class TestBuildSystemPromptDiary(unittest.TestCase):
    def test_four_block_order_with_voice(self):
        result = diary.build_system_prompt_diary(_persona(voice_text=VOICE_TEXT))
        self.assertTrue(result.startswith("# Who you are"))        # declaration first
        self.assertTrue(result.endswith(diary.DIARY_TASK))          # task last
        self.assertLess(result.index("# Who you are"), result.index(IDENTITY_TEXT))
        self.assertLess(result.index(IDENTITY_TEXT), result.index(VOICE_TEXT))
        self.assertLess(result.index(VOICE_TEXT), result.index("# Your private page"))

    def test_empty_voice_three_blocks_no_stray_blank(self):
        result = diary.build_system_prompt_diary(_persona(voice_text=""))
        self.assertNotIn(VOICE_TEXT, result)
        self.assertNotIn("\n\n\n", result)                          # no stray blank block
        self.assertIn(IDENTITY_TEXT, result)
        self.assertTrue(result.endswith(diary.DIARY_TASK))

    def test_file_mode_persona_raises(self):
        with self.assertRaises(ValueError):
            diary.build_system_prompt_diary(Persona(mode="file", raw_text="You are Testbot."))

    def test_excludes_dna_boundaries_stage_layer3(self):
        result = diary.build_system_prompt_diary(
            _persona(voice_text=VOICE_TEXT, boundaries_text="Never mention the storm."))
        self.assertNotIn("# The ground rules", result)             # DNA heading absent
        self.assertNotIn("## Their boundaries, in their own words", result)  # boundaries absent
        self.assertNotIn("Never mention the storm.", result)


class TestSaveEntryKeywordsGuard(unittest.TestCase):
    def test_non_string_keyword_elements_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = diary.save_entry(cfg, {"content": "hi", "keywords": [1, "ok"]}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["keywords"], ["ok"])


if __name__ == "__main__":
    unittest.main()
