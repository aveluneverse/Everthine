"""Tests for the M2 message-override layer: load_overrides()/reset_overrides()
feeding msg(), plus thinking_line()'s deterministic rotation. Conventions
follow tests/test_messages_persona.py and tests/test_persona_loader.py.

Global state note: the override dict, the thinking list, and the rotation
counter are module-level (mirroring persona.py's own cache pattern), so every
test here resets them in setUp/tearDown -- this file runs inside the same
process as every other test file (python -m unittest discover), and a leaked
override would silently break an unrelated module's built-in-string
assertions (tests/test_messages_persona.py in particular pins several exact
default strings).
"""
import unittest

from everthine import messages


class _OverrideResetTest(unittest.TestCase):
    def setUp(self):
        messages.reset_overrides()

    def tearDown(self):
        messages.reset_overrides()


# --- 1. Override a known key; unrelated keys unaffected -------------------

class TestOverrideKnownKey(_OverrideResetTest):
    def test_override_returns_persona_line(self):
        messages.load_overrides({"busy": "Hang on, love - one more second."})
        self.assertEqual(messages.msg("busy"), "Hang on, love - one more second.")

    def test_unrelated_keys_unaffected(self):
        messages.load_overrides({"busy": "Hang on, love - one more second."})
        self.assertEqual(messages.msg("cancel_ack"), "Alright - never mind.")
        self.assertEqual(messages.msg("thinking"), "...")


# --- 2. reset_overrides restores defaults ----------------------------------

class TestResetOverrides(_OverrideResetTest):
    def test_reset_restores_builtin(self):
        messages.load_overrides({"busy": "Hang on, love."})
        self.assertNotEqual(
            messages.msg("busy"),
            "One moment - I'm still finishing my previous thought.")
        messages.reset_overrides()
        self.assertEqual(
            messages.msg("busy"),
            "One moment - I'm still finishing my previous thought.")


# --- 3. load_overrides replaces, never merges ------------------------------

class TestLoadReplacesNotMerges(_OverrideResetTest):
    def test_second_load_drops_keys_only_the_first_load_set(self):
        messages.load_overrides({"busy": "first busy", "cancel_ack": "first cancel"})
        messages.load_overrides({"busy": "second busy"})
        self.assertEqual(messages.msg("busy"), "second busy")
        # cancel_ack was only in the FIRST load; the second load must not
        # merge with it -- it must fall back to the built-in.
        self.assertEqual(messages.msg("cancel_ack"), "Alright - never mind.")


# --- 4. Defense in depth: forbidden keys never survive load_overrides -----

class TestDefenseInDepthForbiddenKeys(_OverrideResetTest):
    def test_unauthorized_silence_dropped_with_warning(self):
        with self.assertLogs("everthine", level="WARNING") as cm:
            messages.load_overrides({"unauthorized_silence": "hi"})
        self.assertTrue(any("unauthorized_silence" in line for line in cm.output))
        self.assertEqual(messages.msg("unauthorized_silence"), "")

    def test_cli_missing_dropped_with_warning(self):
        with self.assertLogs("everthine", level="WARNING") as cm:
            messages.load_overrides({"cli_missing": "nope, all fine here"})
        self.assertTrue(any("cli_missing" in line for line in cm.output))
        self.assertEqual(messages.msg("cli_missing"),
                         "I can't find the Claude Code CLI on this machine.")

    def test_both_forbidden_keys_dropped_other_keys_survive(self):
        with self.assertLogs("everthine", level="WARNING"):
            messages.load_overrides({
                "unauthorized_silence": "hi",
                "cli_missing": "nope",
                "busy": "still allowed through",
            })
        self.assertEqual(messages.msg("unauthorized_silence"), "")
        self.assertEqual(messages.msg("cli_missing"),
                         "I can't find the Claude Code CLI on this machine.")
        self.assertEqual(messages.msg("busy"), "still allowed through")


# --- 5. thinking_line without a loaded list --------------------------------

class TestThinkingLineNoList(_OverrideResetTest):
    def test_no_list_loaded_returns_builtin_thinking(self):
        self.assertEqual(messages.thinking_line(), "...")
        self.assertEqual(messages.thinking_line(), messages.msg("thinking"))

    def test_lines_only_load_leaves_thinking_on_builtin(self):
        # Loading ordinary line overrides (no thinking arg at all) must not
        # disturb the "..." placeholder -- the two are independent channels.
        messages.load_overrides({"busy": "x"})
        self.assertEqual(messages.thinking_line(), "...")


