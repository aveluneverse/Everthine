"""Progressive reply display for Telegram.

Buffers streamed text and edits a placeholder message in place, so the
reply appears to be typed live. Edits are rate limited (Telegram floods
back with RetryAfter otherwise), gated on sentence boundaries so partial
words are not shown, Markdown markers are closed before every edit, and
replies longer than the split threshold continue in a fresh message with
any open code fence reopened.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, RetryAfter

from .messages import msg

logger = logging.getLogger("everthine")

_SENTENCE_BOUNDARY = re.compile(r"[。！？\n]|\.\s|!\s|\?\s")

_SPLIT_THRESHOLD = 3800
_DEFAULT_EDIT_INTERVAL = 1.0
_FLOOD_EDIT_INTERVAL = 2.0
_FORCE_EDIT_TIMEOUT = 3.0


def has_sentence_boundary(text: str) -> bool:
    return bool(_SENTENCE_BOUNDARY.search(text))


def sanitize_markdown(text: str) -> str:
    """Close unclosed Telegram Markdown (V1) so a partial buffer renders."""
    if not text:
        return text
    if text.count("```") % 2 == 1:
        return text + "\n```"
    stripped = re.sub(r"```[\s\S]*?```", "", text)
    if stripped.count("`") % 2 == 1:
        return text + "`"
    stripped = re.sub(r"`[^`]*`", "", stripped)
    if stripped.count("*") % 2 == 1:
        text += "*"
    if stripped.count("_") % 2 == 1:
        text += "_"
    return text


def find_split_point(text: str, max_len: int = _SPLIT_THRESHOLD) -> int:
    """Best index to end the current message: paragraph > sentence > hard."""
    if len(text) <= max_len:
        return len(text)
    region = text[:max_len]
    last_para = region.rfind("\n\n")
    if last_para > 0:
        return last_para + 2
    matches = list(_SENTENCE_BOUNDARY.finditer(region))
    if matches:
        return matches[-1].end()
    return max_len


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(msg("btn_cancel"), callback_data="btn_cancel"),
    ]])


class StreamingDisplay:
    """Drives one progressively-edited reply.

    `message` is the placeholder message to edit; `send_new` is an async
    callable (text, parse_mode=None, reply_markup=None) -> Message used
    when a long reply must continue in a fresh message.
    """

    def __init__(self, message: Message, send_new):
        self._current_msg = message
        self._send_new = send_new
        self._full_text = ""
        self._current_buffer = ""
        self._displayed_len = 0
        self._messages: list = []
        self._message_texts: list = []
        self._last_edit_time = time.monotonic()
        self._is_first_update = True
        self._edit_interval = _DEFAULT_EDIT_INTERVAL
        self._markdown_ok = True

    @property
    def full_text(self) -> str:
        return self._full_text

    @property
    def message_texts(self) -> list:
        return self._message_texts

    async def append(self, chunk: str) -> None:
        self._full_text += chunk
        self._current_buffer += chunk
        await self._maybe_edit()

    async def finalize(self) -> list:
        if len(self._current_buffer) > self._displayed_len:
            await self._do_edit()
        if self._current_buffer:
            await self._edit_current(self._current_buffer, with_markup=False)
        if self._current_msg not in self._messages:
            self._messages.append(self._current_msg)
            self._message_texts.append(self._current_buffer)
        return self._messages

    async def cancel(self) -> list:
        if self._is_first_update:
            try:
                await self._current_msg.delete()
            except Exception:
                pass
            return []
        displayed = self._current_buffer[:self._displayed_len]
        if displayed:
            await self._edit_current(displayed, with_markup=False)
        if self._current_msg not in self._messages:
            self._messages.append(self._current_msg)
            self._message_texts.append(displayed)
        return self._messages

    # ─── internals ─────────────────────────────────────────────────

    async def _maybe_edit(self) -> None:
        new_text = self._current_buffer[self._displayed_len:]
        if not new_text:
            return
        elapsed = time.monotonic() - self._last_edit_time
        boundary = has_sentence_boundary(new_text)
        if ((self._is_first_update and boundary)
                or (elapsed >= self._edit_interval and boundary)
                or elapsed >= _FORCE_EDIT_TIMEOUT):
            await self._do_edit()

    async def _edit_current(self, text: str, with_markup: bool) -> bool:
        """Edit the current message; returns True when the edit landed."""
        markup = cancel_markup() if with_markup else None
        if self._markdown_ok:
            try:
                await self._current_msg.edit_text(
                    sanitize_markdown(text), parse_mode="Markdown",
                    reply_markup=markup)
                return True
            except RetryAfter as exc:
                self._edit_interval = _FLOOD_EDIT_INTERVAL
                await asyncio.sleep(exc.retry_after)
                return False
            except BadRequest:
                self._markdown_ok = False
            except Exception as exc:
                logger.warning("streaming edit failed: %s", exc)
                return False
        try:
            await self._current_msg.edit_text(text, reply_markup=markup)
            return True
        except RetryAfter as exc:
            self._edit_interval = _FLOOD_EDIT_INTERVAL
            await asyncio.sleep(exc.retry_after)
            return False
        except Exception as exc:
            logger.warning("streaming edit failed: %s", exc)
            return False

    async def _do_edit(self) -> None:
        if len(self._current_buffer) > _SPLIT_THRESHOLD:
            await self._split_and_continue()
            return
        if await self._edit_current(self._current_buffer, with_markup=True):
            self._is_first_update = False
            self._displayed_len = len(self._current_buffer)
            self._last_edit_time = time.monotonic()
            if self._edit_interval > _DEFAULT_EDIT_INTERVAL:
                self._edit_interval = _DEFAULT_EDIT_INTERVAL

    async def _split_and_continue(self) -> None:
        split_pos = find_split_point(self._current_buffer)
        first_part = self._current_buffer[:split_pos]
        remainder = self._current_buffer[split_pos:]
        if first_part.count("```") % 2 == 1:
            remainder = "```\n" + remainder

        await self._edit_current(first_part, with_markup=False)
        if self._current_msg not in self._messages:
            self._messages.append(self._current_msg)
            self._message_texts.append(first_part)

        # A flood backlog can pile far past the threshold between edits;
        # peel closed messages off the front until the tail fits one message.
        while len(remainder) > _SPLIT_THRESHOLD:
            pos = find_split_point(remainder)
            head = remainder[:pos]
            rest = remainder[pos:]
            if head.count("```") % 2 == 1:
                rest = "```\n" + rest
            if len(rest) >= len(remainder):
                # A tiny fenced head whose reopen regrows the tail would
                # spin forever; hard-cut to guarantee forward progress.
                head = remainder[:_SPLIT_THRESHOLD]
                rest = remainder[_SPLIT_THRESHOLD:]
                if head.count("```") % 2 == 1:
                    rest = "```\n" + rest
            remainder = rest
            try:
                closed = await self._send_new(
                    sanitize_markdown(head) if self._markdown_ok else head,
                    parse_mode="Markdown" if self._markdown_ok else None)
            except BadRequest:
                self._markdown_ok = False
                closed = await self._send_new(head)
            self._messages.append(closed)
            self._message_texts.append(head)

        try:
            new_msg = await self._send_new(
                sanitize_markdown(remainder) if self._markdown_ok else remainder,
                parse_mode="Markdown" if self._markdown_ok else None,
                reply_markup=cancel_markup())
        except BadRequest:
            self._markdown_ok = False
            new_msg = await self._send_new(remainder, reply_markup=cancel_markup())

        self._current_msg = new_msg
        self._current_buffer = remainder
        self._displayed_len = len(remainder)
        self._last_edit_time = time.monotonic()
        self._is_first_update = False
