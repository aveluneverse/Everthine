"""Telegram application: thin async glue around a testable core.

produce_reply() is the seam that carries the whole thought pipeline:
archive the user's words -> build the warmth injection -> ask the engine
-> stamp the session -> archive the reply -> chunk for Telegram.
Handlers below it only translate Telegram updates in and out.

stream_reply() is the streaming twin of produce_reply(); both share
prepare_exchange().
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from datetime import datetime

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from . import archive, chunking, engine, memory_recall, messages, persona, recent_context
from .config import Config, load_config
from .engine import EngineReply
from .messages import msg
from .session_store import SessionStore
from .streaming_display import StreamingDisplay, cancel_markup

logger = logging.getLogger("everthine")


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


def produce_reply(cfg: Config, store: SessionStore, text: str,
                  now: datetime | None = None, engine_mod=engine) -> list:
    now = now or datetime.now().astimezone()
    prompt, data, memory_block = prepare_exchange(cfg, store, text, now)

    reply = engine_mod.run_once(
        cfg, prompt, session_id=data.get("session_id"),
        system_prompt=persona.build_system_prompt(cfg, memory_block=memory_block))
    if not reply.ok:
        return [msg(reply.error_kind or "generic_glitch")]

    store.stamp_session_started(reply.session_id, now)
    if cfg.archive_enabled and reply.text:
        archive.write_entry(cfg.archive_dir, "companion", reply.text, ts=now)
    # After-reply: the turn just archived becomes memory once its
    # conversation closes. Fail-soft by contract; never runs on the error
    # path above (an early return already skipped it).
    memory_recall.sync(cfg, now)

    out = chunking.split_message(reply.text)
    if store.detect_bloat(cfg, reply.session_id):
        out.append(msg("notebook_full"))
    return out or [msg("generic_glitch")]


async def stream_reply(cfg: Config, store: SessionStore, text: str,
                       display, cancel_flag: threading.Event,
                       now: datetime | None = None,
                       engine_mod=engine):
    """Streaming twin of produce_reply(): drive the engine on a worker
    thread and forward text deltas to the display. Returns the final
    EngineReply, or None when the user cancelled."""
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
    await display.finalize()

    if reply.ok:
        store.stamp_session_started(reply.session_id, now)
        if cfg.archive_enabled and display.full_text:
            archive.write_entry(cfg.archive_dir, "companion",
                                display.full_text, ts=now)
        # After-reply sync, off-loop (mirrors produce_reply; see its comment).
        await asyncio.to_thread(memory_recall.sync, cfg, now)
    return reply


def _authorized(cfg: Config, update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id == cfg.authorized_user_id


def _keyboard(keys: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(msg(k), callback_data=k)]
                                 for k in keys])


async def register_commands(app) -> None:
    """Publish the command menu Telegram shows behind the Menu button.

    The menu is cosmetic, so failures are logged and never fatal: PTB awaits
    post_init outside its network retry loop, and an escaping exception here
    would crash the whole bot at startup."""
    try:
        await app.bot.set_my_commands([BotCommand("start", msg("cmd_start_desc"))])
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

    # Boot-time backfill: makes a pre-existing archive recallable before the
    # first turn (a fresh install, with no cursor yet, ingests everything).
    memory_recall.init(cfg)
    memory_recall.sync(cfg, datetime.now().astimezone())

    store = SessionStore(cfg.session_path)
    busy = {"active": False}
    cancel_flag = threading.Event()

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

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        if not _authorized(cfg, update):
            return
        if busy["active"]:
            await update.message.reply_text(msg("busy"))
            return
        busy["active"] = True
        cancel_flag.clear()
        try:
            if not cfg.streaming_enabled:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id, action=ChatAction.TYPING)
                chunks = await asyncio.to_thread(produce_reply, cfg, store,
                                                 update.message.text)
                for chunk in chunks:
                    await update.message.reply_text(chunk)
                return

            placeholder = await update.message.reply_text(
                messages.thinking_line(), reply_markup=cancel_markup())

            async def send_new(text, parse_mode=None, reply_markup=None):
                return await update.message.reply_text(
                    text, parse_mode=parse_mode, reply_markup=reply_markup)

            display = StreamingDisplay(placeholder, send_new)
            reply = await stream_reply(cfg, store, update.message.text,
                                       display, cancel_flag)
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
        except Exception:
            logger.error("reply pipeline failed unexpectedly", exc_info=True)
            try:
                await update.message.reply_text(msg("generic_glitch"))
            except Exception:
                logger.error("could not deliver the error notice", exc_info=True)
        finally:
            busy["active"] = False
            cancel_flag.clear()

    # Flag off must reproduce M1's sequential update handling exactly: the
    # busy gate and cancel callback only make sense once updates run
    # concurrently, so scope concurrency to streaming mode. PTB maps False to
    # one-update-at-a-time, byte-identical to M1 (which never enabled it).
    app = (ApplicationBuilder().token(cfg.bot_token)
           .concurrent_updates(cfg.streaming_enabled)
           .post_init(register_commands).build())
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.engine_home.mkdir(parents=True, exist_ok=True)
    if not engine.check_claude_available(cfg):
        raise SystemExit(msg("cli_missing"))
    logger.info("everthine is online")
    make_app(cfg).run_polling()
