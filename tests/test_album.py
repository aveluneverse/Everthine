"""album.py: keepsake round-trips, dedup, fail-soft parsing, unflagging, and
the M5/M6 seam read APIs (today / N-day window). Conventions follow
tests/test_stages.py (corrupt-file assertions) and tests/test_config.py
(the minimal-env Config-building style _cfg() below mirrors)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from everthine import album
from everthine.config import load_config

BASE_ENV = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}
NOW = datetime(2026, 7, 6, 21, 30, tzinfo=timezone.utc)


def _cfg(td, **env_overrides):
    env = {**BASE_ENV, "DATA_DIR": str(td), **env_overrides}
    return load_config(env)


class TestWriteAndDedup(unittest.TestCase):
    def test_partner_flag_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            self.assertTrue(album.add_partner_flag(cfg, "his words", 42, NOW))
            [e] = album.all_entries(cfg)
            self.assertEqual(e["direction"], "partner_flagged")
            self.assertEqual(e["message"], {"speaker": "companion", "text": "his words"})
            self.assertEqual(e["message_id"], 42)

    def test_duplicate_message_id_skips(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            self.assertTrue(album.add_partner_flag(cfg, "his words", 42, NOW))
            self.assertFalse(album.add_partner_flag(cfg, "his words again", 42, NOW))
            entries = album.all_entries(cfg)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["message"]["text"], "his words")

    def test_companion_flag_speaker_is_user(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            self.assertTrue(album.add_companion_flag(cfg, "her words", 7, NOW))
            [e] = album.all_entries(cfg)
            self.assertEqual(e["direction"], "companion_flagged")
            self.assertEqual(e["message"], {"speaker": "user", "text": "her words"})
            self.assertEqual(e["message_id"], 7)

    def test_unflag_by_message_id(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            album.add_partner_flag(cfg, "his words", 42, NOW)
            self.assertTrue(album.remove_by_message_id(cfg, 42))
            self.assertEqual(album.all_entries(cfg), [])
            self.assertFalse(album.remove_by_message_id(cfg, 42))

    def test_unflag_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            album.add_partner_flag(cfg, "his words", 42, NOW)
            [e] = album.all_entries(cfg)
            self.assertTrue(album.remove_by_id(cfg, e["id"]))
            self.assertEqual(album.all_entries(cfg), [])
            self.assertFalse(album.remove_by_id(cfg, e["id"]))

    def test_corrupt_file_keeps_corpse_and_starts_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            cfg.album_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.album_path.write_text("{not json", encoding="utf-8")
            entries = album.all_entries(cfg)
            self.assertEqual(entries, [])
            corpses = list(cfg.album_path.parent.glob("album.json.corrupt-*"))
            self.assertEqual(len(corpses), 1)  # her kept moments are never overwritten silently

    def test_album_disabled_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, ALBUM_ENABLED="false")
            self.assertFalse(album.add_partner_flag(cfg, "his words", 42, NOW))
            self.assertFalse(album.add_companion_flag(cfg, "her words", 7, NOW))
            self.assertFalse(cfg.album_path.exists())


class TestSeamAPIs(unittest.TestCase):
    def test_entries_for_today_filters_by_local_date(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            yesterday = NOW - timedelta(days=1)
            album.add_partner_flag(cfg, "yesterday's words", 1, yesterday)
            album.add_partner_flag(cfg, "today's words", 2, NOW)
            today_entries = album.entries_for_today(cfg, NOW)
            self.assertEqual(len(today_entries), 1)
            self.assertEqual(today_entries[0]["message_id"], 2)

    def test_entries_for_days_window_inclusive(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            # Seed today-4 .. today (5 distinct days), message_id == 5 - offset
            # so the expected surviving order (oldest first) is readable at a
            # glance: offsets 2,1,0 -> ids 3,4,5.
            for offset in (4, 3, 2, 1, 0):
                ts = NOW - timedelta(days=offset)
                album.add_partner_flag(cfg, f"day-{offset}", 5 - offset, ts)
            window = album.entries_for_days(cfg, 3, NOW)
            self.assertEqual([e["message_id"] for e in window], [3, 4, 5])


if __name__ == "__main__":
    unittest.main()
