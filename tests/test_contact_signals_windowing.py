"""Windowed contact_signals scan.

contact_signals derives Layer 3's (last_contact, first_today) from the daily
conversation archive. The archive keeps one small file per local day, so a full
walk grows with the history; once a periodic background sweep started calling
this every few minutes (not just once per turn), a whole-archive walk each time
became wasteful. The scan now reads the newest CONTACT_SCAN_RECENT_FILES day
files first and only falls back to the full archive when that window holds no
user entry -- correctness never yields to the fast path.

Two kinds of test live here, deliberately kept apart:

* Equivalence pins (TestContactSignalsEquivalencePins) lock behavior that must
  NOT change. They reference only the public contact_signals surface and a local
  _WINDOW literal, so they pass against BOTH the pre-refactor full scan and the
  windowed scan -- that is exactly what makes them equivalence pins.

* New-behavior pins (TestContactScanReadCount) reference the read seam and the
  window constant the refactor introduces, so they error against the old code
  (the RED) and go green once the window exists. The performance guarantee is a
  read-count seam, never a wall-clock assertion.

Conventions follow tests/test_persona_assembly_wiring.py: an explicit aware
`now` fixed to mid-afternoon (far from midnight, so the first_today date math is
never disturbed) and explicit entry timestamps, so nothing depends on the wall
clock.
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from everthine import archive, persona
from everthine.config import Config

# Aware local, fixed date, mid-afternoon: subtracting an hour for each day file
# never rolls a local date over into a neighbor.
NOW = datetime(2026, 7, 3, 14, 30, 0).astimezone()

# Mirrors persona.CONTACT_SCAN_RECENT_FILES. Kept as a local literal rather than
# importing the constant so the equivalence pins below also run green against
# the pre-refactor code, which does not define that constant yet.
_WINDOW = 45


def _naive_local(ts: datetime) -> datetime:
    """Mirror the code's normalization so expected values line up exactly."""
    return ts.astimezone().replace(tzinfo=None)


def _cfg(data_dir: Path) -> Config:
    return Config(bot_token="x", authorized_user_id=1, data_dir=data_dir)


class TestContactSignalsEquivalencePins(unittest.TestCase):
    """Behavior these pin holds identical on the full scan and the windowed
    scan. They are written to pass against the pre-refactor code first, proving
    they lock the CURRENT contract rather than the new implementation."""

    def test_orphan_user_entry_beyond_recent_window_is_recovered(self):
        # The single user entry lives in the OLDEST file, far past the newest
        # _WINDOW files; every newer file is companion-only. A full scan finds
        # it, so the windowed scan must too -- through its full-archive fallback.
        # This is the safety net: it fails only if the fallback is dropped.
        n_files = _WINDOW + 5
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))
            d = cfg.archive_dir
            orphan_ts = NOW - timedelta(days=n_files - 1, hours=1)
            archive.write_entry(d, "user", "long ago", ts=orphan_ts)
            for i in range(1, n_files):
                archive.write_entry(d, "companion", f"day {i}",
                                    ts=NOW - timedelta(days=n_files - 1 - i, hours=1))
            last_contact, _ = persona.contact_signals(cfg, NOW)
            self.assertEqual(last_contact, _naive_local(orphan_ts))

    def test_recent_user_entry_is_last_contact_with_many_files(self):
        # A larger-than-window archive whose newest file carries the user entry:
        # windowed and full scans agree the maximum user timestamp is that recent
        # one, and today has an entry so first_today is False.
        n_files = _WINDOW + 10
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))
            d = cfg.archive_dir
            for i in range(n_files - 1):
                archive.write_entry(d, "companion", f"day {i}",
                                    ts=NOW - timedelta(days=n_files - 1 - i, hours=1))
            recent_user = NOW - timedelta(hours=1)  # today, the newest file
            archive.write_entry(d, "user", "just now", ts=recent_user)
            last_contact, first_today = persona.contact_signals(cfg, NOW)
            self.assertEqual(last_contact, _naive_local(recent_user))
            self.assertFalse(first_today)

    def test_first_today_true_when_no_entry_today_across_many_files(self):
        # More than a window of files, newest dated YESTERDAY: nothing lands on
        # today's local date, so first_today is True either way.
        n_files = _WINDOW + 5
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))
            d = cfg.archive_dir
            for i in range(n_files):
                # i == n_files - 1 -> one day before today; none reach today.
                archive.write_entry(d, "user", f"day {i}",
                                    ts=NOW - timedelta(days=n_files - i, hours=1))
            _, first_today = persona.contact_signals(cfg, NOW)
            self.assertTrue(first_today)

    def test_first_today_false_when_entry_today_across_many_files(self):
        # Same shape, but the newest file is dated today -> first_today False.
        n_files = _WINDOW + 5
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))
            d = cfg.archive_dir
            for i in range(n_files):
                # i == n_files - 1 -> today.
                archive.write_entry(d, "user", f"day {i}",
                                    ts=NOW - timedelta(days=n_files - 1 - i, hours=1))
            _, first_today = persona.contact_signals(cfg, NOW)
            self.assertFalse(first_today)


