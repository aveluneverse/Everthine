"""Tests for M2's bot-startup persona wiring: make_app(cfg) now calls
persona.init(cfg) + messages.load_overrides(*persona.line_overrides(cfg))
before building the PTB Application, so a persona's re-voiced lines and
thinking rotation are active for every reply this app instance sends -- and a
broken persona folder fails at BOOT (app-build time), not mid-reply.

Conventions follow tests/test_bot_stream.py (bare Config, offline make_app,
FakeCommandApp for register_commands) and tests/test_persona_assembly_wiring.py
(tmp persona folder fixtures, module-cache reset in setUp/tearDown).

Global state note: the persona cache (persona.py) and the message-override
store (messages.py) are both process-global. make_app's new wiring populates
both, so every test class here resets both in setUp AND tearDown -- not just
one -- so test order never matters, both within this file and across the
whole suite (alphabetically, this module sorts before test_messages_persona.py
and test_messages_overrides.py; a leaked override here would otherwise break
their built-in-string assertions).
"""
import inspect
import tempfile
import unittest
from pathlib import Path

from everthine import bot, memory_recall, messages, persona
from everthine.config import Config, ConfigError

IDENTITY_TEXT = "I am Alex: warm, steady, and endlessly curious about Sam's day.\n"

SETTINGS_YAML_WITH_LINES = """\
companion:
  name: Alex
partner:
  name: Sam
lines:
  busy: "One second, love - still finishing that thought."
  thinking:
    - "...turning that over."
    - "...still with you, one moment."
"""

SETTINGS_YAML_NO_LINES = """\
companion:
  name: Alex
partner:
  name: Sam
"""

SETTINGS_YAML_CMD_START_DESC = """\
companion:
  name: Alex
partner:
  name: Sam
lines:
  cmd_start_desc: "Ring the bell"
"""


