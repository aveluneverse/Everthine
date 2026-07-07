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

M4 Task 8 extends this same file with /stage and /album: a second
CallbackQueryHandler (pattern=r"^(stg|alb)_") drives their button flows,
tested here via a NEW _stage_album_handler() lookup (on_button's own bare,
unpatterned CallbackQueryHandler is no longer the only one in
app.handlers[0], so class-type lookup alone can no longer disambiguate
them) and a NEW _command_handler(app, name) lookup (app.handlers[0] may
now hold three CommandHandlers -- start/stage/album -- at once). These
scenarios need a FOLDER-mode persona with a real stages.md, unlike every
T7 scenario above (file-mode personas never have stages -- see
persona.Persona.stages' own docstring): _StageAlbumUITestCase overrides
_cfg() accordingly, leaving every T7 class above using the base file-mode
_cfg() untouched.
"""
import asyncio
import inspect
import itertools
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from telegram import CallbackQuery as TgCallbackQuery
from telegram import InlineKeyboardMarkup, ReactionTypeEmoji
from telegram import Update as TgUpdate
from telegram import User as TgUser
from telegram.error import BadRequest
from telegram.ext import (CallbackQueryHandler, CommandHandler,
                          MessageHandler, MessageReactionHandler)

from everthine import (album, archive, bot, engine, memory_embed,
                       memory_recall, messages, persona, stages)
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


def _command_handler(app, command):
    """The registered callback for one specific /command name. app.handlers[0]
    may hold THREE CommandHandlers at once once M4 T8's flag-gated /stage and
    /album join /start, so _handler's plain by-class lookup can no longer
    disambiguate between them; this filters on CommandHandler.commands
    (the frozenset of lowercased names PTB matches against) instead."""
    for h in app.handlers[0]:
        if isinstance(h, CommandHandler) and command in h.commands:
            return h.callback
    raise AssertionError(f"no CommandHandler registered for /{command}")


def _stage_album_handler(app):
    """The M4 T8 CallbackQueryHandler (pattern=r"^(stg|alb)_"). app.handlers[0]
    holds TWO CallbackQueryHandlers whenever /stage or /album is active: this
    one, and on_button's own bare, unpatterned one (matches every callback
    query unconditionally -- see bot.py's handler-registration comment for
    why registration ORDER, not just existence, keeps the two from
    colliding). Discriminated here by which one actually carries a pattern,
    since on_button's does not."""
    for h in app.handlers[0]:
        if isinstance(h, CallbackQueryHandler) and h.pattern is not None:
            return h.callback
    raise AssertionError("no stage/album CallbackQueryHandler registered")


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
        # M4 T8: the reply_markup each edit_text call was given, positionally
        # aligned with .edits (markup_edits[-1] pairs with edits[-1]) -- for
        # tests that need to inspect a /stage or /album view's button layout
        # after an edit, not just its text. .markup is the markup a
        # reply_text call was given, stashed on the CHILD message it
        # returns (a fresh /stage or /album command response is a new
        # message, never an edit of this one).
        self.markup_edits: list = []
        self.markup = None
        # N4: a tag-only streamed reply deletes its placeholder instead of
        # leaving it stuck on the waiting line -- recorded so the e2e test
        # can pin the deletion. No T7/T8 scenario above reads this.
        self.deleted = False

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        reply = FakeMessage(text)
        reply.markup = reply_markup
        self.replies.append(reply)
        return reply

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        # Mirrors real PTB semantics: an edit returns a NEW Message and
        # never mutates this object's .text -- the immutability trap the
        # cache design documents. Edits are recorded for assertions;
        # edits[-1] is what a real Telegram client would be displaying.
        self.edits.append(text)
        self.markup_edits.append(reply_markup)
        return FakeMessage(text, message_id=self.message_id)

    async def set_reaction(self, reaction, is_big=None):
        if self.set_reaction_error is not None:
            raise self.set_reaction_error
        self.reactions_set.append(reaction)
        return True


class FakeCallbackQuery:
    """Minimal telegram.CallbackQuery stand-in (M4 T8): enough surface for
    on_stage_album_button (data, answer, edit_message_text) to run against.
    edit_message_text delegates to the message this query is attached to,
    mirroring how a real CallbackQuery's edit just proxies to
    Bot.edit_message_text against that same chat/message_id -- so
    assertions read off the message's own .edits/.markup_edits exactly as
    the button-less command tests already do."""

    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers: list = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        return await self.message.edit_text(text, parse_mode=parse_mode,
                                            reply_markup=reply_markup)


class FakeUpdate:
    def __init__(self, message, user_id=1, chat_id=1, callback_query=None):
        self.message = message
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(chat_id)
        self.message_reaction = None
        # M4 T8: a callback-query update carries no incoming .message of its
        # own in the pattern this file's other fakes use (the message
        # parameter above is the message the button is ATTACHED to, reused
        # so _authorized's update.effective_user check still works
        # uniformly for every update shape).
        self.callback_query = callback_query


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


# --- M4 final review: a heart VARIANT swap ("❤️" -> "❤", both read as
#     "heart" to a person but are distinct Telegram reaction strings) must
#     not un-keep an already-kept moment. Reviewer-reproduced: a single
#     update carrying old=["❤️"] new=["❤"] used to hit the added-branch
#     first (dedup no-ops, the entry already exists) and then the
#     removed-branch second, deleting the very entry the message's still-
#     visible heart implies is still kept. -------------------------------

class TestHeartVariantSwapKeepsTheKeepsake(_AlbumWiringTestCase):
    def test_variant_swap_does_not_delete_the_kept_entry(self):
        cfg = self._cfg()
        app, handle_reaction, sent = self._seed(cfg, reply_text="a nice reply")

        # An initial heart keeps the moment (a pre-existing entry -- the
        # scenario the reviewer's dedup-then-delete sequence depends on).
        first_heart = FakeReactionUpdate(
            user_id=1, chat_id=1, message_id=sent.message_id,
            old_emojis=[], new_emojis=["❤️"])
        asyncio.run(handle_reaction(first_heart, FakeContext()))
        self.assertEqual(len(album.all_entries(cfg)), 1)

        # The client swaps the heart glyph variant: old and new are both
        # heart emoji, so this is one update carrying both a heart "add"
        # and a heart "remove" at once -- not an actual un-heart.
        swap_update = FakeReactionUpdate(
            user_id=1, chat_id=1, message_id=sent.message_id,
            old_emojis=["❤️"], new_emojis=["❤"])
        context = FakeContext()
        asyncio.run(handle_reaction(swap_update, context))

        entries = album.all_entries(cfg)
        self.assertEqual(len(entries), 1)
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


