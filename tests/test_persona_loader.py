"""Tests for the M2 persona loading layer: folder-vs-file detection,
fail-loud settings.yaml validation, and file-mode passthrough. Conventions
follow tests/test_config.py and tests/test_messages_persona.py.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from everthine.config import Config, ConfigError
from everthine.persona import Persona, PersonaSettings, load_persona

VALID_SETTINGS_YAML = """\
companion:
  name: Alex
  birthday: 1993-06-14
partner:
  name: Sam
  birthday: 1995-02-11
relationship:
  anniversary: 2025-11-03
  living: together
  reunion_response: gentle
lines:
  busy: "One second - still finishing the last thought."
  thinking:
    - "...thumb resting on the page, thinking."
    - "...gone quiet for a moment, sorting the words."
"""

IDENTITY_TEXT = "I am Alex: warm, steady, and endlessly curious about Sam's day.\n"


def _cfg(persona_path: Path) -> Config:
    return Config(bot_token="x", authorized_user_id=1, persona_path=persona_path)


def _write_folder(root: Path, *, identity=IDENTITY_TEXT, settings=VALID_SETTINGS_YAML,
                   voice=None, boundaries=None, skip_identity=False, skip_settings=False):
    if not skip_identity:
        (root / "identity.md").write_text(identity, encoding="utf-8")
    if not skip_settings:
        (root / "settings.yaml").write_text(settings, encoding="utf-8")
    if voice is not None:
        (root / "voice.md").write_text(voice, encoding="utf-8")
    if boundaries is not None:
        (root / "boundaries.md").write_text(boundaries, encoding="utf-8")


class TestModeDetection(unittest.TestCase):
    def test_folder_path_is_folder_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root)
            persona = load_persona(_cfg(root))
            self.assertEqual(persona.mode, "folder")

    def test_file_path_is_file_mode(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            p.write_text("You are Testbot.\n", encoding="utf-8")
            persona = load_persona(_cfg(p))
            self.assertEqual(persona.mode, "file")


class TestIdentityRequired(unittest.TestCase):
    def test_missing_identity_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, skip_identity=True)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("identity.md", str(cm.exception))

    def test_blank_identity_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, identity="   \n\t \n")
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("identity.md", str(cm.exception))

    def test_identity_with_bom_loads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, skip_identity=True)
            (root / "identity.md").write_bytes(b"\xef\xbb\xbf" + IDENTITY_TEXT.encode("utf-8"))
            persona = load_persona(_cfg(root))
            self.assertEqual(persona.identity_text, IDENTITY_TEXT.strip())
            self.assertFalse(persona.identity_text.startswith("\ufeff"))


class TestSettingsRequired(unittest.TestCase):
    def test_missing_settings_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, skip_settings=True)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("settings.yaml", str(cm.exception))

    def test_invalid_yaml_syntax_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings="companion:\n\tname: Alex\n")
            with self.assertRaises(ConfigError):
                load_persona(_cfg(root))

    def test_top_level_not_a_mapping_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings="- just\n- a\n- list\n")
            with self.assertRaises(ConfigError):
                load_persona(_cfg(root))

    def test_companion_section_not_a_mapping_raises(self):
        # A plausible real-world typo: `companion: Alex` instead of nesting
        # `name:` under it. Must fail loud with a ConfigError, not an
        # uncaught AttributeError from treating a string like a mapping.
        settings = "companion: Alex\npartner:\n  name: Sam\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("companion", str(cm.exception))

    def test_lines_not_a_mapping_raises(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    "lines: nope\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("lines", str(cm.exception))


class TestNames(unittest.TestCase):
    def test_companion_name_missing_raises(self):
        settings = "partner:\n  name: Sam\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("companion.name", str(cm.exception))

    def test_companion_name_blank_raises(self):
        settings = 'companion:\n  name: "   "\npartner:\n  name: Sam\n'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("companion.name", str(cm.exception))

    def test_partner_name_missing_raises(self):
        settings = "companion:\n  name: Alex\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("partner.name", str(cm.exception))


class TestDates(unittest.TestCase):
    def test_malformed_bare_date_raises(self):
        settings = ("companion:\n  name: Alex\n  birthday: 2025-13-40\n"
                    "partner:\n  name: Sam\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("companion.birthday", str(cm.exception))

    def test_malformed_string_date_raises(self):
        settings = ('companion:\n  name: Alex\n  birthday: "not-a-date"\n'
                    "partner:\n  name: Sam\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("companion.birthday", str(cm.exception))

    def test_datetime_with_time_part_raises(self):
        settings = ("companion:\n  name: Alex\n  birthday: 1993-06-14T10:00:00\n"
                    "partner:\n  name: Sam\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("companion.birthday", str(cm.exception))

    def test_valid_dates_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root)
            persona = load_persona(_cfg(root))
            self.assertEqual(persona.settings.companion_birthday, date(1993, 6, 14))
            self.assertEqual(persona.settings.partner_birthday, date(1995, 2, 11))
            self.assertEqual(persona.settings.anniversary, date(2025, 11, 3))


class TestEnums(unittest.TestCase):
    def test_living_invalid_raises(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    "relationship:\n  living: married\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("relationship.living", str(cm.exception))

    def test_living_as_list_raises_configerror(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    "relationship:\n  living:\n    - together\n    - long_distance\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("relationship.living", str(cm.exception))

    def test_reunion_response_invalid_raises(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    "relationship:\n  reunion_response: ecstatic\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("relationship.reunion_response", str(cm.exception))

    def test_reunion_response_as_list_raises_configerror(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    "relationship:\n  reunion_response:\n    - gentle\n    - expressive\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("relationship.reunion_response", str(cm.exception))

    def test_defaults_when_relationship_section_absent(self):
        settings = "companion:\n  name: Alex\npartner:\n  name: Sam\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            persona = load_persona(_cfg(root))
            self.assertEqual(persona.settings.living, "together")
            self.assertEqual(persona.settings.reunion_response, "gentle")

    def test_reunion_response_expressive_and_neutral_allowed(self):
        for value in ("expressive", "neutral"):
            settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                        f"relationship:\n  reunion_response: {value}\n")
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _write_folder(root, settings=settings)
                persona = load_persona(_cfg(root))
                self.assertEqual(persona.settings.reunion_response, value)


class TestLinesSecurity(unittest.TestCase):
    def test_unauthorized_silence_forbidden(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    'lines:\n  unauthorized_silence: "nope"\n')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("unauthorized_silence", str(cm.exception))

    def test_cli_missing_forbidden(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    'lines:\n  cli_missing: "nope"\n')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("cli_missing", str(cm.exception))


class TestLinesUnknownKey(unittest.TestCase):
    def test_unknown_key_warns_and_is_ignored(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    'lines:\n  bananas: "nope"\n  busy: "Override busy."\n')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertLogs(logger="everthine", level="WARNING") as cm:
                persona = load_persona(_cfg(root))
            self.assertTrue(any("bananas" in line for line in cm.output))
            self.assertEqual(persona.settings.lines["busy"], "Override busy.")
            self.assertNotIn("bananas", persona.settings.lines)

    def test_unknown_top_level_key_warns_and_is_ignored(self):
        settings = "companion:\n  name: Alex\npartner:\n  name: Sam\nfuture_field: 1\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertLogs(logger="everthine", level="WARNING") as cm:
                load_persona(_cfg(root))
            self.assertTrue(any("future_field" in line for line in cm.output))


class TestThinking(unittest.TestCase):
    def test_thinking_not_a_list_raises(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    'lines:\n  thinking: "nope"\n')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("thinking", str(cm.exception))

    def test_thinking_empty_list_raises(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    "lines:\n  thinking: []\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError):
                load_persona(_cfg(root))

    def test_thinking_list_with_blank_string_raises(self):
        settings = ("companion:\n  name: Alex\npartner:\n  name: Sam\n"
                    'lines:\n  thinking:\n    - "ok"\n    - "   "\n')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, settings=settings)
            with self.assertRaises(ConfigError):
                load_persona(_cfg(root))

    def test_valid_thinking_stored_separately_from_lines(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root)
            persona = load_persona(_cfg(root))
            self.assertEqual(
                persona.settings.thinking,
                ["...thumb resting on the page, thinking.",
                 "...gone quiet for a moment, sorting the words."])
            self.assertNotIn("thinking", persona.settings.lines)


class TestVoiceAndBoundaries(unittest.TestCase):
    def test_absent_default_to_empty_string(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root)
            persona = load_persona(_cfg(root))
            self.assertEqual(persona.voice_text, "")
            self.assertEqual(persona.boundaries_text, "")

    def test_present_content_is_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root, voice="Speak softly and slowly.\n",
                           boundaries="Never discuss the weather.\n")
            persona = load_persona(_cfg(root))
            self.assertEqual(persona.voice_text, "Speak softly and slowly.")
            self.assertEqual(persona.boundaries_text, "Never discuss the weather.")

    def test_present_but_undecodable_voice_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_folder(root)
            (root / "voice.md").write_bytes(b"\xff\xfe\xfa\xfb")
            with self.assertRaises(ConfigError) as cm:
                load_persona(_cfg(root))
            self.assertIn("voice.md", str(cm.exception))


class TestStagesFile(unittest.TestCase):
    """M4 T3: the optional stages.md file. Conventions follow
    tests/test_bot_persona_wiring.py's setUp (a TemporaryDirectory kept
    alive for the whole test method via addCleanup) since, unlike the
    with-block fixtures above, the returned Config must still be valid
    after the helper method itself returns.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def _folder_without_stages(self) -> Config:
        _write_folder(self.root)
        return _cfg(self.root)

    def _folder_with_stages(self, text: str) -> Config:
        _write_folder(self.root)
        (self.root / "stages.md").write_text(text, encoding="utf-8")
        return _cfg(self.root)

    def test_absent_file_is_none(self):
        persona = load_persona(self._folder_without_stages())
        self.assertIsNone(persona.stages)

    def test_sections_parse_in_order_verbatim(self):
        p = self._folder_with_stages(
            "# My stages\n\n## Settling in\n\ncalm text\nsecond line\n\n"
            "## Deep water\n\ndeep text\n")
        persona = load_persona(p)
        self.assertEqual(persona.stages, (
            ("Settling in", "calm text\nsecond line"),
            ("Deep water", "deep text")))

    def test_empty_section_body_fails_loud(self):
        p = self._folder_with_stages("## Settling in\n\n## Deep water\n\ntext\n")
        with self.assertRaises(ConfigError) as ctx:
            load_persona(p)
        self.assertIn("Settling in", str(ctx.exception))

    def test_duplicate_names_fail_loud(self):
        p = self._folder_with_stages("## Same\n\na\n\n## Same\n\nb\n")
        with self.assertRaises(ConfigError):
            load_persona(p)

    def test_no_sections_fails_loud(self):
        p = self._folder_with_stages("just prose, no headings\n")
        with self.assertRaises(ConfigError):
            load_persona(p)

    def test_title_only_file_fails_loud(self):
        p = self._folder_with_stages("# My stages\n")
        with self.assertRaises(ConfigError):
            load_persona(p)

    def test_empty_section_name_fails_loud(self):
        p = self._folder_with_stages("##   \n\ntext\n")
        with self.assertRaises(ConfigError):
            load_persona(p)

    def test_empty_stages_file_is_none(self):
        # An empty/whitespace-only optional file counts as absent (same
        # tolerance as voice/boundaries), not as a broken one.
        p = self._folder_with_stages("   \n")
        self.assertIsNone(load_persona(p).stages)


class TestFileMode(unittest.TestCase):
    def test_existing_file_raw_text_is_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            p.write_text("  Hello there.  \n", encoding="utf-8")
            persona = load_persona(_cfg(p))
            self.assertEqual(persona.mode, "file")
            self.assertEqual(persona.raw_text, "Hello there.")
            self.assertEqual(persona.identity_text, "")
            self.assertIsNone(persona.settings)

    def test_missing_file_raw_text_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "does-not-exist.md"
            persona = load_persona(_cfg(p))
            self.assertEqual(persona.mode, "file")
            self.assertIsNone(persona.raw_text)

    def test_empty_file_raw_text_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.md"
            p.write_text("   \n", encoding="utf-8")
            persona = load_persona(_cfg(p))
            self.assertIsNone(persona.raw_text)

    def test_undecodable_file_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.md"
            p.write_bytes(b"\xff\xfe\xfa\xfb")
            persona = load_persona(_cfg(p))  # must not raise
            self.assertEqual(persona.mode, "file")
            self.assertIsNone(persona.raw_text)


if __name__ == "__main__":
    unittest.main()
