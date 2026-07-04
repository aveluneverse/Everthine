"""Tests for the M2 static composition layer: Layer 1 (identity declaration)
+ Layer 2 (the seven ground rules) + the boundaries embed. Conventions follow
tests/test_persona_loader.py. Personas are built directly via the dataclasses
(no filesystem needed); build_system_prompt() and its wiring into
compose_stable() are out of scope here (a later task does the wiring).

Two collision traps worth flagging for future readers of this file: (1)
DNA_RULES rule 3 itself contains the sentence "Their boundaries file may
list words..." -- so a bare `assertNotIn("Their boundaries", result)` would
fail even when the optional boundaries block is correctly omitted. Tests
below use the full heading "## Their boundaries, in their own words" (only
present when the block is actually appended) instead. (2) that same
boundaries heading also starts with "## ", same as the seven DNA rule
headings -- so heading counts are pinned with "^## \\d+. " (numbered
headings only) rather than a bare "^## ".
"""
import re
import unittest

from everthine.layers import (
    BOUNDARIES_TEMPLATE,
    DECLARATION_TEMPLATE,
    DNA_RULES,
    LIVING_LINE_LONG_DISTANCE,
    LIVING_LINE_TOGETHER,
    compose_stable,
)
from everthine.persona import Persona, PersonaSettings

IDENTITY_TEXT = ("Ledger-keeper by day, storyteller by night, always half a "
                  "page ahead in the book on the nightstand.")
VOICE_TEXT = "Short sentences. Warm and a little wry, never flowery."
BOUNDARIES_TEXT = "Never bring up the accident from last spring."


def _persona(*, identity_text=IDENTITY_TEXT, voice_text="", boundaries_text="",
             companion_name="Alex", partner_name="Sam", living="together"):
    settings = PersonaSettings(
        companion_name=companion_name, partner_name=partner_name, living=living)
    return Persona(
        mode="folder",
        identity_text=identity_text,
        voice_text=voice_text,
        boundaries_text=boundaries_text,
        settings=settings,
    )


class TestBlockOrder(unittest.TestCase):
    def test_declaration_identity_voice_dna_boundaries_order(self):
        persona = _persona(voice_text=VOICE_TEXT, boundaries_text=BOUNDARIES_TEXT)
        result = compose_stable(persona)
        i_declaration = result.index("# Who you are")
        i_identity = result.index(IDENTITY_TEXT)
        i_voice = result.index(VOICE_TEXT)
        i_dna = result.index("# The ground rules")
        i_boundaries = result.index("## Their boundaries, in their own words")
        self.assertLess(i_declaration, i_identity)
        self.assertLess(i_identity, i_voice)
        self.assertLess(i_voice, i_dna)
        self.assertLess(i_dna, i_boundaries)


class TestNamesEmbedded(unittest.TestCase):
    def test_names_filled_and_no_placeholder_residue(self):
        persona = _persona(companion_name="Alex", partner_name="Sam")
        result = compose_stable(persona)
        expected_declaration = DECLARATION_TEMPLATE.format(
            companion_name="Alex", partner_name="Sam")
        self.assertIn(expected_declaration, result)
        self.assertNotIn("{companion_name}", result)
        self.assertNotIn("{partner_name}", result)


class TestVoiceAndBoundariesOmission(unittest.TestCase):
    def test_empty_voice_leaves_no_stray_blank_block(self):
        persona = _persona(voice_text="")
        result = compose_stable(persona)
        self.assertNotIn("\n\n\n", result)

    def test_empty_boundaries_block_omitted(self):
        persona = _persona(boundaries_text="")
        result = compose_stable(persona)
        self.assertNotIn("## Their boundaries, in their own words", result)

    def test_present_voice_and_boundaries_are_included(self):
        persona = _persona(voice_text=VOICE_TEXT, boundaries_text=BOUNDARIES_TEXT)
        result = compose_stable(persona)
        self.assertIn(VOICE_TEXT, result)
        self.assertIn("## Their boundaries, in their own words", result)
        self.assertIn(BOUNDARIES_TEXT, result)
        self.assertNotIn("\n\n\n", result)


class TestLivingFrame(unittest.TestCase):
    def test_together_present_long_distance_absent(self):
        persona = _persona(living="together")
        result = compose_stable(persona)
        self.assertIn(LIVING_LINE_TOGETHER, result)
        self.assertNotIn(LIVING_LINE_LONG_DISTANCE, result)
        self.assertNotIn("{living_line}", result)

    def test_long_distance_present_together_absent(self):
        persona = _persona(living="long_distance")
        result = compose_stable(persona)
        self.assertIn(LIVING_LINE_LONG_DISTANCE, result)
        self.assertNotIn(LIVING_LINE_TOGETHER, result)
        self.assertNotIn("{living_line}", result)


class TestDNAIntegrity(unittest.TestCase):
    def test_exactly_seven_numbered_rule_headings(self):
        headings = re.findall(r"^## \d+\. ", DNA_RULES, flags=re.M)
        self.assertEqual(len(headings), 7)

    def test_numbered_headings_survive_full_composition(self):
        persona = _persona(voice_text=VOICE_TEXT, boundaries_text=BOUNDARIES_TEXT)
        result = compose_stable(persona)
        headings = re.findall(r"^## \d+\. ", result, flags=re.M)
        self.assertEqual(len(headings), 7)

    def test_approved_wording_pin(self):
        self.assertIn("need care, not a fact-check", DNA_RULES)


class TestDeterminism(unittest.TestCase):
    def test_two_calls_same_persona_are_identical(self):
        persona = _persona(voice_text=VOICE_TEXT, boundaries_text=BOUNDARIES_TEXT)
        self.assertEqual(compose_stable(persona), compose_stable(persona))


class TestFileModeRejected(unittest.TestCase):
    def test_file_mode_persona_raises(self):
        persona = Persona(mode="file", raw_text="You are Testbot.")
        with self.assertRaises(ValueError):
            compose_stable(persona)


if __name__ == "__main__":
    unittest.main()
