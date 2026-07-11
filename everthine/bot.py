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

The inner life hangs off two hooks. make_app's _post_init is a composite
startup hook: the command menu first, then scheduler.start_tick's background
tick -- a TICK_INTERVAL_S heartbeat, owned by scheduler.py (M7 T6 moved it
out of this module), that hands diary.write_once and portrait.update_once
each one complete attempt per round and, when scheduler_enabled, also runs
one proactive reach-out (scheduler.nudge_once -> deliver) as a third segment.
So the tick now DOES send -- but only through that proactive segment; the
diary and portrait segments still never send a Telegram message. The tick
arms for a folder-mode persona whenever ANY of diary_enabled /
portrait_enabled / scheduler_enabled is on; each organ's own INFO line at
start ("diary: inner-life tick started" / "portrait: armed (interval {n}d)"
/ "scheduler: proactive armed (...)") only prints when that organ's own flag
is actually on, so a single-organ run never claims a line that isn't true.
And both of on_text's reply paths fire reflection.reflect_once after a
SUCCESSFUL reply, fire-and-forget through _schedule_reflection. With every
flag off (diary_enabled / portrait_enabled / scheduler_enabled /
reflection_enabled), all of it is structurally absent -- no task, no tick,
no proactive reach-out, no reflection, behavior identical to M4.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
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

from . import (album, archive, chunking, diary, engine, memory_recall,
              messages, persona, recent_context, reflection, scheduler,
              stages)
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

# Telegram's Bot API accepts only ITS OWN fixed list of reaction emoji;
# anything else earns a 400 straight from the API (merge-acceptance bug,
# 2026-07-10: a model-chosen 🫂 on a get-well message died silently and
# the gesture never reached her). The set below is that list, spelled the
# way Telegram spells it -- WITHOUT the U+FE0F variation selector -- so
# incoming emoji are matched only after _normalize_reaction strips theirs.
_TG_REACTION_EMOJIS = frozenset({
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬",
    "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴",
    "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆",
    "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓",
    "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗",
    "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄",
    "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀", "😡",
})

# The nearest accepted cousin for emoji a persona plausibly reaches for
# that Telegram's reaction list doesn't carry. Deliberately small and
# same-sentiment-only: a wrong-but-accepted emoji is worse than none, so
# anything unmapped is skipped (with a named log line), never guessed at.
_REACTION_FALLBACKS = {
    "🫂": "🤗",   # people hugging -> hugging face
    "☺": "🥰",   # classic smile -> smiling with hearts
    "😊": "🥰",   # smiling eyes -> smiling with hearts
    "😅": "😁",   # relieved grin -> grin
    "💕": "❤",   # every plain love-heart variant -> the heart Telegram has
    "💖": "❤",
    "💗": "❤",
    "💓": "❤",
    "💞": "❤",
    "🩷": "❤",
}


def _normalize_reaction(emoji: str) -> str:
    """Telegram spells its accepted reactions WITHOUT the U+FE0F variation
    selector (its heart is "❤", not "❤️"), while a
    model-composed [react:...] tag usually carries one. Strip every FE0F so
    both spellings of the same emoji meet the whitelist -- and the API --
    in Telegram's own form."""
    return emoji.replace("\ufe0f", "")

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

# /stage's road-so-far render cap (M5 T8c, D5 decision N=8): how many of the
# newest milestones the view renders in full. Above this, a single count
# line stands in for everything older -- nothing is deleted, the whole
# history stays on disk, only the RENDER is clipped so a long road never
# floods one message.
STAGE_ROAD_CLIP = 8

def decide_start_buttons(has_session: bool) -> list:
    if has_session:
        return ["btn_resume", "btn_warm", "btn_clean"]
    return ["btn_clean"]


