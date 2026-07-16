"""Tests for bot.py's inner-life wiring that stays in bot.py after M7 T6:
the post-reply reflection hook, prepare_exchange's diary inner_block, and
the streaming/non-streaming diary page-turn counting.

The background inner-life tick that used to be pinned here moved to
scheduler.py in M7 T6; its mounting and loop-survival tests now live in
tests/test_scheduler_tick.py, and the startup hook that arms it is pinned by
tests/test_bot_stream.py's TestPostInitComposite.

The post-reply reflection -- after a SUCCESSFUL reply on either path, on_text
fires reflection.reflect_once fire-and-forget through
context.application.create_task(asyncio.to_thread(...)), never awaiting it,
never letting a scheduling failure touch the reply.

L1 rollback pin: reflection_enabled False schedules nothing at all (not even
a task), so with it off the bot's observable behavior is exactly M4's.

Conventions follow tests/test_bot_stage_album_wiring.py (the on_text closure
is pulled out of app.handlers[0] and driven directly with hand-rolled
Update/Context fakes; the engine is swapped at engine.run_once /
engine.stream_once, the only seam on_text's default engine_mod leaves open)
and tests/test_bot_memory_wiring.py (tmp dir + the full set of process-global
resets a make_app call can touch, registered in Windows-safe LIFO order so
the memory sqlite handle closes before the tmp dir is deleted). The
reflection context carries a RecordingApplication whose create_task captures
the fire-and-forget coroutine so a test can drive it deterministically after
on_text returns instead of racing asyncio.run's loop teardown.
"""
import asyncio
import itertools
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from telegram.ext import MessageHandler

from everthine import (bot, diary, engine, memory_embed, memory_recall,
                       messages, persona, reflection)
from everthine.config import Config
from everthine.engine import EngineReply
from everthine.session_store import SessionStore


# --- shared fixtures --------------------------------------------------------

def _install_resets(tc):
    """Every process-global a make_app call can touch,
    reset around the test in Windows-safe LIFO order: the tmp-dir cleanup is
    registered FIRST so it runs LAST, after memory_recall.reset() has closed
    the sqlite handle living inside that same tmp dir."""
    tc._td = tempfile.TemporaryDirectory()
    tc.addCleanup(tc._td.cleanup)
    tc.addCleanup(memory_recall.reset)
    tc.addCleanup(memory_embed.set_embed_fn, None)
    tc.addCleanup(persona.reset_persona_cache)
    tc.addCleanup(messages.reset_overrides)
    memory_recall.reset()
    memory_embed.set_embed_fn(lambda text: [1.0, 0.0])
    persona.reset_persona_cache()
    messages.reset_overrides()
    tc.root = Path(tc._td.name)


def _folder_cfg(root, **overrides):
    """A folder-mode persona (settings present -> current_settings != None, so
    the diary tick and reflection are both live) with memory off."""
    folder = root / "persona"
    if not (folder / "identity.md").exists():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "identity.md").write_text(
            "I am Theo, warm and steady.\n", encoding="utf-8")
        (folder / "settings.yaml").write_text(
            "companion:\n  name: Theo\npartner:\n  name: Wren\n", encoding="utf-8")
    kwargs = dict(bot_token="x", authorized_user_id=1,
                  data_dir=root / "data", persona_path=folder,
                  memory_enabled=False, streaming_enabled=False)
    kwargs.update(overrides)
    return Config(**kwargs)


def _handler(app, handler_cls):
    """Pull the registered callback for a handler class out of app.handlers[0]
    -- on_text is a closure with no existence outside a built Application."""
    for h in app.handlers[0]:
        if isinstance(h, handler_cls):
            return h.callback
    raise AssertionError(f"no {handler_cls.__name__} registered")


async def _run_captured(coros):
    """Await the fire-and-forget coroutines a RecordingApplication captured --
    driving reflect_once deterministically, off the turn that scheduled it."""
    for coro in coros:
        await coro


class FakeUser:
    def __init__(self, id):
        self.id = id


class FakeChat:
    def __init__(self, id):
        self.id = id


