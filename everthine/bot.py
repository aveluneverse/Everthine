"""Telegram application: thin async glue around a testable core.

produce_reply() is the seam that carries the whole thought pipeline:
archive the user's words -> build the warmth injection -> ask the engine
-> stamp the session -> archive the reply -> chunk for Telegram.
Handlers below it only translate Telegram updates in and out.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from . import archive, chunking, engine, persona, recent_context
from .config import Config, load_config
from .messages import msg
from .session_store import SessionStore

logger = logging.getLogger("everthine")

_busy = False


def decide_start_buttons(has_session: bool) -> list:
    if has_session:
        return ["btn_resume", "btn_warm", "btn_clean"]
    return ["btn_clean"]


def produce_reply(cfg: Config, store: SessionStore, text: str,
                  now: datetime | None = None, engine_mod=engine) -> list:
    now = now or datetime.now().astimezone()
    if cfg.archive_enabled:
        archive.write_entry(cfg.archive_dir, "user", text, ts=now)

    data = store.load()
    block = None
    try:
        block = recent_context.build_block(cfg, data, cfg.archive_dir, now)
    except Exception:
        logger.warning("warmth injection failed; continuing without it", exc_info=True)
    prompt = recent_context.prepend(block, text)

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


def _authorized(cfg: Config, update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id == cfg.authorized_user_id


def _keyboard(keys: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(msg(k), callback_data=k)]
                                 for k in keys])


def make_app(cfg: Config):
    store = SessionStore(cfg.session_path)

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
        now = datetime.now().astimezone()
        if query.data == "btn_warm":
            store.warm_restart()
            await query.edit_message_text(msg("warm_ack"))
        elif query.data == "btn_clean":
            store.clean_start(now)
            await query.edit_message_text(msg("clean_ack"))
        else:
            await query.edit_message_text(msg("resume_ack"))

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        global _busy
        if not update.message or not update.message.text:
            return
        if not _authorized(cfg, update):
            return
        if _busy:
            await update.message.reply_text(msg("busy"))
            return
        _busy = True
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                               action=ChatAction.TYPING)
            chunks = await asyncio.to_thread(produce_reply, cfg, store,
                                             update.message.text)
            for chunk in chunks:
                await update.message.reply_text(chunk)
        finally:
            _busy = False

    app = ApplicationBuilder().token(cfg.bot_token).build()
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
