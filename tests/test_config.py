import unittest

from everthine.config import Config, ConfigError, load_config

BASE = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}


class TestConfig(unittest.TestCase):
    def test_minimal_env_loads(self):
        cfg = load_config(BASE)
        self.assertEqual(cfg.authorized_user_id, 42)
        self.assertEqual(cfg.claude_cmd, ["claude"])
        self.assertIsNone(cfg.claude_model)
        self.assertEqual(cfg.command_timeout_s, 300)
        self.assertTrue(cfg.injection_enabled)
        self.assertEqual(cfg.lookback_hours, 36)
        self.assertEqual(cfg.injection_max_chars, 12000)

    def test_missing_token_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"AUTHORIZED_USER_ID": "42"})

    def test_bad_user_id_raises(self):
        with self.assertRaises(ConfigError):
            load_config({**BASE, "AUTHORIZED_USER_ID": "not-a-number"})

    def test_overrides(self):
        cfg = load_config({**BASE, "CLAUDE_MODEL": "opus", "COMMAND_TIMEOUT_S": "120",
                           "CLAUDE_CMD": "claude-custom --flag",
                           "INJECTION_ENABLED": "false"})
        self.assertEqual(cfg.claude_model, "opus")
        self.assertEqual(cfg.command_timeout_s, 120)
        self.assertEqual(cfg.claude_cmd, ["claude-custom", "--flag"])
        self.assertFalse(cfg.injection_enabled)


if __name__ == "__main__":
    unittest.main()