# --- T7a: the notebook_full system notice is sent after the companion
#     reply, in order, but NEVER cached -- so hearting it misses the cache
#     (album_expired) and a system line can never land in the keepsake
#     album, while the companion reply itself stays cached and heartable. --

class TestBloatNoticeNotCached(_AlbumWiringTestCase):
    def test_bloat_notice_follows_reply_and_is_not_cacheable(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        handle_reaction = _handler(app, MessageReactionHandler)
        her_message = FakeMessage("tell me about your day")

        with mock.patch.object(bot.SessionStore, "detect_bloat", return_value=True):
            with mock.patch.object(
                    engine, "run_once",
                    return_value=EngineReply("it was quiet and good",
                                             "sess-bloat", ok=True)):
                asyncio.run(on_text(FakeUpdate(her_message), FakeContext()))

        # The order she saw is unchanged: companion reply first, the system
        # notice after it.
        self.assertEqual([r.text for r in her_message.replies],
                         ["it was quiet and good", messages.msg("notebook_full")])
        companion_reply, notice = her_message.replies[0], her_message.replies[1]

        # The companion reply was cached: her heart on it lands silently.
        heart_reply = FakeReactionUpdate(
            user_id=1, chat_id=1, message_id=companion_reply.message_id,
            old_emojis=[], new_emojis=["❤️"])
        ctx_reply = FakeContext()
        asyncio.run(handle_reaction(heart_reply, ctx_reply))
        self.assertEqual(ctx_reply.bot.sent_messages, [])
        entries = album.all_entries(cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["message"]["text"], "it was quiet and good")

        # The system notice was NOT cached: a heart on it misses the cache
        # (album_expired) and nothing new enters the album.
        heart_notice = FakeReactionUpdate(
            user_id=1, chat_id=1, message_id=notice.message_id,
            old_emojis=[], new_emojis=["❤️"])
        ctx_notice = FakeContext()
        asyncio.run(handle_reaction(heart_notice, ctx_notice))
        self.assertEqual(ctx_notice.bot.sent_messages,
                         [(1, messages.msg("album_expired"))])
        self.assertEqual(len(album.all_entries(cfg)), 1)


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


# --- N4: the tag-only reply (the reaction IS the whole response). Both
#     paths must delete/skip the placeholder rather than leave it stuck on
#     the waiting line or degrade to a glitch apology, while the reaction
#     still lands and nothing tag-shaped reaches the conversation archive. --

class TestTagOnlyReplyWiring(_AlbumWiringTestCase):
    _GLITCHES = ("generic_glitch", "nonzero", "timeout", "cli_missing")

    def _assert_no_glitch(self, replies):
        glitch_texts = {messages.msg(k) for k in self._GLITCHES}
        for reply in replies:
            self.assertNotIn(reply.text, glitch_texts)

    def test_streamed_tag_only_deletes_placeholder_and_keeps_reaction(self):
        cfg = self._cfg(streaming_enabled=True)
        app, her_message = self._drive_stream(
            cfg, ok_script(["[react:❤️]"]), text="I love you")

        # Only the placeholder was ever sent, and it was deleted -- never
        # left stuck on the thinking line, never a glitch apology.
        self.assertEqual(len(her_message.replies), 1)
        placeholder = her_message.replies[0]
        self.assertTrue(placeholder.deleted)
        self._assert_no_glitch(her_message.replies)
        # The reaction still landed on her message and the heart was kept.
        self.assertEqual(her_message.reactions_set, [[ReactionTypeEmoji("❤️")]])
        entries = album.all_entries(cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["direction"], "companion_flagged")
        # The gesture said everything: no companion line entered the archive.
        companion = [e for e in archive.iter_entries(cfg.archive_dir)
                     if e["speaker"] == "companion"]
        self.assertEqual(companion, [])

    def test_nonstreaming_tag_only_sends_no_text_but_consumes_reaction(self):
        cfg = self._cfg()  # streaming off (base default)
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        her_message = FakeMessage("I love you")
        update = FakeUpdate(her_message)
        with mock.patch.object(
                engine, "run_once",
                return_value=EngineReply("[react:❤️]", "sess-tagonly", ok=True)):
            asyncio.run(on_text(update, FakeContext()))

        # Zero reply_text sent -- the gesture is the whole message.
        self.assertEqual(her_message.replies, [])
        # ...but the reaction was consumed and the heart kept.
        self.assertEqual(her_message.reactions_set, [[ReactionTypeEmoji("❤️")]])
        entries = album.all_entries(cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["direction"], "companion_flagged")
        companion = [e for e in archive.iter_entries(cfg.archive_dir)
                     if e["speaker"] == "companion"]
        self.assertEqual(companion, [])


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


# --- M4 Task 8: /stage and /album command UI. ------------------------------
#
# Unlike every scenario above, these need a FOLDER-mode persona with a real
# stages.md -- file-mode personas never have stages (persona.Persona.stages'
# own docstring: "mode == 'file': ... stages, which is always None in file
# mode"). STAGE_SECTIONS mirrors persona.py's own stages.md section format
# ("## name" heading, body text below each, per _parse_stages).

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)

STAGE_SECTIONS = (
    ("Settling in", "Gentle and curious, still learning Sam's rhythms."),
    ("In rhythm", "Comfortable, easy, a shared cadence."),
    ("Deep water", "Fully open, nothing held back."),
)
STAGE_NAMES = tuple(name for name, _ in STAGE_SECTIONS)


def _write_stage_persona(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "identity.md").write_text(
        "I am Alex, warm and steady.\n", encoding="utf-8")
    (root / "settings.yaml").write_text(
        "companion:\n  name: Alex\npartner:\n  name: Sam\n", encoding="utf-8")
    body = "\n\n".join(f"## {name}\n{text}" for name, text in STAGE_SECTIONS)
    (root / "stages.md").write_text(body + "\n", encoding="utf-8")
    return root


def _button_labels(markup: InlineKeyboardMarkup) -> list:
    """Flatten an InlineKeyboardMarkup's rows into one list of button
    labels, row-then-column order -- the shape most scenarios below only
    need to assert presence/absence/order over; a test that cares about
    row GROUPING (e.g. the album nav row) reads .inline_keyboard directly
    instead."""
    return [button.text for row in markup.inline_keyboard for button in row]


def _on_button_handler(app):
    """The ORIGINAL M1.5 CallbackQueryHandler (on_button, pattern=None,
    matches every callback query unconditionally). Discriminated from the
    M4 T8 one by the absence of a pattern -- the mirror image of
    _stage_album_handler below."""
    for h in app.handlers[0]:
        if isinstance(h, CallbackQueryHandler) and h.pattern is None:
            return h.callback
    raise AssertionError("no on_button CallbackQueryHandler registered")


