"""Tests for M7 Task 7: TokenMaskFilter, the root-logger filter that masks
Telegram bot tokens by SHAPE wherever they show up in a rendered log line.

Background: python-telegram-bot's underlying httpx client logs every
outgoing request at INFO, and long-polling's own getUpdates call embeds the
complete bot token in that URL. Anyone who pastes an INFO-level log for help
hands their live token to whoever reads it -- a real, repeated leak source.
TokenMaskFilter rewrites `bot<digits>:<secret>` to `bot<TOKEN>` in place,
never reading this process's actual configured token (shape only, never
value -- this project's own rule for anything token-shaped).

Two groups of tests:

  - TestTokenMaskFilter* (below): the filter's own rewriting behavior,
    driven directly against a private logger + a real StreamHandler writing
    into an in-memory buffer, with TokenMaskFilter attached at the HANDLER --
    the same attachment point bot.main() uses, so these tests exercise the
    real code path a production log line takes, not a shortcut around it.
    A per-test logger name (test id()) keeps every test's logger instance
    private: logging.getLogger(name) is a process-wide singleton keyed by
    name, so two tests sharing a name would leak handlers between them.

  - TestInstallTokenMaskFilterWiring: the main()-time wiring smoke test.
    bot.main() itself is untestable directly here -- it calls load_config()
    against real environment variables, engine.check_claude_available()
    against a real Claude CLI subprocess, and run_polling() blocks forever
    against live Telegram, exactly the reasons bot._allowed_updates() was
    already pulled out as its own directly-testable function rather than
    left inline. bot._install_token_mask_filter() is this task's equivalent
    extraction: main() calls it immediately after logging.basicConfig(),
    and this test calls it directly against a controlled root logger.
"""
import io
import logging
import unittest

from everthine import bot


