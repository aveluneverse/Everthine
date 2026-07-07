import asyncio
import unittest
import warnings

from telegram.error import BadRequest, RetryAfter
from telegram.warnings import PTBDeprecationWarning

from everthine.streaming_display import (StreamingDisplay, cancel_markup,
                                         find_split_point,
                                         has_sentence_boundary,
                                         sanitize_markdown)


class FakeMessage:
    def __init__(self, name="m0"):
        self.name = name
        self.edits = []
        self.deleted = False
        self.fail_markdown = False

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        if self.fail_markdown and parse_mode == "Markdown":
            raise BadRequest("cannot parse entities")
        self.edits.append({"text": text, "parse_mode": parse_mode,
                           "has_markup": reply_markup is not None})

    async def delete(self):
        self.deleted = True


class FakeSender:
    def __init__(self):
        self.sent = []

    async def __call__(self, text, parse_mode=None, reply_markup=None):
        m = FakeMessage(name=f"m{len(self.sent) + 1}")
        self.sent.append({"text": text, "parse_mode": parse_mode,
                          "has_markup": reply_markup is not None, "msg": m})
        return m


def make_display():
    msg0 = FakeMessage()
    sender = FakeSender()
    d = StreamingDisplay(msg0, sender)
    d._edit_interval = 0.0  # deterministic tests: no wall-clock gating
    return d, msg0, sender


class TestReactTag(unittest.TestCase):
    async def _display_after(self, *chunks):
        d, _, _ = make_display()
        for c in chunks:
            await d.append(c)
        return d

    def test_tag_captured_and_stripped(self):
        d = asyncio.run(self._display_after("[react:❤️] hello there"))
        self.assertEqual(d.reaction_emoji, "❤️")
        self.assertEqual(d.full_text, "hello there")

    def test_tag_split_across_chunks_within_window(self):
        d = asyncio.run(self._display_after("[rea", "ct:\U0001f525] warm words"))
        self.assertEqual(d.reaction_emoji, "\U0001f525")
        self.assertEqual(d.full_text, "warm words")

    def test_tag_boundary_split_after_emoji(self):
        # "[react:🔥" already satisfies REACT_TAG ("]" and trailing space
        # are optional), but the terminator is still in flight: committing
        # on that full-head match would leak "] warm" as visible text.
        d = asyncio.run(self._display_after("[react:", "\U0001f525", "] warm"))
        self.assertEqual(d.reaction_emoji, "\U0001f525")
        self.assertEqual(d.full_text, "warm")

    def test_tag_vs16_emoji_split(self):
        # U+2764 heart ends one chunk, U+FE0F variation selector opens the
        # next: the captured emoji must keep both codepoints and the "]"
        # must not leak into the visible text.
        d = asyncio.run(self._display_after("[react:❤", "️] hi"))
        self.assertEqual(d.reaction_emoji, "❤️")
        self.assertEqual(d.full_text, "hi")

    def test_oversized_first_chunk_still_caught(self):
        # 7/4 lesson: the window test must use the length BEFORE the chunk
        # arrived, or a first chunk larger than the window slips through.
        d = asyncio.run(self._display_after("[react:❤️] " + "x" * 500))
        self.assertEqual(d.reaction_emoji, "❤️")
        self.assertNotIn("[react:", d.full_text)

    def test_mid_text_react_is_not_a_tag(self):
        d = asyncio.run(self._display_after("I will [react:❤️] later"))
        self.assertIsNone(d.reaction_emoji)
        self.assertIn("[react:❤️]", d.full_text)

    def test_no_tag_no_capture(self):
        d = asyncio.run(self._display_after("plain reply"))
        self.assertIsNone(d.reaction_emoji)

    def test_finalize_flushes_undecided_head(self):
        # A cancelled/finalized display mid-buffer must not lose the head:
        # whatever is still undecided when the stream ends can never
        # complete a tag, so it must flush through unstripped.
        async def run():
            d, _, _ = make_display()
            await d.append("[rea")
            await d.finalize()
            return d

        d = asyncio.run(run())
        self.assertIsNone(d.reaction_emoji)
        self.assertEqual(d.full_text, "[rea")

    def test_tag_only_reply_still_strips_on_finalize(self):
        # Whole reply is just the tag: finalize must strip and capture,
        # not flush the raw tag through as visible text.
        async def run():
            d, _, _ = make_display()
            await d.append("[react:❤️]")
            await d.finalize()
            return d

        d = asyncio.run(run())
        self.assertEqual(d.reaction_emoji, "❤️")
        self.assertEqual(d.full_text, "")

    def test_cancel_flushes_undecided_head(self):
        async def run():
            d, _, _ = make_display()
            await d.append("[rea")
            await d.cancel()
            return d

        d = asyncio.run(run())
        self.assertIsNone(d.reaction_emoji)
        self.assertEqual(d.full_text, "[rea")


