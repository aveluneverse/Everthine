"""Tests for M4 Task 7: bot.py's heart-reaction pipeline.

Her heart on a companion message flows into the keepsake album; his
captured [react:emoji] tag (T6 stripped it, parking the emoji for this
task) sets a Telegram reaction on her message, and a heart among those
also flags it into the album from his side. Both directions share one
process-global-free cache -- make_app's own `_message_cache` closure --
so a partner heart can be resolved back to the companion text it landed
on without ever touching the album's own storage for the lookup.

Conventions follow tests/test_bot_memory_wiring.py (tmp dir + full
process-global reset around every make_app call: memory_recall.reset(),
memory_embed.set_embed_fn(None), persona.reset_persona_cache(),
messages.reset_overrides(), registered via addCleanup in the Windows-safe
LIFO order so memory_recall's sqlite handle closes before the tmp dir
tries to delete it) and tests/test_bot_persona_wiring.py's file-mode
persona fixture (a single .md file needs no folder-mode validation).

Handlers are closures defined inside make_app (on_text, handle_reaction),
so there is no way to call them without building a real Application
first; this file extracts the registered callback for a given handler
class out of app.handlers[0] and drives it directly with a fake Update/
Context pair via asyncio.run(...), exactly as if PTB had delivered a real
update. The engine itself is swapped out with mock.patch.object(engine,
"run_once", ...) rather than threading a fake engine_mod through --
on_text always calls produce_reply with the module-level default
(engine_mod=engine), so that is the only seam available to a caller that
never gets to pick produce_reply's kwargs.

Most scenarios here run with streaming_enabled=False: the reaction
pipeline's cache-fill and consumption steps are identical in spirit on
both reply paths (see bot.py's on_text), and the non-streaming path is
far simpler to drive with hand-written fakes. The streaming branch is
covered end-to-end too (TestStreamingPathReactionWiring, plus the
streaming halves of TestReactionConsumptionGates): the REAL on_text
streaming path over a REAL StreamingDisplay, with only engine.stream_once
scripted (via _drive_stream on the base class) and Telegram represented
by FakeMessage objects whose edit_text mirrors PTB's semantics of
returning a NEW Message rather than mutating the one it was called on.
On top of those, stream_reply's sent_sink plumbing keeps one direct,
narrow test of its own further down, pinning the trap that semantics
implies: a streamed reply's placeholder object never reflects its own
final text, so on_text must cache the text it already knows from
display.message_texts, never message.text.
"""
import asyncio
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram import ReactionTypeEmoji
from telegram.error import BadRequest
from telegram.ext import (CallbackQueryHandler, CommandHandler,
                          MessageHandler, MessageReactionHandler)

from everthine import album, bot, engine, memory_embed, memory_recall, messages, persona
from everthine.config import Config
from everthine.engine import EngineReply


def _make_fake(mapping: dict):
    """A plain (non-recording) dict-driven fake embed function -- mirrors
    test_bot_memory_wiring.py's helper of the same name."""
    def fake(text):
        return list(mapping.get(text, [0.0, 1.0]))
    return fake


def _handler(app, handler_cls):
    """Pull the registered callback for a given handler class out of
    app.handlers[0] (PTB's default group) -- on_text and handle_reaction
    are closures with no existence outside a built Application."""
    for h in app.handlers[0]:
        if isinstance(h, handler_cls):
            return h.callback
    raise AssertionError(f"no {handler_cls.__name__} registered")


class FakeUser:
    def __init__(self, id):
        self.id = id


class FakeChat:
    def __init__(self, id):
        self.id = id


