import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from everthine import archive
from everthine.memory_store import (
    CHUNK_GAP_MINUTES,
    CHUNK_MAX_CHARS,
    CHUNK_MAX_ROUNDS,
    Chunk,
    MemoryStore,
    chunk_entries,
)


def _entry(speaker: str, text: str, ts: datetime) -> dict:
    return {"timestamp": ts, "speaker": speaker, "text": text}


def _padded(i: int, total_len: int = 900) -> str:
    """A deterministic total_len-char string carrying a per-entry marker,
    so size-limit tests can identify which entry ended up where."""
    marker = f"seg{i:02d}-"
    return marker + "x" * (total_len - len(marker))


class TestChunkEntries(unittest.TestCase):
    def test_gap_closes_chunk(self):
        # 1. A 30-minute gap between entries closes the chunk (two chunks result).
        t0 = datetime(2026, 7, 3, 9, 0)
        entries = [
            _entry("user", "hello", t0),
            _entry("companion", "hi there", t0 + timedelta(minutes=CHUNK_GAP_MINUTES)),
        ]
        now = t0 + timedelta(minutes=CHUNK_GAP_MINUTES)
        chunks = chunk_entries(entries, now)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].text, "user: hello")
        self.assertTrue(chunks[0].closed)
        self.assertEqual(chunks[1].text, "companion: hi there")
        self.assertFalse(chunks[1].closed)

    def test_max_rounds_closes_chunk(self):
        # 2. Reaching 8 entries closes the chunk (9 entries -> chunks of 8 and 1).
        t0 = datetime(2026, 7, 3, 9, 0)
        entries = [_entry("user", f"msg{i}", t0 + timedelta(minutes=i)) for i in range(9)]
        now = entries[-1]["timestamp"]
        chunks = chunk_entries(entries, now)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0].text.split("\n")), CHUNK_MAX_ROUNDS)
        self.assertEqual(chunks[1].text, "user: msg8")

    def test_size_limit_closes_chunk_before_overflow(self):
        # 3a. A chunk closes before the entry that would push its text over 6000 chars.
        t0 = datetime(2026, 7, 3, 9, 0)
        entries = [_entry("user", _padded(i), t0 + timedelta(minutes=i)) for i in range(7)]
        now = entries[-1]["timestamp"]
        chunks = chunk_entries(entries, now)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0].text.split("\n")), 6)
        self.assertLessEqual(len(chunks[0].text), CHUNK_MAX_CHARS)
        self.assertEqual(chunks[1].text, f"user: {_padded(6)}")

    def test_oversized_single_entry_becomes_own_chunk(self):
        # 3b. An oversized single entry still becomes its own chunk (never drop content).
        t0 = datetime(2026, 7, 3, 9, 0)
        huge = "y" * (CHUNK_MAX_CHARS + 1000)
        entries = [
            _entry("user", huge, t0),
            _entry("companion", "ok", t0 + timedelta(minutes=1)),
        ]
        now = t0 + timedelta(minutes=1)
        chunks = chunk_entries(entries, now)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].text, f"user: {huge}")
        self.assertTrue(chunks[0].closed)
        self.assertEqual(chunks[1].text, "companion: ok")

    def test_trailing_chunk_open_when_now_within_gap(self):
        # 4. Trailing chunk with `now` within 30 min of the last entry -> closed=False.
        t0 = datetime(2026, 7, 3, 9, 0)
        entries = [_entry("user", "hi", t0)]
        now = t0 + timedelta(minutes=CHUNK_GAP_MINUTES - 1)
        chunks = chunk_entries(entries, now)
        self.assertEqual(len(chunks), 1)
        self.assertFalse(chunks[0].closed)

    def test_trailing_chunk_closed_when_now_past_gap(self):
        # 5. Trailing chunk with `now` >= 30 min after the last entry -> closed=True.
        t0 = datetime(2026, 7, 3, 9, 0)
        entries = [_entry("user", "hi", t0)]
        now = t0 + timedelta(minutes=CHUNK_GAP_MINUTES)
        chunks = chunk_entries(entries, now)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].closed)

    def test_chunk_id_stable_and_unique_per_text(self):
        # 6. Same input -> same chunk_id (stability); two different texts in the
        # same second -> different chunk_id (hash segment).
        t0 = datetime(2026, 7, 3, 9, 0)
        entries_a = [_entry("user", "hello world", t0)]
        chunks_a1 = chunk_entries(entries_a, t0)
        chunks_a2 = chunk_entries(entries_a, t0)
        self.assertEqual(chunks_a1[0].chunk_id, chunks_a2[0].chunk_id)

        entries_b = [_entry("user", "a completely different message", t0)]
        chunks_b = chunk_entries(entries_b, t0)
        self.assertNotEqual(chunks_a1[0].chunk_id, chunks_b[0].chunk_id)
        ts_a = chunks_a1[0].chunk_id.split("-")[0]
        ts_b = chunks_b[0].chunk_id.split("-")[0]
        self.assertEqual(ts_a, ts_b)  # same second -> identical ts-compact segment

    def test_lines_stripped_and_blank_entry_skipped(self):
        # 7. Chunk text lines are "speaker: text" with leading/trailing whitespace
        # stripped; an entry that is only whitespace is skipped.
        t0 = datetime(2026, 7, 3, 9, 0)
        entries = [
            _entry("user", "  hello  ", t0),
            _entry("companion", "   ", t0 + timedelta(minutes=1)),
            _entry("user", "world", t0 + timedelta(minutes=2)),
        ]
        now = t0 + timedelta(minutes=2)
        chunks = chunk_entries(entries, now)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "user: hello\nuser: world")

    def test_empty_entries_returns_empty_list(self):
        # 8. Empty entries list -> [].
        self.assertEqual(chunk_entries([], datetime(2026, 7, 3, 9, 0)), [])

    def test_tz_aware_entries_and_now_do_not_raise(self):
        # 9. tz-aware entry timestamps with an aware `now` do not raise (no
        # naive/aware TypeError), and gap logic still works.
        tz8 = timezone(timedelta(hours=8))
        t0 = datetime(2026, 7, 3, 9, 0, tzinfo=tz8)
        t1 = t0 + timedelta(minutes=CHUNK_GAP_MINUTES)
        entries = [
            _entry("user", "hello", t0),
            _entry("companion", "hi", t1),
        ]
        now = t1.astimezone(timezone.utc) + timedelta(minutes=CHUNK_GAP_MINUTES + 1)
        chunks = chunk_entries(entries, now)  # must not raise
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].closed)
        self.assertTrue(chunks[1].closed)

    def test_ts_compact_format_pin(self):
        # 10. ts_compact format pin.
        t0 = datetime.fromisoformat("2026-07-05T14:23:17.123456+08:00")
        entries = [_entry("user", "pin me", t0)]
        chunks = chunk_entries(entries, t0)
        self.assertTrue(chunks[0].chunk_id.startswith("20260705_142317-"))
        self.assertEqual(chunks[0].ts, t0.isoformat())

    def test_naive_now_with_aware_entries_do_not_raise(self):
        # Extra: the reverse aware/naive mix (aware entries, naive `now`) must
        # not raise either. The exact closed-value is timezone-dependent (not
        # asserted); only "does not raise" and chunk shape are.
        tz8 = timezone(timedelta(hours=8))
        t0 = datetime(2026, 7, 3, 9, 0, tzinfo=tz8)
        entries = [_entry("user", "hello", t0)]
        now = datetime(2026, 7, 3, 9, 40)  # naive
        chunks = chunk_entries(entries, now)  # must not raise
        self.assertEqual(len(chunks), 1)

    def test_multiline_entry_text_preserved_verbatim(self):
        # Extra: multi-line entry text stays as-is inside its line slot; inner
        # newlines are not flattened.
        t0 = datetime(2026, 7, 3, 9, 0)
        entries = [_entry("user", "line one\nline two", t0)]
        chunks = chunk_entries(entries, t0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "user: line one\nline two")

    def test_chunk_is_frozen_dataclass_instance(self):
        # Sanity: chunk_entries yields Chunk instances (not plain dicts/tuples).
        t0 = datetime(2026, 7, 3, 9, 0)
        chunks = chunk_entries([_entry("user", "hi", t0)], t0)
        self.assertIsInstance(chunks[0], Chunk)
        with self.assertRaises(Exception):
            chunks[0].closed = True  # frozen -> must not be assignable


