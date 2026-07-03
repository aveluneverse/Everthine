import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from everthine import archive, bot
from everthine.config import Config
from everthine.engine import EngineReply
from everthine.session_store import SessionStore

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


class FakeDisplay:
    def __init__(self):
        self.chunks = []
        self.finalized = False
        self.cancelled = False

    @property
    def full_text(self):
        return "".join(self.chunks)

    @property
    def message_texts(self):
        return [self.full_text] if self.chunks else []

    async def append(self, chunk):
        self.chunks.append(chunk)

    async def finalize(self):
        self.finalized = True
        return ["m"] if self.chunks else []

    async def cancel(self):
        self.cancelled = True
        return ["m"] if self.chunks else []


class ScriptedEngine:
    """stream_once stand-in: pushes a scripted event list onto the queue."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def stream_once(self, cfg, prompt, session_id=None, system_prompt=None,
                    events=None, cancel=None):
        self.calls.append({"prompt": prompt, "session_id": session_id,
                           "system_prompt": system_prompt})
        for event in self.script:
            events.put(event)


def ok_script(text_chunks, session_id="sess-stream"):
    events = [{"type": "text", "text": c} for c in text_chunks]
    full = "".join(text_chunks)
    events.append({"type": "done",
                   "reply": EngineReply(full, session_id, ok=True)})
    return events


class TestStreamReply(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.cfg = Config(bot_token="x", authorized_user_id=1,
                          data_dir=root / "data")
        self.store = SessionStore(self.cfg.session_path)

    async def test_happy_path_streams_stamps_and_archives(self):
        eng = ScriptedEngine(ok_script(["Nice ", "to hear."]))
        display = FakeDisplay()
        cancel = threading.Event()
        reply = await bot.stream_reply(self.cfg, self.store, "hello",
                                       display, cancel, now=NOW, engine_mod=eng)
        self.assertTrue(reply.ok)
        self.assertEqual(display.full_text, "Nice to hear.")
        self.assertTrue(display.finalized)
        self.assertEqual(self.store.load()["session_id"], "sess-stream")
        texts = [e["text"] for e in archive.iter_entries(self.cfg.archive_dir)]
        self.assertIn("hello", texts)
        self.assertIn("Nice to hear.", texts)
        self.assertTrue(eng.calls[0]["system_prompt"])

    async def test_injection_prefix_reaches_engine(self):
        archive.write_entry(self.cfg.archive_dir, "user", "we saw the comet",
                            ts=NOW.replace(hour=10))
        eng = ScriptedEngine(ok_script(["ok."]))
        await bot.stream_reply(self.cfg, self.store, "good morning",
                               FakeDisplay(), threading.Event(),
                               now=NOW, engine_mod=eng)
        self.assertIn("we saw the comet", eng.calls[0]["prompt"])
        self.assertTrue(eng.calls[0]["prompt"].endswith("good morning"))

    async def test_pre_cancel_shows_nothing_and_skips_stamp(self):
        eng = ScriptedEngine(ok_script(["never shown"]))
        display = FakeDisplay()
        cancel = threading.Event()
        cancel.set()
        reply = await bot.stream_reply(self.cfg, self.store, "hello",
                                       display, cancel, now=NOW, engine_mod=eng)
        self.assertIsNone(reply)
        self.assertTrue(display.cancelled)
        self.assertIsNone(self.store.load()["session_id"])
        texts = [e["text"] for e in archive.iter_entries(self.cfg.archive_dir)]
        self.assertNotIn("never shown", texts)

    async def test_engine_error_without_text_shows_friendly_message(self):
        script = [{"type": "done",
                   "reply": EngineReply("", None, ok=False, error_kind="timeout")}]
        display = FakeDisplay()
        reply = await bot.stream_reply(self.cfg, self.store, "hello",
                                       display, threading.Event(),
                                       now=NOW, engine_mod=ScriptedEngine(script))
        self.assertFalse(reply.ok)
        self.assertTrue(display.finalized)
        self.assertEqual(len(display.chunks), 1)
        self.assertTrue(display.chunks[0])
        # The fallback apology reaches the display only; EngineReply.text
        # stays the engine's ground truth so on_text can tell "no real
        # output" apart from "real partial output".
        self.assertEqual(reply.text, "")
        self.assertIsNone(self.store.load()["session_id"])

    async def test_partial_then_death_keeps_partial_no_stamp(self):
        script = [{"type": "text", "text": "partial "},
                  {"type": "done",
                   "reply": EngineReply("partial ", None, ok=False,
                                        error_kind="nonzero")}]
        display = FakeDisplay()
        reply = await bot.stream_reply(self.cfg, self.store, "hello",
                                       display, threading.Event(),
                                       now=NOW, engine_mod=ScriptedEngine(script))
        self.assertFalse(reply.ok)
        self.assertEqual(display.full_text, "partial ")
        self.assertIsNone(self.store.load()["session_id"])
        texts = [e["text"] for e in archive.iter_entries(self.cfg.archive_dir)]
        self.assertNotIn("partial ", texts)


class TestButtons(unittest.TestCase):
    def test_start_buttons_unchanged(self):
        self.assertEqual(bot.decide_start_buttons(False), ["btn_clean"])
        self.assertEqual(bot.decide_start_buttons(True),
                         ["btn_resume", "btn_warm", "btn_clean"])


if __name__ == "__main__":
    unittest.main()