class FakeMessage:
    """Minimal telegram.Message stand-in: enough surface for both of
    on_text's branches (text, message_id, reply_text, edit_text) and for
    reaction consumption (set_reaction) to run against. Auto-assigned
    message_ids are unique per instance, mirroring real Telegram
    message_ids."""
    _counter = itertools.count(9001)

    def __init__(self, text, message_id=None, set_reaction_error=None):
        self.text = text
        self.message_id = (message_id if message_id is not None
                           else next(FakeMessage._counter))
        self.set_reaction_error = set_reaction_error
        self.reactions_set: list = []
        self.replies: list = []
        self.edits: list = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        reply = FakeMessage(text)
        self.replies.append(reply)
        return reply

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        # Mirrors real PTB semantics: an edit returns a NEW Message and
        # never mutates this object's .text -- the immutability trap the
        # cache design documents. Edits are recorded for assertions;
        # edits[-1] is what a real Telegram client would be displaying.
        self.edits.append(text)
        return FakeMessage(text, message_id=self.message_id)

    async def set_reaction(self, reaction, is_big=None):
        if self.set_reaction_error is not None:
            raise self.set_reaction_error
        self.reactions_set.append(reaction)
        return True


class FakeUpdate:
    def __init__(self, message, user_id=1, chat_id=1):
        self.message = message
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(chat_id)
        self.message_reaction = None


class FakeMessageReaction:
    """Minimal telegram.MessageReactionUpdated stand-in: user/chat/
    message_id/old_reaction/new_reaction, the only surface handle_reaction
    touches. old_reaction/new_reaction hold real ReactionTypeEmoji
    instances (not a hand-rolled lookalike) so bot.py's own
    isinstance(..., ReactionTypeEmoji) filtering sees them correctly."""
    def __init__(self, user_id, chat_id, message_id, old_emojis, new_emojis):
        self.user = FakeUser(user_id) if user_id is not None else None
        self.chat = FakeChat(chat_id)
        self.message_id = message_id
        self.old_reaction = [ReactionTypeEmoji(e) for e in old_emojis]
        self.new_reaction = [ReactionTypeEmoji(e) for e in new_emojis]


class FakeReactionUpdate:
    def __init__(self, user_id, chat_id, message_id, old_emojis, new_emojis):
        self.message_reaction = FakeMessageReaction(
            user_id, chat_id, message_id, old_emojis, new_emojis)


class FakeBot:
    """Records send_message calls -- the seam handle_reaction uses to
    answer a heart on an expired (cache-missed) message; there is no
    update.message to reply_text through on a reaction update."""
    def __init__(self):
        self.sent_messages: list = []

    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


class ScriptedEngine:
    """stream_once stand-in: pushes a scripted event list onto the queue --
    mirrors tests/test_bot_stream.py's helper of the same name."""
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


