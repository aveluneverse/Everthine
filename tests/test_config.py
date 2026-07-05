import unittest
from pathlib import Path

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

    def test_bool_true_variants(self):
        for raw in ("1", "true", "YES", "On"):
            with self.subTest(raw=raw):
                cfg = load_config({**BASE, "INJECTION_ENABLED": raw})
                self.assertTrue(cfg.injection_enabled)

    def test_bool_false_variants(self):
        for raw in ("0", "false", "no", "OFF"):
            with self.subTest(raw=raw):
                cfg = load_config({**BASE, "INJECTION_ENABLED": raw})
                self.assertFalse(cfg.injection_enabled)

    def test_bool_absent_or_empty_uses_default(self):
        self.assertTrue(load_config(BASE).injection_enabled)
        self.assertTrue(load_config({**BASE, "INJECTION_ENABLED": ""}).injection_enabled)

    def test_bool_garbage_raises(self):
        with self.assertRaises(ConfigError):
            load_config({**BASE, "INJECTION_ENABLED": "enabled"})

    def test_bad_claude_cmd_raises(self):
        with self.assertRaises(ConfigError):
            load_config({**BASE, "CLAUDE_CMD": 'claude "unbalanced'})

    def test_derived_paths(self):
        cfg = load_config({**BASE, "DATA_DIR": "companion-data"})
        self.assertEqual(cfg.engine_home, Path("companion-data") / "engine")
        self.assertEqual(cfg.archive_dir, Path("companion-data") / "archive")
        self.assertEqual(cfg.session_path, Path("companion-data") / "session.json")

    def test_streaming_defaults(self):
        cfg = load_config(BASE)
        self.assertTrue(cfg.streaming_enabled)
        self.assertEqual(cfg.stream_stall_timeout_s, 60)
        self.assertEqual(cfg.stream_total_timeout_s, 1200)

    def test_streaming_overrides(self):
        cfg = load_config({**BASE, "STREAMING_ENABLED": "false",
                           "STREAM_STALL_TIMEOUT_S": "30",
                           "STREAM_TOTAL_TIMEOUT_S": "45"})
        self.assertFalse(cfg.streaming_enabled)
        self.assertEqual(cfg.stream_stall_timeout_s, 30)
        self.assertEqual(cfg.stream_total_timeout_s, 45)

    def test_non_positive_ints_raise(self):
        for key in ("COMMAND_TIMEOUT_S", "LOOKBACK_HOURS",
                    "INJECTION_MAX_CHARS", "STREAM_STALL_TIMEOUT_S",
                    "STREAM_TOTAL_TIMEOUT_S"):
            for bad in ("0", "-5"):
                with self.assertRaises(ConfigError, msg=f"{key}={bad}"):
                    load_config({**BASE, key: bad})

    def test_memory_defaults(self):
        cfg = load_config(BASE)
        self.assertTrue(cfg.memory_enabled)
        self.assertEqual(cfg.memory_top_k, 3)
        self.assertEqual(cfg.memory_embedding_model, "BAAI/bge-small-zh-v1.5")

    def test_memory_top_k_non_positive_raises(self):
        for bad in ("0", "-2"):
            with self.assertRaises(ConfigError, msg=f"MEMORY_TOP_K={bad}"):
                load_config({**BASE, "MEMORY_TOP_K": bad})

    def test_memory_enabled_garbage_raises(self):
        with self.assertRaises(ConfigError):
            load_config({**BASE, "MEMORY_ENABLED": "maybe"})

    def test_memory_db_path_derived(self):
        cfg = load_config({**BASE, "DATA_DIR": "companion-data"})
        self.assertEqual(cfg.memory_db_path, Path("companion-data") / "memory.db")


if __name__ == "__main__":
    unittest.main()
