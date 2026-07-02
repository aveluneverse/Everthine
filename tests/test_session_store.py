import json
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from everthine.config import Config
from everthine.session_store import SessionStore, slug_for

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

    def test_hostname_written(self):
        self.store.save(session_id="abc")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["hostname"], socket.gethostname())


class TestBloat(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slug_for(Path("C:/aaa/bbb")), "C--aaa-bbb")
        self.assertEqual(slug_for(Path("C:/a b/c")), "C--a-b-c")
        s = slug_for(Path("C:/aaa/bbb"))
        self.assertNotIn("/", s)
        self.assertNotIn("\\", s)
        self.assertNotIn(" ", slug_for(Path("C:/Lair of X/y")))

    def test_detect_bloat_by_lines(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(bot_token="x", authorized_user_id=1, data_dir=Path(td) / "data")
            store = SessionStore(Path(td) / "session.json")
            fake_home = Path(td) / "home"
            proj = fake_home / ".claude" / "projects" / slug_for(cfg.engine_home.resolve())
            proj.mkdir(parents=True)
            (proj / "sess-1.jsonl").write_text("{}\n" * 800, encoding="utf-8")
            self.assertTrue(store.detect_bloat(cfg, "sess-1", home=fake_home))
            (proj / "sess-2.jsonl").write_text("{}\n" * 10, encoding="utf-8")
            self.assertFalse(store.detect_bloat(cfg, "sess-2", home=fake_home))


if __name__ == "__main__":
    unittest.main()
