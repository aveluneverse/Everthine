"""observatory.py loaders: the seven fail-soft readers that turn data/'s
inner-life files into plain lists/dicts for the observatory page (the
rendering layer has its own file, tests/test_observatory_render.py; the
CLI's own end-to-end tests are below, in this file). The module is a
deliberate island -- no config, bot, engine, persona, or facts import,
pinned mechanically below -- so every loader is driven with plain Paths
against seeded files in tempdirs.

Every fixture in this file is FICTIONAL, written for these tests alone: an
invented couple -- "Wren" (the companion) and "Ivy" (the person) -- whose
invented days (a plum tart, a harbor loop, a cat named Clementine) exist
nowhere but here. No fixture quotes any real conversation, diary,
reflection, or kept moment.

sqlite handles are closed via addCleanup registered AFTER the tempdir's own
cleanup (LIFO runs them first): an open sqlite handle blocks tmp-dir
deletion on Windows, the same trap tests/test_memory_store.py documents.
"""
import ast
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from everthine import observatory, portrait_viewer


class _TmpDirTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)


# ---------------------------------------------------------------------
# 1. load_diary_entries
# ---------------------------------------------------------------------

class LoadDiaryEntriesTest(_TmpDirTest):
    def setUp(self):
        super().setUp()
        self.diary_dir = self.root / "diary"

    def _seed(self, name, data):
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        (self.diary_dir / name).write_text(text, encoding="utf-8")

    def test_entries_come_back_in_filename_order(self):
        # Seeded newest-first on purpose: output order is filename order,
        # never creation order.
        self._seed("2026-07-02_220000.json",
                   {"date": "2026-07-02", "content": "Ivy brought home a plum tart.",
                    "mood": "content", "keywords": ["tart", "evening"]})
        self._seed("2026-07-01_213000.json",
                   {"date": "2026-07-01", "content": "We walked the harbor loop twice.",
                    "mood": "calm", "keywords": ["harbor"]})
        entries = observatory.load_diary_entries(self.diary_dir)
        self.assertEqual([e["date"] for e in entries], ["2026-07-01", "2026-07-02"])
        self.assertEqual(entries[0], {
            "date": "2026-07-01",
            "content": "We walked the harbor loop twice.",
            "mood": "calm",
            "keywords": ["harbor"],
        })

    def test_missing_date_falls_back_to_filename_stem(self):
        self._seed("2026-07-03_090000.json", {"content": "A quiet morning, tea gone cold."})
        entries = observatory.load_diary_entries(self.diary_dir)
        self.assertEqual(entries[0]["date"], "2026-07-03_090000")

    def test_blank_or_wrong_typed_date_also_falls_back(self):
        self._seed("2026-07-04_090000.json", {"content": "page", "date": "   "})
        self._seed("2026-07-05_090000.json", {"content": "page", "date": 20260705})
        entries = observatory.load_diary_entries(self.diary_dir)
        self.assertEqual([e["date"] for e in entries],
                         ["2026-07-04_090000", "2026-07-05_090000"])

    def test_missing_or_blank_content_skips_entry(self):
        self._seed("2026-07-01_213000.json", {"date": "2026-07-01", "mood": "calm"})
        self._seed("2026-07-02_213000.json", {"date": "2026-07-02", "content": "   "})
        self._seed("2026-07-03_213000.json", {"date": "2026-07-03", "content": "kept page"})
        with self.assertLogs("everthine", level="WARNING"):
            entries = observatory.load_diary_entries(self.diary_dir)
        self.assertEqual([e["content"] for e in entries], ["kept page"])

    def test_bad_json_file_skipped_others_kept(self):
        self._seed("2026-07-01_213000.json", "{not valid json")
        self._seed("2026-07-02_213000.json", {"content": "still here"})
        with self.assertLogs("everthine", level="WARNING"):
            entries = observatory.load_diary_entries(self.diary_dir)
        self.assertEqual([e["content"] for e in entries], ["still here"])

    def test_non_object_payload_skipped(self):
        self._seed("2026-07-01_213000.json", json.dumps(["not", "a", "dict"]))
        with self.assertLogs("everthine", level="WARNING"):
            self.assertEqual(observatory.load_diary_entries(self.diary_dir), [])

    def test_mood_and_keywords_degrade(self):
        self._seed("2026-07-01_213000.json",
                   {"content": "page one", "mood": 7, "keywords": "not-a-list"})
        self._seed("2026-07-02_213000.json",
                   {"content": "page two", "keywords": ["kept", 3, None, "also-kept"]})
        entries = observatory.load_diary_entries(self.diary_dir)
        self.assertEqual(entries[0]["mood"], "")
        self.assertEqual(entries[0]["keywords"], [])
        self.assertEqual(entries[1]["mood"], "")
        self.assertEqual(entries[1]["keywords"], ["kept", "also-kept"])

    def test_missing_dir_is_empty(self):
        self.assertEqual(observatory.load_diary_entries(self.root / "no-diary"), [])

    def test_empty_dir_is_empty(self):
        self.diary_dir.mkdir(parents=True)
        self.assertEqual(observatory.load_diary_entries(self.diary_dir), [])


# ---------------------------------------------------------------------
# 2. load_reflections
# ---------------------------------------------------------------------