class TestContactScanReadCount(unittest.TestCase):
    """New-behavior pins for the windowing itself. They reference the read seam
    and the window constant the refactor introduces, so they error against the
    pre-refactor code (the expected RED) and pass once the window exists."""

    def test_window_constant_is_forty_five(self):
        self.assertEqual(persona.CONTACT_SCAN_RECENT_FILES, 45)

    def test_recent_hit_reads_at_most_the_window(self):
        # Comfortably more files than the window, user entry in the NEWEST file:
        # the scan must read no more than the window and never the whole archive.
        window = persona.CONTACT_SCAN_RECENT_FILES
        n_files = window + 15
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))
            d = cfg.archive_dir
            for i in range(n_files - 1):
                archive.write_entry(d, "companion", f"day {i}",
                                    ts=NOW - timedelta(days=n_files - 1 - i, hours=1))
            archive.write_entry(d, "user", "today", ts=NOW - timedelta(hours=1))

            reads = []
            original = persona._read_day_file

            def counting(day_file):
                reads.append(day_file)
                return original(day_file)

            persona._read_day_file = counting
            try:
                last_contact, _ = persona.contact_signals(cfg, NOW)
            finally:
                persona._read_day_file = original

            self.assertIsNotNone(last_contact)             # recent window hit
            self.assertLessEqual(len(reads), window)        # windowed, per the spec
            self.assertLess(len(reads), n_files)            # did NOT walk everything
            self.assertGreater(len(reads), 0)               # actually read the window

    def test_fallback_consults_full_archive_when_window_has_no_user(self):
        # No user entry in the recent window forces the full-archive fallback;
        # the fallback path is the untouched pre-window scan (archive.iter_entries)
        # and it must still recover the orphaned last_contact. Read count on the
        # fallback is intentionally NOT bounded -- correctness comes first.
        window = persona.CONTACT_SCAN_RECENT_FILES
        n_files = window + 5
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))
            d = cfg.archive_dir
            orphan_ts = NOW - timedelta(days=n_files - 1, hours=1)
            archive.write_entry(d, "user", "long ago", ts=orphan_ts)
            for i in range(1, n_files):
                archive.write_entry(d, "companion", f"day {i}",
                                    ts=NOW - timedelta(days=n_files - 1 - i, hours=1))

            called = []
            original = archive.iter_entries

            def spy(*args, **kwargs):
                called.append(True)
                return original(*args, **kwargs)

            archive.iter_entries = spy
            try:
                last_contact, _ = persona.contact_signals(cfg, NOW)
            finally:
                archive.iter_entries = original

            self.assertTrue(called)                                  # fallback engaged
            self.assertEqual(last_contact, _naive_local(orphan_ts))  # correctness kept


if __name__ == "__main__":
    unittest.main()