class FakeMessage:
    _counter = itertools.count(7001)

    def __init__(self, text):
        self.text = text
        self.message_id = next(FakeMessage._counter)
        self.replies = []
        self.edits = []
        self.reactions_set = []
        # N4: a tag-only streamed reply deletes its placeholder.
        self.deleted = False

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        reply = FakeMessage(text)
        self.replies.append(reply)
        return reply

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append(text)
        return FakeMessage(text)

    async def set_reaction(self, reaction, is_big=None):
        self.reactions_set.append(reaction)
        return True


class FakeUpdate:
    def __init__(self, message, user_id=1, chat_id=1):
        self.message = message
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(chat_id)
        self.message_reaction = None
        self.callback_query = None


class FakeBot:
    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text):
        pass


class RecordingApplication:
    """Stands in for context.application: create_task records the coroutine
    (never runs it here) so a test can await it after on_text returns."""

    def __init__(self):
        self.created = []

    def create_task(self, coro, update=None):
        self.created.append(coro)
        return coro


class FakeContext:
    def __init__(self, application=None):
        self.bot = FakeBot()
        self.application = (application if application is not None
                            else RecordingApplication())


class ScriptedEngine:
    """stream_once stand-in: pushes a scripted event list onto the queue."""

    def __init__(self, script):
        self.script = script

    def stream_once(self, cfg, prompt, session_id=None, system_prompt=None,
                    events=None, cancel=None):
        for event in self.script:
            events.put(event)


def ok_script(text_chunks, session_id="sess-stream"):
    full = "".join(text_chunks)
    return [{"type": "text", "text": c} for c in text_chunks] + [
        {"type": "done", "reply": EngineReply(full, session_id, ok=True)}]


# --- 3. reflection hook, non-streaming path --------------------------------

class TestReflectionHookNonStreaming(unittest.TestCase):
    def setUp(self):
        _install_resets(self)

    def test_success_fires_reflection_with_joined_chunks_and_aware_now(self):
        cfg = _folder_cfg(self.root, streaming_enabled=False)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("tell me about your day, i really missed you")
        context = FakeContext()
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "run_once",
                               return_value=EngineReply("it was quiet and warm",
                                                        "s1", ok=True)):
            asyncio.run(on_text(FakeUpdate(her), context))
            self.assertEqual(len(context.application.created), 1)
            asyncio.run(_run_captured(context.application.created))
        reflect.assert_called_once()
        args = reflect.call_args.args
        self.assertEqual(args[0], cfg)
        self.assertEqual(args[1], "tell me about your day, i really missed you")
        self.assertEqual(args[2], "it was quiet and warm")  # "\n".join(chunks)
        self.assertIsNotNone(args[3].utcoffset())

    def test_engine_failure_fires_no_reflection(self):
        cfg = _folder_cfg(self.root, streaming_enabled=False)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("are you there? i want to talk")
        context = FakeContext()
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "run_once",
                               return_value=EngineReply("", "s", ok=False,
                                                        error_kind="timeout")):
            asyncio.run(on_text(FakeUpdate(her), context))
        self.assertEqual(context.application.created, [])
        reflect.assert_not_called()


# --- 4. reflection hook, streaming path ------------------------------------