def prepare_exchange(cfg: Config, store: SessionStore, text: str, now) -> tuple:
    """Archive the user's turn, assemble the engine prompt, recall long-term
    memories relevant to it, and gather his own recent diary days.

    Returns (prompt, session_data, memory_block, inner_block). Both blocks
    are independently fail-soft: the reply proceeds without either one if it
    cannot be built, and both are None-by-default so a flag-off run is
    byte-identical to the pre-feature prompt.
    """
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
    # His own recent diary days (the Layer 3 inner-life block). Gated on the
    # diary flag so flag-off stays byte-identical to the pre-feature prompt,
    # and fail-soft the same way recall is: a broken diary read must never
    # take the reply down with it.
    inner_block = None
    if cfg.diary_enabled:
        try:
            inner_block = diary.unshared_block(cfg)
        except Exception:
            logger.warning("diary block failed; continuing without it",
                           exc_info=True)
    return recent_context.prepend(block, text), data, memory_block, inner_block


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
                  on_react=None, on_extra=None) -> list:
    """Build the non-streaming reply: the plain chunk list every caller
    already depends on, plus two opt-in side channels (on_react, on_extra).

    Trap: on_extra is the ONLY exit for the "notebook full" SYSTEM notice.
    When on_extra is None that notice is silently dropped -- never folded
    into the returned chunk list, because a system line must not ride home
    as companion speech (T7a). A future caller that wants the notice must
    pass a sink to receive it.
    """
    now = now or datetime.now().astimezone()
    prompt, data, memory_block, inner_block = prepare_exchange(cfg, store, text, now)

    reply = engine_mod.run_once(
        cfg, prompt, session_id=data.get("session_id"),
        system_prompt=persona.build_system_prompt(
            cfg, memory_block=memory_block, inner_block=inner_block))
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
    # T7a: the "notebook is full" line is a SYSTEM notice, not companion
    # speech -- it leaves through the opt-in on_extra sink (same convention
    # as on_react above) instead of riding home in the chunk list. on_text
    # sends chunks cached (so a heart on one keeps that moment) but sends
    # extras uncached: a cached system line would be resolvable to a
    # keepsake and pollute the album the instant she hearts it.
    if store.detect_bloat(cfg, reply.session_id) and on_extra is not None:
        on_extra(msg("notebook_full"))
    if out:
        return out
    if emoji is not None:
        return []          # tag-only: the reaction is the whole response
    return [msg("generic_glitch")]


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
    prompt, data, memory_block, inner_block = await asyncio.to_thread(
        prepare_exchange, cfg, store, text, now)
    # Off-loop too: this retires the standing event-loop debt -- as the
    # archive grows, building the system prompt synchronously here would
    # block every other update.
    system_prompt = await asyncio.to_thread(
        persona.build_system_prompt, cfg, memory_block=memory_block,
        inner_block=inner_block)

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
    emoji = _normalize_reaction(emoji)
    if emoji not in _TG_REACTION_EMOJIS:
        fallback = _REACTION_FALLBACKS.get(emoji)
        if fallback is None:
            # A known 400: don't spend the API call. Named, not silent --
            # the gesture was real even though Telegram can't carry it.
            logger.info("reaction %r is outside Telegram's accepted set "
                        "and has no fallback; skipped", emoji)
            return
        logger.info("reaction %r is outside Telegram's accepted set; "
                    "sending its nearest cousin %r instead", emoji, fallback)
        emoji = fallback
    try:
        await update.message.set_reaction([ReactionTypeEmoji(emoji)])
    except Exception:
        # The whitelist above tracks Telegram's accepted reaction set, but
        # that set is neither fixed nor under this project's control; a
        # stale list must never take the whole reply down with it.
        logger.warning("could not set a Telegram reaction on the partner's "
                       "message", exc_info=True)
    if emoji in _HEART_EMOJIS:
        # T7b: album writes touch disk -- off the event loop so a slow
        # write never stalls other updates.
        await asyncio.to_thread(album.add_companion_flag, cfg,
                                update.message.text,
                                update.message.message_id, now)