def _fake_secret(n: int) -> str:
    """A synthetic, deterministic stand-in for a token's secret half: never
    a real token, and never derived from one -- this project's own rule is
    that a token's actual VALUE is never read by anything that does not
    strictly need it, including this test suite. Repeats a fixed
    alphabet-safe pattern out to exactly `n` characters so callers can ask
    for "clearly over the 30-char floor" or "clearly under it" without
    hand-counting a literal string.
    """
    pattern = "AaZz09-_"
    return (pattern * (n // len(pattern) + 1))[:n]


FAKE_SECRET = _fake_secret(35)          # well over the 30-char floor
FAKE_TOKEN = f"bot123456789:{FAKE_SECRET}"

FAKE_SECRET_2 = _fake_secret(40)        # a second, distinct fake token
FAKE_TOKEN_2 = f"bot987654321:{FAKE_SECRET_2}"

SHORT_SECRET = "short"                  # 5 chars: well under the floor
SHORT_TOKEN = f"bot123:{SHORT_SECRET}"


class _FilteredLoggerTestCase(unittest.TestCase):
    """A fresh, private logger wired to a StreamHandler(io.StringIO()),
    with TokenMaskFilter attached to that HANDLER -- not the logger --
    mirroring bot._install_token_mask_filter()'s own attachment point.
    """

    def setUp(self):
        self.buf = io.StringIO()
        handler = logging.StreamHandler(self.buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(bot.TokenMaskFilter())
        self.logger = logging.getLogger(f"test_token_mask.{id(self)}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.handlers = [handler]

    def output(self) -> str:
        return self.buf.getvalue()


# --- 1. Direct msg path ------------------------------------------------

class TestDirectMessagePath(_FilteredLoggerTestCase):
    def test_token_is_masked_and_original_secret_is_gone(self):
        self.logger.info(
            f"GET https://api.telegram.org/{FAKE_TOKEN}/getUpdates ok")
        out = self.output()
        self.assertIn("bot<TOKEN>", out)
        self.assertNotIn(FAKE_SECRET, out)
        self.assertNotIn(FAKE_TOKEN, out)


# --- 2. args-tuple path (getMessage() must expand it first) ------------

class TestArgsTuplePath(_FilteredLoggerTestCase):
    def test_token_inside_a_percent_s_arg_is_masked(self):
        url = f"https://api.telegram.org/{FAKE_TOKEN}/getUpdates"
        self.logger.info("GET %s ok", url)
        out = self.output()
        self.assertIn("bot<TOKEN>", out)
        self.assertNotIn(FAKE_SECRET, out)


# --- 3. Multiple tokens on one line: all of them get masked -------------

class TestMultipleTokensOneLine(_FilteredLoggerTestCase):
    def test_two_distinct_tokens_both_masked(self):
        self.logger.info(f"{FAKE_TOKEN} and also {FAKE_TOKEN_2} both here")
        out = self.output()
        self.assertEqual(out.count("bot<TOKEN>"), 2)
        self.assertNotIn(FAKE_SECRET, out)
        self.assertNotIn(FAKE_SECRET_2, out)


# --- 4. Short "secret" is not a token shape: left untouched -------------

class TestShortSecretNotMasked(_FilteredLoggerTestCase):
    """The >=30-char floor is what keeps ordinary text merely containing
    the substring "bot<digits>:" -- far short of a real token's length --
    from ever being mistaken for one. See TokenMaskFilter's own module-level
    comment for the false-positive trade-off the OTHER direction (no word
    boundary before "bot") deliberately accepts instead.
    """

    def test_bot_colon_short_string_passes_through_unmasked(self):
        self.logger.info(f"weird but harmless: {SHORT_TOKEN}")
        out = self.output()
        self.assertIn(SHORT_TOKEN, out)
        self.assertNotIn("bot<TOKEN>", out)


# --- 5. Non-str msg must never crash the filter -------------------------

class TestNonStrMessageNeverCrashes(_FilteredLoggerTestCase):
    def test_exception_instance_as_msg_logs_fine(self):
        self.logger.info(ValueError("boom"))
        self.assertIn("boom", self.output())

    def test_dict_as_msg_logs_fine_and_still_masks_inside_its_repr(self):
        self.logger.info({"note": "token leak check", "token": FAKE_TOKEN})
        out = self.output()
        self.assertIn("bot<TOKEN>", out)
        self.assertNotIn(FAKE_SECRET, out)

    def test_getmessage_raising_is_swallowed_and_returns_true(self):
        # A %-style msg whose args do not satisfy its own placeholder
        # raises TypeError INSIDE record.getMessage() itself. The filter
        # must swallow that and let the record through unmasked rather
        # than crash the handler chain -- one unmasked line beats losing
        # logging altogether.
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname=__file__, lineno=1,
            msg="%d", args=("not-an-int",), exc_info=None)
        self.assertTrue(bot.TokenMaskFilter().filter(record))


# --- 6. filter() always returns True: masks, never drops ----------------

class TestFilterNeverDrops(_FilteredLoggerTestCase):
    def test_filter_call_returns_true_on_a_real_token_record(self):
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname=__file__, lineno=1,
            msg=FAKE_TOKEN, args=None, exc_info=None)
        self.assertTrue(bot.TokenMaskFilter().filter(record))

    def test_masked_line_still_reaches_the_handler_output(self):
        self.logger.info(FAKE_TOKEN)
        self.assertIn("bot<TOKEN>", self.output())


# --- 7. main()-time wiring: every root handler gets the filter ----------

class TestInstallTokenMaskFilterWiring(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self._saved_handlers = root.handlers[:]
        self.addCleanup(setattr, root, "handlers", self._saved_handlers)
        self.root = root

    def test_attaches_a_tokenmaskfilter_to_every_existing_handler(self):
        h1 = logging.StreamHandler(io.StringIO())
        h2 = logging.StreamHandler(io.StringIO())
        self.root.handlers = [h1, h2]

        bot._install_token_mask_filter()

        for handler in self.root.handlers:
            self.assertTrue(
                any(isinstance(f, bot.TokenMaskFilter) for f in handler.filters),
                f"{handler} is missing a TokenMaskFilter")

    def test_zero_handlers_edge_case_still_ends_with_a_filtered_handler(self):
        self.root.handlers = []

        bot._install_token_mask_filter()

        self.assertGreaterEqual(len(self.root.handlers), 1)
        for handler in self.root.handlers:
            self.assertTrue(
                any(isinstance(f, bot.TokenMaskFilter) for f in handler.filters))


if __name__ == "__main__":
    unittest.main()