class TestReflectionHookStreaming(unittest.TestCase):
    def setUp(self):
        _install_resets(self)

    def test_success_fires_reflection_with_full_text(self):
        cfg = _folder_cfg(self.root, streaming_enabled=True)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("did you sleep okay last night, love?")
        context = FakeContext()
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "stream_once",
                               ScriptedEngine(ok_script(["I did, ",
                                                         "thank you."])).stream_once):
            asyncio.run(on_text(FakeUpdate(her), context))
            self.assertEqual(len(context.application.created), 1)
            asyncio.run(_run_captured(context.application.created))
        reflect.assert_called_once()
        args = reflect.call_args.args
        self.assertEqual(args[1], "did you sleep okay last night, love?")
        self.assertEqual(args[2], "I did, thank you.")  # display.full_text
        self.assertIsNotNone(args[3].utcoffset())

    def test_tag_only_reply_fires_no_reflection(self):
        # A tag-only success has empty display.full_text -- a gesture, with
        # no language to reflect on. The empty-text gate must skip it even
        # though the turn itself SUCCEEDED (contrast test_success above,
        # which fires on real language).
        cfg = _folder_cfg(self.root, streaming_enabled=True)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("i love you")
        context = FakeContext()
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "stream_once",
                               ScriptedEngine(ok_script(["[react:❤️]"])).stream_once):
            asyncio.run(on_text(FakeUpdate(her), context))
        self.assertEqual(context.application.created, [])
        reflect.assert_not_called()
        # The placeholder was deleted, not left on the waiting line.
        self.assertTrue(her.replies[0].deleted)

    def test_empty_ok_reply_fires_no_reflection_and_glitches_placeholder(self):
        # ok-but-empty: a SUCCESSFUL turn with no text and no tag. Like the
        # tag-only case its display.full_text is empty, so the empty-text
        # gate skips reflection even though the turn succeeded -- and the
        # placeholder is edited to the generic glitch line (not deleted,
        # never left on the waiting line).
        cfg = _folder_cfg(self.root, streaming_enabled=True)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("are you still there?")
        context = FakeContext()
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "stream_once",
                               ScriptedEngine(ok_script([])).stream_once):
            asyncio.run(on_text(FakeUpdate(her), context))
        self.assertEqual(context.application.created, [])
        reflect.assert_not_called()
        placeholder = her.replies[0]
        self.assertFalse(placeholder.deleted)
        self.assertEqual(placeholder.edits[-1], messages.msg("generic_glitch"))

    def test_cancelled_turn_fires_no_reflection(self):
        cfg = _folder_cfg(self.root, streaming_enabled=True)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("never mind, i changed my mind")
        context = FakeContext()

        async def fake_stream_reply(*a, **k):
            return None  # a cancelled turn yields None

        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(bot, "stream_reply", new=fake_stream_reply):
            asyncio.run(on_text(FakeUpdate(her), context))
        self.assertEqual(context.application.created, [])
        reflect.assert_not_called()

    def test_engine_failure_fires_no_reflection(self):
        cfg = _folder_cfg(self.root, streaming_enabled=True)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("i had the strangest dream last night")
        context = FakeContext()
        script = [{"type": "text", "text": "partial"},
                  {"type": "done",
                   "reply": EngineReply("partial", None, ok=False,
                                        error_kind="nonzero")}]
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "stream_once",
                               ScriptedEngine(script).stream_once):
            asyncio.run(on_text(FakeUpdate(her), context))
        self.assertEqual(context.application.created, [])
        reflect.assert_not_called()


# --- 5. L1 pins: reflection off schedules nothing; both off == M4 behavior --

class TestReflectionL1Pin(unittest.TestCase):
    def setUp(self):
        _install_resets(self)

    def test_flag_off_nonstream_creates_no_task(self):
        cfg = _folder_cfg(self.root, streaming_enabled=False,
                          reflection_enabled=False)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("a perfectly good message to reflect on")
        context = FakeContext()
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "run_once",
                               return_value=EngineReply("mm, warm", "s", ok=True)):
            asyncio.run(on_text(FakeUpdate(her), context))
        self.assertEqual(context.application.created, [])
        reflect.assert_not_called()

    def test_flag_off_streaming_creates_no_task(self):
        cfg = _folder_cfg(self.root, streaming_enabled=True,
                          reflection_enabled=False)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her = FakeMessage("a perfectly good message to reflect on")
        context = FakeContext()
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "stream_once",
                               ScriptedEngine(ok_script(["all good here."])).stream_once):
            asyncio.run(on_text(FakeUpdate(her), context))
        self.assertEqual(context.application.created, [])
        reflect.assert_not_called()

    def test_reflection_flag_off_no_reflection(self):
        # The tick-absence half of this L1 pin (all inner-life flags off arms
        # no tick) moved to tests/test_scheduler_tick.py with the tick itself
        # (M7 T6). What stays here is the reflection L1: a successful reply
        # schedules nothing when reflection_enabled is off.
        cfg = _folder_cfg(self.root, streaming_enabled=False,
                          diary_enabled=False, portrait_enabled=False,
                          reflection_enabled=False)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        context = FakeContext()
        her = FakeMessage("a long enough ordinary message here")
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "run_once",
                               return_value=EngineReply("ok love", "s", ok=True)):
            asyncio.run(on_text(FakeUpdate(her), context))
        self.assertEqual(context.application.created, [])
        reflect.assert_not_called()


