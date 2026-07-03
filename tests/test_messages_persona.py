import unittest
from pathlib import Path

from everthine import messages
from everthine.config import Config
from everthine.persona import DEFAULT_PERSONA, build_system_prompt

CFG = Config(bot_token="x", authorized_user_id=1, persona_path=Path("does/not/exist.md"))


class TestMessages(unittest.TestCase):
    def test_known_keys_exist(self):
        for key in ("busy", "generic_glitch", "timeout",
                    "cli_missing", "auth", "nonzero", "notebook_full",
                    "btn_resume", "btn_warm", "btn_clean",
                    "btn_cancel", "cancel_ack", "thinking",
                    "start_fresh", "start_has_session", "warm_ack", "clean_ack", "resume_ack"):
            self.assertTrue(messages.msg(key))

    def test_unauthorized_silence_is_intentionally_empty(self):
        self.assertEqual(messages.msg("unauthorized_silence"), "")

    def test_unknown_key_falls_back(self):
        self.assertEqual(messages.msg("no-such-key"), messages.msg("generic_glitch"))


class TestPersona(unittest.TestCase):
    def test_missing_file_uses_default(self):
        self.assertEqual(build_system_prompt(CFG), DEFAULT_PERSONA)

    def test_persona_file_wins(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            p.write_text("You are Testbot.\n", encoding="utf-8")
            cfg = Config(bot_token="x", authorized_user_id=1, persona_path=p)
            self.assertEqual(build_system_prompt(cfg), "You are Testbot.")


if __name__ == "__main__":
    unittest.main()