def _seed_conversation(archive_dir, start, rounds):
    """Write `rounds` (a list of (speaker, text)) to the real archive as one
    conversation, one minute apart starting at `start`. Returns the list of
    aware timestamps actually used, in order."""
    timestamps = []
    ts = start
    for speaker, text in rounds:
        assert archive.write_entry(archive_dir, speaker, text, ts=ts)
        timestamps.append(ts)
        ts = ts + timedelta(minutes=1)
    return timestamps


def _fake_embed(texts, model_name):
    return [[float(len(t)), 1.0] for t in texts]


class _RecordingEmbed:
    """Fake embed callable that records every call's texts + model_name."""

    def __init__(self):
        self.calls = []

    def __call__(self, texts, model_name):
        self.calls.append((list(texts), model_name))
        return [[float(len(t)), 1.0] for t in texts]


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        # Registered before any per-test store cleanup, so LIFO ordering
        # closes the sqlite handle(s) first and only then removes the tmp
        # dir -- an open handle blocks tmp-dir deletion on Windows.
        self.addCleanup(self._td.cleanup)
        self.archive_dir = Path(self._td.name) / "archive"
        self.db_path = Path(self._td.name) / "memory.db"

    def _new_store(self, path=None) -> MemoryStore:
        store = MemoryStore(path or self.db_path)
        self.addCleanup(store.close)
        return store

    def test_first_sync_ingests_whole_archive(self):
        # 1. Fresh db, cursor None -> built-in backfill of the whole archive.
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "hello"), ("companion", "hi there")])
        t1 = t0 + timedelta(minutes=CHUNK_GAP_MINUTES + 10)
        _seed_conversation(self.archive_dir, t1, [("user", "back again"), ("companion", "welcome back")])
        now = t1 + timedelta(hours=1)

        store = self._new_store()
        inserted = store.sync_from_archive(self.archive_dir, now, _fake_embed, "model-a")

        self.assertEqual(len(inserted), 2)
        rows = store.load_all()
        self.assertEqual(len(rows), 2)
        texts = {r[2] for r in rows}
        self.assertEqual(
            texts,
            {"user: hello\ncompanion: hi there", "user: back again\ncompanion: welcome back"},
        )
        for _chunk_id, _ts, _text, vector in rows:
            self.assertTrue(all(isinstance(x, float) for x in vector))

    def test_second_sync_is_idempotent(self):
        # 2. Second sync immediately after: zero new inserts, count unchanged.
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "hello"), ("companion", "hi there")])
        now = t0 + timedelta(hours=1)

        store = self._new_store()
        store.sync_from_archive(self.archive_dir, now, _fake_embed, "model-a")
        count_before = len(store.load_all())

        inserted_again = store.sync_from_archive(self.archive_dir, now, _fake_embed, "model-a")

        self.assertEqual(inserted_again, [])
        self.assertEqual(len(store.load_all()), count_before)

    def test_append_then_sync_ingests_only_new_closed_chunk(self):
        # 3. Append new entries then sync -> only the new closed chunk inserted.
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "first")])
        now1 = t0 + timedelta(hours=1)

        store = self._new_store()
        store.sync_from_archive(self.archive_dir, now1, _fake_embed, "model-a")
        self.assertEqual(len(store.load_all()), 1)

        t1 = now1 + timedelta(hours=1)
        _seed_conversation(self.archive_dir, t1, [("user", "second")])
        now2 = t1 + timedelta(hours=1)

        inserted = store.sync_from_archive(self.archive_dir, now2, _fake_embed, "model-a")

        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0][0].text, "user: second")
        self.assertEqual(len(store.load_all()), 2)

    def test_open_trailing_chunk_deferred_then_arrives_whole(self):
        # 4. Open trailing chunk not inserted, cursor doesn't move past it;
        # once `now` clears the gap the whole chunk arrives (not a fragment).
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "closed one"), ("companion", "closed two")])
        t1 = t0 + timedelta(minutes=CHUNK_GAP_MINUTES + 5)
        _seed_conversation(self.archive_dir, t1, [("user", "open one"), ("companion", "open two")])

        store = self._new_store()
        now1 = t1 + timedelta(minutes=5)  # 4 min after "open two" -> still within the gap
        inserted1 = store.sync_from_archive(self.archive_dir, now1, _fake_embed, "model-a")

        self.assertEqual(len(inserted1), 1)
        self.assertEqual(inserted1[0][0].text, "user: closed one\ncompanion: closed two")
        self.assertEqual(len(store.load_all()), 1)

        now2 = t1 + timedelta(minutes=CHUNK_GAP_MINUTES + 5)  # now past the gap
        inserted2 = store.sync_from_archive(self.archive_dir, now2, _fake_embed, "model-a")

        self.assertEqual(len(inserted2), 1)
        self.assertEqual(inserted2[0][0].text, "user: open one\ncompanion: open two")
        self.assertEqual(len(store.load_all()), 2)

    def test_cursor_boundary_entry_not_reingested(self):
        # 5. The entry exactly at the cursor timestamp is not re-ingested as
        # a fragment on the next sync.
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "a"), ("companion", "b")])
        now1 = t0 + timedelta(hours=1)

        store = self._new_store()
        store.sync_from_archive(self.archive_dir, now1, _fake_embed, "model-a")
        self.assertEqual(len(store.load_all()), 1)

        now2 = now1 + timedelta(hours=1)
        inserted2 = store.sync_from_archive(self.archive_dir, now2, _fake_embed, "model-a")

        self.assertEqual(inserted2, [])
        rows = store.load_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "user: a\ncompanion: b")

    def test_vector_blob_roundtrip(self):
        # 6. Vector blob round-trips through struct pack/unpack.
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "hello")])
        now = t0 + timedelta(hours=1)
        known_vector = [0.25, -1.5, 3.0]

        def embed(texts, model_name):
            return [list(known_vector) for _ in texts]

        store = self._new_store()
        store.sync_from_archive(self.archive_dir, now, embed, "model-a")

        rows = store.load_all()
        self.assertEqual(len(rows), 1)
        vector = rows[0][3]
        self.assertEqual(len(vector), len(known_vector))
        for actual, expected in zip(vector, known_vector):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_ensure_model_same_then_different_rebuilds(self):
        # 7. Same name+dim twice -> True, chunks preserved. Different name
        # -> False, chunks emptied, cursor gone, meta updated; a following
        # sync re-ingests from the archive start (rebuild pin).
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "hello")])
        now = t0 + timedelta(hours=1)

        store = self._new_store()
        self.assertTrue(store.ensure_model("model-a", 3))
        store.sync_from_archive(self.archive_dir, now, _fake_embed, "model-a")
        self.assertEqual(len(store.load_all()), 1)

        self.assertTrue(store.ensure_model("model-a", 3))
        self.assertEqual(len(store.load_all()), 1)

        self.assertFalse(store.ensure_model("model-b", 5))
        self.assertEqual(store.load_all(), [])

        inserted = store.sync_from_archive(self.archive_dir, now, _fake_embed, "model-b")
        self.assertEqual(len(inserted), 1)
        self.assertEqual(len(store.load_all()), 1)

    def test_load_all_excludes_non_positive_weight(self):
        # 8. A manually zeroed weight disappears from load_all.
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "keep me")])
        t1 = t0 + timedelta(minutes=CHUNK_GAP_MINUTES + 5)
        _seed_conversation(self.archive_dir, t1, [("user", "hide me")])
        now = t1 + timedelta(hours=1)

        store = self._new_store()
        store.sync_from_archive(self.archive_dir, now, _fake_embed, "model-a")
        rows = store.load_all()
        self.assertEqual(len(rows), 2)
        hidden_id = next(r[0] for r in rows if r[2] == "user: hide me")

        store._conn.execute("UPDATE chunks SET weight = 0 WHERE chunk_id = ?", (hidden_id,))
        store._conn.commit()

        rows_after = store.load_all()
        texts_after = [r[2] for r in rows_after]
        self.assertEqual(len(rows_after), 1)
        self.assertNotIn("user: hide me", texts_after)
        self.assertIn("user: keep me", texts_after)

    def test_db_file_auto_created_including_missing_parent_dir(self):
        # 9. DB file (and its missing parent dir) is auto-created.
        nested_path = Path(self._td.name) / "nested" / "sub" / "memory.db"
        self.assertFalse(nested_path.parent.exists())

        self._new_store(nested_path)

        self.assertTrue(nested_path.exists())

    def test_concurrent_sync_and_load_do_not_raise(self):
        # 10. Lock smoke: concurrent sync + load_all from multiple threads.
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "hello")])
        now = t0 + timedelta(hours=1)

        store = self._new_store()
        errors = []

        def worker():
            try:
                for _ in range(5):
                    store.sync_from_archive(self.archive_dir, now, _fake_embed, "model-a")
                    store.load_all()
            except Exception as exc:  # pragma: no cover - failure path only
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(len(store.load_all()), 1)

    def test_batch_embed_called_once_with_both_texts(self):
        # 11. Batch embed contract: one call for all closed chunks this run,
        # each carrying the model_name argument through.
        t0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
        _seed_conversation(self.archive_dir, t0, [("user", "first chat")])
        t1 = t0 + timedelta(minutes=CHUNK_GAP_MINUTES + 5)
        _seed_conversation(self.archive_dir, t1, [("user", "second chat")])
        now = t1 + timedelta(hours=1)

        store = self._new_store()
        recorder = _RecordingEmbed()
        inserted = store.sync_from_archive(self.archive_dir, now, recorder, "model-xyz")

        self.assertEqual(len(inserted), 2)
        self.assertEqual(len(recorder.calls), 1)
        texts, model_name = recorder.calls[0]
        self.assertEqual(set(texts), {"user: first chat", "user: second chat"})
        self.assertEqual(model_name, "model-xyz")


if __name__ == "__main__":
    unittest.main()