def _write_folder(root: Path, *, settings: str, skip_identity: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if not skip_identity:
        (root / "identity.md").write_text(IDENTITY_TEXT, encoding="utf-8")
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    return root


class _GlobalStateResetTest(unittest.TestCase):
    """Base for any test that calls make_app: clears the persona cache and
    the message overrides before AND after every test."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        # make_app opens the memory store (M3 T5); close it before the tmp
        # dir deletes -- registered after the tmp cleanup, so LIFO runs it
        # first, releasing the sqlite handle Windows would otherwise hold.
        self.addCleanup(memory_recall.reset)
        self.root = Path(self._td.name)
        persona.reset_persona_cache()
        messages.reset_overrides()

    def tearDown(self):
        persona.reset_persona_cache()
        messages.reset_overrides()


# --- 8. FILE persona: defaults intact --------------------------------------

class TestFilePersonaLeavesDefaultsIntact(_GlobalStateResetTest):
    def test_defaults_intact_with_file_persona(self):
        cfg = Config(bot_token="x", authorized_user_id=1,
                     persona_path=self.root / "does-not-exist.md",
                     data_dir=self.root / "data")
        bot.make_app(cfg)
        self.assertEqual(messages.msg("busy"),
                         "One moment - I'm still finishing my previous thought.")
        self.assertEqual(messages.thinking_line(), "...")


# --- 9. FOLDER persona: overrides + thinking rotation active ---------------

class TestFolderPersonaLoadsOverrides(_GlobalStateResetTest):
    def test_busy_and_thinking_overridden_by_folder_persona(self):
        folder = _write_folder(self.root / "persona", settings=SETTINGS_YAML_WITH_LINES)
        cfg = Config(bot_token="x", authorized_user_id=1,
                     persona_path=folder, data_dir=self.root / "data")
        bot.make_app(cfg)
        self.assertEqual(messages.msg("busy"),
                         "One second, love - still finishing that thought.")
        self.assertEqual(messages.thinking_line(), "...turning that over.")
        self.assertEqual(messages.thinking_line(), "...still with you, one moment.")
        self.assertEqual(messages.thinking_line(), "...turning that over.")

    def test_folder_persona_with_no_lines_section_leaves_defaults_intact(self):
        # The brief's legacy guarantee explicitly covers this case too: "with
        # a FILE persona (OR NO lines in settings), every message and the
        # thinking placeholder must behave exactly as today." A folder
        # persona that never sets a `lines:` section at all (SETTINGS_YAML_
        # NO_LINES) must be indistinguishable from file mode here.
        folder = _write_folder(self.root / "persona", settings=SETTINGS_YAML_NO_LINES)
        cfg = Config(bot_token="x", authorized_user_id=1,
                     persona_path=folder, data_dir=self.root / "data")
        bot.make_app(cfg)
        self.assertEqual(messages.msg("busy"),
                         "One moment - I'm still finishing my previous thought.")
        self.assertEqual(messages.thinking_line(), "...")


# --- 10. BROKEN folder persona: fail at boot -------------------------------

class TestBrokenFolderFailsAtBoot(_GlobalStateResetTest):
    def test_missing_identity_raises_configerror_at_app_build(self):
        folder = _write_folder(self.root / "persona", settings=SETTINGS_YAML_NO_LINES,
                               skip_identity=True)
        cfg = Config(bot_token="x", authorized_user_id=1,
                     persona_path=folder, data_dir=self.root / "data")
        with self.assertRaises(ConfigError):
            bot.make_app(cfg)
        # A failed init() must not leave a half-populated cache: load_persona
        # raises before the module-level slot is ever assigned.
        self.assertIsNone(persona._persona_cache)
        # And the messages layer must still be at its untouched defaults --
        # load_overrides() is never reached when persona.init() raises first.
        self.assertEqual(messages.msg("busy"),
                         "One moment - I'm still finishing my previous thought.")


# --- 11. Source-inspection pin: placeholder call site ----------------------

class TestPlaceholderSourcePin(unittest.TestCase):
    """Brittle-but-honest (per brief): a *behavioral* test cannot tell
    thinking_line() apart from the old msg("thinking") call it replaces, since
    both return "..." whenever no rotation list is loaded. Reading the source
    directly is the only way to pin that the streaming placeholder call site
    was actually switched over (and stays switched over in later tasks)."""

    def test_streaming_placeholder_calls_thinking_line_not_msg_thinking(self):
        source = inspect.getsource(bot)
        on_text_start = source.index("async def on_text(")
        self.assertIn("messages.thinking_line()", source[on_text_start:])
        self.assertNotIn('msg("thinking")', source)


# --- cmd_start_desc wiring test (brief section 4) --------------------------

class _FakeCommandBot:
    def __init__(self):
        self.set_my_commands_calls = []

    async def set_my_commands(self, commands):
        self.set_my_commands_calls.append(commands)


class _FakeCommandApp:
    def __init__(self):
        self.bot = _FakeCommandBot()


class TestCmdStartDescWiring(unittest.IsolatedAsyncioTestCase):
    """cmd_start_desc overrides must flow into the command-menu registration.
    make_app's startup wiring loads persona overrides before the app is even
    built -- well before PTB's post_init later calls register_commands -- so
    by the time register_commands runs, msg("cmd_start_desc") already
    reflects the persona's line. Proven end-to-end with the same
    FakeCommandApp pattern as tests/test_bot_stream.py's TestRegisterCommands.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        # make_app opens the memory store (M3 T5); close it before the tmp
        # dir deletes -- registered after the tmp cleanup, so LIFO runs it
        # first, releasing the sqlite handle Windows would otherwise hold.
        self.addCleanup(memory_recall.reset)
        self.root = Path(self._td.name)
        persona.reset_persona_cache()
        messages.reset_overrides()

    def tearDown(self):
        persona.reset_persona_cache()
        messages.reset_overrides()

    async def test_override_reaches_command_menu_registration(self):
        folder = _write_folder(self.root / "persona", settings=SETTINGS_YAML_CMD_START_DESC)
        cfg = Config(bot_token="x", authorized_user_id=1,
                     persona_path=folder, data_dir=self.root / "data")
        bot.make_app(cfg)  # startup wiring loads the override

        app = _FakeCommandApp()
        await bot.register_commands(app)
        commands = app.bot.set_my_commands_calls[0]
        self.assertEqual(commands[0].description, "Ring the bell")


if __name__ == "__main__":
    unittest.main()
