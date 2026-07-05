import unittest
from datetime import datetime, timedelta, timezone

from everthine.memory_store import (
    CHUNK_GAP_MINUTES,
    CHUNK_MAX_CHARS,
    CHUNK_MAX_ROUNDS,
    Chunk,
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


if __name__ == "__main__":
    unittest.main()
