import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from everthine.config import Config
from everthine import engine

FAKE = str(Path(__file__).resolve().parent / "fake_claude.py")


class StreamTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_MODE", None)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_STATE", None)

    def cfg(self, **kw):
        base = dict(bot_token="x", authorized_user_id=1,
                    claude_cmd=[sys.executable, FAKE], command_timeout_s=15,
                    stream_stall_timeout_s=60,
                    data_dir=Path(self._td.name) / "data")
        base.update(kw)
        return Config(**base)

    def run_stream(self, cfg_obj, prompt, **kw):
        events = queue.Queue()
        engine.stream_once(cfg_obj, prompt, events=events, **kw)
        out = []
        while True:
            e = events.get(timeout=5)
            out.append(e)
            if e["type"] == "done":
                return out


class TestBuildCmdStreaming(StreamTestBase):
    def test_streaming_flags(self):
        c = engine.build_cmd(self.cfg(), None, None, streaming=True)
        self.assertEqual(c[c.index("--output-format") + 1], "stream-json")
        self.assertIn("--include-partial-messages", c)
        self.assertIn("--verbose", c)
        self.assertIn("-p", c)
        self.assertNotIn("--allowedTools", c)

    def test_default_still_json(self):
        c = engine.build_cmd(self.cfg(), None, None)
        self.assertEqual(c[c.index("--output-format") + 1], "json")


class TestStreamOnce(StreamTestBase):
    def test_stream_ok_deltas_then_done(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream_ok"
        events = self.run_stream(self.cfg(), "hi")
        texts = [e["text"] for e in events if e["type"] == "text"]
        self.assertEqual("".join(texts), "Hello there, friend.")
        done = events[-1]["reply"]
        self.assertTrue(done.ok)
        self.assertEqual(done.session_id, "fake-stream-123")
        self.assertEqual(done.text, "Hello there, friend.")

    def test_stream_resume_echoes_session(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream_ok"
        events = self.run_stream(self.cfg(), "hi", session_id="keep-me")
        self.assertEqual(events[-1]["reply"].session_id, "keep-me")

    def test_stream_die_mid_keeps_partial_and_fails(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream_die_mid"
        events = self.run_stream(self.cfg(), "hi")
        done = events[-1]["reply"]
        self.assertFalse(done.ok)
        self.assertEqual(done.error_kind, "nonzero")
        self.assertEqual(done.text, "partial thought")

    def test_stream_result_error_fails(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream_result_error"
        events = self.run_stream(self.cfg(), "hi")
        done = events[-1]["reply"]
        self.assertFalse(done.ok)
        self.assertEqual(done.error_kind, "nonzero")

    def test_stream_auth_retry_before_first_delta(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream_auth_once"
        with tempfile.TemporaryDirectory() as td:
            os.environ["FAKE_CLAUDE_STATE"] = str(Path(td) / "state")
            events = self.run_stream(self.cfg(), "hi")
        done = events[-1]["reply"]
        self.assertTrue(done.ok)
        texts = [e["text"] for e in events if e["type"] == "text"]
        self.assertEqual("".join(texts), "Hello there, friend.")

    def test_stream_stall_guard_kills(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream_stall"
        start = time.monotonic()
        events = self.run_stream(self.cfg(stream_stall_timeout_s=1,
                                          command_timeout_s=30), "hi")
        elapsed = time.monotonic() - start
        done = events[-1]["reply"]
        self.assertFalse(done.ok)
        self.assertEqual(done.error_kind, "timeout")
        self.assertLess(elapsed, 15)

    def test_unexpected_failure_still_emits_done(self):
        # engine_home's parent is an existing FILE, so mkdir() raises before
        # any subprocess exists; the consumer must still get its done event.
        os.environ["FAKE_CLAUDE_MODE"] = "stream_ok"
        occupied = Path(self._td.name) / "data-as-file"
        occupied.write_text("not a directory", encoding="utf-8")
        events = queue.Queue()
        with self.assertLogs("everthine", level="ERROR"):
            engine.stream_once(self.cfg(data_dir=occupied), "hi", events=events)
        e = events.get(timeout=5)
        self.assertEqual(e["type"], "done")
        self.assertFalse(e["reply"].ok)
        self.assertEqual(e["reply"].error_kind, "nonzero")
        self.assertTrue(events.empty())

    def test_cancel_event_kills_promptly(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream_stall"
        cancel = threading.Event()
        events = queue.Queue()
        worker = threading.Thread(
            target=engine.stream_once,
            kwargs=dict(cfg=self.cfg(stream_stall_timeout_s=60,
                                     command_timeout_s=60),
                        prompt="hi", events=events, cancel=cancel),
            daemon=True)
        start = time.monotonic()
        worker.start()
        first = events.get(timeout=10)
        self.assertEqual(first["type"], "text")
        cancel.set()
        while True:
            e = events.get(timeout=10)
            if e["type"] == "done":
                break
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - start, 20)


if __name__ == "__main__":
    unittest.main()