class TestTagOnlyFinalize(unittest.IsolatedAsyncioTestCase):
    """N4: a tag-only reply (the gesture IS the whole response) must delete
    the placeholder instead of leaving it stuck on the waiting line, and
    return an empty message list so no cache/archive entry is fabricated.
    A genuinely empty reply with NO captured tag keeps the old behavior
    (the placeholder survives for the caller's own fallback)."""

    async def test_tag_only_finalize_deletes_placeholder_and_returns_empty(self):
        d, msg0, _ = make_display()
        await d.append("[react:❤️]")
        messages = await d.finalize()
        self.assertEqual(messages, [])
        self.assertTrue(msg0.deleted)
        self.assertEqual(d.message_texts, [])
        self.assertEqual(d.reaction_emoji, "❤️")
        self.assertEqual(d.full_text, "")

    async def test_tag_only_finalize_swallows_delete_failure(self):
        class DeleteFails(FakeMessage):
            async def delete(self):
                raise RuntimeError("delete boom")

        msg0 = DeleteFails()
        d = StreamingDisplay(msg0, FakeSender())
        d._edit_interval = 0.0
        await d.append("[react:❤️]")
        with self.assertLogs("everthine", level="WARNING"):
            messages = await d.finalize()
        self.assertEqual(messages, [])
        self.assertEqual(d.message_texts, [])
        self.assertEqual(d.reaction_emoji, "❤️")

    async def test_empty_reply_no_tag_keeps_placeholder(self):
        # No text, no tag: the tag-only branch must NOT fire -- the
        # placeholder survives (the caller shows its own glitch fallback),
        # exactly as before this task.
        d, msg0, _ = make_display()
        messages = await d.finalize()
        self.assertFalse(msg0.deleted)
        self.assertEqual(messages, [msg0])
        self.assertIsNone(d.reaction_emoji)


class TestPureHelpers(unittest.TestCase):
    def test_sentence_boundary(self):
        self.assertTrue(has_sentence_boundary("Done. Next"))
        self.assertTrue(has_sentence_boundary("line\nbreak"))
        self.assertFalse(has_sentence_boundary("no boundary here"))

    def test_sanitize_markdown_closes_fence_and_inline(self):
        self.assertTrue(sanitize_markdown("```py\ncode").endswith("\n```"))
        self.assertTrue(sanitize_markdown("a `tick").endswith("`"))
        self.assertTrue(sanitize_markdown("a *bold").endswith("*"))
        self.assertTrue(sanitize_markdown("a _ital").endswith("_"))
        self.assertEqual(sanitize_markdown("clean *b* text"), "clean *b* text")

    def test_find_split_point_prefers_paragraph(self):
        text = ("a" * 100 + "\n\n") + "b" * 4000
        self.assertEqual(find_split_point(text, max_len=3800), 102)

    def test_find_split_point_sentence_then_hard(self):
        text = "x" * 50 + ". " + "y" * 4000
        self.assertEqual(find_split_point(text, max_len=3800), 52)
        self.assertEqual(find_split_point("z" * 5000, max_len=3800), 3800)

    def test_cancel_markup_shape(self):
        markup = cancel_markup()
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "btn_cancel")
        self.assertTrue(button.text)