class _StageAlbumUITestCase(_AlbumWiringTestCase):
    """Task 8's UI tests need a FOLDER-mode persona with a real stages.md on
    top of every reset _AlbumWiringTestCase already does (tmp dir +
    memory_recall/memory_embed/persona-cache/messages-overrides resets).
    _cfg() here SHADOWS the base class's file-mode one for every test class
    below that inherits from THIS class instead of directly from
    _AlbumWiringTestCase; every T7 class above is unaffected, since Python
    resolves self._cfg() through each test case's own MRO."""

    def _cfg(self, **overrides):
        folder = self.root / "persona"
        if not (folder / "identity.md").exists():
            _write_stage_persona(folder)
        kwargs = dict(bot_token="x", authorized_user_id=1,
                     data_dir=self.root / "data", persona_path=folder,
                     memory_enabled=False, streaming_enabled=False,
                     stages_enabled=True, album_enabled=True)
        kwargs.update(overrides)
        return Config(**kwargs)


# --- /stage: the view itself (current stage, history, edge buttons) -------

class TestStageView(_StageAlbumUITestCase):
    def test_stage_cmd_shows_current_and_history(self):
        cfg = self._cfg()
        stages.advance(cfg.stage_path, STAGE_NAMES, "our first trip", NOW)
        app = bot.make_app(cfg)
        stage_cmd = _command_handler(app, "stage")
        message = FakeMessage("/stage")

        asyncio.run(stage_cmd(FakeUpdate(message), FakeContext()))

        reply = message.replies[0]
        history_line = f'{NOW.date().isoformat()} · In rhythm · "our first trip"'
        self.assertEqual(
            reply.text,
            messages.msg("stage_intro").format(stage="In rhythm") + "\n" + history_line)
        # After one advance (index 0 -> 1 of 3), neither edge applies --
        # both advance and retreat show; edge-hiding itself is
        # test_stage_buttons_hidden_at_edges's job, not this test's.
        labels = _button_labels(reply.markup)
        self.assertIn(messages.msg("btn_stage_advance"), labels)
        self.assertIn(messages.msg("btn_stage_retreat"), labels)
        self.assertIn(messages.msg("btn_stage_close"), labels)

    def test_stage_buttons_hidden_at_edges(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        stage_cmd = _command_handler(app, "stage")

        bottom = FakeMessage("/stage")
        asyncio.run(stage_cmd(FakeUpdate(bottom), FakeContext()))
        bottom_labels = _button_labels(bottom.replies[0].markup)
        self.assertIn(messages.msg("btn_stage_advance"), bottom_labels)
        self.assertNotIn(messages.msg("btn_stage_retreat"), bottom_labels)
        self.assertIn(messages.msg("btn_stage_close"), bottom_labels)

        for _ in range(len(STAGE_NAMES) - 1):
            stages.advance(cfg.stage_path, STAGE_NAMES, "", NOW)
        top = FakeMessage("/stage")
        asyncio.run(stage_cmd(FakeUpdate(top), FakeContext()))
        top_labels = _button_labels(top.replies[0].markup)
        self.assertNotIn(messages.msg("btn_stage_advance"), top_labels)
        self.assertIn(messages.msg("btn_stage_retreat"), top_labels)
        self.assertIn(messages.msg("btn_stage_close"), top_labels)

    def test_stage_close_clears_buttons(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        asyncio.run(callback(
            FakeUpdate(stage_message, callback_query=FakeCallbackQuery("stg_close", stage_message)),
            FakeContext()))

        self.assertEqual(stage_message.edits[-1],
                         messages.msg("stage_intro").format(stage="Settling in"))
        self.assertEqual(list(stage_message.markup_edits[-1].inline_keyboard), [])

    def test_stage_cmd_absent_when_flag_off(self):
        cfg = self._cfg(stages_enabled=False)
        app = bot.make_app(cfg)
        self.assertFalse(any(isinstance(h, CommandHandler) and "stage" in h.commands
                             for h in app.handlers[0]))

        # A persona with no stages.md never registers /stage, even with the
        # flag on -- there is nothing to advance or retreat through.
        plain = self.root / "plain_persona"
        plain.mkdir(parents=True, exist_ok=True)
        (plain / "identity.md").write_text("I am Alex.\n", encoding="utf-8")
        (plain / "settings.yaml").write_text(
            "companion:\n  name: Alex\npartner:\n  name: Sam\n", encoding="utf-8")
        cfg2 = self._cfg(stages_enabled=True, persona_path=plain,
                         data_dir=self.root / "data2")
        app2 = bot.make_app(cfg2)
        self.assertFalse(any(isinstance(h, CommandHandler) and "stage" in h.commands
                             for h in app2.handlers[0]))


# --- T8c: the /stage road-so-far render is clipped at the newest N=8
#     milestones. Above that, a single count line stands in for everything
#     collapsed (all still kept on disk); the newest 8 render in order. At
#     or below 8, the view is byte-identical to before. ---------------------

def _write_history_state(cfg, n):
    """Seed cfg.stage_path with a well-shaped state carrying n history
    entries, each uniquely noted 'step 0'..'step n-1' so a specific
    milestone's presence/absence is checkable by substring. current sits at
    the top stage so resolve_index is stable and irrelevant to the road."""
    cfg.stage_path.parent.mkdir(parents=True, exist_ok=True)
    history = [{"stage": STAGE_NAMES[i % len(STAGE_NAMES)],
                "date": "2026-01-01", "note": f"step {i}"} for i in range(n)]
    cfg.stage_path.write_text(
        json.dumps({"current": STAGE_NAMES[-1], "history": history}),
        encoding="utf-8")


class TestStageRoadClip(_StageAlbumUITestCase):
    def test_eight_entries_render_in_full_without_count_line(self):
        cfg = self._cfg()
        _write_history_state(cfg, 8)
        text, _ = bot._stage_view(cfg, STAGE_NAMES)
        lines = text.split("\n")
        self.assertEqual(len(lines), 1 + 8)  # intro + all 8, no count line
        for i in range(8):
            self.assertIn(f"step {i}", text)

    def test_nine_entries_clip_to_newest_eight_with_count_line(self):
        cfg = self._cfg()
        _write_history_state(cfg, 9)
        text, _ = bot._stage_view(cfg, STAGE_NAMES)
        lines = text.split("\n")
        self.assertEqual(len(lines), 1 + 1 + 8)  # intro + count + newest 8
        self.assertEqual(lines[1], messages.msg("stage_road_clipped").format(n=1))
        self.assertNotIn("step 0", text)  # oldest collapsed
        self.assertIn("step 8", text)     # newest survives
        for i in range(1, 9):             # exactly steps 1..8 remain
            self.assertIn(f"step {i}", text)

    def test_eighty_entries_count_line_is_seventy_two(self):
        cfg = self._cfg()
        _write_history_state(cfg, 80)
        text, _ = bot._stage_view(cfg, STAGE_NAMES)
        lines = text.split("\n")
        self.assertEqual(len(lines), 1 + 1 + 8)
        self.assertEqual(lines[1], messages.msg("stage_road_clipped").format(n=72))
        self.assertNotIn("step 71", text)  # last collapsed milestone
        self.assertIn("step 72", text)     # first surviving milestone
        self.assertIn("step 79", text)     # newest


# --- /stage: the advance flow (note prompt -> typed note / skip / cancel) -

class TestStageAdvanceFlow(_StageAlbumUITestCase):
    def test_advance_flow_with_note(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        asyncio.run(callback(
            FakeUpdate(stage_message, callback_query=FakeCallbackQuery("stg_adv", stage_message)),
            FakeContext()))
        self.assertEqual(stage_message.edits[-1], messages.msg("stage_note_prompt"))
        self.assertEqual(_button_labels(stage_message.markup_edits[-1]),
                         [messages.msg("btn_note_skip"), messages.msg("btn_note_cancel")])

        note_message = FakeMessage("what a lovely evening")
        with mock.patch.object(engine, "run_once") as run_once:
            asyncio.run(on_text(FakeUpdate(note_message), FakeContext()))
        run_once.assert_not_called()  # a note is not an engine turn

        self.assertEqual(note_message.replies[0].text, messages.msg("note_saved_ack"))
        self.assertEqual(note_message.replies[1].text,
                         messages.msg("stage_advanced_ack").format(stage="In rhythm"))
        state = stages.load_state(cfg.stage_path)
        self.assertEqual(state["current"], "In rhythm")
        self.assertEqual(state["history"][-1]["note"], "what a lovely evening")

    def test_advance_flow_skip(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        asyncio.run(callback(
            FakeUpdate(stage_message, callback_query=FakeCallbackQuery("stg_adv", stage_message)),
            FakeContext()))
        asyncio.run(callback(
            FakeUpdate(stage_message,
                      callback_query=FakeCallbackQuery("stg_note_skip", stage_message)),
            FakeContext()))

        self.assertEqual(stage_message.edits[-1],
                         messages.msg("stage_advanced_ack").format(stage="In rhythm"))
        state = stages.load_state(cfg.stage_path)
        self.assertEqual(state["current"], "In rhythm")
        self.assertEqual(state["history"][-1]["note"], "")

        # Pending must be cleared -- a later plain message reaches the engine.
        plain = FakeMessage("just chatting")
        with mock.patch.object(engine, "run_once",
                               return_value=EngineReply("hi", "sess", ok=True)):
            asyncio.run(on_text(FakeUpdate(plain), FakeContext()))
        self.assertEqual(plain.replies[0].text, "hi")

    def test_advance_cancel_leaves_state(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        asyncio.run(callback(
            FakeUpdate(stage_message, callback_query=FakeCallbackQuery("stg_adv", stage_message)),
            FakeContext()))
        asyncio.run(callback(
            FakeUpdate(stage_message,
                      callback_query=FakeCallbackQuery("stg_note_cancel", stage_message)),
            FakeContext()))

        state = stages.load_state(cfg.stage_path)
        self.assertIsNone(state["current"])
        self.assertEqual(state["history"], [])
        self.assertEqual(stage_message.edits[-1],
                         messages.msg("stage_intro").format(stage="Settling in"))

        plain = FakeMessage("hey there")
        with mock.patch.object(engine, "run_once",
                               return_value=EngineReply("hey!", "sess", ok=True)):
            asyncio.run(on_text(FakeUpdate(plain), FakeContext()))
        self.assertEqual(plain.replies[0].text, "hey!")

    def test_note_timeout_message_goes_to_engine(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        clock = {"t": 1000.0}
        with mock.patch("time.monotonic", side_effect=lambda: clock["t"]):
            asyncio.run(callback(
                FakeUpdate(stage_message,
                          callback_query=FakeCallbackQuery("stg_adv", stage_message)),
                FakeContext()))
            clock["t"] += bot.NOTE_TIMEOUT_S + 1
            late = FakeMessage("sorry, got busy")
            with mock.patch.object(engine, "run_once",
                                   return_value=EngineReply("no worries", "sess", ok=True)):
                asyncio.run(on_text(FakeUpdate(late), FakeContext()))

        self.assertEqual(late.replies[0].text, "no worries")
        # The stale "note" never landed -- state is untouched.
        self.assertIsNone(stages.load_state(cfg.stage_path)["current"])

    def test_stale_advance_at_top_stage_discards_note_honestly(self):
        """Fix round 1, Minor #3: Telegram keeps buttons alive on OLD
        messages, and stg_adv arms the note prompt unconditionally -- so a
        stale advance press can arrive with the state already at the top
        stage. stages.advance() then returns None and writes nothing: the
        typed note is discarded, and the old behavior of replying "Kept,
        word for word." about it was a lie. The honest fallback chosen (per
        the review's stated options): answer with the current /stage view
        -- the same shape the cancel path restores -- which shows exactly
        where things stand and why nothing advanced; the pending slot is
        cleared either way."""
        cfg = self._cfg()
        for _ in range(len(STAGE_NAMES) - 1):
            stages.advance(cfg.stage_path, STAGE_NAMES, "", NOW)  # to the top
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        callback = _stage_album_handler(app)
        stale_stage_message = FakeMessage("old stage view")

        # Arms the prompt even though the state is already at the top.
        asyncio.run(callback(
            FakeUpdate(stale_stage_message,
                      callback_query=FakeCallbackQuery("stg_adv", stale_stage_message)),
            FakeContext()))

        note_message = FakeMessage("a note with nowhere to go")
        with mock.patch.object(engine, "run_once") as run_once:
            asyncio.run(on_text(FakeUpdate(note_message), FakeContext()))
        run_once.assert_not_called()  # still intercepted, never an engine turn

        reply_texts = [r.text for r in note_message.replies]
        self.assertNotIn(messages.msg("note_saved_ack"), reply_texts)
        date = NOW.date().isoformat()
        expected_view = (messages.msg("stage_intro").format(stage="Deep water")
                         + f"\n{date} · In rhythm"
                         + f"\n{date} · Deep water")
        self.assertEqual(reply_texts, [expected_view])
        state = stages.load_state(cfg.stage_path)
        self.assertEqual(state["current"], "Deep water")
        self.assertEqual(len(state["history"]), 2)  # nothing new written

        # Pending was cleared: the next message is ordinary chat again.
        plain = FakeMessage("anyway, how was your day?")
        with mock.patch.object(engine, "run_once",
                               return_value=EngineReply("lovely", "sess", ok=True)):
            asyncio.run(on_text(FakeUpdate(plain), FakeContext()))
        self.assertEqual(plain.replies[0].text, "lovely")


# --- /stage: the retreat flow (confirm -> yes / no) ------------------------

class TestStageRetreatFlow(_StageAlbumUITestCase):
    def test_retreat_requires_confirm(self):
        cfg = self._cfg()
        stages.advance(cfg.stage_path, STAGE_NAMES, "", NOW)
        app = bot.make_app(cfg)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        asyncio.run(callback(
            FakeUpdate(stage_message, callback_query=FakeCallbackQuery("stg_ret", stage_message)),
            FakeContext()))
        self.assertEqual(stage_message.edits[-1],
                         messages.msg("stage_retreat_confirm").format(stage="Settling in"))
        self.assertEqual(_button_labels(stage_message.markup_edits[-1]),
                         [messages.msg("btn_retreat_yes"), messages.msg("btn_retreat_no")])
        # No mutation until "yes" is actually pressed.
        self.assertEqual(stages.load_state(cfg.stage_path)["current"], "In rhythm")

        asyncio.run(callback(
            FakeUpdate(stage_message,
                      callback_query=FakeCallbackQuery("stg_ret_yes", stage_message)),
            FakeContext()))
        self.assertEqual(stage_message.edits[-1],
                         messages.msg("stage_retreated_ack").format(stage="Settling in"))
        self.assertEqual(stages.load_state(cfg.stage_path)["current"], "Settling in")

    def test_retreat_no_restores_stage_view(self):
        cfg = self._cfg(data_dir=self.root / "data-no")
        stages.advance(cfg.stage_path, STAGE_NAMES, "", NOW)
        app = bot.make_app(cfg)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        asyncio.run(callback(
            FakeUpdate(stage_message, callback_query=FakeCallbackQuery("stg_ret", stage_message)),
            FakeContext()))
        asyncio.run(callback(
            FakeUpdate(stage_message,
                      callback_query=FakeCallbackQuery("stg_ret_no", stage_message)),
            FakeContext()))

        # The one prior advance() call already wrote a (note-less) history
        # entry, so the restored view carries it too -- this is the SAME
        # rendering test_stage_cmd_shows_current_and_history already pins
        # for a note-bearing entry, just without the quoted note.
        history_line = f'{NOW.date().isoformat()} · In rhythm'
        self.assertEqual(
            stage_message.edits[-1],
            messages.msg("stage_intro").format(stage="In rhythm") + "\n" + history_line)
        self.assertEqual(stages.load_state(cfg.stage_path)["current"], "In rhythm")


# --- busy interplay: stg_adv/stg_ret are gated, a pending note is not -----

class TestStageBusyGating(_StageAlbumUITestCase):
    def test_busy_blocks_stage_mutations_with_toast(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        entered = threading.Event()
        release = threading.Event()

        class BlockingEngine:
            def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
                entered.set()
                release.wait(5)
                return EngineReply("a real reply", "sess-busy", ok=True)

        busy_trigger = FakeMessage("are you there?")

        async def scenario():
            with mock.patch.object(engine, "run_once", BlockingEngine().run_once):
                turn_task = asyncio.create_task(on_text(FakeUpdate(busy_trigger), FakeContext()))
                await asyncio.to_thread(entered.wait, 5)

                query_adv = FakeCallbackQuery("stg_adv", stage_message)
                await callback(FakeUpdate(stage_message, callback_query=query_adv), FakeContext())
                self.assertEqual(query_adv.answers, [messages.msg("busy")])
                self.assertEqual(stage_message.edits, [])  # no state mutation

                query_ret = FakeCallbackQuery("stg_ret", stage_message)
                await callback(FakeUpdate(stage_message, callback_query=query_ret), FakeContext())
                self.assertEqual(query_ret.answers, [messages.msg("busy")])
                self.assertEqual(stage_message.edits, [])

                release.set()
                await turn_task

        asyncio.run(scenario())
        self.assertEqual(busy_trigger.replies[0].text, "a real reply")
        # Neither busy-blocked press armed a pending note or mutated state.
        self.assertIsNone(stages.load_state(cfg.stage_path)["current"])

    def test_busy_blocks_retreat_confirm_yes_with_toast(self):
        """Fix round 1, Important #1 (reviewer-reproduced breach): the
        retreat CONFIRM is an already-armed mutation with no pending slot
        to absorb an interleaving text -- so unlike the note flow, this
        interleaving IS reachable through the real handler surface:
        stg_ret pressed while idle opens the confirm; a plain text message
        then starts a turn (busy=True); stg_ret_yes pressed mid-turn used
        to sail straight through and persist the retreat under the running
        prompt. It must be refused with the busy toast, no state change,
        the confirm message left exactly as it was (so the yes stays live
        for after the reply lands, mirroring btn_warm/btn_clean)."""
        cfg = self._cfg()
        stages.advance(cfg.stage_path, STAGE_NAMES, "", NOW)  # retreat exists
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        # Arm the confirm while idle -- legitimately past the stg_ret gate.
        asyncio.run(callback(
            FakeUpdate(stage_message, callback_query=FakeCallbackQuery("stg_ret", stage_message)),
            FakeContext()))
        self.assertEqual(
            stage_message.edits,
            [messages.msg("stage_retreat_confirm").format(stage="Settling in")])

        entered = threading.Event()
        release = threading.Event()

        class BlockingEngine:
            def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
                entered.set()
                release.wait(5)
                return EngineReply("a real reply", "sess-busy", ok=True)

        busy_trigger = FakeMessage("oh wait, one more thing")

        async def scenario():
            with mock.patch.object(engine, "run_once", BlockingEngine().run_once):
                turn_task = asyncio.create_task(on_text(FakeUpdate(busy_trigger), FakeContext()))
                await asyncio.to_thread(entered.wait, 5)

                query_yes = FakeCallbackQuery("stg_ret_yes", stage_message)
                await callback(FakeUpdate(stage_message, callback_query=query_yes),
                               FakeContext())
                self.assertEqual(query_yes.answers, [messages.msg("busy")])

                release.set()
                await turn_task

        asyncio.run(scenario())
        # No mutation happened, and the confirm message was left as-is
        # (still exactly the one edit from arming it).
        self.assertEqual(stages.load_state(cfg.stage_path)["current"], "In rhythm")
        self.assertEqual(len(stage_message.edits), 1)
        self.assertEqual(busy_trigger.replies[0].text, "a real reply")

    def test_busy_blocks_note_skip_with_toast(self):
        """Fix round 1, Important #1 symmetry: stg_note_skip also mutates
        stage state (advance with an empty note), so it sits behind the
        same busy gate as stg_ret_yes. The armed-pending version of this
        interleaving is not reachable today (see
        test_pending_note_check_precedes_busy_gate_in_on_text below for
        why pending and busy cannot currently coexist), so this gate is
        defense in depth against the same future-writer scenario that test
        pins -- but the gate itself IS directly observable: while busy the
        press must answer with the toast and touch nothing, instead of
        falling through into the skip branch at all."""
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_text = _handler(app, MessageHandler)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stage view")

        entered = threading.Event()
        release = threading.Event()

        class BlockingEngine:
            def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
                entered.set()
                release.wait(5)
                return EngineReply("a real reply", "sess-busy", ok=True)

        busy_trigger = FakeMessage("are you there?")

        async def scenario():
            with mock.patch.object(engine, "run_once", BlockingEngine().run_once):
                turn_task = asyncio.create_task(on_text(FakeUpdate(busy_trigger), FakeContext()))
                await asyncio.to_thread(entered.wait, 5)

                query_skip = FakeCallbackQuery("stg_note_skip", stage_message)
                await callback(FakeUpdate(stage_message, callback_query=query_skip),
                               FakeContext())
                self.assertEqual(query_skip.answers, [messages.msg("busy")])
                self.assertEqual(stage_message.edits, [])

                release.set()
                await turn_task

        asyncio.run(scenario())
        self.assertIsNone(stages.load_state(cfg.stage_path)["current"])

    def test_pending_note_check_precedes_busy_gate_in_on_text(self):
        """Per the brief: a pending note never reaches the engine, and is
        orthogonal to busy -- it must never be blocked by the busy gate. A
        literal end-to-end runtime replay of "note arrives while busy=True"
        turns out to be UNREACHABLE through
        the actual handler surface, and this is worth spelling out rather
        than faking: busy["active"] is written to True in exactly one place
        in the whole module -- on_text's own normal-chat branch -- which sits
        AFTER the pending-note check in source order. So the only door into
        busy=True is a call to on_text that the pending-note check did NOT
        intercept, which by definition means pending_note was already
        inactive at that moment; and the only door that ARMS pending_note
        (stg_adv, in on_stage_album_button) is itself refused while busy is
        already True (test_busy_blocks_stage_mutations_with_toast above). The
        two states can therefore never actually coexist -- an early draft of
        this test tried to force it by starting a busy turn while a note was
        already pending, and the busy-triggering message itself got consumed
        as the note instead (proving the same point the hard way).

        NOTE (fix round 1): this unreachability argument is specific to the
        pending-NOTE slot, whose on_text interception absorbs any text that
        could otherwise start a busy turn while it is armed. It does NOT
        transfer to the retreat confirm, which has no such slot -- the
        reviewer reproduced exactly that interleaving, fixed above in
        test_busy_blocks_retreat_confirm_yes_with_toast.

        What the brief's claim actually reduces to, then, is a SOURCE-ORDER
        guarantee: on_text must check pending_note before it ever looks at
        busy, so that IF the two states were ever reachable together (e.g. a
        future change adds another writer of busy["active"]), the note would
        still win. Pinned by source order, the same way
        test_bot_persona_wiring.py's TestPlaceholderSourcePin pins a
        source-level fact no behavioral test on the same return value could
        distinguish.
        """
        source = inspect.getsource(bot)
        on_text_start = source.index("async def on_text(")
        pending_check = source.index('pending_note["active"]', on_text_start)
        busy_check = source.index('if busy["active"]:', on_text_start)
        self.assertLess(pending_check, busy_check)


# --- M4 final review: stale-button guard for disabled features. Telegram
#     keeps an inline keyboard alive forever once sent, and the combined
#     stg_/alb_ handler is registered whenever EITHER flag is on
#     (`stage_active or cfg.album_enabled` in make_app) -- so a press on a
#     stg_ button surviving from before STAGES_ENABLED was flipped off (or
#     an alb_ button from before ALBUM_ENABLED was) must not fall through
#     into its branch just because the OTHER feature kept the handler
#     registered. Reviewer-reproduced: a stale stg_ret_yes still retreated
#     and persisted state with stages disabled; a stale alb_del still
#     deleted a keepsake with the album disabled (violating its
#     never-lost doctrine). The guard sits BEFORE the busy gate, right
#     after `data = query.data`. -----------------------------------------

class TestStaleButtonGuardForDisabledFeatures(_StageAlbumUITestCase):
    def test_stale_stage_retreat_yes_is_refused_when_stages_disabled(self):
        # The combined handler is registered here because album is ON --
        # stages being off must still refuse a stg_ press on its own terms.
        cfg = self._cfg(stages_enabled=False, album_enabled=True)
        stages.advance(cfg.stage_path, STAGE_NAMES, "", NOW)  # seed: In rhythm
        app = bot.make_app(cfg)
        callback = _stage_album_handler(app)
        stage_message = FakeMessage("stale stage view")

        query = FakeCallbackQuery("stg_ret_yes", stage_message)
        asyncio.run(callback(
            FakeUpdate(stage_message, callback_query=query), FakeContext()))

        self.assertEqual(query.answers, [None])
        self.assertEqual(stage_message.edits, [])
        self.assertEqual(stages.load_state(cfg.stage_path)["current"], "In rhythm")

    def test_stale_album_delete_is_refused_when_album_disabled(self):
        # Seed the keepsake through a cfg with the album ON (add_partner_flag
        # itself gates on cfg.album_enabled), then rebuild with it OFF --
        # same data_dir, so the same album.json -- to drive the stale press.
        seed_cfg = self._cfg(album_enabled=True)
        album.add_partner_flag(seed_cfg, "a kept moment", 9200, NOW)
        entry_id = album.all_entries(seed_cfg)[0]["id"]

        # The combined handler is registered here because stages are ON --
        # the album being off must still refuse an alb_ press on its own
        # terms.
        cfg = self._cfg(album_enabled=False, stages_enabled=True)
        app = bot.make_app(cfg)
        callback = _stage_album_handler(app)
        album_message = FakeMessage("stale album view")

        query = FakeCallbackQuery(f"alb_del:{entry_id}:0", album_message)
        asyncio.run(callback(
            FakeUpdate(album_message, callback_query=query), FakeContext()))

        self.assertEqual(query.answers, [None])
        self.assertEqual(album_message.edits, [])
        self.assertEqual(len(album.all_entries(cfg)), 1)


# --- The M4 T8 CallbackQueryHandler must not steal on_button's callbacks --

class TestOnButtonUnaffectedByStageAlbumHandler(_StageAlbumUITestCase):
    """on_button (M1.5, untouched) is registered SECOND once /stage or
    /album is active (see make_app's handler-registration comment); these
    pin that the ordering trick actually works both ways: the new handler's
    own pattern rejects btn_-shaped data (so it falls through), and
    on_button, reached second, still answers and acts on it normally."""

    def test_stage_album_handler_pattern_rejects_btn_prefixed_data(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        pattern = None
        for h in app.handlers[0]:
            if isinstance(h, CallbackQueryHandler) and h.pattern is not None:
                pattern = h.pattern
                break
        self.assertIsNotNone(pattern, "no stage/album CallbackQueryHandler registered")
        self.assertIsNone(pattern.match("btn_clean"))
        self.assertIsNotNone(pattern.match("stg_adv"))
        self.assertIsNotNone(pattern.match("alb_del:keep_1:0"))

    def test_btn_clean_still_works_with_stage_album_handler_registered_first(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        on_button = _on_button_handler(app)
        message = FakeMessage("start view")
        query = FakeCallbackQuery("btn_clean", message)

        asyncio.run(on_button(FakeUpdate(message, callback_query=query), FakeContext()))

        self.assertEqual(query.answers, [None])
        self.assertEqual(message.edits[-1], messages.msg("clean_ack"))


# --- Dispatch order (fix round 1, Important #2): the two tests above are
#     order-INDEPENDENT -- swapping the two add_handler calls keeps them
#     green while every stg_/alb_ button silently dies (on_button, matching
#     unconditionally and now first, would claim every callback update and
#     log "unknown button callback"). These pin the order itself. ----------

class TestCallbackDispatchOrder(_StageAlbumUITestCase):
    """Which handler CLAIMS a given callback update, replayed with PTB's own
    dispatch rule. process_update itself cannot be driven here -- it hard-
    requires app.initialize(), which performs a live bot.get_me() network
    call -- so _first_claimer replays the exact per-group algorithm read
    from PTB 22.6's Application.process_update source: walk the group's
    handlers in registration order, call each handler.check_update(update)
    (a REAL telegram.Update carrying a REAL CallbackQuery, so every
    handler's own isinstance/pattern logic runs for real), and the first
    truthy result wins ("Only a max of 1 handler per group is handled").
    RED-provability was demonstrated by temporarily swapping the two
    add_handler calls in make_app: the stg_/alb_ test and the index test
    below both go RED (on_button claims everything), while the two
    order-independent tests in the class above stay green -- exactly the
    blind spot the reviewer named."""

    def _first_claimer(self, app, data):
        query = TgCallbackQuery(
            id="q1", from_user=TgUser(id=1, first_name="u", is_bot=False),
            chat_instance="ci", data=data)
        update = TgUpdate(update_id=1, callback_query=query)
        for handler in app.handlers[0]:
            check = handler.check_update(update)
            if check is not None and check is not False:
                return handler
        return None

    def test_stg_and_alb_callbacks_dispatch_to_stage_album_handler(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        for data in ("stg_adv", "stg_ret_yes", "stg_close",
                     "alb_page:1", "alb_del:keep_x:0"):
            claimer = self._first_claimer(app, data)
            self.assertIsNotNone(claimer, f"no handler claimed {data!r}")
            self.assertIsNotNone(
                claimer.pattern,
                f"{data!r} was claimed by the bare on_button handler -- "
                "the patterned stage/album handler must be registered first")
            self.assertIs(claimer.callback, _stage_album_handler(app))

    def test_btn_callbacks_still_dispatch_to_on_button(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        for data in ("btn_clean", "btn_warm", "btn_resume", "btn_cancel"):
            claimer = self._first_claimer(app, data)
            self.assertIsNotNone(claimer, f"no handler claimed {data!r}")
            self.assertIsNone(claimer.pattern)
            self.assertIs(claimer.callback, _on_button_handler(app))

    def test_patterned_handler_registered_before_bare_on_button(self):
        # Belt to the semantic braces above: in group 0's registration
        # order, the patterned CallbackQueryHandler's index is strictly
        # less than the bare one's.
        cfg = self._cfg()
        app = bot.make_app(cfg)
        cq_handlers = [(i, h.pattern is not None)
                       for i, h in enumerate(app.handlers[0])
                       if isinstance(h, CallbackQueryHandler)]
        self.assertEqual(len(cq_handlers), 2)
        (first_idx, first_is_patterned), (second_idx, second_is_patterned) = cq_handlers
        self.assertLess(first_idx, second_idx)
        self.assertTrue(first_is_patterned)
        self.assertFalse(second_is_patterned)


# --- /album: paginated listing + delete + empty state ----------------------

class TestAlbumCommandUI(_StageAlbumUITestCase):
    def test_album_cmd_empty_line(self):
        cfg = self._cfg()
        app = bot.make_app(cfg)
        album_cmd = _command_handler(app, "album")
        message = FakeMessage("/album")

        asyncio.run(album_cmd(FakeUpdate(message), FakeContext()))

        reply = message.replies[0]
        self.assertEqual(reply.text, messages.msg("album_empty"))
        self.assertEqual(list(reply.markup.inline_keyboard), [])

    def test_album_cmd_lists_pages_and_deletes(self):
        cfg = self._cfg()
        for i in range(7):
            album.add_partner_flag(cfg, f"moment number {i}", 9000 + i,
                                   NOW + timedelta(minutes=i))
        app = bot.make_app(cfg)
        album_cmd = _command_handler(app, "album")
        callback = _stage_album_handler(app)

        page0 = FakeMessage("/album")
        asyncio.run(album_cmd(FakeUpdate(page0), FakeContext()))
        reply = page0.replies[0]
        self.assertEqual(reply.text, messages.msg("cmd_album_desc"))
        rows = reply.markup.inline_keyboard
        self.assertEqual(len(rows), 6)  # 5 entries + one nav row
        self.assertTrue(rows[0][0].text.endswith("moment number 6"))  # newest first
        self.assertTrue(rows[4][0].text.endswith("moment number 2"))
        nav_row = rows[-1]
        self.assertEqual([b.text for b in nav_row], ["▶"])  # only "next" on page 0

        next_cb = nav_row[0].callback_data
        asyncio.run(callback(
            FakeUpdate(reply, callback_query=FakeCallbackQuery(next_cb, reply)), FakeContext()))
        self.assertEqual(reply.edits[-1], messages.msg("cmd_album_desc"))
        page1_rows = reply.markup_edits[-1].inline_keyboard
        self.assertEqual(len(page1_rows), 3)  # 2 entries + one nav row
        self.assertTrue(page1_rows[0][0].text.endswith("moment number 1"))
        self.assertTrue(page1_rows[1][0].text.endswith("moment number 0"))
        self.assertEqual([b.text for b in page1_rows[-1]], ["◀"])  # only "prev"

        del_cb = page1_rows[0][0].callback_data
        self.assertTrue(del_cb.startswith("alb_del:"))
        asyncio.run(callback(
            FakeUpdate(reply, callback_query=FakeCallbackQuery(del_cb, reply)), FakeContext()))
        self.assertEqual(len(album.all_entries(cfg)), 6)
        # Re-rendered the SAME page (now shrunk to one entry) rather than
        # going empty or crashing on the now-out-of-range slot.
        page1_after = reply.markup_edits[-1].inline_keyboard
        self.assertEqual(len(page1_after), 2)  # 1 entry + nav row
        self.assertTrue(page1_after[0][0].text.endswith("moment number 0"))

    def test_malformed_album_callback_data_is_refused_quietly(self):
        """Fix round 1, Minor #4: callback data is client-supplied bytes,
        not a trusted surface -- a crafted or truncated alb_ payload
        ("alb_page:abc", a bare "alb_del:", a two-segment "alb_del:x") used
        to escape the handler as an uncaught ValueError out of int()/tuple
        unpacking. It must be refused quietly: a logged warning, no edit,
        no crash -- and validation runs BEFORE any album mutation, so a
        garbled page number can never half-apply a delete."""
        cfg = self._cfg()
        album.add_partner_flag(cfg, "a kept moment", 9100, NOW)
        app = bot.make_app(cfg)
        callback = _stage_album_handler(app)

        for bad in ("alb_page:abc", "alb_del:", "alb_del:only_id_no_page",
                    "alb_del:keep_x:not_a_page"):
            message = FakeMessage("album view")
            query = FakeCallbackQuery(bad, message)
            with self.assertLogs("everthine", level="WARNING"):
                asyncio.run(callback(
                    FakeUpdate(message, callback_query=query), FakeContext()))
            self.assertEqual(message.edits, [], f"{bad!r} produced an edit")
            self.assertEqual(query.answers, [None])

        # Nothing was deleted along the way, including by the alb_del
        # variants whose page segment was garbage.
        self.assertEqual(len(album.all_entries(cfg)), 1)


# --- T7 review fold-in: _cache_sent gated on cfg.album_enabled ------------

class TestCacheSentAlbumGating(unittest.TestCase):
    """The T7 review named this a one-line fold-in for M4 T8: _cache_sent
    filled the message cache even with album_enabled=False -- pure memory
    waste, since handle_reaction (its only reader) is not even registered
    in that case (see TestAlbumFlagGating above). There is no reaction
    handler to probe this behaviorally when the flag is off (that IS the
    point of the fix), so -- exactly like test_bot_persona_wiring.py's
    TestPlaceholderSourcePin -- reading the source directly is the only
    honest way to pin that the early-exit gate actually exists."""

    def test_cache_sent_checks_album_enabled_before_caching(self):
        source = inspect.getsource(bot)
        start = source.index("def _cache_sent(")
        # The next sibling closure (start_cmd) is "async def", not "def" --
        # searched by name rather than a generic "\n    def " scan, which
        # would miss it.
        end = source.index("\n    async def start_cmd(", start + 1)
        body = source[start:end]
        self.assertIn("cfg.album_enabled", body)


# --- register_commands: menu grows with the enabled organs -----------------

class _FakeCommandBot:
    def __init__(self):
        self.set_my_commands_calls = []

    async def set_my_commands(self, commands):
        self.set_my_commands_calls.append(commands)


class _FakeCommandApp:
    """Mirrors test_bot_stream.py's/test_bot_persona_wiring.py's own
    FakeCommandApp shape exactly -- bot_data is only set when a caller
    explicitly supplies it, so a bare _FakeCommandApp() has NO bot_data
    attribute at all, matching every existing direct-call test site for
    register_commands byte-for-byte."""

    def __init__(self, bot_data=None):
        self.bot = _FakeCommandBot()
        if bot_data is not None:
            self.bot_data = bot_data


class TestCommandMenuOrganWiring(_StageAlbumUITestCase):
    def test_command_menu_matches_enabled_organs(self):
        cfg = self._cfg()  # stages+album on, persona has stages
        app = bot.make_app(cfg)
        # make_app's own side of the wiring: cfg travels to register_commands
        # through app.bot_data, since post_init is passed register_commands
        # by bare reference (test_bot_stream.py pins that identity).
        self.assertIs(app.bot_data.get("cfg"), cfg)

        fake = _FakeCommandApp(bot_data=app.bot_data)
        asyncio.run(bot.register_commands(fake))
        commands = fake.bot.set_my_commands_calls[0]
        self.assertEqual([c.command for c in commands], ["start", "stage", "album"])
        self.assertEqual(commands[1].description, messages.msg("cmd_stage_desc"))
        self.assertEqual(commands[2].description, messages.msg("cmd_album_desc"))

        cfg_off = self._cfg(stages_enabled=False, album_enabled=False,
                            data_dir=self.root / "data-off")
        app_off = bot.make_app(cfg_off)
        fake_off = _FakeCommandApp(bot_data=app_off.bot_data)
        asyncio.run(bot.register_commands(fake_off))
        commands_off = fake_off.bot.set_my_commands_calls[0]
        self.assertEqual([c.command for c in commands_off], ["start"])
        self.assertFalse(any(isinstance(h, CommandHandler) and "stage" in h.commands
                             for h in app_off.handlers[0]))
        self.assertFalse(any(isinstance(h, CommandHandler) and "album" in h.commands
                             for h in app_off.handlers[0]))

    def test_register_commands_still_start_only_with_bare_app(self):
        # No bot_data at all (the exact shape every pre-M4-T8 direct-call
        # site uses, in this file and in test_bot_stream.py/
        # test_bot_persona_wiring.py) must stay byte-identical to the
        # original start-only menu.
        fake = _FakeCommandApp()
        asyncio.run(bot.register_commands(fake))
        commands = fake.bot.set_my_commands_calls[0]
        self.assertEqual([c.command for c in commands], ["start"])


if __name__ == "__main__":
    unittest.main()
