import asyncio
import tempfile
import threading
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from telegram.error import NetworkError, RetryAfter
from telegram.warnings import PTBDeprecationWarning

from everthine import archive, bot, memory_embed, memory_recall, messages, scheduler
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

    async def test_consumer_exception_reaps_worker(self):
        # If the consumer side raises (e.g. RetryAfter bubbling from a peel
        # burst), the live worker must be signalled to stop - otherwise it
        # keeps generating for the full timeout holding the reply lock, and
        # the next message's placeholder sits silent behind a zombie.
        class RaisingDisplay(FakeDisplay):
            async def append(self, chunk):
                raise RetryAfter(1)

        eng = ScriptedEngine(ok_script(["boom"]))
        display = RaisingDisplay()
        cancel = threading.Event()
        with warnings.catch_warnings():
            # PTB 22.6 deprecates an int retry_after in favour of timedelta; we
            # exercise the still-supported int path on purpose, so hush that
            # orthogonal library notice to keep the suite output clean.
            warnings.simplefilter("ignore", PTBDeprecationWarning)
            with self.assertRaises(RetryAfter):
                await bot.stream_reply(self.cfg, self.store, "hello",
                                       display, cancel, now=NOW, engine_mod=eng)
        self.assertTrue(cancel.is_set())


class TestButtons(unittest.TestCase):
    def test_start_buttons_unchanged(self):
        self.assertEqual(bot.decide_start_buttons(False), ["btn_clean"])
        self.assertEqual(bot.decide_start_buttons(True),
                         ["btn_resume", "btn_warm", "btn_clean"])


class FakeCommandBot:
    """Records set_my_commands calls; the seam register_commands drives."""

    def __init__(self):
        self.set_my_commands_calls = []

    async def set_my_commands(self, commands):
        self.set_my_commands_calls.append(commands)


class FakeCommandApp:
    def __init__(self):
        self.bot = FakeCommandBot()


class TestRegisterCommands(unittest.IsolatedAsyncioTestCase):
    """register_commands publishes the menu Telegram shows behind the
    Menu button (Bot API set_my_commands)."""

    async def test_publishes_start_command_with_catalog_description(self):
        app = FakeCommandApp()
        await bot.register_commands(app)

        self.assertEqual(len(app.bot.set_my_commands_calls), 1)
        commands = app.bot.set_my_commands_calls[0]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].command, "start")
        self.assertEqual(commands[0].description, messages.msg("cmd_start_desc"))

    async def test_network_failure_is_logged_never_fatal(self):
        # PTB 22.6 awaits post_init outside its bootstrap retry loop and
        # outside any exception guard (only KeyboardInterrupt/SystemExit are
        # caught), so anything escaping here would crash the whole process at
        # startup. The menu is cosmetic; a transient network blip must not
        # take the bot down before it serves a single reply.
        class FailingCommandBot(FakeCommandBot):
            async def set_my_commands(self, commands):
                raise NetworkError("boom")

        app = FakeCommandApp()
        app.bot = FailingCommandBot()
        with self.assertLogs("everthine", level="WARNING"):
            await bot.register_commands(app)