# --- reflection scheduling robustness (self-review): the reply must survive
#     a create_task that throws, and a context with no Application at all ----

class TestReflectionSchedulingRobustness(unittest.TestCase):
    def setUp(self):
        _install_resets(self)

    def test_scheduling_failure_never_breaks_the_reply(self):
        cfg = _folder_cfg(self.root, streaming_enabled=False)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)

        class BoomApplication:
            def create_task(self, coro, update=None):
                raise RuntimeError("scheduler down")

        context = FakeContext(application=BoomApplication())
        her = FakeMessage("a nice long message to reflect upon")
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "run_once",
                               return_value=EngineReply("still here", "s", ok=True)):
            with self.assertLogs("everthine", level="WARNING") as cm:
                asyncio.run(on_text(FakeUpdate(her), context))  # must not raise
        # The reply landed and the error path never fired (one reply, no
        # generic_glitch tacked on behind it).
        self.assertEqual([r.text for r in her.replies], ["still here"])
        reflect.assert_not_called()
        self.assertTrue(any("schedule the post-reply reflection" in m
                            for m in cm.output))

    def test_bare_context_without_application_is_a_silent_no_op(self):
        cfg = _folder_cfg(self.root, streaming_enabled=False)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)

        class BareContext:
            def __init__(self):
                self.bot = FakeBot()  # no .application, like pre-M5 handler tests

        context = BareContext()
        her = FakeMessage("a nice long message to reflect upon")
        with mock.patch.object(reflection, "reflect_once") as reflect, \
             mock.patch.object(engine, "run_once",
                               return_value=EngineReply("here", "s", ok=True)):
            asyncio.run(on_text(FakeUpdate(her), context))  # no AttributeError
        reflect.assert_not_called()
        self.assertEqual(her.replies[0].text, "here")


# --- 6. M5 T7: prepare_exchange gathers the diary inner_block (fail-soft,
#        flag-gated) as the 4th element of its return tuple ----------------

def _seed_unshared_entry(cfg, *, content="the private page body",
                         reflection="a closing thought"):
    """Seed one unshared diary entry on disk (the T2 shape)."""
    cfg.diary_dir.mkdir(parents=True, exist_ok=True)
    (cfg.diary_dir / "2026-07-06_210000.json").write_text(json.dumps({
        "date": "2026-07-06", "mood": "quiet", "keywords": [],
        "content": content, "reflection": reflection, "shared": False}),
        encoding="utf-8")


class TestPrepareExchangeInnerBlock(unittest.TestCase):
    def setUp(self):
        _install_resets(self)

    def _now(self):
        return datetime(2026, 7, 7, 14, 0).astimezone()

    def test_flag_on_with_unshared_entry_returns_inner_block(self):
        cfg = _folder_cfg(self.root, diary_enabled=True)
        _seed_unshared_entry(cfg)
        store = SessionStore(cfg.session_path)
        result = bot.prepare_exchange(cfg, store, "hi there friend", self._now())
        self.assertEqual(len(result), 5)  # (prompt, data, memory, inner, facts)
        inner_block = result[3]
        self.assertIsNotNone(inner_block)
        self.assertIn("# Your own recent days", inner_block)

    def test_flag_off_returns_none_even_with_unshared_entry(self):
        cfg = _folder_cfg(self.root, diary_enabled=False)
        _seed_unshared_entry(cfg)  # data present; the gate must still say None
        store = SessionStore(cfg.session_path)
        _, _, _, inner_block, _ = bot.prepare_exchange(cfg, store, "hi there", self._now())
        self.assertIsNone(inner_block)

    def test_diary_block_failure_is_fail_soft(self):
        cfg = _folder_cfg(self.root, diary_enabled=True)
        store = SessionStore(cfg.session_path)
        with mock.patch.object(diary, "unshared_block",
                               side_effect=RuntimeError("boom")), \
             self.assertLogs("everthine", level="WARNING") as cm:
            _, _, _, inner_block, _ = bot.prepare_exchange(cfg, store, "hi there", self._now())
        self.assertIsNone(inner_block)  # a broken diary read never breaks the reply
        self.assertTrue(any("diary block failed" in m for m in cm.output))