class _AlbumWiringTestCase(unittest.TestCase):
    """Base for every scenario below: a tmp dir plus the full set of
    process-global resets a make_app call can touch. Registration order
    matters on Windows -- the tmp-dir cleanup is registered FIRST so it
    runs LAST (addCleanup is LIFO), after memory_recall.reset() has
    already closed the sqlite handle living inside that same tmp dir."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        self.addCleanup(memory_embed.set_embed_fn, None)
        self.addCleanup(persona.reset_persona_cache)
        self.addCleanup(messages.reset_overrides)
        memory_recall.reset()
        memory_embed.set_embed_fn(_make_fake({}))
        persona.reset_persona_cache()
        messages.reset_overrides()
        self.root = Path(self._td.name)

    def _cfg(self, **overrides) -> Config:
        persona_file = self.root / "persona.md"
        if not persona_file.exists():
            persona_file.write_text("You are Testbot, warm and steady.",
                                    encoding="utf-8")
        kwargs = dict(bot_token="x", authorized_user_id=1,
                     data_dir=self.root / "data", persona_path=persona_file,
                     memory_enabled=False, streaming_enabled=False,
                     album_enabled=True)
        kwargs.update(overrides)
        return Config(**kwargs)

    def _seed(self, cfg, reply_text="a nice reply"):
        """Build one app and drive its on_text (engine mocked) so the
        reaction pipeline's cache gets populated the same way a real turn
        would -- on_text and handle_reaction are closures sharing the same
        make_app call's _message_cache, so both handlers must come from
        this one app for a later heart to resolve against this seed."""
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        handle_reaction = _handler(app, MessageReactionHandler)
        her_message = FakeMessage("her incoming message")
        update = FakeUpdate(her_message)
        with mock.patch.object(engine, "run_once",
                               return_value=EngineReply(reply_text, "sess-seed",
                                                        ok=True)):
            asyncio.run(on_text(update, FakeContext()))
        sent = her_message.replies[0]
        return app, handle_reaction, sent

    def _drive_stream(self, cfg, script, text="her words"):
        """Drive one REAL streamed turn through the REAL on_text closure:
        make_app builds the app, engine.stream_once is the only thing
        scripted (the module-attribute seam, since on_text's stream_reply
        call uses the module-level engine default), and a real
        StreamingDisplay runs over FakeMessage objects. cfg must have
        streaming_enabled=True. Returns (app, her_message); the
        placeholder the display edited is her_message.replies[0], and any
        split continuations follow it in the same list."""
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her_message = FakeMessage(text)
        update = FakeUpdate(her_message)
        with mock.patch.object(engine, "stream_once",
                               ScriptedEngine(script).stream_once):
            asyncio.run(on_text(update, FakeContext()))
        return app, her_message


# --- His side: the captured [react:emoji] tag sets a reaction, and a heart
#     among those also flags her message into the album. --------------------

class TestCompanionReactionConsumption(_AlbumWiringTestCase):
    def test_companion_heart_tag_sets_reaction_and_flags_user_message(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her_message = FakeMessage("I love that sunset too")
        update = FakeUpdate(her_message)

        with mock.patch.object(
                engine, "run_once",
                return_value=EngineReply("[react:❤️] me too",
                                         "sess-1", ok=True)):
            asyncio.run(on_text(update, FakeContext()))

        self.assertEqual(her_message.reactions_set,
                         [[ReactionTypeEmoji("❤️")]])
        entries = album.all_entries(cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["direction"], "companion_flagged")
        self.assertEqual(entries[0]["message"],
                         {"speaker": "user", "text": "I love that sunset too"})
        self.assertEqual(entries[0]["message_id"], her_message.message_id)
        # The reply itself still landed, tag stripped.
        self.assertEqual(her_message.replies[0].text, "me too")

    def test_companion_non_heart_tag_sets_reaction_but_no_flag(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her_message = FakeMessage("that's exciting news")
        update = FakeUpdate(her_message)

        with mock.patch.object(
                engine, "run_once",
                return_value=EngineReply("[react:\U0001f525] love that energy",
                                         "sess-2", ok=True)):
            asyncio.run(on_text(update, FakeContext()))

        self.assertEqual(her_message.reactions_set,
                         [[ReactionTypeEmoji("\U0001f525")]])
        self.assertEqual(album.all_entries(cfg), [])

    def test_set_reaction_failure_never_breaks_reply(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her_message = FakeMessage(
            "hello there", set_reaction_error=BadRequest("REACTION_INVALID"))
        update = FakeUpdate(her_message)

        with mock.patch.object(
                engine, "run_once",
                return_value=EngineReply("[react:\U0001f937] not sure either",
                                         "sess-3", ok=True)):
            with self.assertLogs("everthine", level="WARNING"):
                asyncio.run(on_text(update, FakeContext()))  # must not raise

        # The reply text still went out despite the reaction call blowing up.
        self.assertEqual(len(her_message.replies), 1)
        self.assertEqual(her_message.replies[0].text, "not sure either")
        # An invalid emoji is never a heart -- no album write either.
        self.assertEqual(album.all_entries(cfg), [])


# --- Her side: a heart added on a cached companion message keeps it (or,
#     cache miss, tells her honestly); a heart removed un-keeps it. --------

class TestPartnerHeartReactions(_AlbumWiringTestCase):
    def test_partner_heart_on_cached_message_lands_in_album(self):
        cfg = self._cfg()
        app, handle_reaction, sent = self._seed(cfg, reply_text="a nice reply")

        reaction_update = FakeReactionUpdate(
            user_id=1, chat_id=1, message_id=sent.message_id,
            old_emojis=[], new_emojis=["❤️"])
        context = FakeContext()
        asyncio.run(handle_reaction(reaction_update, context))

        entries = album.all_entries(cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["direction"], "partner_flagged")
        self.assertEqual(entries[0]["message"],
                         {"speaker": "companion", "text": "a nice reply"})
        self.assertEqual(entries[0]["message_id"], sent.message_id)
        # Her gesture is the ritual -- the bot never narrates it.
        self.assertEqual(context.bot.sent_messages, [])

    def test_partner_heart_on_expired_message_answers_album_expired(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        handle_reaction = _handler(app, MessageReactionHandler)

        reaction_update = FakeReactionUpdate(
            user_id=1, chat_id=77, message_id=424242,  # never cached
            old_emojis=[], new_emojis=["❤️"])
        context = FakeContext()
        asyncio.run(handle_reaction(reaction_update, context))

        self.assertEqual(context.bot.sent_messages,
                         [(77, messages.msg("album_expired"))])
        self.assertEqual(album.all_entries(cfg), [])

    def test_partner_unheart_removes_entry_silently(self):
        cfg = self._cfg()
        app, handle_reaction, sent = self._seed(cfg, reply_text="a keepsake")
        add_update = FakeReactionUpdate(
            user_id=1, chat_id=1, message_id=sent.message_id,
            old_emojis=[], new_emojis=["❤️"])
        asyncio.run(handle_reaction(add_update, FakeContext()))
        self.assertEqual(len(album.all_entries(cfg)), 1)

        remove_update = FakeReactionUpdate(
            user_id=1, chat_id=1, message_id=sent.message_id,
            old_emojis=["❤️"], new_emojis=[])
        context = FakeContext()
        asyncio.run(handle_reaction(remove_update, context))

        self.assertEqual(album.all_entries(cfg), [])
        self.assertEqual(context.bot.sent_messages, [])

    def test_unauthorized_reaction_is_ignored(self):
        cfg = self._cfg()
        app, handle_reaction, sent = self._seed(cfg, reply_text="a nice reply")

        reaction_update = FakeReactionUpdate(
            user_id=999, chat_id=1, message_id=sent.message_id,
            old_emojis=[], new_emojis=["❤️"])
        context = FakeContext()
        asyncio.run(handle_reaction(reaction_update, context))

        self.assertEqual(album.all_entries(cfg), [])
        self.assertEqual(context.bot.sent_messages, [])

    def test_reaction_user_none_is_ignored(self):
        cfg = self._cfg()
        app, handle_reaction, sent = self._seed(cfg, reply_text="a nice reply")

        reaction_update = FakeReactionUpdate(
            user_id=None, chat_id=1, message_id=sent.message_id,
            old_emojis=[], new_emojis=["❤️"])
        context = FakeContext()
        asyncio.run(handle_reaction(reaction_update, context))

        self.assertEqual(album.all_entries(cfg), [])
        self.assertEqual(context.bot.sent_messages, [])


# --- Consumption gates (fix round 1). Two invariants the first cut of T7
#     broke:
#     (1) ALBUM_ENABLED=false must be byte-equivalent to the pre-feature
#         baseline -- the L1 rollback. A visible Telegram reaction set on
#         her message while the flag is off IS user-facing feature
#         behavior, no matter that the album write itself was gated; when
#         off, the captured emoji must be discarded exactly as T6 left it.
#     (2) A reaction is a success gesture. The non-streaming path is
#         success-only by construction (produce_reply's failure path
#         early-returns before the on_react sink fires), but the streaming
#         path consumed display.reaction_emoji whenever the turn was not
#         cancelled -- a mid-stream engine death after emitting a tag
#         would heart+flag her message right next to an error notice. ----

class TestReactionConsumptionGates(_AlbumWiringTestCase):
    def test_album_flag_off_discards_captured_emoji_non_stream(self):
        cfg = self._cfg(album_enabled=False)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her_message = FakeMessage("a quiet evening in")
        update = FakeUpdate(her_message)

        with mock.patch.object(
                engine, "run_once",
                return_value=EngineReply("[react:❤️] kept forever",
                                         "sess-off", ok=True)):
            asyncio.run(on_text(update, FakeContext()))

        # Flag off == pre-feature baseline: no visible reaction, no album
        # write, and the reply itself still lands tag-stripped (the strip
        # is T6 behavior, active regardless of the album flag).
        self.assertEqual(her_message.reactions_set, [])
        self.assertEqual(album.all_entries(cfg), [])
        self.assertEqual(her_message.replies[0].text, "kept forever")

    def test_album_flag_off_discards_captured_emoji_streaming(self):
        cfg = self._cfg(album_enabled=False, streaming_enabled=True)
        app, her_message = self._drive_stream(
            cfg, ok_script(["[react:❤️] kept forever."]))

        self.assertEqual(her_message.reactions_set, [])
        self.assertEqual(album.all_entries(cfg), [])
        # The streamed reply still landed tag-stripped in the placeholder.
        placeholder = her_message.replies[0]
        self.assertEqual(placeholder.edits[-1], "kept forever.")

    def test_failed_stream_with_tag_sets_no_reaction_and_no_flag(self):
        cfg = self._cfg(streaming_enabled=True)
        script = [
            {"type": "text", "text": "[react:❤️] partial thought"},
            {"type": "done",
             "reply": EngineReply("[react:❤️] partial thought", None,
                                  ok=False, error_kind="nonzero")},
        ]
        app, her_message = self._drive_stream(cfg, script, text="are you ok?")

        # The tag was captured (display strips it either way), but the turn
        # FAILED: no reaction lands on her message and nothing enters the
        # album -- a heart next to an error notice would read as him
        # keeping the moment his reply died on.
        self.assertEqual(her_message.reactions_set, [])
        self.assertEqual(album.all_entries(cfg), [])
        # The failure path itself is unchanged: partial text kept in the
        # placeholder, then the error notice as a fresh message.
        placeholder = her_message.replies[0]
        self.assertEqual(placeholder.edits[-1], "partial thought")
        self.assertEqual(her_message.replies[1].text, messages.msg("nonzero"))


# --- Streaming path end-to-end (fix round 1): the REAL on_text streaming
#     branch -- real StreamingDisplay, real sent_sink -> cache fill, real
#     consumption -- with only engine.stream_once scripted. ---------------

class TestStreamingPathReactionWiring(_AlbumWiringTestCase):
    def test_streamed_heart_tag_sets_reaction_and_flags_her_message(self):
        cfg = self._cfg(streaming_enabled=True)
        app, her_message = self._drive_stream(
            cfg, ok_script(["[react:❤️] warm and here."]),
            text="I kept your note")

        self.assertEqual(her_message.reactions_set,
                         [[ReactionTypeEmoji("❤️")]])
        entries = album.all_entries(cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["direction"], "companion_flagged")
        self.assertEqual(entries[0]["message"],
                         {"speaker": "user", "text": "I kept your note"})
        self.assertEqual(entries[0]["message_id"], her_message.message_id)
        # The visible reply was tag-free.
        placeholder = her_message.replies[0]
        self.assertEqual(placeholder.edits[-1], "warm and here.")

    def test_streamed_multi_message_turn_seeds_cache_for_partner_hearts(self):
        cfg = self._cfg(streaming_enabled=True)
        # Long enough to force StreamingDisplay's split-and-continue: the
        # turn lands as TWO messages (the edited placeholder + a fresh
        # continuation), and BOTH must be heartable afterwards.
        long_text = "A steady line we said together. " * 130  # ~4160 chars
        app, her_message = self._drive_stream(cfg, ok_script([long_text]))
        handle_reaction = _handler(app, MessageReactionHandler)

        placeholder, continuation = her_message.replies[0], her_message.replies[1]

        for target in (placeholder, continuation):
            reaction_update = FakeReactionUpdate(
                user_id=1, chat_id=1, message_id=target.message_id,
                old_emojis=[], new_emojis=["❤️"])
            context = FakeContext()
            asyncio.run(handle_reaction(reaction_update, context))
            # Cache hit, both times: silent keep, never album_expired.
            self.assertEqual(context.bot.sent_messages, [])

        entries = album.all_entries(cfg)
        self.assertEqual(len(entries), 2)
        texts = {e["message_id"]: e["message"]["text"] for e in entries}
        for e in entries:
            self.assertEqual(e["direction"], "partner_flagged")
            self.assertEqual(e["message"]["speaker"], "companion")
        # Each entry carries the text that message was DISPLAYING: the
        # placeholder's final edit, and the continuation's sent content --
        # and the two halves reassemble the full streamed reply exactly.
        self.assertEqual(texts[placeholder.message_id], placeholder.edits[-1])
        self.assertEqual(texts[continuation.message_id], continuation.text)
        self.assertEqual(texts[placeholder.message_id]
                         + texts[continuation.message_id], long_text)
        # And the immutability trap, pinned end-to-end: the placeholder
        # object's own .text is still the thinking line, so the cache
        # demonstrably stored display.message_texts, not message.text.
        self.assertNotEqual(texts[placeholder.message_id], placeholder.text)


# --- _message_cache's own lazy pruning: MESSAGE_CACHE_TTL_S and
#     MESSAGE_CACHE_MAX are named exactly in the spec, so both get a direct
#     test rather than resting on the cache-miss coverage above (which
#     proves a message that was NEVER cached is honestly reported, not
#     that one which WAS cached correctly ages or falls off the cap). -----

class TestMessageCachePruning(_AlbumWiringTestCase):
    def _send_turn(self, on_text, text):
        message = FakeMessage(text)
        with mock.patch.object(engine, "run_once",
                               return_value=EngineReply(f"reply to {text}",
                                                        "sess", ok=True)):
            asyncio.run(on_text(FakeUpdate(message), FakeContext()))
        return message.replies[0]

    def test_expired_cache_entry_is_pruned_on_next_insert(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        handle_reaction = _handler(app, MessageReactionHandler)

        clock = {"t": 1000.0}
        with mock.patch("time.monotonic", side_effect=lambda: clock["t"]):
            first_sent = self._send_turn(on_text, "first turn")
            # Past the TTL: the next cache insert must prune this entry.
            clock["t"] += bot.MESSAGE_CACHE_TTL_S + 1
            second_sent = self._send_turn(on_text, "second turn")

        expired_reaction = FakeReactionUpdate(
            user_id=1, chat_id=5, message_id=first_sent.message_id,
            old_emojis=[], new_emojis=["❤️"])
        expired_context = FakeContext()
        asyncio.run(handle_reaction(expired_reaction, expired_context))
        self.assertEqual(expired_context.bot.sent_messages,
                         [(5, messages.msg("album_expired"))])

        fresh_reaction = FakeReactionUpdate(
            user_id=1, chat_id=5, message_id=second_sent.message_id,
            old_emojis=[], new_emojis=["❤️"])
        fresh_context = FakeContext()
        asyncio.run(handle_reaction(fresh_reaction, fresh_context))
        self.assertEqual(fresh_context.bot.sent_messages, [])
        self.assertEqual(len(album.all_entries(cfg)), 1)

    def test_cache_eviction_at_max_cap_drops_oldest_first(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        handle_reaction = _handler(app, MessageReactionHandler)

        sent = [self._send_turn(on_text, f"turn {i}")
               for i in range(bot.MESSAGE_CACHE_MAX + 1)]

        # The oldest entry is now over the cap and must have been evicted.
        oldest_reaction = FakeReactionUpdate(
            user_id=1, chat_id=9, message_id=sent[0].message_id,
            old_emojis=[], new_emojis=["❤️"])
        oldest_context = FakeContext()
        asyncio.run(handle_reaction(oldest_reaction, oldest_context))
        self.assertEqual(oldest_context.bot.sent_messages,
                         [(9, messages.msg("album_expired"))])

        # The most recent entry must still be resolvable.
        newest_reaction = FakeReactionUpdate(
            user_id=1, chat_id=9, message_id=sent[-1].message_id,
            old_emojis=[], new_emojis=["❤️"])
        newest_context = FakeContext()
        asyncio.run(handle_reaction(newest_reaction, newest_context))
        self.assertEqual(newest_context.bot.sent_messages, [])
        self.assertEqual(len(album.all_entries(cfg)), 1)


# --- Flag gating: ALBUM_ENABLED=false must byte-match M3's handler table
#     (no reaction handler, no reaction updates requested from Telegram). --

class TestAlbumFlagGating(_AlbumWiringTestCase):
    def test_album_flag_off_no_reaction_handler_registered(self):
        cfg = self._cfg(album_enabled=False)
        app = bot.make_app(cfg)
        # Byte-equivalence with M3's handler table, pinned exactly: the
        # same single default group, the same three handlers in the same
        # order, and nothing else -- not merely "no reaction handler".
        self.assertEqual(list(app.handlers.keys()), [0])
        self.assertEqual([type(h) for h in app.handlers[0]],
                         [CommandHandler, CallbackQueryHandler, MessageHandler])

    def test_allowed_updates_include_reaction_only_when_album_on(self):
        cfg_on = self._cfg(album_enabled=True)
        cfg_off = self._cfg(album_enabled=False)
        self.assertEqual(bot._allowed_updates(cfg_on),
                         ["message", "callback_query", "message_reaction"])
        self.assertEqual(bot._allowed_updates(cfg_off),
                         ["message", "callback_query"])


# --- stream_reply's sent_sink: pins the Message-immutability trap. A real
#     telegram.Message.edit_text() returns a NEW Message rather than
#     mutating the one it was called on, so caching must use the text the
#     caller already knows (display.message_texts), never message.text off
#     whatever object display.finalize() hands back. -------------------------

class FakeStreamMessage:
    def __init__(self, message_id, text):
        self.message_id = message_id
        # A real placeholder's .text never changes after construction, no
        # matter how many times it gets edited -- this fake pins that by
        # deliberately NOT reflecting the streamed content here.
        self.text = "...thinking..."


class FakeSentDisplay:
    """Stands in for StreamingDisplay: finalize() returns Message-shaped
    objects whose .text is stale by construction (see FakeStreamMessage),
    while message_texts carries the true final content -- exactly the
    split real StreamingDisplay produces."""
    def __init__(self, final_text):
        self._final_text = final_text
        self.reaction_emoji = None

    @property
    def full_text(self):
        return self._final_text

    @property
    def message_texts(self):
        return [self._final_text] if self._final_text else []

    async def append(self, chunk):
        pass

    async def finalize(self):
        if not self._final_text:
            return []
        return [FakeStreamMessage(5555, self._final_text)]

    async def cancel(self):
        return []


class TestStreamReplySentSink(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        self.addCleanup(memory_embed.set_embed_fn, None)
        self.addCleanup(persona.reset_persona_cache)
        self.addCleanup(messages.reset_overrides)
        memory_recall.reset()
        memory_embed.set_embed_fn(_make_fake({}))
        persona.reset_persona_cache()
        messages.reset_overrides()
        self.root = Path(self._td.name)

    async def test_sent_sink_pairs_with_message_texts_not_message_dot_text(self):
        import threading

        cfg = Config(bot_token="x", authorized_user_id=1,
                     data_dir=self.root / "data", memory_enabled=False)
        from everthine.session_store import SessionStore
        store = SessionStore(cfg.session_path)
        display = FakeSentDisplay("the real streamed reply")
        eng = ScriptedEngine(ok_script(["the real ", "streamed reply"]))
        sink: list = []

        reply = await bot.stream_reply(cfg, store, "hello", display,
                                       threading.Event(), engine_mod=eng,
                                       sent_sink=sink)

        self.assertTrue(reply.ok)
        self.assertEqual(len(sink), 1)
        # sink holds the Message-shaped object; its OWN .text is stale
        # (matching real Telegram edit_text semantics) -- the caller is
        # expected to pair it with display.message_texts by position,
        # never trust message.text directly.
        self.assertEqual(sink[0].text, "...thinking...")
        self.assertEqual(display.message_texts, ["the real streamed reply"])


if __name__ == "__main__":
    unittest.main()
