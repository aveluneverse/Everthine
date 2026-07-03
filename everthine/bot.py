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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from . import archive, chunking, engine, persona, recent_context
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
    """Archive the user's turn and assemble the engine prompt."""
    if cfg.archive_enabled:
        archive.write_entry(cfg.archive_dir, "user", text, ts=now)
    data = store.load()
    block = None
    try:
        block = recent_context.build_block(cfg, data, cfg.archive_dir, now)
    except Exception:
        logger.warning("warmth injection failed; continuing without it",
                       exc_info=True)
    return recent_context.prepend(block, text), data


def produce_reply(cfg: Config, store: SessionStore, text: str,
                  now: datetime | None = None, engine_mod=engine) -> list:
    now = now or datetime.now().astimezone()
    prompt, data = prepare_exchange(cfg, store, text, now)

    reply = engine_mod.run_once(cfg, prompt, session_id=data.get("session_id"),
                                system_prompt=persona.build_system_prompt(cfg))
    if not reply.ok:
        return [msg(reply.error_kind or "generic_glitch")]

    store.stamp_session_started(reply.session_id, now)
    if cfg.archive_enabled and reply.text:
        archive.write_entry(cfg.archive_dir, "companion", reply.text, ts=now)

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
    prompt, data = prepare_exchange(cfg, store, text, now)

    events: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=engine_mod.stream_once,
        kwargs=dict(cfg=cfg, prompt=prompt, session_id=data.get("session_id"),
                    system_prompt=persona.build_system_prompt(cfg),
                    events=events, cancel=cancel_flag),
        daemon=True)
    worker.start()

    reply = None
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
    return reply


def _authorized(cfg: Config, update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id == cfg.authorized_user_id


def _keyboard(keys: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(msg(k), callback_data=k)]
                                 for k in keys])


def make_app(cfg: Config):
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
                msg("thinking"), reply_markup=cancel_markup())

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

    app = (ApplicationBuilder().token(cfg.bot_token)
           .concurrent_updates(True).build())
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