class LoadReflectionsTest(_TmpDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.root / "reflections.jsonl"

    def _seed(self, lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_lines_come_back_in_file_order(self):
        self._seed([
            json.dumps({"id": "aaaa1111", "created_at": "2026-07-01T21:00:00+08:00",
                        "text": "Ivy's laugh over the burnt toast stayed with me."}),
            json.dumps({"id": "bbbb2222", "created_at": "2026-07-02T09:10:00+08:00",
                        "text": "The harbor gulls again; she counts them wrong on purpose."}),
        ])
        entries = observatory.load_reflections(self.path)
        self.assertEqual([e["created_at"] for e in entries],
                         ["2026-07-01T21:00:00+08:00", "2026-07-02T09:10:00+08:00"])
        self.assertEqual(entries[0]["text"],
                         "Ivy's laugh over the burnt toast stayed with me.")
        # Only the two pinned output keys survive; `id` is not part of the shape.
        self.assertEqual(set(entries[0]), {"created_at", "text"})

    def test_bad_line_skipped_rest_kept(self):
        self._seed(["{broken line",
                    json.dumps({"created_at": "2026-07-02T09:10:00+08:00", "text": "kept"})])
        entries = observatory.load_reflections(self.path)
        self.assertEqual([e["text"] for e in entries], ["kept"])

    def test_missing_or_blank_text_skips_line(self):
        self._seed([json.dumps({"created_at": "2026-07-01T21:00:00+08:00"}),
                    json.dumps({"created_at": "2026-07-01T22:00:00+08:00", "text": "   "}),
                    json.dumps({"created_at": "2026-07-01T23:00:00+08:00", "text": "kept"})])
        entries = observatory.load_reflections(self.path)
        self.assertEqual([e["text"] for e in entries], ["kept"])

    def test_non_object_line_skipped(self):
        self._seed([json.dumps(["not", "an", "object"]),
                    json.dumps({"text": "kept"})])
        self.assertEqual(len(observatory.load_reflections(self.path)), 1)

    def test_missing_or_wrong_typed_created_at_degrades_to_empty(self):
        self._seed([json.dumps({"text": "no clock on this one"}),
                    json.dumps({"created_at": 5, "text": "numeric clock"})])
        entries = observatory.load_reflections(self.path)
        self.assertEqual([e["created_at"] for e in entries], ["", ""])

    def test_blank_lines_skipped(self):
        self._seed([json.dumps({"text": "kept"}), "", "   "])
        self.assertEqual(len(observatory.load_reflections(self.path)), 1)

    def test_missing_file_is_empty(self):
        self.assertEqual(observatory.load_reflections(self.path), [])


# ---------------------------------------------------------------------
# 3. load_portraits (+ the portrait_viewer rename's back-compat alias)
# ---------------------------------------------------------------------

class LoadPortraitsTest(_TmpDirTest):
    def setUp(self):
        super().setUp()
        self.history_dir = self.root / "portrait_history"

    def _seed(self, name, data):
        self.history_dir.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        (self.history_dir / name).write_text(text, encoding="utf-8")

    def test_reads_snapshots_through_portrait_viewer(self):
        self._seed("2026-07-01.json",
                   {"updated": "2026-07-01",
                    "content": "I notice I hum while Ivy reads.",
                    "opinions": [], "observations": []})
        entries = observatory.load_portraits(self.history_dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["updated"], "2026-07-01")
        self.assertEqual(entries[0]["content"], "I notice I hum while Ivy reads.")

    def test_fail_soft_carries_through_the_reuse(self):
        self._seed("2026-07-01.json", "{broken snapshot")
        self._seed("2026-07-02.json",
                   {"updated": "2026-07-02", "content": "kept snapshot"})
        with self.assertLogs("everthine", level="WARNING"):
            entries = observatory.load_portraits(self.history_dir)
        self.assertEqual([e["content"] for e in entries], ["kept snapshot"])

    def test_back_compat_alias_is_the_same_function(self):
        # The rename's whole contract: the public name and the old private
        # name are one object, so anything that held the old name kept
        # working (and tests/test_portrait_viewer.py passing unchanged is
        # the rest of the proof).
        self.assertIs(portrait_viewer._load_entries, portrait_viewer.load_entries)

    def test_missing_dir_is_empty(self):
        self.assertEqual(observatory.load_portraits(self.history_dir), [])


# ---------------------------------------------------------------------
# 4. load_album
# ---------------------------------------------------------------------

class LoadAlbumTest(_TmpDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.root / "album.json"

    def _seed(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.path.write_text(text, encoding="utf-8")

    @staticmethod
    def _entry(direction, speaker, text, timestamp="2026-07-01T20:00:00+08:00"):
        return {"id": "keep_20260701_200000_000000", "timestamp": timestamp,
                "direction": direction,
                "message": {"speaker": speaker, "text": text},
                "message_id": 101}

    def test_normal_entries_round_trip(self):
        self._seed({"version": 1, "entries": [
            self._entry("partner_flagged", "companion", "I saved you the last slice."),
            {"id": "keep_20260702_210000_000000",
             "timestamp": "2026-07-02T21:00:00+08:00",
             "direction": "companion_flagged",
             "message": {"speaker": "user", "text": "Look, the gulls lined up for you."},
             "message_id": 102},
        ]})
        entries = observatory.load_album(self.path)
        self.assertEqual(entries, [
            {"direction": "partner_flagged", "speaker": "companion",
             "message": "I saved you the last slice.",
             "timestamp": "2026-07-01T20:00:00+08:00"},
            {"direction": "companion_flagged", "speaker": "user",
             "message": "Look, the gulls lined up for you.",
             "timestamp": "2026-07-02T21:00:00+08:00"},
        ])

    def test_top_level_not_dict_is_empty(self):
        self._seed(json.dumps([{"message": {"text": "not the real shape"}}]))
        with self.assertLogs("everthine", level="WARNING"):
            self.assertEqual(observatory.load_album(self.path), [])

    def test_entries_not_list_is_empty(self):
        self._seed({"version": 1, "entries": "not-a-list"})
        with self.assertLogs("everthine", level="WARNING"):
            self.assertEqual(observatory.load_album(self.path), [])

    def test_non_dict_message_entry_skipped(self):
        # Controller-confirmed schema record (2026-07-12): on disk, an
        # entry's `message` is a nested {"speaker","text"} dict (album.py),
        # never a bare string -- an entry carrying anything else is dropped
        # whole, and its neighbors survive.
        good = self._entry("partner_flagged", "companion", "kept moment")
        bad = dict(good, message="a bare string, not the real shape")
        self._seed({"version": 1, "entries": [bad, good]})
        entries = observatory.load_album(self.path)
        self.assertEqual([e["message"] for e in entries], ["kept moment"])

    def test_missing_or_blank_message_text_skipped(self):
        blank = self._entry("partner_flagged", "companion", "   ")
        textless = {"direction": "partner_flagged",
                    "message": {"speaker": "companion"},
                    "timestamp": "2026-07-01T20:00:00+08:00"}
        kept = self._entry("partner_flagged", "companion", "kept")
        self._seed({"version": 1, "entries": [blank, textless, kept]})
        entries = observatory.load_album(self.path)
        self.assertEqual([e["message"] for e in entries], ["kept"])

    def test_direction_speaker_timestamp_degrade_to_empty(self):
        self._seed({"version": 1, "entries": [
            {"message": {"text": "kept without the trimmings"}}]})
        entries = observatory.load_album(self.path)
        self.assertEqual(entries, [{"direction": "", "speaker": "",
                                    "message": "kept without the trimmings",
                                    "timestamp": ""}])

    def test_non_dict_entry_skipped(self):
        self._seed({"version": 1, "entries": ["junk", 12,
                                              {"message": {"text": "kept"}}]})
        self.assertEqual(len(observatory.load_album(self.path)), 1)

    def test_bad_json_is_empty(self):
        self._seed("{broken album")
        with self.assertLogs("everthine", level="WARNING"):
            self.assertEqual(observatory.load_album(self.path), [])

    def test_missing_file_is_empty(self):
        self.assertEqual(observatory.load_album(self.path), [])


# ---------------------------------------------------------------------
# 5. load_facts + load_facts_cursor
# ---------------------------------------------------------------------

class LoadFactsTest(_TmpDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.root / "facts.json"

    def _seed(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.path.write_text(text, encoding="utf-8")

    def test_wrapper_shape_round_trips(self):
        self._seed({"facts": [
            {"text": "Ivy prefers peppermint tea.", "category": "preference",
             "date": "2026-07-01"},
            {"text": "Her cat is named Clementine.", "category": "life",
             "date": "2026-07-02"},
        ]})
        facts = observatory.load_facts(self.path)
        self.assertEqual(facts, [
            {"category": "preference", "date": "2026-07-01",
             "text": "Ivy prefers peppermint tea."},
            {"category": "life", "date": "2026-07-02",
             "text": "Her cat is named Clementine."},
        ])

    def test_bare_list_top_level_is_invalid(self):
        # Controller-confirmed schema record (2026-07-12): facts.json is a
        # {"facts": [...]} dict wrapper (facts.py), never a bare list. A
        # bare-list file is mis-shaped -> [] with a warning, not a parse.
        self._seed(json.dumps([{"text": "x", "category": "c", "date": "2026-07-01"}]))
        with self.assertLogs("everthine", level="WARNING"):
            self.assertEqual(observatory.load_facts(self.path), [])

    def test_facts_key_missing_or_not_list_is_empty(self):
        for payload in ({"version": 1}, {"facts": "not-a-list"}):
            with self.subTest(payload=payload):
                self._seed(payload)
                with self.assertLogs("everthine", level="WARNING"):
                    self.assertEqual(observatory.load_facts(self.path), [])

    def test_missing_or_blank_text_skips_fact(self):
        self._seed({"facts": [
            {"category": "preference", "date": "2026-07-01"},
            {"text": "   ", "category": "preference", "date": "2026-07-01"},
            {"text": "kept", "category": "life", "date": "2026-07-02"},
        ]})
        facts = observatory.load_facts(self.path)
        self.assertEqual([f["text"] for f in facts], ["kept"])

    def test_category_and_date_degrade_to_empty(self):
        self._seed({"facts": [{"text": "kept", "category": 3, "date": None}]})
        self.assertEqual(observatory.load_facts(self.path),
                         [{"category": "", "date": "", "text": "kept"}])

    def test_non_dict_fact_skipped(self):
        self._seed({"facts": ["junk", 42, {"text": "kept"}]})
        self.assertEqual(len(observatory.load_facts(self.path)), 1)

    def test_bad_json_is_empty(self):
        self._seed("{broken book")
        with self.assertLogs("everthine", level="WARNING"):
            self.assertEqual(observatory.load_facts(self.path), [])

    def test_missing_file_is_empty(self):
        self.assertEqual(observatory.load_facts(self.path), [])


class LoadFactsCursorTest(_TmpDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.root / "facts_state.json"

    def _seed(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.path.write_text(text, encoding="utf-8")

    def test_value_round_trips(self):
        self._seed({"last_extracted_ts": "2026-07-02T21:00:00+08:00"})
        self.assertEqual(observatory.load_facts_cursor(self.path),
                         "2026-07-02T21:00:00+08:00")

    def test_unset_sentinel_passes_through(self):
        # "" is the owner's never-extracted sentinel: a successful read,
        # not a failure -- it must come back as "", never as None.
        self._seed({"last_extracted_ts": ""})
        self.assertEqual(observatory.load_facts_cursor(self.path), "")

    def test_missing_file_is_none_and_quiet(self):
        with self.assertNoLogs("everthine", level="WARNING"):
            self.assertIsNone(observatory.load_facts_cursor(self.path))

    def test_bad_json_is_none(self):
        self._seed("{broken state")
        with self.assertLogs("everthine", level="WARNING"):
            self.assertIsNone(observatory.load_facts_cursor(self.path))

    def test_wrong_typed_value_is_none(self):
        self._seed({"last_extracted_ts": 123})
        with self.assertLogs("everthine", level="WARNING"):
            self.assertIsNone(observatory.load_facts_cursor(self.path))

    def test_top_level_not_dict_is_none(self):
        self._seed(json.dumps(["not", "a", "dict"]))
        with self.assertLogs("everthine", level="WARNING"):
            self.assertIsNone(observatory.load_facts_cursor(self.path))


# ---------------------------------------------------------------------
# 6. load_conversation_window
# ---------------------------------------------------------------------

class LoadConversationWindowTest(_TmpDirTest):
    TODAY = date(2026, 7, 20)

    def setUp(self):
        super().setUp()
        self.archive_dir = self.root / "archive"

    def _seed_day(self, day_iso, items):
        """items: dicts are json-dumped; raw strings go in verbatim (for
        deliberately broken lines)."""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        raw = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
               for item in items]
        (self.archive_dir / f"{day_iso}.jsonl").write_text(
            "\n".join(raw) + "\n", encoding="utf-8")

    @staticmethod
    def _msg(speaker, text, ts="2026-07-20T09:00:00+08:00"):
        return {"timestamp": ts, "speaker": speaker, "text": text}

    def test_day_14_included_day_15_excluded(self):
        # days=14 with today=2026-07-20 -> the window covers 07-07..07-20:
        # the 14th day back (07-07) is IN, the 15th (07-06) is OUT.
        self._seed_day("2026-07-06", [self._msg("user", "the old harbor plan")])
        self._seed_day("2026-07-07", [self._msg("user", "tart day begins")])
        self._seed_day("2026-07-20", [self._msg("companion", "today's line")])
        window, days_before, msgs_before = observatory.load_conversation_window(
            self.archive_dir, 14, self.TODAY)
        self.assertEqual([e["text"] for e in window],
                         ["tart day begins", "today's line"])
        self.assertEqual(days_before, 1)
        self.assertEqual(msgs_before, 1)

    def test_earlier_counts_span_files(self):
        self._seed_day("2026-06-01", [self._msg("user", "gull census, day one"),
                                      self._msg("companion", "you counted twelve")])
        self._seed_day("2026-06-02", [self._msg("user", "make that thirteen")])
        self._seed_day("2026-07-20", [self._msg("user", "now")])
        window, days_before, msgs_before = observatory.load_conversation_window(
            self.archive_dir, 14, self.TODAY)
        self.assertEqual(len(window), 1)
        self.assertEqual(days_before, 2)
        self.assertEqual(msgs_before, 3)

    def test_earlier_message_count_skips_junk_lines(self):
        # The "N more messages" figure counts only lines the window's own
        # rules would have kept -- junk never inflates it.
        self._seed_day("2026-06-01", ["{broken",
                                      self._msg("user", "one real message"),
                                      json.dumps({"speaker": "user"})])
        _, days_before, msgs_before = observatory.load_conversation_window(
            self.archive_dir, 14, self.TODAY)
        self.assertEqual(days_before, 1)
        self.assertEqual(msgs_before, 1)

    def test_bad_filename_skipped_with_warning(self):
        self._seed_day("2026-07-20", [self._msg("user", "kept")])
        (self.archive_dir / "notes.jsonl").write_text(
            json.dumps(self._msg("user", "not a day file")) + "\n", encoding="utf-8")
        with self.assertLogs("everthine", level="WARNING"):
            window, days_before, msgs_before = observatory.load_conversation_window(
                self.archive_dir, 14, self.TODAY)
        self.assertEqual([e["text"] for e in window], ["kept"])
        self.assertEqual((days_before, msgs_before), (0, 0))

    def test_bad_lines_and_missing_fields_skipped(self):
        self._seed_day("2026-07-20", [
            "{broken json",
            json.dumps(["not", "an", "object"]),
            {"timestamp": "t", "speaker": "user"},                  # no text
            {"timestamp": "t", "text": "who said this?"},           # no speaker
            {"timestamp": "t", "speaker": "user", "text": "   "},   # blank text
            self._msg("user", "the only survivor"),
        ])
        window, _, _ = observatory.load_conversation_window(
            self.archive_dir, 14, self.TODAY)
        self.assertEqual([e["text"] for e in window], ["the only survivor"])

    def test_window_is_chronological_across_files(self):
        # Seeded out of order; output follows filename (= date) order.
        self._seed_day("2026-07-19", [self._msg("user", "yesterday's walk")])
        self._seed_day("2026-07-18", [self._msg("user", "the day before")])
        window, _, _ = observatory.load_conversation_window(
            self.archive_dir, 14, self.TODAY)
        self.assertEqual([e["text"] for e in window],
                         ["the day before", "yesterday's walk"])
        self.assertEqual([e["date"] for e in window],
                         ["2026-07-18", "2026-07-19"])

    def test_missing_timestamp_degrades_to_empty(self):
        self._seed_day("2026-07-20", [{"speaker": "user", "text": "no clock on this one"}])
        window, _, _ = observatory.load_conversation_window(
            self.archive_dir, 14, self.TODAY)
        self.assertEqual(window[0]["timestamp"], "")

    def test_missing_dir_is_all_empty(self):
        self.assertEqual(
            observatory.load_conversation_window(self.root / "no-archive", 14, self.TODAY),
            ([], 0, 0))

    def test_empty_dir_is_all_empty(self):
        self.archive_dir.mkdir(parents=True)
        self.assertEqual(
            observatory.load_conversation_window(self.archive_dir, 14, self.TODAY),
            ([], 0, 0))


# ---------------------------------------------------------------------
# 7. load_memory_stats
# ---------------------------------------------------------------------

class LoadMemoryStatsTest(_TmpDirTest):
    def setUp(self):
        super().setUp()
        self.db_path = self.root / "memory.db"

    def _create_db(self, rows=()):
        # The minimal shape of memory_store's chunks table this loader
        # queries; the helper closes its handle immediately (Windows: an
        # open sqlite handle blocks the tmp dir's deletion).
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, ts TEXT NOT NULL)")
            conn.executemany("INSERT INTO chunks VALUES (?, ?)", list(rows))
            conn.commit()
        finally:
            conn.close()

    def test_counts_and_time_range(self):
        self._create_db([("c1", "2026-07-01T09:00:00+08:00"),
                         ("c2", "2026-07-05T21:30:00+08:00")])
        stats = observatory.load_memory_stats(self.db_path)
        self.assertEqual(stats["chunk_count"], 2)
        self.assertEqual(stats["earliest_ts"], "2026-07-01T09:00:00+08:00")
        self.assertEqual(stats["latest_ts"], "2026-07-05T21:30:00+08:00")
        self.assertEqual(stats["db_size_bytes"], self.db_path.stat().st_size)
        self.assertGreater(stats["db_size_bytes"], 0)

    def test_empty_table_reports_zero_not_none(self):
        self._create_db()
        stats = observatory.load_memory_stats(self.db_path)
        self.assertEqual(stats["chunk_count"], 0)
        self.assertIsNone(stats["earliest_ts"])
        self.assertIsNone(stats["latest_ts"])

    def test_missing_db_is_none(self):
        with self.assertLogs("everthine", level="WARNING"):
            self.assertIsNone(observatory.load_memory_stats(self.db_path))

    def test_db_without_chunks_table_is_none(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.close()
        with self.assertLogs("everthine", level="WARNING"):
            self.assertIsNone(observatory.load_memory_stats(self.db_path))

    def test_not_a_database_is_none(self):
        self.db_path.write_text("these are not the bytes you want", encoding="utf-8")
        with self.assertLogs("everthine", level="WARNING"):
            self.assertIsNone(observatory.load_memory_stats(self.db_path))

    def test_read_only_uri_rejects_writes(self):
        # Proof that mode=ro is real, not decorative: an INSERT down the
        # loader's exact URI (same helper, same connection string) must
        # raise inside sqlite itself. addCleanup (LIFO, registered after
        # setUp's tmpdir cleanup) closes this handle before the tmp dir is
        # deleted -- the Windows trap test_memory_store.py documents.
        self._create_db([("c1", "2026-07-01T09:00:00+08:00")])
        conn = sqlite3.connect(observatory._memory_db_uri(self.db_path), uri=True)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO chunks VALUES ('c9', '2026-07-06T00:00:00+08:00')")
        # And the loader still reads happily through the same URI.
        stats = observatory.load_memory_stats(self.db_path)
        self.assertEqual(stats["chunk_count"], 1)


# ---------------------------------------------------------------------
# The island contract, pinned mechanically
# ---------------------------------------------------------------------

class IslandContractTest(unittest.TestCase):
    BANNED = {"config", "bot", "engine", "persona", "facts"}

    def test_observatory_imports_none_of_the_bot_side(self):
        # The island rule: observatory must not import config, bot, engine,
        # persona, or facts. Checked at the AST level rather than as a
        # source substring (the style of the album guard in
        # tests/test_persona_assembly.py) because the module docstring
        # legitimately NAMES facts.py and album.py as schema owners -- what
        # is banned is importing them, not mentioning them.
        source = Path(observatory.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                banned_hits = set(name.split(".")) & self.BANNED
                self.assertFalse(
                    banned_hits,
                    f"observatory imports a banned module: {sorted(banned_hits)}")


# ---------------------------------------------------------------------
# CLI: main() end-to-end (obs T3) -- everything above is exercised again
# here, but through the real entry point against a seeded tmp data-dir
# standing in for a real installation, the way
# tests/test_portrait_viewer.py already drives its own sibling CLI.
# ---------------------------------------------------------------------

class _CliTest(_TmpDirTest):
    """Shared seeding + running helpers for main()-level tests. self.root
    IS the --data-dir main() is pointed at: with seven sources instead of
    portrait_viewer's one, building each directly under self.root keeps
    every path matching the loader docstrings the brief pins (data/diary,
    data/reflections.jsonl, data/portrait_history, data/album.json,
    data/facts.json + data/facts_state.json, data/archive, data/memory.db).
    """

    def setUp(self):
        super().setUp()
        self.out = self.root / "observatory.html"

    def _run(self, extra_args=()):
        # main() prints the output path on success; a parser.error() call
        # (the --days boundary tests below) prints its usage text to
        # stderr. Redirecting both keeps a full-suite run print-clean --
        # the same reason test_portrait_viewer.py's own _run redirects
        # stdout for its one-knob sibling CLI.
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return observatory.main(["--data-dir", str(self.root), *extra_args])

    def _html(self):
        return self.out.read_text(encoding="utf-8")

    def _assert_in_section(self, html, section_id, snippet):
        """Assert `snippet` falls inside <section id="section_id"> specifically,
        not just somewhere on the page -- proving main() routed that
        loader's output into the matching render_page() key, not merely
        that the two happen to coexist on the same document."""
        order = [sid for sid, _ in observatory.SECTION_ORDER]
        start = html.index(f'<section id="{section_id}">')
        i = order.index(section_id)
        end = (html.index(f'<section id="{order[i + 1]}">', start)
               if i + 1 < len(order) else len(html))
        self.assertIn(snippet, html[start:end])

    # -- one seeding helper per source, mkdir-as-needed -------------------
    def _seed_diary(self, name, data):
        d = self.root / "diary"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _seed_reflections(self, lines):
        text = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n"
        (self.root / "reflections.jsonl").write_text(text, encoding="utf-8")

    def _seed_portrait(self, name, data):
        d = self.root / "portrait_history"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _seed_album(self, entries):
        payload = {"version": 1, "entries": entries}
        (self.root / "album.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _seed_facts(self, facts):
        (self.root / "facts.json").write_text(
            json.dumps({"facts": facts}, ensure_ascii=False), encoding="utf-8")

    def _seed_facts_state(self, last_extracted_ts):
        (self.root / "facts_state.json").write_text(
            json.dumps({"last_extracted_ts": last_extracted_ts}), encoding="utf-8")

    def _seed_archive_day(self, day_iso, messages):
        d = self.root / "archive"
        d.mkdir(parents=True, exist_ok=True)
        raw = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n"
        (d / f"{day_iso}.jsonl").write_text(raw, encoding="utf-8")

    def _seed_memory_db(self, rows=()):
        # Closed immediately in a finally, matching LoadMemoryStatsTest's
        # own _create_db above: an open sqlite handle blocks the tmp
        # dir's deletion on Windows, and this helper never needs to keep
        # its write handle open past the seed itself (unlike
        # test_read_only_uri_rejects_writes above, which needs addCleanup
        # because it keeps a handle open across assertions).
        conn = sqlite3.connect(str(self.root / "memory.db"))
        try:
            conn.execute(
                "CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, ts TEXT NOT NULL)")
            conn.executemany("INSERT INTO chunks VALUES (?, ?)", list(rows))
            conn.commit()
        finally:
            conn.close()


class MainDefaultArgsTest(unittest.TestCase):
    def test_data_dir_and_days_defaults(self):
        args = observatory._build_parser().parse_args([])
        self.assertEqual(args.data_dir, "data")
        self.assertEqual(args.days, 14)

    def test_lang_defaults_to_zh(self):
        # T5 r1: Traditional Chinese is the product default interface language.
        args = observatory._build_parser().parse_args([])
        self.assertEqual(args.lang, "zh")


class MainThreeStateTest(_CliTest):
    """Brief item 1: all seven sources full, all seven empty, and a mixed
    data-dir where each section lands on its own correct state
    independent of its neighbors."""

    def test_all_seven_sources_populated(self):
        self._seed_portrait("2026-07-01.json",
                            {"updated": "2026-07-01",
                             "content": "I notice I save the last biscuit for Ivy.",
                             "opinions": [], "observations": []})
        self._seed_diary("2026-07-01_213000.json",
                         {"date": "2026-07-01",
                          "content": "We watched the lighthouse beam sweep past twice.",
                          "mood": "content", "keywords": ["lighthouse"]})
        self._seed_reflections([
            {"created_at": "2026-07-01T21:30:00+08:00",
             "text": "Ivy laughed at the cold sea glass in her coat pocket."}])
        self._seed_album([
            {"id": "keep_20260701_200000_000000",
             "timestamp": "2026-07-01T20:00:00+08:00",
             "direction": "partner_flagged",
             "message": {"speaker": "companion", "text": "the candlelit dinner, kept whole"},
             "message_id": 1}])
        self._seed_facts([
            {"text": "Ivy keeps sea glass in her coat pocket.",
             "category": "interest", "date": "2026-07-01"}])
        self._seed_facts_state("2026-07-01T21:00:00+08:00")
        today_iso = date.today().isoformat()
        self._seed_archive_day(today_iso, [
            {"timestamp": "2026-07-01T09:00:00+08:00", "speaker": "user",
             "text": "the lighthouse walk, first line of the day"}])
        self._seed_memory_db([("c1", "2026-07-01T09:00:00+08:00")])

        # English is the pinned canonical here (memory template etc.); zh is
        # the CLI default now (T5 r1), so this end-to-end run asks for en.
        rc = self._run(["--lang", "en"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())
        html = self._html()
        for section_id, _title in observatory.SECTION_ORDER:
            self.assertIn(f'<section id="{section_id}">', html)
        self._assert_in_section(html, "portrait", "I notice I save the last biscuit for Ivy.")
        self._assert_in_section(html, "diary", "We watched the lighthouse beam sweep past twice.")
        self._assert_in_section(html, "reflections",
                                "Ivy laughed at the cold sea glass in her coat pocket.")
        self._assert_in_section(html, "keepsakes", "the candlelit dinner, kept whole")
        self._assert_in_section(html, "facts", "Ivy keeps sea glass in her coat pocket.")
        self._assert_in_section(html, "conversation", "the lighthouse walk, first line of the day")
        self._assert_in_section(html, "memory", "Remembered fragments: 1")

    def test_prints_output_path(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = observatory.main(["--data-dir", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn("observatory.html", buf.getvalue())

    def test_all_seven_sources_empty(self):
        # data-dir exists (tempfile.TemporaryDirectory always creates it)
        # but none of the seven sources have ever been written -- a brand
        # new install's very first run. Pins the English empty-state
        # canonical, so it runs in explicit en mode (zh is the CLI default).
        rc = self._run(["--lang", "en"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())
        html = self._html()
        for message in (observatory.EMPTY_PORTRAIT, observatory.EMPTY_DIARY,
                        observatory.EMPTY_REFLECTIONS, observatory.EMPTY_KEEPSAKES,
                        observatory.EMPTY_FACTS, observatory.EMPTY_CONVERSATION,
                        observatory.MEMORY_UNAVAILABLE):
            self.assertIn(message, html)

    def test_mixed_sources_each_section_independent(self):
        # Only diary and facts ever get written; the other five sources
        # never exist on disk at all. Each section must still land on its
        # own correct state -- populated where data exists, empty where it
        # doesn't -- with no cross-talk between them.
        self._seed_diary("2026-07-01_213000.json",
                         {"date": "2026-07-01", "content": "a quiet page, alone for now"})
        self._seed_facts([{"text": "prefers tea before the harbor walk",
                           "category": "interest", "date": "2026-07-01"}])
        # Pins the English empty-state canonical for the five absent sources;
        # runs in explicit en mode (zh is the CLI default, T5 r1).
        rc = self._run(["--lang", "en"])
        self.assertEqual(rc, 0)
        html = self._html()
        self._assert_in_section(html, "diary", "a quiet page, alone for now")
        self._assert_in_section(html, "facts", "prefers tea before the harbor walk")
        self.assertIn(observatory.EMPTY_PORTRAIT, html)
        self.assertIn(observatory.EMPTY_REFLECTIONS, html)
        self.assertIn(observatory.EMPTY_KEEPSAKES, html)
        self.assertIn(observatory.EMPTY_CONVERSATION, html)
        self.assertIn(observatory.MEMORY_UNAVAILABLE, html)


class MainLanguageTest(_CliTest):
    """T5 r1: zh is the product default (main() with no --lang); --lang en
    restores the English canonical. The zh strings themselves are transcribed
    verbatim in tests/test_observatory_render.py -- here we only pin which
    language the CLI reaches for, referencing the module's own tables so this
    file stays ASCII."""

    def test_default_language_is_zh(self):
        rc = self._run()  # no --lang
        self.assertEqual(rc, 0)
        html = self._html()
        title = observatory.CHROME_ZH["page_title"]
        self.assertIn(f"<title>{title}</title>", html)
        self.assertIn(f"<h1>{title}</h1>", html)
        self.assertIn('<html lang="zh-Hant">', html)
        # The English canonical is absent from the default page.
        self.assertNotIn("<title>Observatory</title>", html)

    def test_lang_en_restores_english_chrome(self):
        rc = self._run(["--lang", "en"])
        self.assertEqual(rc, 0)
        html = self._html()
        self.assertIn("<title>Observatory</title>", html)
        self.assertIn('<html lang="en">', html)
        self.assertIn(observatory.EMPTY_PORTRAIT, html)

    def test_invalid_lang_is_a_parser_error(self):
        with self.assertRaises(SystemExit):
            self._run(["--lang", "fr"])
        self.assertFalse(self.out.exists())


class MainXssTest(_CliTest):
    def test_script_tag_never_reaches_the_page(self):
        # Brief item 2: injected through one source (diary content) is
        # enough to prove the CLI-to-render_page() path never bypasses
        # html.escape -- per-seam escaping for all seven sources is
        # tests/test_observatory_render.py's job, not this integration
        # suite's.
        self._seed_diary("2026-07-01_213000.json",
                         {"date": "2026-07-01", "content": "<script>alert(1)</script>"})
        rc = self._run()
        self.assertEqual(rc, 0)
        html = self._html()
        # The only <script on the page is the page-end tab switcher (T5 r1);
        # the injected payload is escaped, never a second raw script tag.
        self.assertEqual(html.count("<script"), 1)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


class MainOutputLocationTest(_CliTest):
    def test_output_path_is_exactly_data_dir_slash_observatory_html(self):
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self.out, self.root / "observatory.html")
        self.assertTrue(self.out.exists())
        self.assertEqual(self.out.parent, self.root)
        # Never lands one level up, alongside the data dir rather than
        # inside it -- the concrete failure mode the brief's "must land
        # inside --data-dir" rule guards against.
        self.assertFalse((self.root.parent / "observatory.html").exists())

    def test_output_dir_created_when_data_dir_does_not_exist_yet(self):
        # portrait_viewer's own precedent: --data-dir need not already
        # exist (mkdir(parents=True, exist_ok=True) on the output's
        # parent). self.root already exists (tempfile made it); point at
        # a not-yet-created child instead.
        fresh = self.root / "brand-new-install"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = observatory.main(["--data-dir", str(fresh)])
        self.assertEqual(rc, 0)
        out = fresh / "observatory.html"
        self.assertTrue(out.exists())
        self.assertEqual(out.parent, fresh)


class MainDaysWindowTest(_CliTest):
    def test_days_restricts_the_conversation_window(self):
        # Brief item 4: --days 7 with fixtures three days back (inside)
        # and ten days back (outside). main() reads the real clock
        # (date.today(), per the "clock only in the CLI layer" hard
        # rule), so the fixture dates are computed relative to it rather
        # than hardcoded, the way any test of clock-driven code must be.
        today = date.today()
        inside = (today - timedelta(days=3)).isoformat()
        outside = (today - timedelta(days=10)).isoformat()
        self._seed_archive_day(inside, [
            {"timestamp": f"{inside}T09:00:00+08:00", "speaker": "user",
             "text": "within the seven-day window"}])
        self._seed_archive_day(outside, [
            {"timestamp": f"{outside}T09:00:00+08:00", "speaker": "user",
             "text": "ten days back, out of the window"}])

        # English "Earlier:" template is the pinned canonical here; en mode.
        rc = self._run(["--days", "7", "--lang", "en"])
        self.assertEqual(rc, 0)
        html = self._html()
        self._assert_in_section(html, "conversation", "within the seven-day window")
        self.assertNotIn("ten days back, out of the window", html)
        self.assertIn("Earlier: 1 more day(s), 1 more line(s)", html)


class MainDaysBoundaryTest(_CliTest):
    """Brief item 5: 0 and -3 are syntactically valid ints (argparse's
    type=int accepts both -- verified separately that "--days -3" is
    parsed as the value -3, not misread as a stray option), so
    parser.error()'s manual `days < 1` check, not argparse's own type
    coercion, is what turns them into a SystemExit; 1 is the smallest
    value that must NOT raise."""

    def test_zero_days_is_a_parser_error(self):
        with self.assertRaises(SystemExit):
            self._run(["--days", "0"])
        self.assertFalse(self.out.exists())

    def test_negative_days_is_a_parser_error(self):
        with self.assertRaises(SystemExit):
            self._run(["--days", "-3"])
        self.assertFalse(self.out.exists())

    def test_one_day_is_accepted(self):
        rc = self._run(["--days", "1"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())


class MainRerunTest(_CliTest):
    def test_rerunning_main_overwrites_the_file(self):
        # Brief item 6: two consecutive runs, both exit 0, and the second
        # file is provably fresh output (content comparison) rather than
        # a stale leftover from the first -- a second diary page is added
        # between runs so the two renders can only be equal if main()
        # failed to pick up the change.
        self._seed_diary("2026-07-01_213000.json",
                         {"date": "2026-07-01", "content": "first run content"})
        rc1 = self._run()
        self.assertEqual(rc1, 0)
        first_html = self._html()
        self.assertIn("first run content", first_html)

        self._seed_diary("2026-07-02_213000.json",
                         {"date": "2026-07-02", "content": "second run content"})
        rc2 = self._run()
        self.assertEqual(rc2, 0)
        second_html = self._html()
        self.assertIn("first run content", second_html)
        self.assertIn("second run content", second_html)
        self.assertNotEqual(first_html, second_html)


if __name__ == "__main__":
    unittest.main()
