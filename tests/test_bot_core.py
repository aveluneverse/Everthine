import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from everthine import archive, bot, messages
from everthine.config import Config
from everthine.engine import EngineReply
from everthine.session_store import SessionStore

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)


class FakeEngineOK:
    def __init__(self):
        self.calls = []

    def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
        self.calls.append({"prompt": prompt, "session_id": session_id,
                           "system_prompt": system_prompt})
        return EngineReply("nice to hear from you", "sess-new", ok=True)


class FakeEngineFail:
    def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
        return EngineReply("", session_id, ok=False, error_kind="timeout")


class FakeEngineReact:
    def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
        return EngineReply("[react:❤️] warm", "sess-react", ok=True)


class FakeEngineTagOnly:
    def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
        return EngineReply("[react:❤️]", "sess-tagonly", ok=True)


class FakeEngineEmptyText:
    def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
        return EngineReply("", "sess-empty", ok=True)


class TestProduceReply(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.cfg = Config(bot_token="x", authorized_user_id=1, data_dir=root / "data")
        self.store = SessionStore(self.cfg.session_path)

    def tearDown(self):
        self._td.cleanup()

    def test_happy_path_replies_and_stamps(self):
        eng = FakeEngineOK()
        chunks = bot.produce_reply(self.cfg, self.store, "hello", now=NOW, engine_mod=eng)
        self.assertEqual(chunks, ["nice to hear from you"])
        self.assertEqual(self.store.load()["session_id"], "sess-new")
        self.assertTrue(eng.calls[0]["system_prompt"])

    def test_archive_written_both_sides(self):
        bot.produce_reply(self.cfg, self.store, "hello", now=NOW, engine_mod=FakeEngineOK())
        texts = [e["text"] for e in archive.iter_entries(self.cfg.archive_dir)]
        self.assertIn("hello", texts)
        self.assertIn("nice to hear from you", texts)

    def test_injection_prefix_reaches_engine_on_new_session(self):
        archive.write_entry(self.cfg.archive_dir, "user", "we talked about stars",
                            ts=NOW.replace(hour=10))
        eng = FakeEngineOK()
        bot.produce_reply(self.cfg, self.store, "good morning", now=NOW, engine_mod=eng)
        self.assertIn("we talked about stars", eng.calls[0]["prompt"])
        self.assertTrue(eng.calls[0]["prompt"].endswith("good morning"))

    def test_engine_failure_returns_friendly_error(self):
        chunks = bot.produce_reply(self.cfg, self.store, "hello", now=NOW,
                                   engine_mod=FakeEngineFail())
        self.assertEqual(len(chunks), 1)
        self.assertNotEqual(chunks[0].strip(), "")
        self.assertIsNone(self.store.load()["session_id"])

    def test_archives_stripped_text_when_reply_has_react_tag(self):
        # A tag in the archive would flow into M3's memory index as literal
        # content, so the archive must always see the cleaned text.
        chunks = bot.produce_reply(self.cfg, self.store, "hi", now=NOW,
                                   engine_mod=FakeEngineReact())
        self.assertEqual(chunks, ["warm"])
        texts = [e["text"] for e in archive.iter_entries(self.cfg.archive_dir)]
        self.assertIn("warm", texts)
        for t in texts:
            self.assertNotIn("[react:", t)

    def test_tag_only_reply_returns_empty_and_fires_react_sink(self):
        # N4: a reply that is JUST a gesture (the whole text is the tag) is
        # not a glitch -- produce_reply returns no chunks, and the captured
        # emoji still reaches the on_react sink so the reaction can land.
        sink = []
        chunks = bot.produce_reply(self.cfg, self.store, "hi", now=NOW,
                                   engine_mod=FakeEngineTagOnly(),
                                   on_react=sink.append)
        self.assertEqual(chunks, [])
        self.assertEqual(sink, ["❤️"])
        # Nothing was said, so no companion text entered the archive.
        texts = [e["text"] for e in archive.iter_entries(self.cfg.archive_dir)]
        for t in texts:
            self.assertNotIn("[react:", t)

    def test_empty_reply_without_tag_still_glitches(self):
        # The pre-N4 semantic pin must not regress: a genuinely empty reply
        # with no tag is still a glitch apology, never an empty list.
        chunks = bot.produce_reply(self.cfg, self.store, "hi", now=NOW,
                                   engine_mod=FakeEngineEmptyText())
        self.assertEqual(chunks, [messages.msg("generic_glitch")])


class TestBloatExtraSink(unittest.TestCase):
    """T7a: the notebook_full system notice no longer rides home in
    produce_reply's chunk list (where on_text would _cache_sent it, letting
    her heart a system line into the keepsake album). It flows through an
    opt-in on_extra sink instead -- same convention as on_react -- so the
    return value stays exactly the companion chunk list every caller depends
    on, and the notice can be sent on different terms (uncached)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.cfg = Config(bot_token="x", authorized_user_id=1, data_dir=root / "data")
        self.store = SessionStore(self.cfg.session_path)

    def tearDown(self):
        self._td.cleanup()

    def test_bloat_reaches_extra_sink_and_not_return_list(self):
        extra: list = []
        with mock.patch.object(self.store, "detect_bloat", return_value=True):
            chunks = bot.produce_reply(self.cfg, self.store, "hello", now=NOW,
                                       engine_mod=FakeEngineOK(),
                                       on_extra=extra.append)
        self.assertEqual(chunks, ["nice to hear from you"])
        self.assertEqual(extra, [messages.msg("notebook_full")])
        self.assertNotIn(messages.msg("notebook_full"), chunks)

    def test_no_bloat_leaves_extra_sink_untouched(self):
        extra: list = []
        with mock.patch.object(self.store, "detect_bloat", return_value=False):
            chunks = bot.produce_reply(self.cfg, self.store, "hello", now=NOW,
                                       engine_mod=FakeEngineOK(),
                                       on_extra=extra.append)
        self.assertEqual(chunks, ["nice to hear from you"])
        self.assertEqual(extra, [])


class TestExtractReact(unittest.TestCase):
    def test_extracts_and_strips(self):
        emoji, text = bot._extract_react("[react:❤️] kept words")
        self.assertEqual(emoji, "❤️")
        self.assertEqual(text, "kept words")

    def test_plain_text_untouched(self):
        self.assertEqual(bot._extract_react("hello"), (None, "hello"))


class TestHelpers(unittest.TestCase):
    def test_start_buttons(self):
        self.assertEqual(bot.decide_start_buttons(False), ["btn_clean"])
        self.assertEqual(bot.decide_start_buttons(True),
                         ["btn_resume", "btn_warm", "btn_clean"])


if __name__ == "__main__":
    unittest.main()