def _schedule_reflection(context, cfg: Config, user_msg: str,
                         reply_text: str) -> None:
    """Fire-and-forget the post-reply reflection (M5 T6): one private beat
    of thought, scheduled off the reply path and never awaited by it.

    Call sites gate on SUCCESS (produce_reply's on_react sink fired for the
    non-streaming path; reply.ok for the streaming one) -- this helper owns
    the rest: the reflection_enabled L1 gate (flag off creates NOTHING, not
    even a task; should_reflect's own "disabled" reason is the second line
    of defense), the nothing-to-reflect-on gate (an empty reply_text is a
    gesture, not language), and the guarantee that scheduling can never
    break the reply it trails -- a context without an application (this
    project's own pre-M5 handler tests drive on_text exactly that way,
    mirroring register_commands' bare-app tolerance) degrades to a no-op,
    and a create_task that itself blows up is logged and swallowed, the
    orphaned coroutine closed so it never warns at garbage collection.

    reflect_once never raises (its own contract) and yields to a live
    conversation by itself (try_run_once inside), so the task needs no
    supervision once it exists; asyncio.to_thread keeps its blocking engine
    call off the event loop. Application.create_task is the right scheduler
    HERE, in contrast to the tick (see scheduler.start_tick): by the time any
    reply succeeds the application is running, so PTB tracks the task and a
    graceful stop() waits for an in-flight reflection (bounded by the
    reflection's own engine timeout) instead of tearing the loop down under
    its engine call.
    """
    if not cfg.reflection_enabled or not reply_text:
        return
    application = getattr(context, "application", None)
    if application is None:
        return
    coro = asyncio.to_thread(reflection.reflect_once, cfg, user_msg,
                             reply_text, datetime.now().astimezone())
    try:
        application.create_task(coro)
    except Exception:
        coro.close()
        logger.warning("could not schedule the post-reply reflection",
                       exc_info=True)


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
    history = state.get("history") or []
    # T8c: clip the render (never the data) to the newest STAGE_ROAD_CLIP
    # milestones. When there are more, a count line right after the intro
    # says how many older ones are collapsed (all still kept), then only the
    # newest N render, in the same chronological order.
    collapsed = len(history) - STAGE_ROAD_CLIP
    if collapsed > 0:
        lines.append(msg("stage_road_clipped").format(n=collapsed))
        history = history[-STAGE_ROAD_CLIP:]
    for entry in history:
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
    parameter: make_app's composite startup hook (_post_init, what it hands to
    ApplicationBuilder().post_init) awaits this with just `app`, never a
    cfg-bound wrapper, so there is no parameter slot for it. The inner-life
    tick, wired in that same closure, takes cfg and the store as explicit
    arguments instead -- a menu is cosmetic and degrades to start-only when the
    channel is missing, where a tick wired to nothing must fail loud, so the
    two travel differently on purpose. bot_data is PTB's own per-Application
    storage, sanctioned for exactly this kind of side channel. Every existing
    direct-call site (this project's own tests included) hands this a bare app
    with no bot_data attribute at all, which degrades to the original
    start-only menu, byte-identical to before M4 T8. The startup hook's own
    behavior (menu first, then tick; neither half able to crash boot) is pinned
    by test_bot_stream.py's TestPostInitComposite.
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

    # M5 T7: successful-reply counter for the diary page-turn. Once she has
    # exchanged two successful replies (counted over this process's lifetime;
    # a restart honestly resets it, mirroring the source architecture's own
    # semantics), the unshared diary pages that surfaced in her prompt are
    # marked shared so they stop repeating. Both success points gate the
    # count and the page-turn on cfg.diary_enabled, so a flag-off run never
    # counts and never marks -- byte-identical to the pre-feature behavior.
    reply_turns = {"n": 0}

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
        if data.startswith("stg_") and not stage_active:
            logger.warning("stage callback %r pressed while stages are inactive", data)
            await query.answer()
            return
        if data.startswith("alb_") and not cfg.album_enabled:
            logger.warning("album callback %r pressed while the album is disabled", data)
            await query.answer()
            return
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
                    # T7b: off-loop -- stages.advance writes to disk.
                    new_stage = await asyncio.to_thread(
                        stages.advance, cfg.stage_path, names, "", now)
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
                # T7b: stages.retreat persists to disk -- run it off-loop.
                new_stage = await asyncio.to_thread(
                    stages.retreat, cfg.stage_path, names, now)
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
                # T7b: album.remove_by_id writes to disk -- run it off-loop.
                await asyncio.to_thread(album.remove_by_id, cfg, entry_id)
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
                # T7b: stages.advance persists to disk -- run it off-loop.
                new_stage = await asyncio.to_thread(
                    stages.advance, cfg.stage_path, _stage_names(cfg),
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
                # the plain chunk list every other caller depends on. T7a:
                # extra_sink collects the notebook_full SYSTEM notice, sent
                # after the companion chunks but never cached.
                react_sink: list = []
                extra_sink: list = []
                chunks = await asyncio.to_thread(produce_reply, cfg, store,
                                                 update.message.text,
                                                 on_react=react_sink.append,
                                                 on_extra=extra_sink.append)
                for chunk in chunks:
                    sent = await update.message.reply_text(chunk)
                    _cache_sent(sent, chunk)
                # After the companion text, in order: the system notice(s).
                # NOT _cache_sent -- a heart on a system line must miss the
                # cache (album_expired), never resolve into the album.
                for extra in extra_sink:
                    await update.message.reply_text(extra)
                await _consume_react(cfg, update,
                                    react_sink[0] if react_sink else None,
                                    datetime.now().astimezone())
                if react_sink:
                    # Success signal (the same one _consume_react's docstring
                    # names): produce_reply's failure path early-returns
                    # before its on_react sink ever fires. A tag-only success
                    # returns an empty chunk list, so "\n".join is "" and the
                    # empty-text gate inside _schedule_reflection skips it
                    # naturally -- a gesture has no language to reflect on.
                    # chunks is now pure companion speech: T7a moved the
                    # notebook_full system line out into extra_sink, so it no
                    # longer leaks into what he reflects on.
                    _schedule_reflection(context, cfg, update.message.text,
                                         "\n".join(chunks))
                    if cfg.diary_enabled:
                        reply_turns["n"] += 1
                        if reply_turns["n"] >= 2:
                            await asyncio.to_thread(diary.mark_shared, cfg)
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
                # display.full_text is the tag-free ground truth of what she
                # actually saw; empty means a tag-only turn -- a gesture,
                # with no language to reflect on (the helper's own gate).
                _schedule_reflection(context, cfg, update.message.text,
                                     display.full_text)
                if cfg.diary_enabled:
                    reply_turns["n"] += 1
                    if reply_turns["n"] >= 2:
                        await asyncio.to_thread(diary.mark_shared, cfg)
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
        if added & _HEART_EMOJIS and removed & _HEART_EMOJIS:
            return  # heart variant swap: still hearted, keep the keepsake
        if added & _HEART_EMOJIS:
            cached = _message_cache.get(reaction.message_id)
            if cached is None:
                await context.bot.send_message(reaction.chat.id,
                                               msg("album_expired"))
            else:
                cached_text, _ = cached
                # T7b: album writes are disk I/O -- run them off-loop.
                await asyncio.to_thread(
                    album.add_partner_flag, cfg, cached_text,
                    reaction.message_id, datetime.now().astimezone())
        if removed & _HEART_EMOJIS:
            await asyncio.to_thread(album.remove_by_message_id, cfg,
                                    reaction.message_id)

    # Flag off must reproduce M1's sequential update handling exactly: the
    # busy gate and cancel callback only make sense once updates run
    # concurrently, so scope concurrency to streaming mode. PTB maps False to
    # one-update-at-a-time, byte-identical to M1 (which never enabled it).
    async def _post_init(app_) -> None:
        """The composite startup hook (M7 T6): publish the command menu first,
        then arm the inner-life tick. Each half self-guards -- PTB 22.6 awaits
        post_init via run_until_complete OUTSIDE any exception guard, so an
        escaping exception here crashes the whole process at startup; a cosmetic
        menu blip or a background-tick failure must never be that. The tick now
        lives in scheduler.py; store reaches it as an explicit argument from
        this closure's scope, never a bot_data side channel -- a parameter that
        cannot be supplied fails loudly at once, where a silent side-channel
        miss would arm a tick wired to nothing that no one would notice."""
        await register_commands(app_)          # keeps its own internal guard
        try:
            # _cache_sent rides along so a heart on a PROACTIVE message
            # resolves to its text exactly like a heart on a reply does;
            # its own album_enabled gate makes this a no-op when the album
            # is off (merge-acceptance fix, 2026-07-11).
            scheduler.start_tick(app_, cfg, store, cache_sink=_cache_sent)
        except Exception:
            logger.warning("inner-life tick failed to start", exc_info=True)

    app = (ApplicationBuilder().token(cfg.bot_token)
           .concurrent_updates(cfg.streaming_enabled)
           .post_init(_post_init).build())
    # cfg still reaches register_commands at call time through this side
    # channel (see its own docstring); the inner-life tick, by contrast, takes
    # cfg and the session store as explicit arguments from _post_init's scope.
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


# ---------------------------------------------------------------------
# Root-logger token masking (M7 T7)
# ---------------------------------------------------------------------

# python-telegram-bot's underlying httpx client logs every outgoing request
# at INFO, and long-polling's own getUpdates call embeds the complete bot
# token in that URL, verbatim. Anyone who pastes their INFO-level log for
# help (a support request, a GitHub issue, a terminal screenshot) hands
# their live token to whoever reads it -- a real, repeated leak source this
# pattern exists to close. It matches that URL shape wherever it appears in
# a rendered log line: literal "bot", the numeric bot id, a colon, then a
# secret of at least 30 URL-safe characters (a real Telegram secret runs
# 35). Shape-only, on purpose: this project's own rule is that a token's
# actual VALUE is never read by tooling that does not strictly need it, so
# this cannot and does not compare against the real configured token.
#
# The trade-off that follows from matching by shape alone: the pattern is
# NOT anchored to a word boundary before "bot", so an unrelated string that
# merely CONTAINS that shape -- e.g. "androbot123456789:" followed by 30+
# safe characters that are not a token at all -- would also get masked.
# That is accepted deliberately: over-masking a look-alike costs one
# slightly mangled log line; under-masking a real token costs the secret
# itself. The >=30 floor is what keeps ordinary short text like
# "bot123:short" (nowhere near a real secret's length) from ever matching.
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{30,}")
_TOKEN_MASK = "bot<TOKEN>"


class TokenMaskFilter(logging.Filter):
    """A logging.Filter that rewrites Telegram bot tokens by SHAPE in every
    record it sees, wherever it is attached, and never drops one.

    filter() always returns True. A logging.Filter's contract lets filter()
    return False to drop a record entirely, but that is the wrong tool
    here: this filter's whole job is to REWRITE a record in place, never to
    decide what does or does not get logged -- conflating the two would
    mean a bug in this filter could silently blind the app to real errors.
    The same reasoning is why the body below never lets an exception
    escape: record.getMessage() can itself raise (a %-style msg whose args
    do not satisfy its own placeholders, or a msg object whose __str__
    raises), and a filter that crashes on one malformed record would break
    logging for that whole call site. Falling back to letting the record
    through completely unmasked on any such trouble is the deliberate
    choice -- one unmasked line is a far smaller cost than losing logging
    altogether.

    Attached at the HANDLER, not the logger -- see
    _install_token_mask_filter's own docstring for why that distinction is
    load-bearing here.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
            masked = _TOKEN_RE.sub(_TOKEN_MASK, text)
        except Exception:
            return True
        if masked != text:
            # args are already consumed by getMessage()'s own %-expansion
            # above; msg is now the final string, so args must be cleared,
            # or a later getMessage() call (a handler's own formatter) would
            # try to re-apply them to already-formatted text.
            record.msg = masked
            record.args = None
        return True


def _install_token_mask_filter() -> None:
    """Attach a TokenMaskFilter to every handler on the root logger. Called
    from main(), right after logging.basicConfig() -- kept as its own
    function (mirroring _allowed_updates just above) so it is directly
    testable without booting the whole application: main() itself calls
    load_config() against real environment variables,
    engine.check_claude_available() against a real Claude CLI subprocess,
    and run_polling() blocks forever against live Telegram, none of which
    belong anywhere near a unit test.

    HANDLER-level, deliberately not logger-level: a filter added to a
    Logger object only inspects records logged directly ON that logger.
    httpx and python-telegram-bot log through their own logger instances
    ("httpx", "telegram.ext.Application", ...), never through "everthine",
    and those records only reach the root logger's OWN handlers via
    propagation -- propagation hands a record straight to each ancestor's
    handlers, without re-running that ancestor's own logger-level filters.
    So a filter sitting on the root Logger object itself would still never
    see them. Attaching to every handler on the root logger is what
    actually covers every propagated record, regardless of which logger
    emitted it.

    Zero handlers is a theoretical edge here (logging.basicConfig() only
    installs one when the root logger does not already own one, and
    nothing in this codebase clears it afterward), but if it ever happens
    this falls back to the same single-StreamHandler shape basicConfig()
    itself would have installed, so masking is never silently absent just
    because the handler list came up empty.
    """
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.addFilter(TokenMaskFilter())


# ---------------------------------------------------------------------
# LOG_LEVEL env knob (M7 whole-branch final review)
# ---------------------------------------------------------------------

# M7's named-skip design routes the routine reasons a tick stays quiet --
# quiet hours, a missed dice roll, an active cooldown, a still-active
# partner window -- down to DEBUG, so "why didn't he say anything just
# now" has an answer. That answer is invisible at the production default
# (INFO) unless something can raise the root logger's level without a
# code change. LOG_LEVEL is that switch: unset or empty keeps today's INFO
# default, DEBUG surfaces every named skip reason each tick, and an
# unrecognized value falls back to INFO with a one-line warning naming it.

def _resolve_log_level(raw: str | None) -> tuple[int, str | None]:
    """Resolve a LOG_LEVEL env value to (numeric level, bad_value).

    bad_value is None on success, including the unset/empty case, which
    quietly resolves to logging.INFO -- config.py's own _get_bool /
    _get_int helpers treat an absent or blank env var the same way, as
    "use the default," not as an error. A non-empty value that does not
    name a real level (case-insensitive: "debug" / "Debug" / "DEBUG" all
    resolve alike) also falls back to logging.INFO, but is returned as
    bad_value instead of None so the caller -- which has an active
    logger, this function does not -- can warn about it. A plain function
    with no I/O and no global state, so it is directly testable without
    touching real logging.
    """
    if raw is None or str(raw).strip() == "":
        return logging.INFO, None
    name = str(raw).strip().upper()
    level = getattr(logging, name, None)
    if isinstance(level, int):
        return level, None
    return logging.INFO, raw


def _apply_log_level(raw: str | None) -> None:
    """Resolve LOG_LEVEL and apply it to the root logger, warning once if
    the value did not name a real level. Split from _resolve_log_level so
    that stays a plain, I/O-free function while this thin wrapper is what
    main() actually calls -- directly testable the same way
    _install_token_mask_filter() is, against the real root logger, without
    booting the whole application.
    """
    level, bad = _resolve_log_level(raw)
    logging.getLogger().setLevel(level)
    if bad is not None:
        logger.warning("LOG_LEVEL=%r is not a valid logging level name; using INFO", bad)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    _install_token_mask_filter()
    cfg = load_config()
    # LOG_LEVEL is applied here, not folded into the basicConfig() call
    # above: .env loads inside load_config() (config.py's load_dotenv()),
    # which runs AFTER basicConfig(). Reading os.environ at the
    # basicConfig() call site would only ever see a shell-exported
    # LOG_LEVEL, never one that lives solely in .env. basicConfig() keeps
    # its INFO bootstrap default -- logging starts exactly when it always
    # has -- and this adjusts the level moments later, once .env is
    # actually loaded; nothing logs in between, so no message is ever
    # emitted at the wrong level in the gap.
    _apply_log_level(os.environ.get("LOG_LEVEL"))
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.engine_home.mkdir(parents=True, exist_ok=True)
    if not engine.check_claude_available(cfg):
        raise SystemExit(msg("cli_missing"))
    logger.info("everthine is online")
    make_app(cfg).run_polling(allowed_updates=_allowed_updates(cfg))
