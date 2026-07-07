"""Tests for the LOG_LEVEL env knob (M7 whole-branch final review fix).

M7's named-skip design routes the routine reasons a tick stays quiet --
quiet hours, a missed dice roll, an active cooldown, a still-active partner
window -- to DEBUG, so "why didn't he say anything just now" has an answer.
That answer is invisible at the production default (INFO) unless something
can raise the root logger's level without a code change. LOG_LEVEL is that
switch.

Two functions split the concern:

  - _resolve_log_level (TestResolveLogLevel): a plain, I/O-free function
    that turns a raw env string into (numeric level, bad_value). No global
    state, so these tests never touch real logging.

  - _apply_log_level (TestApplyLogLevelWiring): the thin wrapper main()
    actually calls, which sets the ROOT logger's level and, on an
    unrecognized value, logs one warning naming it. Exercised against the
    real root logger the same way test_token_mask.py's
    TestInstallTokenMaskFilterWiring exercises _install_token_mask_filter:
    save the current level, restore it in addCleanup, never leave global
    logging state behind for later tests.
"""
import logging
import unittest

from everthine import bot


class TestResolveLogLevel(unittest.TestCase):
    """Pure function: no logging side effects, so no setUp/cleanup needed."""

    def test_valid_level_name_is_case_insensitive(self):
        for raw in ("DEBUG", "debug", "Debug", "dEbUg"):
            with self.subTest(raw=raw):
                level, bad = bot._resolve_log_level(raw)
                self.assertEqual(level, logging.DEBUG)
                self.assertIsNone(bad)

    def test_other_real_level_names_resolve(self):
        for raw, expected in (("WARNING", logging.WARNING),
                              ("ERROR", logging.ERROR),
                              ("CRITICAL", logging.CRITICAL),
                              ("INFO", logging.INFO)):
            with self.subTest(raw=raw):
                level, bad = bot._resolve_log_level(raw)
                self.assertEqual(level, expected)
                self.assertIsNone(bad)

    def test_unset_or_blank_defaults_to_info_quietly(self):
        # None (var truly unset) and "" / whitespace-only (var set but
        # blank) all resolve to the default with no bad_value -- the same
        # "absent means default, not an error" convention config.py's own
        # _get_bool / _get_int apply to every other env var in this project.
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                level, bad = bot._resolve_log_level(raw)
                self.assertEqual(level, logging.INFO)
                self.assertIsNone(bad)

    def test_unrecognized_value_falls_back_to_info_and_is_reported(self):
        level, bad = bot._resolve_log_level("NOT_A_LEVEL")
        self.assertEqual(level, logging.INFO)
        self.assertEqual(bad, "NOT_A_LEVEL")

    def test_non_int_attribute_match_is_not_mistaken_for_a_level(self):
        # Defensive guard: getattr(logging, name, None) finding SOMETHING is
        # not enough on its own -- it must also be an int level. Every real
        # all-caps attribute the logging module actually has today happens
        # to be an int level, so this patches in a fake counter-example
        # rather than relying on one existing by accident.
        logging.NOTALEVEL = "not-an-int"
        self.addCleanup(delattr, logging, "NOTALEVEL")
        level, bad = bot._resolve_log_level("NotALevel")
        self.assertEqual(level, logging.INFO)
        self.assertEqual(bad, "NotALevel")


class TestApplyLogLevelWiring(unittest.TestCase):
    """_apply_log_level is what main() actually calls: resolve, then set the
    ROOT logger's level, then warn if the value was no good. Runs against
    the real root logger, saved and restored around every test.
    """

    def setUp(self):
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)

    def test_debug_sets_the_root_logger_level(self):
        bot._apply_log_level("DEBUG")
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_unset_sets_the_root_logger_to_info(self):
        bot._apply_log_level(None)
        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_bad_value_falls_back_to_info_and_warns_with_the_value_named(self):
        with self.assertLogs("everthine", level="WARNING") as cm:
            bot._apply_log_level("NOT_A_LEVEL")
        self.assertEqual(logging.getLogger().level, logging.INFO)
        self.assertTrue(any("NOT_A_LEVEL" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