# --- 7. End-to-end: an unshared entry reaches the engine's system_prompt as
#        the block (mood + reflection), and its CONTENT never does ----------

class TestInnerBlockEndToEnd(unittest.TestCase):
    def setUp(self):
        _install_resets(self)

    def _seed_sentinels(self, cfg):
        cfg.diary_dir.mkdir(parents=True, exist_ok=True)
        (cfg.diary_dir / "2026-07-06_210000.json").write_text(json.dumps({
            "date": "2026-07-06", "mood": "quiet", "keywords": [],
            "content": "CONTENT_SENTINEL_NEVER_INJECTED the whole page body",
            "reflection": "REFLECTION_SENTINEL_SURFACES a closing line",
            "shared": False}), encoding="utf-8")

    def _run(self, cfg):
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        captured = {}

        def fake_run_once(cfg_, prompt, session_id=None, system_prompt=None):
            captured["system_prompt"] = system_prompt
            return EngineReply("mm, warm", "s1", ok=True)

        her = FakeMessage("tell me about your evening, i missed you today")
        with mock.patch.object(engine, "run_once", side_effect=fake_run_once):
            asyncio.run(on_text(FakeUpdate(her), FakeContext()))
        return captured["system_prompt"]

    def test_unshared_block_reaches_prompt_content_never(self):
        cfg = _folder_cfg(self.root, streaming_enabled=False,
                          diary_enabled=True, reflection_enabled=False)
        self._seed_sentinels(cfg)
        sp = self._run(cfg)
        self.assertIn("# Your own recent days", sp)
        self.assertIn("REFLECTION_SENTINEL_SURFACES", sp)   # the closing thought surfaces
        self.assertNotIn("CONTENT_SENTINEL_NEVER_INJECTED", sp)  # the page body never does

    def test_flag_off_prompt_has_no_block_l1(self):
        cfg = _folder_cfg(self.root, streaming_enabled=False,
                          diary_enabled=False, reflection_enabled=False)
        self._seed_sentinels(cfg)  # data present, flag off -> no block (L1 rollback)
        sp = self._run(cfg)
        self.assertNotIn("# Your own recent days", sp)
        self.assertNotIn("REFLECTION_SENTINEL_SURFACES", sp)


# --- 8. Page-turn timing: mark_shared fires only on the SECOND successful
#        reply, a failed reply never counts, and the diary flag gates it ----