class TestDisplay(unittest.IsolatedAsyncioTestCase):
    async def test_edit_on_sentence_boundary(self):
        d, msg0, _ = make_display()
        await d.append("Hello there")
        self.assertEqual(msg0.edits, [])
        await d.append(". More")
        self.assertEqual(len(msg0.edits), 1)
        self.assertTrue(msg0.edits[0]["has_markup"])

    async def test_finalize_flushes_and_drops_markup(self):
        d, msg0, _ = make_display()
        await d.append("Short reply.")
        messages = await d.finalize()
        self.assertEqual(len(messages), 1)
        self.assertFalse(msg0.edits[-1]["has_markup"])
        self.assertEqual(d.full_text, "Short reply.")
        self.assertEqual(d.message_texts, ["Short reply."])

    async def test_markdown_failure_falls_back_to_plain(self):
        d, msg0, _ = make_display()
        msg0.fail_markdown = True
        await d.append("odd *asterisk. next")
        await d.finalize()
        self.assertTrue(all(e["parse_mode"] is None for e in msg0.edits))

    async def test_split_continues_in_new_message(self):
        d, msg0, sender = make_display()
        await d.append("para one." + "a" * 3900 + "\n\n")
        await d.append("tail piece.")
        await d.finalize()
        self.assertEqual(len(sender.sent), 1)
        self.assertTrue(sender.sent[0]["has_markup"])
        self.assertEqual(len(d.message_texts), 2)
        self.assertIn("tail piece.", d.message_texts[1])

    async def test_split_reopens_code_fence(self):
        d, _, sender = make_display()
        await d.append("```\n" + ("code line.\n" * 400))
        await d.finalize()
        self.assertEqual(len(sender.sent), 1)
        self.assertTrue(sender.sent[0]["text"].startswith("```"))

    async def test_exact_threshold_does_not_split(self):
        d, msg0, sender = make_display()
        text = "x" * 3798 + ". "
        await d.append(text)
        self.assertEqual(sender.sent, [])
        self.assertEqual(len(msg0.edits), 1)
        messages = await d.finalize()
        self.assertEqual(len(messages), 1)
        self.assertEqual(d.message_texts, [text])

    async def test_flood_backlog_peels_into_multiple_messages(self):
        d, _, sender = make_display()
        text = "Sentence number one. " * 450
        await d.append(text)
        await d.finalize()
        self.assertGreaterEqual(len(sender.sent), 2)
        for part in d.message_texts:
            self.assertTrue(part)
            self.assertLessEqual(len(part), 4096)
        self.assertEqual("".join(d.message_texts), text)

    async def test_split_send_retries_on_flood(self):
        # A peel/continuation send that floods (RetryAfter) must not lose the
        # reply: the paced send sleeps the advised interval and retries the
        # same send until it lands, so the whole backlog is delivered intact.
        class FloodOnceSender(FakeSender):
            def __init__(self):
                super().__init__()
                self.flooded = False

            async def __call__(self, text, parse_mode=None, reply_markup=None):
                if not self.flooded:
                    self.flooded = True
                    raise RetryAfter(0)
                return await super().__call__(text, parse_mode=parse_mode,
                                              reply_markup=reply_markup)

        msg0 = FakeMessage()
        sender = FloodOnceSender()
        d = StreamingDisplay(msg0, sender)
        d._edit_interval = 0.0  # deterministic tests: no wall-clock gating
        text = "Sentence number one. " * 450
        with warnings.catch_warnings():
            # int retry_after is deprecated in PTB 22.6 (timedelta is coming);
            # we test the still-supported int path, so hush that library notice.
            warnings.simplefilter("ignore", PTBDeprecationWarning)
            await d.append(text)
            await d.finalize()
        self.assertTrue(sender.flooded)
        self.assertGreaterEqual(len(sender.sent), 2)
        for part in d.message_texts:
            self.assertTrue(part)
            self.assertLessEqual(len(part), 4096)
        self.assertEqual("".join(d.message_texts), text)

    async def test_peel_loop_terminates_on_boundaryless_fenced_run(self):
        d, _, _ = make_display()
        real_send = d._send_new

        async def yielding_send(*args, **kwargs):
            await asyncio.sleep(0)  # yield so the watchdog can fire on a spin
            return await real_send(*args, **kwargs)

        d._send_new = yielding_send
        text = "```\n" + "x" * 8200

        async def run():
            await d.append(text)
            return await d.finalize()

        messages = await asyncio.wait_for(run(), timeout=5)
        self.assertTrue(messages)
        for part in d.message_texts:
            self.assertTrue(part)
            self.assertLessEqual(len(part), 4096)
        self.assertEqual("".join(d.message_texts).replace("```\n", ""),
                         text.replace("```\n", ""))

    async def test_cancel_before_first_edit_deletes_placeholder(self):
        d, msg0, _ = make_display()
        await d.append("no boundary yet")
        messages = await d.cancel()
        self.assertTrue(msg0.deleted)
        self.assertEqual(messages, [])

    async def test_cancel_after_display_keeps_partial(self):
        d, msg0, _ = make_display()
        await d.append("Shown. ")
        await d.append("hidden tail")
        messages = await d.cancel()
        self.assertEqual(len(messages), 1)
        self.assertFalse(msg0.deleted)
        self.assertIn("Shown.", d.message_texts[0])
        self.assertNotIn("hidden tail", d.message_texts[0])

    async def test_cancel_right_after_split_keeps_both_messages(self):
        d, msg0, sender = make_display()
        await d.append("para one." + "a" * 3900 + "\n\n")
        messages = await d.cancel()
        self.assertFalse(msg0.deleted)
        self.assertFalse(sender.sent[0]["msg"].deleted)
        self.assertEqual(len(messages), 2)
        self.assertEqual(len(d.message_texts), 2)


if __name__ == "__main__":
    unittest.main()