# --- 6. thinking_line rotation, deterministic + reset semantics -----------

class TestThinkingLineRotation(_OverrideResetTest):
    def test_rotates_in_order_and_wraps(self):
        messages.load_overrides({}, ["a", "b"])
        self.assertEqual(messages.thinking_line(), "a")
        self.assertEqual(messages.thinking_line(), "b")
        self.assertEqual(messages.thinking_line(), "a")
        self.assertEqual(messages.thinking_line(), "b")

    def test_counter_resets_on_load_overrides(self):
        messages.load_overrides({}, ["a", "b"])
        messages.thinking_line()  # consumes "a" -> counter now points at "b"
        messages.load_overrides({}, ["x", "y"])  # fresh load rewinds it
        self.assertEqual(messages.thinking_line(), "x")

    def test_counter_resets_on_reset_overrides(self):
        messages.load_overrides({}, ["a", "b"])
        messages.thinking_line()  # consumes "a"
        messages.reset_overrides()
        messages.load_overrides({}, ["a", "b"])
        self.assertEqual(messages.thinking_line(), "a")

    def test_single_item_list_repeats_itself(self):
        messages.load_overrides({}, ["only one"])
        self.assertEqual(messages.thinking_line(), "only one")
        self.assertEqual(messages.thinking_line(), "only one")


# --- 7. Unknown key still falls back to generic_glitch ---------------------

class TestUnknownKeyFallsBack(_OverrideResetTest):
    def test_unknown_key_returns_generic_glitch(self):
        self.assertEqual(messages.msg("no-such-key"), messages.msg("generic_glitch"))

    def test_unknown_key_unaffected_by_active_overrides(self):
        messages.load_overrides({"busy": "x"})
        self.assertEqual(messages.msg("no-such-key"), messages.msg("generic_glitch"))


# --- 8. M4 stage/album keys: all 17 pinned verbatim, placeholders usable ---

class TestStageAlbumKeys(_OverrideResetTest):
    def test_new_keys_exist_with_exact_values(self):
        self.assertEqual(messages.msg("cmd_stage_desc"), "Where the two of you are")
        self.assertEqual(messages.msg("cmd_album_desc"), "Moments you both kept")
        self.assertEqual(messages.msg("stage_intro"), "You are here: {stage}")
        self.assertEqual(messages.msg("btn_stage_advance"), "Take a step forward")
        self.assertEqual(messages.msg("btn_stage_retreat"), "Step back one stage")
        self.assertEqual(messages.msg("btn_stage_close"), "Close")
        self.assertEqual(messages.msg("stage_note_prompt"),
                         "A word to mark this step? The next message you "
                         "send is kept beside it, word for word -- it goes "
                         "to the record, not into the chat. Or skip below.")
        self.assertEqual(messages.msg("btn_note_skip"), "No note -- step forward")
        self.assertEqual(messages.msg("btn_note_cancel"), "Cancel this step")
        self.assertEqual(messages.msg("stage_advanced_ack"), "You are now at: {stage}")
        self.assertEqual(messages.msg("note_saved_ack"),
                         "Kept beside this step, word for word.")
        self.assertEqual(messages.msg("stage_retreat_confirm"), 'Step back to "{stage}"?')
        self.assertEqual(messages.msg("btn_retreat_yes"), "Yes, step back")
        self.assertEqual(messages.msg("btn_retreat_no"), "Stay here")
        self.assertEqual(messages.msg("stage_retreated_ack"), "Back at: {stage}")
        self.assertEqual(messages.msg("album_expired"),
                         "That message is too old for me to reach now - it was not kept.")
        self.assertEqual(messages.msg("album_empty"),
                         "Nothing kept yet. React with a heart to keep a moment.")

    def test_placeholder_keys_contain_and_format_stage(self):
        # Catalog's first placeholder keys: pin that "{stage}" survives
        # verbatim in the default AND that str.format() actually works on it
        # (a stray brace or typo'd field name would pass the exact-value
        # check above but blow up at call time).
        for key in ("stage_intro", "stage_advanced_ack",
                    "stage_retreat_confirm", "stage_retreated_ack"):
            with self.subTest(key=key):
                self.assertIn("{stage}", messages.msg(key))
                formatted = messages.msg(key).format(stage="X")
                self.assertNotIn("{stage}", formatted)
                self.assertIn("X", formatted)
        self.assertEqual(messages.msg("stage_advanced_ack").format(stage="X"),
                         "You are now at: X")


if __name__ == "__main__":
    unittest.main()
