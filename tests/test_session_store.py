import json
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from everthine.config import Config
from everthine.session_store import SessionStore

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "session.json"
        self.store = SessionStore(self.path)

    def tearDown(self):
        self._td.cleanup()

    def test_fresh_defaults(self):
        data = self.store.load()
        self.assertIsNone(data["session_id"])
        self.assertIsNone(data["recent_context_floor"])

    def test_save_load_roundtrip(self):
        self.store.save(session_id="abc")
        self.assertEqual(self.store.load()["session_id"], "abc")

    def test_hostname_guard_clears_foreign_session(self):
        self.store.save(session_id="abc")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["hostname"] = "some-other-machine"
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertIsNone(self.store.load()["session_id"])

    def test_stamp_only_on_change(self):
        self.store.stamp_session_started("s1", NOW)
        first = self.store.load()["session_started_at"]
        self.store.stamp_session_started("s1", NOW.replace(hour=13))
        self.assertEqual(self.store.load()["session_started_at"], first)
        self.store.stamp_session_started("s2", NOW.replace(hour=14))
        self.assertNotEqual(self.store.load()["session_started_at"], first)

    def test_warm_vs_clean(self):
        self.store.stamp_session_started("s1", NOW)
        self.store.warm_restart()
        data = self.store.load()
        self.assertIsNone(data["session_id"])
        self.assertIsNone(data["recent_context_floor"])
        self.store.stamp_session_started("s2", NOW)
        self.store.clean_start(NOW)
        data = self.store.load()
        self.assertIsNone(data["session_id"])
        self.assertEqual(data["recent_context_floor"], NOW.isoformat())

    def test_warm_restart_preserves_clean_floor(self):
        self.store.clean_start(NOW)
        self.store.stamp_session_started("s3", NOW.replace(hour=13))
        self.store.warm_restart()
        data = self.store.load()
        self.assertIsNone(data["session_id"])
        self.assertIsNone(data["session_started_at"])
        self.assertEqual(data["recent_context_floor"], NOW.isoformat())

    def test_hostname_written(self):
        self.store.save(session_id="abc")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["hostname"], socket.gethostname())


class TestBloat(unittest.TestCase):
    def _home_with(self, td, session_id, lines=None, size=None):
        home = Path(td) / "home"
        proj = home / ".claude" / "projects" / "any-slug-name-here"
        proj.mkdir(parents=True)
        f = proj / f"{session_id}.jsonl"
        if size is not None:
            f.write_bytes(b"x" * size)
        else:
            f.write_text("{}\n" * (lines or 0), encoding="utf-8")
        return home

    def test_detect_bloat_by_lines(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(bot_token="x", authorized_user_id=1)
            store = SessionStore(Path(td) / "session.json")
            home = self._home_with(td, "sess-1", lines=800)
            self.assertTrue(store.detect_bloat(cfg, "sess-1", home=home))

    def test_small_session_not_bloated(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(bot_token="x", authorized_user_id=1)
            store = SessionStore(Path(td) / "session.json")
            home = self._home_with(td, "sess-2", lines=10)
            self.assertFalse(store.detect_bloat(cfg, "sess-2", home=home))

    def test_detect_bloat_by_size(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(bot_token="x", authorized_user_id=1)
            store = SessionStore(Path(td) / "session.json")
            home = self._home_with(td, "sess-3", size=2 * 1024 * 1024)
            self.assertTrue(store.detect_bloat(cfg, "sess-3", home=home))

    def test_missing_transcript_or_home_is_calm(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(bot_token="x", authorized_user_id=1)
            store = SessionStore(Path(td) / "session.json")
            self.assertFalse(store.detect_bloat(cfg, "ghost", home=Path(td) / "nope"))
            self.assertFalse(store.detect_bloat(cfg, None, home=Path(td)))


if __name__ == "__main__":
    unittest.main()