class TestConcurrencyScope(unittest.TestCase):
    """Concurrency (and the live busy gate + cancel callback it enables)
    must exist only in streaming mode, so flag-off reproduces M1's
    sequential update handling exactly.

    PTB maps concurrent_updates(False) -> SimpleUpdateProcessor(1) and never
    calling it also yields 1, so the two are byte-identical; concurrent_updates
    is a positive int (0 is rejected by PTB), so the meaningful assertion is the
    semantic one: 1 == one update at a time (M1), >1 == concurrent.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        # make_app opens the memory store (M3 T5); close it before the tmp
        # dir deletes -- registered after the tmp cleanup, so LIFO runs it
        # first, releasing the sqlite handle Windows would otherwise hold.
        self.addCleanup(memory_recall.reset)
        # make_app's memory_recall.init(cfg) warm-loads the embedding model;
        # inject a trivial fake before it runs so these concurrency-scope
        # tests never pull in the real SentenceTransformer (memory_embed.py's
        # documented invariant: tests never import the real model).
        memory_embed.set_embed_fn(lambda text: [1.0, 0.0])
        self.addCleanup(memory_embed.set_embed_fn, None)
        self._root = Path(self._td.name)

    def _app(self, streaming):
        cfg = Config(bot_token="x", authorized_user_id=1,
                     data_dir=self._root / "data",
                     streaming_enabled=streaming)
        return bot.make_app(cfg)

    def test_streaming_off_is_sequential_like_m1(self):
        self.assertEqual(self._app(False).concurrent_updates, 1)

    def test_streaming_on_enables_concurrency(self):
        self.assertGreater(self._app(True).concurrent_updates, 1)

    def test_post_init_registers_menu_then_starts_tick(self):
        # M7 T6 (behavior pin, replacing the M5/M6 identity pin): post_init is
        # now make_app's own _post_init closure (no longer a module-level
        # bot.post_init), so its identity is nothing to pin -- what matters is
        # what the builder's startup hook DOES. Application (PTB 22.6) stores
        # that hook verbatim on this public attribute (confirmed by reading
        # telegram.ext.Application.__init__ in the installed package), so
        # driving app.post_init here drives the exact closure the builder got.
        # The order is load-bearing: the (cosmetic) command menu first, then
        # the inner-life tick -- and start_tick takes the session store as an
        # explicit argument from the closure's scope, never a bot_data side
        # channel (see make_app). Driven against a fake app so neither half
        # touches the network or arms a real task.
        hook = self._app(False).post_init
        manager = mock.Mock()
        reg = mock.AsyncMock()
        tick = mock.Mock()
        manager.attach_mock(reg, "register_commands")
        manager.attach_mock(tick, "start_tick")
        fake = FakeStartupApp(Config(bot_token="x", authorized_user_id=1,
                                     diary_enabled=False))
        with mock.patch.object(bot, "register_commands", reg), \
                mock.patch.object(scheduler, "start_tick", tick):
            asyncio.run(hook(fake))
        self.assertEqual([c[0] for c in manager.mock_calls],
                         ["register_commands", "start_tick"])


class FakeStartupApp(FakeCommandApp):
    """FakeCommandApp plus a bot_data dict, so the composite startup hook can
    be driven against it without a real Application: register_commands reads
    app.bot_data["cfg"], and start_tick (patched in these tests) would park its
    task in app.bot_data. cfg here drives only the menu half; the tick half
    takes cfg/store from the make_app closure's own scope, not this app."""

    def __init__(self, cfg):
        super().__init__()
        self.bot_data = {"cfg": cfg}


class TestPostInitComposite(unittest.IsolatedAsyncioTestCase):
    """M7 T6 (behavior pin): the builder's startup hook is now make_app's
    _post_init closure -- register the command menu, then start the inner-life
    tick -- and neither half may ever take the bot's boot down with it (PTB
    22.6 awaits post_init via run_until_complete outside any exception guard,
    so anything escaping here crashes the whole process at startup). The hook
    is pulled off a real built Application (app.post_init) and driven against a
    FakeStartupApp, so no network is touched and no real tick task is armed."""

    def setUp(self):
        # make_app opens the memory store and warm-loads the embedding model;
        # inject the trivial fake before make_app runs and close the sqlite
        # handle before the tmp dir deletes (LIFO: cleanup registered first
        # runs last, after memory_recall.reset()).
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        memory_embed.set_embed_fn(lambda text: [1.0, 0.0])
        self.addCleanup(memory_embed.set_embed_fn, None)
        self._root = Path(self._td.name)

    def _hook(self):
        """The _post_init closure the builder actually received."""
        cfg = Config(bot_token="x", authorized_user_id=1,
                     data_dir=self._root / "data", streaming_enabled=False)
        return bot.make_app(cfg).post_init

    def _fake(self):
        return FakeStartupApp(Config(bot_token="x", authorized_user_id=1,
                                     diary_enabled=False))

    async def test_startup_hook_awaits_register_commands(self):
        app = self._fake()
        with mock.patch.object(bot, "register_commands",
                               new_callable=mock.AsyncMock) as reg, \
                mock.patch.object(scheduler, "start_tick"):
            await self._hook()(app)
        reg.assert_awaited_once_with(app)

    async def test_menu_network_error_does_not_take_boot_down(self):
        # register_commands' own guard swallows the failure and the hook as a
        # whole survives it (the tick half is patched to a no-op here).
        class FailingCommandBot(FakeCommandBot):
            async def set_my_commands(self, commands):
                raise NetworkError("boom")

        app = self._fake()
        app.bot = FailingCommandBot()
        with mock.patch.object(scheduler, "start_tick"):
            with self.assertLogs("everthine", level="WARNING"):
                await self._hook()(app)  # must not raise

    async def test_tick_start_failure_does_not_take_boot_down(self):
        app = self._fake()
        with mock.patch.object(scheduler, "start_tick",
                               side_effect=RuntimeError("boom")):
            with self.assertLogs("everthine", level="WARNING") as cm:
                await self._hook()(app)  # must not raise
        self.assertTrue(any("tick failed to start" in m for m in cm.output))
        # The menu was still published before the tick attempt blew up.
        self.assertEqual(len(app.bot.set_my_commands_calls), 1)


if __name__ == "__main__":
    unittest.main()
