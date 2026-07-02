import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from everthine import archive, recent_context
from everthine.config import Config

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
CFG = Config(bot_token="x", authorized_user_id=1)


def entry_times(entries):
    return [e["timestamp"] for e in entries]


class TestArchive(unittest.TestCase):
    def test_write_and_iter_ordered_across_days(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            archive.write_entry(d, "user", "day one", ts=NOW - timedelta(days=1))
            archive.write_entry(d, "companion", "day two", ts=NOW)
            entries = list(archive.iter_entries(d))
            self.assertEqual([e["text"] for e in entries], ["day one", "day two"])

    def test_since_filter_and_corrupt_line(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            archive.write_entry(d, "user", "old", ts=NOW - timedelta(hours=50))
            archive.write_entry(d, "user", "new", ts=NOW - timedelta(hours=1))
            files = sorted(d.glob("*.jsonl"))
            with files[0].open("a", encoding="utf-8") as fh:
                fh.write("this is not json\n")
            got = [e["text"] for e in archive.iter_entries(d, since=NOW - timedelta(hours=36))]
            self.assertEqual(got, ["new"])


class TestInjection(unittest.TestCase):
    def _dir_with(self, *entries):
        td = tempfile.TemporaryDirectory()
        d = Path(td.name)
        for speaker, text, ts in entries:
            archive.write_entry(d, speaker, text, ts=ts)
        return td, d

    def test_disabled_returns_none(self):
        cfg = Config(bot_token="x", authorized_user_id=1, injection_enabled=False)
        self.assertIsNone(recent_context.build_block(cfg, {}, Path("."), NOW))

    def test_basic_window(self):
        td, d = self._dir_with(("user", "hello", NOW - timedelta(hours=2)))
        with td:
            block = recent_context.build_block(CFG, {"session_started_at": None,
                                                     "recent_context_floor": None}, d, NOW)
            self.assertIn("hello", block)

    def test_upper_bound_excludes_in_session_content(self):
        started = (NOW - timedelta(hours=1)).isoformat()
        td, d = self._dir_with(("user", "before-session", NOW - timedelta(hours=3)),
                               ("user", "inside-session", NOW - timedelta(minutes=10)))
        with td:
            block = recent_context.build_block(CFG, {"session_started_at": started,
                                                     "recent_context_floor": None}, d, NOW)
            self.assertIn("before-session", block)
            self.assertNotIn("inside-session", block)

    def test_live_turn_excluded_on_fresh_session(self):
        td, d = self._dir_with(("user", "earlier today", NOW - timedelta(hours=2)),
                               ("user", "the live message", NOW))
        with td:
            block = recent_context.build_block(CFG, {"session_started_at": None,
                                                     "recent_context_floor": None}, d, NOW)
            self.assertIn("earlier today", block)
            self.assertNotIn("the live message", block)

    def test_floor_clamps_window(self):
        floor = (NOW - timedelta(hours=1)).isoformat()
        td, d = self._dir_with(("user", "pre-floor", NOW - timedelta(hours=2)))
        with td:
            block = recent_context.build_block(CFG, {"session_started_at": None,
                                                     "recent_context_floor": floor}, d, NOW)
            self.assertIsNone(block)

    def test_decay_to_zero_when_window_empty(self):
        started = (NOW - timedelta(hours=40)).isoformat()
        td, d = self._dir_with(("user", "ancient", NOW - timedelta(hours=48)))
        with td:
            block = recent_context.build_block(CFG, {"session_started_at": started,
                                                     "recent_context_floor": None}, d, NOW)
            self.assertIsNone(block)

    def test_max_chars_keeps_most_recent_lines(self):
        cfg = Config(bot_token="x", authorized_user_id=1, injection_max_chars=200)
        entries = [("user", f"message number {i} padded {'x' * 40}", NOW - timedelta(minutes=60 - i))
                   for i in range(20)]
        td, d = self._dir_with(*entries)
        with td:
            block = recent_context.build_block(cfg, {"session_started_at": None,
                                                     "recent_context_floor": None}, d, NOW)
            self.assertLessEqual(len(block), 200 + 100)
            self.assertIn("message number 19", block)
            self.assertNotIn("message number 0 ", block)

    def test_prepend(self):
        self.assertEqual(recent_context.prepend(None, "hi"), "hi")
        out = recent_context.prepend("CTX", "hi")
        self.assertTrue(out.startswith("CTX"))
        self.assertTrue(out.endswith("hi"))


if __name__ == "__main__":
    unittest.main()
