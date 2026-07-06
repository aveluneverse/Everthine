"""Telegram application: thin async glue around a testable core.

produce_reply() is the seam that carries the whole thought pipeline:
archive the user's words -> build the warmth injection -> ask the engine
-> stamp the session -> archive the reply -> chunk for Telegram.
Handlers below it only translate Telegram updates in and out.

stream_reply() is the streaming twin of produce_reply(); both share
prepare_exchange().

The heart-reaction pipeline (M4 T7) runs in both directions on top of
that: her heart on a companion message flows into the keepsake album
(make_app's handle_reaction, resolving the message via a short-lived
_message_cache closure); his captured [react:emoji] tag -- T6 already
stripped it out of the visible reply -- sets a real Telegram reaction on
her message via _consume_react, flagging it into the album too when the
emoji is a heart.

/stage and /album (M4 T8) are the user-facing controls on top of stages.py
and album.py: /stage shows the current stage and its history, with buttons
to advance (an optional note, captured by a pending-note interception at
the top of on_text) or retreat (behind a yes/no confirm); /album is a
paginated, newest-first keepsake listing with per-entry delete. Both live
behind their own registration gates (stages_enabled + persona has stages;
album_enabled) and share a SECOND CallbackQueryHandler
(on_stage_album_button, pattern=r"^(stg|alb)_") registered ahead of
on_button in the same PTB group -- see make_app's handler-registration
comment for why that ordering, not just the pattern, is what keeps
on_button's own bare CallbackQueryHandler from swallowing these presses.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from datetime import datetime

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReactionTypeEmoji, Update)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler,
                          MessageReactionHandler, filters)

from . import (album, archive, chunking, engine, memory_recall, messages,
              persona, recent_context, stages)
from .config import Config, load_config
from .engine import EngineReply
from .messages import msg
from .session_store import SessionStore
from .streaming_display import REACT_TAG, StreamingDisplay, cancel_markup

logger = logging.getLogger("everthine")

# Heart-reaction pipeline (M4 T7): the one emoji set that carries keepsake
# semantics on both sides (her heart on his message, his heart-tagged reply
# on hers). Two glyphs because "❤️" (heart + variation selector,
# what most clients send) and the bare "❤" both read as "heart" to a
# person and Telegram treats them as distinct reaction strings.
_HEART_EMOJIS = frozenset({"❤️", "❤"})

# make_app's _message_cache: how long a companion message stays resolvable
# to its text after a heart on it (TTL), and how many entries the cache
# holds at once (cap) before the oldest survivors get pruned. Both are
# lazily enforced on insert -- see _cache_sent's docstring.
MESSAGE_CACHE_TTL_S = 24 * 3600
MESSAGE_CACHE_MAX = 500

# make_app's pending_note slot (M4 T8): how long an armed "advance" note
# prompt (stg_adv) stays fresh before a later text message is treated as
# ordinary chat again instead of the note. See on_text's interception
# (checked before the busy gate -- a note mutates no session state) and
# on_stage_album_button's stg_adv branch, which stamps "since".
NOTE_TIMEOUT_S = 300

# /album's page size and per-entry label length (M4 T8). Not named in the
# task's interface list, but pulled out as constants for the same reason
# the message-cache knobs above are: one obvious place to tune them.
ALBUM_PAGE_SIZE = 5
ALBUM_SNIPPET_CHARS = 30


def decide_start_buttons(has_session: bool) -> list:
    if has_session:
        return ["btn_resume", "btn_warm", "btn_clean"]
    return ["btn_clean"]


def prepare_exchange(cfg: Config, store: SessionStore, text: str, now) -> tuple:
    """Archive the user's turn, assemble the engine prompt, and recall
    long-term memories relevant to it."""
    if cfg.archive_enabled:
        archive.write_entry(cfg.archive_dir, "user", text, ts=now)
    data = store.load()
    block = None
    try:
        block = recent_context.build_block(cfg, data, cfg.archive_dir, now)
    except Exception:
        logger.warning("warmth injection failed; continuing without it",
                       exc_info=True)
    # Defense in depth: recall_block is already fail-soft by contract, but
    # current_settings() can raise ConfigError on a broken persona folder --
    # that already fails loudly at boot via persona.init, so mid-turn this
    # degrades quietly instead of breaking the reply.
    memory_block = None
    try:
        settings = persona.current_settings(cfg)
        memory_block = memory_recall.recall_block(cfg, text, now, settings)
    except Exception:
        logger.warning("memory recall failed; continuing without it",
                       exc_info=True)
    return recent_context.prepend(block, text), data, memory_block


def _extract_react(text: str) -> tuple[str | None, str]:
    """Strip a leading [react:emoji] tag, if present: returns (emoji, text).

    Non-streaming twin of StreamingDisplay's chunk-buffered detection -- the
    whole reply is already in hand here, so no windowing is needed, just one
    match against the same REACT_TAG (single source of truth, imported from
    streaming_display) that the stream path uses.
    """
    match = REACT_TAG.match(text)
    if not match:
        return None, text
    return match.group(1), text[match.end():]


def _reaction_emoji_set(reactions) -> set[str]:
    """The plain-emoji strings out of a sequence of telegram.ReactionType
    objects (a MessageReactionUpdated's old_reaction/new_reaction). A
    custom-emoji reaction (ReactionTypeCustomEmoji, no .emoji of its own)
    is silently excluded rather than guessed at -- this project's keepsake
    hearts are plain Unicode emoji only."""
    return {r.emoji for r in reactions if isinstance(r, ReactionTypeEmoji)}


def produce_reply(cfg: Config, store: SessionStore, text: str,
                  now: datetime | None = None, engine_mod=engine,
                  on_react=None) -> list:
    now = now or datetime.now().astimezone()
    prompt, data, memory_block = prepare_exchange(cfg, store, text, now)

    reply = engine_mod.run_once(
        cfg, prompt, session_id=data.get("session_id"),
        system_prompt=persona.build_system_prompt(cfg, memory_block=memory_block))
    if not reply.ok:
        return [msg(reply.error_kind or "generic_glitch")]

    emoji, cleaned = _extract_react(reply.text)
    # T7: on_text (which owns update.message, needed to set the Telegram
    # reaction and to flag a heart into the album) learns the captured
    # emoji through this sink -- produce_reply's own return value stays
    # exactly the plain chunk list every existing caller already depends
    # on, so this is opt-in and invisible when the caller passes nothing.
    if on_react is not None:
        on_react(emoji)

    store.stamp_session_started(reply.session_id, now)
    if cfg.archive_enabled and cleaned:
        # A tag surviving into the archive would flow into M3's memory
        # index as literal content, so the archive always sees cleaned text.
        archive.write_entry(cfg.archive_dir, "companion", cleaned, ts=now)
    # After-reply: the turn just archived becomes memory once its
    # conversation closes. Fail-soft by contract; never runs on the error
    # path above (an early return already skipped it).
    memory_recall.sync(cfg, now)

    out = chunking.split_message(cleaned)
    if store.detect_bloat(cfg, reply.session_id):
        out.append(msg("notebook_full"))
    return out or [msg("generic_glitch")]


async def stream_reply(cfg: Config, store: SessionStore, text: str,
                       display, cancel_flag: threading.Event,
                       now: datetime | None = None,
                       engine_mod=engine, sent_sink: list | None = None):
    """Streaming twin of produce_reply(): drive the engine on a worker
    thread and forward text deltas to the display. Returns the final
    EngineReply, or None when the user cancelled.

    sent_sink (T7), when given, is extended with whatever display.finalize()
    hands back -- the Message objects StreamingDisplay actually sent. A
    cancelled turn never reaches finalize() and so never populates it: a
    cancelled reply is a dead end here exactly as it already is for the
    archive write and the session stamp below, so on_text has nothing to
    cache. Callers must pair each entry positionally with
    display.message_texts, not read .text off it directly -- a real
    telegram.Message.edit_text() returns a NEW Message rather than mutating
    the one it was called on, so the placeholder object finalize() returns
    still carries whatever text it was constructed with, never the text it
    was progressively edited to show.
    """
    now = now or datetime.now().astimezone()
    # Off-loop: prepare_exchange touches disk (archive writes + memory
    # recall), so it must never run synchronously on the event loop.
    prompt, data, memory_block = await asyncio.to_thread(
        prepare_exchange, cfg, store, text, now)
    # Off-loop too: this retires the standing event-loop debt -- as the
    # archive grows, building the system prompt synchronously here would
    # block every other update.
    system_prompt = await asyncio.to_thread(
        persona.build_system_prompt, cfg, memory_block)

    events: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=engine_mod.stream_once,
        kwargs=dict(cfg=cfg, prompt=prompt, session_id=data.get("session_id"),
                    system_prompt=system_prompt,
                    events=events, cancel=cancel_flag),
        daemon=True)
    worker.start()

    reply = None
    try:
        while True:
            if cancel_flag.is_set():
                break
            try:
                event = await asyncio.to_thread(events.get, True, 0.5)
            except queue.Empty:
                continue
            if event["type"] == "text":
                await display.append(event["text"])
            elif event["type"] == "done":
                reply = event["reply"]
                break
    except BaseException:
        # A raising consumer (e.g. RetryAfter bubbling from a peel burst) or a
        # task cancellation must reap the worker: signal it to stop so it drops
        # the reply lock instead of generating for the full timeout behind the
        # next message's placeholder. CancelledError takes this path too.
        cancel_flag.set()
        raise
    finally:
        # Join on every exit path exactly once. T4 guarantees a terminal event,
        # so on the normal path the worker has already finished and this
        # returns promptly.
        await asyncio.to_thread(worker.join, 5)

    if cancel_flag.is_set():
        await display.cancel()
        return None

    if reply is None:
        reply = EngineReply("", data.get("session_id"), ok=False,
                            error_kind="nonzero")
    if not reply.ok and not display.full_text:
        await display.append(msg(reply.error_kind or "generic_glitch"))
    sent = await display.finalize()
    if sent_sink is not None:
        sent_sink.extend(sent)

    if reply.ok:
        store.stamp_session_started(reply.session_id, now)
        if cfg.archive_enabled and display.full_text:
            # display.full_text is already tag-free -- StreamingDisplay
            # strips a leading [react:emoji] before it ever reaches
            # full_text, so no second strip belongs here. A tag surviving
            # into the archive would flow into M3's memory index as literal
            # content, which is exactly what that guarantee prevents.
            archive.write_entry(cfg.archive_dir, "companion",
                                display.full_text, ts=now)
        # After-reply sync, off-loop (mirrors produce_reply; see its comment).
        await asyncio.to_thread(memory_recall.sync, cfg, now)
    return reply


async def _consume_react(cfg: Config, update: Update, emoji: str | None,
                         now: datetime) -> None:
    """His half of the reaction pipeline (T7). T6 already captured and
    stripped a leading [react:emoji] tag out of the visible reply; this
    sets it as a real Telegram reaction on her message and, for a heart,
    keeps the moment in the album. update.message is HERS -- the message
    this turn is replying to -- so her text and message_id are already in
    hand; no cache lookup belongs here (contrast handle_reaction below,
    which reacts to a message of the bot's OWN sending and has only a
    message_id to recover the text from). Both reply paths call this with
    whatever emoji they captured -- on SUCCESSFUL turns only (produce_reply's
    on_react sink for the non-streaming path, which its failure path never
    fills; display.reaction_emoji behind on_text's reply.ok gate for the
    streaming one): a reaction is a success gesture, and one landing next
    to an error notice would read as him keeping the moment his reply died
    on.

    Gated on cfg.album_enabled at the top, covering every call site at
    once: a visible reaction on her message is user-facing feature
    behavior, so flag off must discard the captured emoji exactly as T6
    left it -- byte-equivalent to the pre-feature baseline (the L1
    rollback), the same contract the handler table and album.py's own
    write gate already honor.
    """
    if not cfg.album_enabled or not emoji:
        return
    try:
        await update.message.set_reaction([ReactionTypeEmoji(emoji)])
    except Exception:
        # Telegram's accepted reaction set is neither fixed nor under this
        # project's control; a persona emitting one it no longer recognizes
        # must never take the whole reply down with it.
        logger.warning("could not set a Telegram reaction on the partner's "
                       "message", exc_info=True)
    if emoji in _HEART_EMOJIS:
        album.add_companion_flag(cfg, update.message.text,
                                 update.message.message_id, now)


def _authorized(cfg: Config, update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id == cfg.authorized_user_id


def _keyboard(keys: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(msg(k), callback_data=k)]
                                 for k in keys])


def _stage_registration_active(cfg: Config) -> bool:
    """Whether /stage should exist at all: cfg.stages_enabled AND the active
    persona actually defines a stage sequence. A persona with no stages.md
    (or a file-mode persona, which never has one) has nothing to advance or
    retreat through, so the command has no reason to exist -- mirrors
    persona.build_system_prompt()'s own "folder mode and persona_obj.stages"
    gate on building the stage prompt block.

    persona.py exposes no public "just the stages" accessor (current_settings()
    returns PersonaSettings, which carries lines/thinking but not stages);
    _cached_folder_persona(cfg) is what build_system_prompt() itself calls
    internally for exactly this, so reusing it here matches Persona's own
    load-persona-consistent access pattern verbatim rather than adding a new
    persona.py accessor (that file is off-limits for this task).
    """
    if not cfg.stages_enabled or not cfg.persona_path.is_dir():
        return False
    return bool(persona._cached_folder_persona(cfg).stages)


def _stage_names(cfg: Config) -> tuple:
    """This app's persona-defined stage sequence, or () outside folder mode
    or when the persona has no stages.md. /stage and its callbacks are only
    ever registered when _stage_registration_active(cfg) was true at
    make_app time, so in practice this is never empty on those call sites;
    the empty fallback stays anyway rather than assuming a cached persona
    can never change shape under a long-running process.
    """
    if not cfg.persona_path.is_dir():
        return ()
    stage_sections = persona._cached_folder_persona(cfg).stages
    return tuple(name for name, _ in stage_sections) if stage_sections else ()


def _stage_view(cfg: Config, names: tuple) -> tuple:
    """Render /stage's own view: the current stage plus its road so far
    (T2's stored history, one line per milestone, note appended in quotes
    when the milestone was marked with one), and a button row that only
    ever offers a direction that still exists -- no advance past the last
    stage, no retreat before the first."""
    state = stages.load_state(cfg.stage_path)
    index = stages.resolve_index(state, names)
    lines = [msg("stage_intro").format(stage=names[index])]
    for entry in state.get("history") or []:
        line = f'{entry["date"]} · {entry["stage"]}'
        if entry.get("note"):
            line += f' · "{entry["note"]}"'
        lines.append(line)
    rows = []
    if index < len(names) - 1:
        rows.append([InlineKeyboardButton(msg("btn_stage_advance"), callback_data="stg_adv")])
    if index > 0:
        rows.append([InlineKeyboardButton(msg("btn_stage_retreat"), callback_data="stg_ret")])
    rows.append([InlineKeyboardButton(msg("btn_stage_close"), callback_data="stg_close")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _stage_note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_note_skip"), callback_data="stg_note_skip")],
        [InlineKeyboardButton(msg("btn_note_cancel"), callback_data="stg_note_cancel")],
    ])


def _retreat_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_retreat_yes"), callback_data="stg_ret_yes")],
        [InlineKeyboardButton(msg("btn_retreat_no"), callback_data="stg_ret_no")],
    ])


def _album_page(cfg: Config, page: int) -> tuple:
    """Newest-first entries for one /album page, clamped into range.
    Returns (page_entries, resolved_page, total_pages); total_pages is 0
    (and page_entries always []) exactly when the whole album is empty."""
    entries = list(reversed(album.all_entries(cfg)))
    total_pages = (len(entries) + ALBUM_PAGE_SIZE - 1) // ALBUM_PAGE_SIZE
    if total_pages == 0:
        return [], 0, 0
    page = max(0, min(page, total_pages - 1))
    start = page * ALBUM_PAGE_SIZE
    return entries[start:start + ALBUM_PAGE_SIZE], page, total_pages


def _album_view(cfg: Config, page: int) -> tuple:
    """Render one /album page: msg("album_empty") with no buttons when
    nothing has been kept; otherwise the catalog's own album description as
    a header, one delete button per kept moment (newest first), and a
    </> nav row whenever there is a previous/next page to reach. Always
    returns a real (possibly empty) InlineKeyboardMarkup, never None --
    editing with reply_markup=None would leave Telegram's PREVIOUS markup
    in place rather than clearing it, which an empty InlineKeyboardMarkup
    does unambiguously.
    """
    page_entries, page, total_pages = _album_page(cfg, page)
    if not page_entries:
        return msg("album_empty"), InlineKeyboardMarkup([])
    rows = []
    for entry in page_entries:
        date_label = datetime.fromisoformat(entry["timestamp"]).strftime("%m-%d")
        snippet = entry["message"]["text"][:ALBUM_SNIPPET_CHARS]
        rows.append([InlineKeyboardButton(
            f"[{date_label}] {snippet}",
            callback_data=f"alb_del:{entry['id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"alb_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"alb_page:{page + 1}"))
    if nav:
        rows.append(nav)
    return msg("cmd_album_desc"), InlineKeyboardMarkup(rows)


async def register_commands(app) -> None:
    """Publish the command menu Telegram shows behind the Menu button.

    The menu is cosmetic, so failures are logged and never fatal: PTB awaits
    post_init outside its network retry loop, and an escaping exception here
    would crash the whole bot at startup.

    Always publishes start; /stage and /album (M4 T8) join it only when
    cfg says they should exist at all -- the same _stage_registration_active
    condition their CommandHandler was registered under, and cfg.album_enabled
    for /album. cfg travels here through app.bot_data["cfg"] rather than a
    parameter: this function is handed to ApplicationBuilder().post_init
    verbatim (make_app passes the bare function, never a cfg-bound wrapper),
    because a test (test_bot_stream.py's test_post_init_registers_command_menu)
    pins that the object handed to .post_init(...) IS this exact function
    object. bot_data is PTB's own per-Application storage, sanctioned for
    exactly this kind of side channel. Every existing direct-call site
    (this project's own tests included) hands this a bare app with no
    bot_data attribute at all, which degrades to the original start-only
    menu, byte-identical to before M4 T8.
    """
    bot_data = getattr(app, "bot_data", None)
    cfg = bot_data.get("cfg") if isinstance(bot_data, dict) else None
    commands = [BotCommand("start", msg("cmd_start_desc"))]
    if cfg is not None:
        if _stage_registration_active(cfg):
            commands.append(BotCommand("stage", msg("cmd_stage_desc")))
        if cfg.album_enabled:
            commands.append(BotCommand("album", msg("cmd_album_desc")))
    try:
        await app.bot.set_my_commands(commands)
    except Exception:
        logger.warning("could not publish the command menu; continuing without it",
                       exc_info=True)


def make_app(cfg: Config):
    # Persona startup wiring: the common path for both main() (which calls
    # make_app at the bottom of this file) and tests (which build an app
    # directly, without main()). persona.init(cfg) loads+caches a folder
    # persona fail-loud, so a broken persona folder raises ConfigError right
    # here at app-build time -- i.e. at boot -- instead of surfacing mid-reply
    # on the first turn. File mode: init() is a no-op (clears the cache slot)
    # and line_overrides() returns ({}, None), so load_overrides() below
    # leaves every message and the thinking placeholder at their defaults.
    persona.init(cfg)
    messages.load_overrides(*persona.line_overrides(cfg))

    # M4 T8: whether /stage exists at all this run (stages_enabled AND the
    # persona actually defines stages). Computed once here rather than
    # per-update, and reused below for both handler registration and the
    # command-menu side channel (register_commands reads it back off
    # app.bot_data["cfg"] -- see that function's own docstring for why cfg
    # travels that way instead of as a parameter).
    stage_active = _stage_registration_active(cfg)

    # Boot-time backfill: makes a pre-existing archive recallable before the
    # first turn (a fresh install, with no cursor yet, ingests everything).
    memory_recall.init(cfg)
    memory_recall.sync(cfg, datetime.now().astimezone())

    store = SessionStore(cfg.session_path)
    busy = {"active": False}
    cancel_flag = threading.Event()

    # M4 T8: the single pending-note slot armed by stg_adv and consumed by
    # on_text's interception at the top of that function (before the busy
    # gate -- a note mutates no session state, so it must work even while a
    # reply is streaming). "since" is a time.monotonic() timestamp;
    # NOTE_TIMEOUT_S is the freshness window after which the slot silently
    # expires and a stray reply falls through to ordinary chat instead.
    pending_note = {"active": False, "since": 0.0}

    # Heart-reaction pipeline (M4 T7): companion message_id -> (text,
    # monotonic send time), so a later heart on it (handle_reaction below)
    # can recover what it was without ever reading it back off a Telegram
    # Message object (see stream_reply's sent_sink docstring for why that
    # would be wrong for anything that was edited in place). Local to this
    # make_app call, like busy/cancel_flag/store above -- never shared
    # across app instances, e.g. between tests.
    _message_cache: dict[int, tuple[str, float]] = {}

    def _cache_sent(message, text: str) -> None:
        """Record one companion message actually sent this turn. Callers
        pass the text they already know they sent rather than trusting
        message.text: a real telegram.Message is an immutable snapshot, so
        the placeholder object a streamed reply progressively edits still
        reports whatever text it was first constructed with, never its
        final content, if anyone tried to read it back that way.

        Lazily pruned on every insert: entries older than
        MESSAGE_CACHE_TTL_S are dropped first, then -- if still over
        MESSAGE_CACHE_MAX -- the oldest survivors, oldest first. Insertion
        order is chronological order here: every message_id is unique and
        inserted exactly once, so the dict's own iteration order (Python
        dicts preserve insertion order) already sorts oldest to newest.

        Gated on cfg.album_enabled (M4 T8 fold-in from the T7 review): the
        cache exists solely so a later heart can resolve back to the text
        it landed on, and handle_reaction -- its only reader -- is not even
        registered when the album is off (see make_app's own handler-table
        gate further down). Filling it in that case would be pure memory
        waste with no reader ever able to benefit from it.
        """
        if not cfg.album_enabled or message is None or not text:
            return
        now_mono = time.monotonic()
        for stale_id in [mid for mid, (_, ts) in _message_cache.items()
                         if now_mono - ts > MESSAGE_CACHE_TTL_S]:
            del _message_cache[stale_id]
        _message_cache[message.message_id] = (text, now_mono)
        while len(_message_cache) > MESSAGE_CACHE_MAX:
            del _message_cache[next(iter(_message_cache))]

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(cfg, update):
            return
        has_session = bool(store.load().get("session_id"))
        text = msg("start_has_session") if has_session else msg("start_fresh")
        await update.message.reply_text(text, reply_markup=_keyboard(
            decide_start_buttons(has_session)))

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(cfg, update):
            return
        query = update.callback_query
        # A warm/clean restart pressed mid-stream would rewrite the session
        # store under the running turn; when the stream stamps the old session
        # id back on success it silently undoes the user's "new notebook"
        # request (and re-inflates warmth decay). Refuse while busy - a toast,
        # no state change, no edit - so the buttons stay live for after the
        # reply lands. btn_cancel must still fire (stopping the turn is its
        # whole job); btn_resume only edits ack text and touches no state, so
        # both stay unguarded. Only one query.answer() is allowed per callback.
        if busy["active"] and query.data in ("btn_warm", "btn_clean"):
            await query.answer(msg("busy"))
            return
        await query.answer()
        if query.data == "btn_cancel":
            cancel_flag.set()
            return
        now = datetime.now().astimezone()
        try:
            if query.data == "btn_warm":
                store.warm_restart()
                await query.edit_message_text(msg("warm_ack"))
            elif query.data == "btn_clean":
                store.clean_start(now)
                await query.edit_message_text(msg("clean_ack"))
            elif query.data == "btn_resume":
                await query.edit_message_text(msg("resume_ack"))
            else:
                logger.warning("unknown button callback: %r", query.data)
        except BadRequest:
            pass  # double-click: content unchanged, Telegram rejects the edit

    async def stage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(cfg, update):
            return
        text, markup = _stage_view(cfg, _stage_names(cfg))
        await update.message.reply_text(text, reply_markup=markup)

    async def album_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(cfg, update):
            return
        text, markup = _album_view(cfg, 0)
        await update.message.reply_text(text, reply_markup=markup)

    async def on_stage_album_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """The stg_/alb_ callback family (M4 T8): a second CallbackQueryHandler
        (pattern=r"^(stg|alb)_"), registered ahead of on_button in the same
        PTB handler group -- see make_app's handler-registration comment for
        why the ORDER of those two add_handler calls, not just their
        existence, is what keeps on_button's own bare CallbackQueryHandler
        (pattern=None, matches every callback query unconditionally) from
        also intercepting these presses: PTB dispatches at most one handler
        per group, in registration order, so whichever narrower matcher
        comes first wins and on_button never even sees a stg_/alb_ callback.
        on_button itself is completely untouched -- M1.5's own pin stays
        exactly as it was.

        One query.answer() per callback, same PTB rule on_button follows.
        busy gates every press that MUTATES stage state: the two entry
        points (stg_adv, stg_ret) AND the mutating follow-ups (stg_ret_yes,
        stg_note_skip) -- all four toast msg("busy") and change nothing,
        mirroring the M1.5 btn_warm/btn_clean precedent (a mid-turn stage
        mutation would tear the running prompt away from the state it was
        built against). The follow-ups genuinely need their own gate: an
        earlier revision gated only the entry points on the claim that a
        follow-up "can only fire once its entry point already got past the
        gate", which is wrong for the confirm flow -- the confirm can be
        opened while idle, a plain text message then starts a turn
        (busy=True), and the yes press arrives mid-turn; the retreat path
        has no pending slot to absorb that interleaving text the way the
        note flow does (review-reproduced, fix round 1). The view-only
        presses (stg_note_cancel, stg_ret_no, stg_close) stay live: they
        mutate nothing a running prompt was built from. The alb_ presses
        stay live too: the album never enters a live conversation prompt
        at all (design ruling D1), so a mid-turn album edit cannot tear
        anything.
        """
        if not _authorized(cfg, update):
            return
        query = update.callback_query
        data = query.data
        if busy["active"] and data in ("stg_adv", "stg_ret",
                                       "stg_ret_yes", "stg_note_skip"):
            await query.answer(msg("busy"))
            return
        await query.answer()
        now = datetime.now().astimezone()
        names = _stage_names(cfg)
        try:
            if data == "stg_adv":
                pending_note["active"] = True
                pending_note["since"] = time.monotonic()
                await query.edit_message_text(msg("stage_note_prompt"),
                                              reply_markup=_stage_note_keyboard())
            elif data == "stg_note_skip":
                if pending_note["active"]:
                    pending_note["active"] = False
                    new_stage = stages.advance(cfg.stage_path, names, "", now)
                    if new_stage is not None:
                        await query.edit_message_text(
                            msg("stage_advanced_ack").format(stage=new_stage))
            elif data == "stg_note_cancel":
                pending_note["active"] = False
                text, markup = _stage_view(cfg, names)
                await query.edit_message_text(text, reply_markup=markup)
            elif data == "stg_ret":
                state = stages.load_state(cfg.stage_path)
                index = stages.resolve_index(state, names)
                if index == 0:
                    # Defensive only (the retreat button is hidden at the
                    # bottom): fall back to the plain view rather than
                    # wrapping names[-1].
                    text, markup = _stage_view(cfg, names)
                    await query.edit_message_text(text, reply_markup=markup)
                else:
                    await query.edit_message_text(
                        msg("stage_retreat_confirm").format(stage=names[index - 1]),
                        reply_markup=_retreat_confirm_keyboard())
            elif data == "stg_ret_yes":
                new_stage = stages.retreat(cfg.stage_path, names, now)
                if new_stage is not None:
                    await query.edit_message_text(
                        msg("stage_retreated_ack").format(stage=new_stage))
            elif data == "stg_ret_no":
                text, markup = _stage_view(cfg, names)
                await query.edit_message_text(text, reply_markup=markup)
            elif data == "stg_close":
                text, _unused_markup = _stage_view(cfg, names)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([]))
            elif data.startswith("alb_del:"):
                # Callback data is client-supplied bytes, not a trusted
                # surface: refuse crafted/truncated payloads quietly, and
                # validate BOTH segments before the delete runs so a
                # garbled page number can never half-apply a removal.
                try:
                    _prefix, entry_id, page_str = data.split(":", 2)
                    page = int(page_str)
                except ValueError:
                    logger.warning("malformed album callback data: %r", data)
                    return
                album.remove_by_id(cfg, entry_id)
                text, markup = _album_view(cfg, page)
                await query.edit_message_text(text, reply_markup=markup)
            elif data.startswith("alb_page:"):
                try:
                    page = int(data.split(":", 1)[1])
                except ValueError:
                    logger.warning("malformed album callback data: %r", data)
                    return
                text, markup = _album_view(cfg, page)
                await query.edit_message_text(text, reply_markup=markup)
            else:
                logger.warning("unknown stage/album callback: %r", data)
        except BadRequest:
            pass  # double-click: content unchanged, Telegram rejects the edit

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        if not _authorized(cfg, update):
            return
        if pending_note["active"]:
            if time.monotonic() - pending_note["since"] <= NOTE_TIMEOUT_S:
                pending_note["active"] = False
                now = datetime.now().astimezone()
                new_stage = stages.advance(cfg.stage_path, _stage_names(cfg),
                                           update.message.text, now)
                if new_stage is not None:
                    await update.message.reply_text(msg("note_saved_ack"))
                    await update.message.reply_text(
                        msg("stage_advanced_ack").format(stage=new_stage))
                else:
                    # Already at the top stage: a stale advance press on an
                    # OLD /stage message can arm the prompt regardless of
                    # where the state has moved since (Telegram keeps old
                    # buttons alive). advance() wrote nothing and the note
                    # was discarded, so claiming it was kept would be a
                    # lie -- answer with the current view instead, which
                    # shows exactly where things stand (fix round 1).
                    text, markup = _stage_view(cfg, _stage_names(cfg))
                    await update.message.reply_text(text, reply_markup=markup)
                return
            # Stale: the slot silently expires and this message falls
            # through to ordinary chat below, exactly like any other text.
            pending_note["active"] = False
        if busy["active"]:
            await update.message.reply_text(msg("busy"))
            return
        busy["active"] = True
        cancel_flag.clear()
        try:
            if not cfg.streaming_enabled:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id, action=ChatAction.TYPING)
                # T7: produce_reply's on_react sink is how this branch learns
                # the emoji it captured -- produce_reply's return value stays
                # the plain chunk list every other caller depends on.
                react_sink: list = []
                chunks = await asyncio.to_thread(produce_reply, cfg, store,
                                                 update.message.text,
                                                 on_react=react_sink.append)
                for chunk in chunks:
                    sent = await update.message.reply_text(chunk)
                    _cache_sent(sent, chunk)
                await _consume_react(cfg, update,
                                    react_sink[0] if react_sink else None,
                                    datetime.now().astimezone())
                return

            placeholder = await update.message.reply_text(
                messages.thinking_line(), reply_markup=cancel_markup())

            async def send_new(text, parse_mode=None, reply_markup=None):
                return await update.message.reply_text(
                    text, parse_mode=parse_mode, reply_markup=reply_markup)

            display = StreamingDisplay(placeholder, send_new)
            sent_sink: list = []
            reply = await stream_reply(cfg, store, update.message.text,
                                       display, cancel_flag,
                                       sent_sink=sent_sink)
            # Zip, not read message.text: see stream_reply's sent_sink
            # docstring for why the Message objects themselves cannot be
            # trusted for this. A cancelled turn leaves sent_sink empty
            # (stream_reply never populates it from display.cancel()), so
            # this is a no-op there regardless of what display already
            # tracked internally.
            for sent, text in zip(sent_sink, display.message_texts):
                _cache_sent(sent, text)
            if reply is None:
                if not display.message_texts:
                    await update.message.reply_text(msg("cancel_ack"))
                return
            # reply.text, not display.full_text: the display may hold the
            # injected fallback apology on text-less failures; reply.text is
            # the engine's ground truth of real partial output.
            if not reply.ok and reply.text:
                await update.message.reply_text(
                    msg(reply.error_kind or "generic_glitch"))
            if reply.ok and store.detect_bloat(cfg, reply.session_id):
                await update.message.reply_text(msg("notebook_full"))
            if reply.ok:
                # Mirror of the non-streaming gate (produce_reply's failure
                # path early-returns before its on_react sink ever fires):
                # a reaction is a success gesture, so a stream that died
                # mid-reply after emitting a tag must not heart+flag her
                # message right next to the error notice above.
                await _consume_react(cfg, update, display.reaction_emoji,
                                     datetime.now().astimezone())
        except Exception:
            logger.error("reply pipeline failed unexpectedly", exc_info=True)
            try:
                await update.message.reply_text(msg("generic_glitch"))
            except Exception:
                logger.error("could not deliver the error notice", exc_info=True)
        finally:
            busy["active"] = False
            cancel_flag.clear()

    async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Her half of the reaction pipeline (T7). A heart added on a
        companion message still in _message_cache keeps it in the album,
        silently -- her gesture IS the ritual, the bot never narrates it
        (mirrors _consume_react's own silence on his side). A heart on a
        message the cache no longer holds (past its TTL, over the cap, or
        from before this process started) gets an honest album_expired
        reply instead of a silent no-op, so she always knows whether it
        landed. A heart removed un-keeps the same entry, also silently.
        Registration itself is flag-gated below (defense in depth on top
        of album.py's own cfg.album_enabled gate on every write).
        """
        reaction = update.message_reaction
        if reaction is None:
            return
        # reaction.user can be None even on the user-attributed updates the
        # registration below asks for: an anonymous reactor arrives with
        # actor_chat set instead of user. A missing user must resolve to
        # "not the authorized partner" rather than raise.
        user_id = reaction.user.id if reaction.user else 0
        if user_id != cfg.authorized_user_id:
            return
        old_emojis = _reaction_emoji_set(reaction.old_reaction)
        new_emojis = _reaction_emoji_set(reaction.new_reaction)
        added = new_emojis - old_emojis
        removed = old_emojis - new_emojis
        if added & _HEART_EMOJIS:
            cached = _message_cache.get(reaction.message_id)
            if cached is None:
                await context.bot.send_message(reaction.chat.id,
                                               msg("album_expired"))
            else:
                cached_text, _ = cached
                album.add_partner_flag(cfg, cached_text, reaction.message_id,
                                       datetime.now().astimezone())
        if removed & _HEART_EMOJIS:
            album.remove_by_message_id(cfg, reaction.message_id)

    # Flag off must reproduce M1's sequential update handling exactly: the
    # busy gate and cancel callback only make sense once updates run
    # concurrently, so scope concurrency to streaming mode. PTB maps False to
    # one-update-at-a-time, byte-identical to M1 (which never enabled it).
    app = (ApplicationBuilder().token(cfg.bot_token)
           .concurrent_updates(cfg.streaming_enabled)
           .post_init(register_commands).build())
    # register_commands is passed above by bare reference (a test pins that
    # exact identity -- see its own docstring); cfg reaches it at call time
    # through this side channel instead of a parameter.
    app.bot_data["cfg"] = cfg
    app.add_handler(CommandHandler("start", start_cmd))
    if stage_active or cfg.album_enabled:
        # M4 T8: added ahead of on_button (next line) so PTB's one-handler-
        # per-group dispatch tries this narrower pattern match FIRST --
        # on_button's own bare CallbackQueryHandler (pattern=None) matches
        # every callback query unconditionally and, registered first, would
        # otherwise swallow every stg_/alb_ press before this ever ran. See
        # on_stage_album_button's own docstring for the full reasoning.
        # This order is pinned by TestCallbackDispatchOrder, which replays
        # PTB's first-truthy-check_update-wins dispatch over real Updates.
        app.add_handler(CallbackQueryHandler(on_stage_album_button,
                                             pattern=r"^(stg|alb)_"))
    app.add_handler(CallbackQueryHandler(on_button))
    if stage_active:
        app.add_handler(CommandHandler("stage", stage_cmd))
    if cfg.album_enabled:
        app.add_handler(CommandHandler("album", album_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    if cfg.album_enabled:
        # Defense in depth with album.py's own write-time gate: flag off
        # means this handler does not even exist, matching _allowed_updates
        # never asking Telegram for reaction updates in the first place --
        # byte-identical to M3's handler table at the registration level.
        # message_reaction_types is explicit because PTB's default is
        # MESSAGE_REACTION -- BOTH user-attributed reaction updates and
        # anonymous count updates -- and this handler only understands the
        # former; asking for exactly that beats leaning on the
        # update.message_reaction None-guard inside the handler.
        app.add_handler(MessageReactionHandler(
            handle_reaction,
            message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED))
    return app


def _allowed_updates(cfg: Config) -> list[str]:
    """The update kinds run_polling() should ask Telegram for. PTB's
    long-polling default list does NOT include message reactions -- without
    naming "message_reaction" here explicitly, her hearts never arrive at
    all, silently, no matter how correctly handle_reaction itself is wired.
    A standalone function (rather than inline in main()) so this
    flag-conditioned list is directly testable without booting the whole
    application.
    """
    updates = ["message", "callback_query"]
    if cfg.album_enabled:
        updates.append("message_reaction")
    return updates


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.engine_home.mkdir(parents=True, exist_ok=True)
    if not engine.check_claude_available(cfg):
        raise SystemExit(msg("cli_missing"))
    logger.info("everthine is online")
    make_app(cfg).run_polling(allowed_updates=_allowed_updates(cfg))