class TestMarkSharedTurnCounting(unittest.TestCase):
    def setUp(self):
        _install_resets(self)

    def _app_and_handler(self, **overrides):
        cfg = _folder_cfg(self.root, streaming_enabled=False,
                          reflection_enabled=False, **overrides)
        app = bot.make_app(cfg)
        return cfg, _handler(app, MessageHandler)

    def _drive_success(self, on_text, context, text="a good long message to reply to"):
        with mock.patch.object(engine, "run_once",
                               return_value=EngineReply("warm reply", "s", ok=True)):
            asyncio.run(on_text(FakeUpdate(FakeMessage(text)), context))

    def _drive_failure(self, on_text, context, text="this message fails to reply now"):
        with mock.patch.object(engine, "run_once",
                               return_value=EngineReply("", "s", ok=False,
                                                        error_kind="timeout")):
            asyncio.run(on_text(FakeUpdate(FakeMessage(text)), context))

    def test_fires_only_on_second_successful_reply(self):
        cfg, on_text = self._app_and_handler(diary_enabled=True)
        context = FakeContext()
        with mock.patch.object(diary, "mark_shared") as mark:
            self._drive_success(on_text, context)
            mark.assert_not_called()       # after the 1st success: page still open
            self._drive_success(on_text, context)
            mark.assert_called_once()       # after the 2nd: pages turned
            self.assertEqual(mark.call_args.args[0], cfg)

    def test_failed_reply_never_counts(self):
        cfg, on_text = self._app_and_handler(diary_enabled=True)
        context = FakeContext()
        with mock.patch.object(diary, "mark_shared") as mark:
            self._drive_success(on_text, context)   # success #1
            self._drive_failure(on_text, context)   # must NOT increment
            mark.assert_not_called()
            self._drive_success(on_text, context)   # success #2 -> now it fires
            mark.assert_called_once()

    def test_diary_flag_off_never_marks_shared(self):
        cfg, on_text = self._app_and_handler(diary_enabled=False)
        context = FakeContext()
        with mock.patch.object(diary, "mark_shared") as mark:
            self._drive_success(on_text, context)
            self._drive_success(on_text, context)
            self._drive_success(on_text, context)
            mark.assert_not_called()   # diary off -> no counting, no page-turn


# --- 9. Page-turn timing on the STREAMING path (streaming_enabled=True, the
#        production default): a mirror of section 8, guarding bot.py's second
#        counting site against one-sided drift from its non-streaming twin ----

class TestMarkSharedTurnCountingStreaming(unittest.TestCase):
    def setUp(self):
        _install_resets(self)

    def _app_and_handler(self, **overrides):
        cfg = _folder_cfg(self.root, streaming_enabled=True,
                          reflection_enabled=False, **overrides)
        app = bot.make_app(cfg)
        return cfg, _handler(app, MessageHandler)

    def _drive_success(self, on_text, context, text="a good long message to reply to"):
        with mock.patch.object(engine, "stream_once",
                               ScriptedEngine(ok_script(["a warm ",
                                                         "reply here."])).stream_once):
            asyncio.run(on_text(FakeUpdate(FakeMessage(text)), context))

    def _drive_failure(self, on_text, context, text="this message fails to reply now"):
        # A stream that ends ok=False: mirrors section 4's failure script.
        script = [{"type": "text", "text": "partial"},
                  {"type": "done",
                   "reply": EngineReply("partial", None, ok=False,
                                        error_kind="nonzero")}]
        with mock.patch.object(engine, "stream_once",
                               ScriptedEngine(script).stream_once):
            asyncio.run(on_text(FakeUpdate(FakeMessage(text)), context))

    def test_fires_only_on_second_successful_stream(self):
        cfg, on_text = self._app_and_handler(diary_enabled=True)
        context = FakeContext()
        with mock.patch.object(diary, "mark_shared") as mark:
            self._drive_success(on_text, context)
            mark.assert_not_called()       # after the 1st success: page still open
            self._drive_success(on_text, context)
            mark.assert_called_once()       # after the 2nd: pages turned
            self.assertEqual(mark.call_args.args[0], cfg)

    def test_failed_stream_never_counts(self):
        cfg, on_text = self._app_and_handler(diary_enabled=True)
        context = FakeContext()
        with mock.patch.object(diary, "mark_shared") as mark:
            self._drive_success(on_text, context)   # success #1
            self._drive_failure(on_text, context)   # must NOT increment
            mark.assert_not_called()
            self._drive_success(on_text, context)   # success #2 -> now it fires
            mark.assert_called_once()

    def test_diary_flag_off_never_marks_shared(self):
        cfg, on_text = self._app_and_handler(diary_enabled=False)
        context = FakeContext()
        with mock.patch.object(diary, "mark_shared") as mark:
            self._drive_success(on_text, context)
            self._drive_success(on_text, context)
            self._drive_success(on_text, context)
            mark.assert_not_called()   # diary off -> no counting, no page-turn


if __name__ == "__main__":
    unittest.main()
