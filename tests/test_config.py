import dataclasses
import unittest
from pathlib import Path

from everthine.config import Config, ConfigError, load_config

BASE = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}
REPO_ROOT = Path(__file__).resolve().parent.parent


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
        self.assertEqual(cfg.archive_dir, Path("companion-data") / "archive")
        self.assertEqual(cfg.session_path, Path("companion-data") / "session.json")

    def test_engine_home_defaults_outside_the_repo(self):
        cfg = load_config({"BOT_TOKEN": "t", "AUTHORIZED_USER_ID": "1"})
        home = Path.home()
        self.assertEqual(cfg.engine_home, home / ".everthine" / "engine")
        # The isolation guarantee: the repo (and the data dir) must not
        # be an ancestor of the engine's working directory.
        repo = Path(__file__).resolve().parents[1]
        self.assertNotIn(repo, cfg.engine_home.parents)
        self.assertNotIn(cfg.data_dir.resolve(), cfg.engine_home.parents)

    def test_engine_home_is_injectable(self):
        cfg = load_config({"BOT_TOKEN": "t", "AUTHORIZED_USER_ID": "1"})
        override = dataclasses.replace(cfg, engine_home=Path("elsewhere"))
        self.assertEqual(override.engine_home, Path("elsewhere"))

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

    def test_env_example_documents_memory_vars(self):
        # Guards the .env.example 1:1 catalog going forward: every env var
        # load_config reads must be documented there. Substring match only --
        # robust to whatever comment formatting surrounds each line.
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for var in ("MEMORY_ENABLED", "MEMORY_TOP_K", "MEMORY_EMBEDDING_MODEL"):
            with self.subTest(var=var):
                self.assertIn(var, text)


class TestStageAlbumKnobs(unittest.TestCase):
    def test_defaults(self):
        # Stages ship OFF (owner ruling, 2026-07-10): out of the box the
        # companion is fully warm from the first message -- no closeness to
        # unlock. The album is the default-on half of M4.
        cfg = load_config(BASE)
        self.assertFalse(cfg.stages_enabled)
        self.assertTrue(cfg.album_enabled)

    def test_paths_derive_from_data_dir(self):
        cfg = load_config({**BASE, "DATA_DIR": "elsewhere"})
        self.assertEqual(cfg.stage_path, Path("elsewhere") / "stage.json")
        self.assertEqual(cfg.album_path, Path("elsewhere") / "album.json")

    def test_flags_parse_and_validate(self):
        cfg = load_config({**BASE, "STAGES_ENABLED": "false",
                           "ALBUM_ENABLED": "0"})
        self.assertFalse(cfg.stages_enabled)
        self.assertFalse(cfg.album_enabled)
        with self.assertRaises(ConfigError):
            load_config({**BASE, "STAGES_ENABLED": "maybe"})

    def test_env_example_documents_stage_album_vars(self):
        # Sibling of TestConfig.test_env_example_documents_memory_vars, same
        # shape: guards .env.example's catalog for the album knob -- and pins
        # the stages knob's ABSENCE. Stages are an undocumented, off-by-
        # default capability (owner ruling, 2026-07-10): the flag still
        # parses for anyone who reads the source, but no shipped document
        # advertises it, .env.example included.
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("ALBUM_ENABLED", text)
        self.assertNotIn("STAGES_ENABLED", text)


class TestDiaryReflectionKnobs(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config(BASE)
        self.assertTrue(cfg.diary_enabled)
        self.assertTrue(cfg.reflection_enabled)
        self.assertEqual(cfg.diary_window_start_hour, 21)
        self.assertEqual(cfg.diary_max_daily, 1)
        self.assertEqual(cfg.reflection_daily_cap, 12)

    def test_paths_derive_from_data_dir(self):
        cfg = load_config({**BASE, "DATA_DIR": "elsewhere"})
        self.assertEqual(cfg.diary_dir, Path("elsewhere") / "diary")
        self.assertEqual(cfg.diary_state_path, Path("elsewhere") / "diary_state.json")
        self.assertEqual(cfg.reflections_path, Path("elsewhere") / "reflections.jsonl")
        self.assertEqual(cfg.reflection_state_path, Path("elsewhere") / "reflection_state.json")

    def test_flags_and_hour_round_trip(self):
        cfg = load_config({**BASE, "DIARY_ENABLED": "false",
                           "REFLECTION_ENABLED": "false",
                           "DIARY_WINDOW_START_HOUR": "0"})
        self.assertFalse(cfg.diary_enabled)
        self.assertFalse(cfg.reflection_enabled)
        self.assertEqual(cfg.diary_window_start_hour, 0)

        cfg2 = load_config({**BASE, "DIARY_WINDOW_START_HOUR": "23"})
        self.assertEqual(cfg2.diary_window_start_hour, 23)

    def test_bad_values_raise(self):
        for key, bad in (
            ("DIARY_WINDOW_START_HOUR", "24"),
            ("DIARY_WINDOW_START_HOUR", "-1"),
            ("DIARY_WINDOW_START_HOUR", "abc"),
            ("DIARY_MAX_DAILY", "0"),
            ("REFLECTION_DAILY_CAP", "0"),
        ):
            with self.subTest(key=key, bad=bad):
                with self.assertRaises(ConfigError):
                    load_config({**BASE, key: bad})

    def test_env_example_documents_inner_life_vars(self):
        # Sibling of TestStageAlbumKnobs.test_env_example_documents_stage_album_vars,
        # same shape: guards .env.example's 1:1 catalog for the diary/reflection knobs.
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for var in ("DIARY_ENABLED", "REFLECTION_ENABLED", "DIARY_WINDOW_START_HOUR",
                    "DIARY_MAX_DAILY", "REFLECTION_DAILY_CAP"):
            with self.subTest(var=var):
                self.assertIn(var, text)


class TestPortraitKnobs(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config(BASE)
        self.assertIs(cfg.portrait_enabled, True)
        self.assertEqual(cfg.portrait_interval_days, 7)

    def test_paths_derive_from_data_dir(self):
        cfg = load_config({**BASE, "DATA_DIR": "elsewhere"})
        self.assertEqual(cfg.portrait_path, Path("elsewhere") / "portrait.json")
        self.assertEqual(cfg.portrait_history_dir, Path("elsewhere") / "portrait_history")

    def test_flag_and_interval_round_trip(self):
        cfg = load_config({**BASE, "PORTRAIT_ENABLED": "false",
                           "PORTRAIT_INTERVAL_DAYS": "14"})
        self.assertIs(cfg.portrait_enabled, False)
        self.assertEqual(cfg.portrait_interval_days, 14)

    def test_bad_interval_raises(self):
        for bad in ("0", "-5", "abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    load_config({**BASE, "PORTRAIT_INTERVAL_DAYS": bad})

    def test_env_example_documents_portrait_vars(self):
        # Sibling of TestDiaryReflectionKnobs.test_env_example_documents_inner_life_vars,
        # same shape: guards .env.example's 1:1 catalog for the portrait knobs.
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for var in ("PORTRAIT_ENABLED", "PORTRAIT_INTERVAL_DAYS"):
            with self.subTest(var=var):
                self.assertIn(var, text)


class TestSchedulerKnobs(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config(BASE)
        self.assertIs(cfg.scheduler_enabled, True)
        self.assertIs(cfg.greeting_enabled, True)
        self.assertEqual(cfg.greeting_hour, 8)
        self.assertIs(cfg.miss_you_enabled, True)
        self.assertEqual(cfg.miss_you_after_hours, 6)
        self.assertIs(cfg.share_enabled, True)
        self.assertEqual(cfg.share_max_daily, 2)
        self.assertEqual(cfg.quiet_start_hour, 23)
        self.assertEqual(cfg.quiet_end_hour, 8)
        self.assertEqual(cfg.proactive_daily_max, 4)

    def test_scheduler_state_path_derives_from_data_dir(self):
        cfg = load_config({**BASE, "DATA_DIR": "elsewhere"})
        self.assertEqual(cfg.scheduler_state_path, Path("elsewhere") / "scheduler_state.json")

    def test_overrides_round_trip(self):
        cfg = load_config({**BASE,
                            "SCHEDULER_ENABLED": "false",
                            "GREETING_ENABLED": "false",
                            "GREETING_HOUR": "6",
                            "MISS_YOU_ENABLED": "false",
                            "MISS_YOU_AFTER_HOURS": "3",
                            "SHARE_ENABLED": "false",
                            "SHARE_MAX_DAILY": "5",
                            "QUIET_START_HOUR": "0",
                            "QUIET_END_HOUR": "7",
                            "PROACTIVE_DAILY_MAX": "9"})
        self.assertIs(cfg.scheduler_enabled, False)
        self.assertIs(cfg.greeting_enabled, False)
        self.assertEqual(cfg.greeting_hour, 6)
        self.assertIs(cfg.miss_you_enabled, False)
        self.assertEqual(cfg.miss_you_after_hours, 3)
        self.assertIs(cfg.share_enabled, False)
        self.assertEqual(cfg.share_max_daily, 5)
        self.assertEqual(cfg.quiet_start_hour, 0)
        self.assertEqual(cfg.quiet_end_hour, 7)
        self.assertEqual(cfg.proactive_daily_max, 9)

    def test_quiet_hours_may_equal_start_and_end(self):
        # start == end is the documented "disabled" spelling for the quiet
        # window (interpreted by the scheduler, not this layer) -- the config
        # layer must accept it since each bound is independently a legal hour.
        cfg = load_config({**BASE, "QUIET_START_HOUR": "5", "QUIET_END_HOUR": "5"})
        self.assertEqual(cfg.quiet_start_hour, 5)
        self.assertEqual(cfg.quiet_end_hour, 5)

    def test_bool_garbage_raises(self):
        for key in ("SCHEDULER_ENABLED", "GREETING_ENABLED", "MISS_YOU_ENABLED", "SHARE_ENABLED"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    load_config({**BASE, key: "maybe"})

    def test_bad_hour_values_raise(self):
        for key in ("GREETING_HOUR", "QUIET_START_HOUR", "QUIET_END_HOUR"):
            for bad in ("24", "-1", "abc"):
                with self.subTest(key=key, bad=bad):
                    with self.assertRaises(ConfigError):
                        load_config({**BASE, key: bad})

    def test_non_positive_ints_raise(self):
        for key in ("MISS_YOU_AFTER_HOURS", "SHARE_MAX_DAILY", "PROACTIVE_DAILY_MAX"):
            for bad in ("0", "-5"):
                with self.subTest(key=key, bad=bad):
                    with self.assertRaises(ConfigError):
                        load_config({**BASE, key: bad})

    def test_env_example_documents_scheduler_vars(self):
        # Sibling of TestPortraitKnobs.test_env_example_documents_portrait_vars,
        # same shape: guards .env.example's 1:1 catalog for the scheduler knobs.
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for var in ("SCHEDULER_ENABLED", "GREETING_ENABLED", "GREETING_HOUR",
                    "MISS_YOU_ENABLED", "MISS_YOU_AFTER_HOURS",
                    "SHARE_ENABLED", "SHARE_MAX_DAILY",
                    "QUIET_START_HOUR", "QUIET_END_HOUR", "PROACTIVE_DAILY_MAX"):
            with self.subTest(var=var):
                self.assertIn(var, text)


class TestFactsKnobs(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config(BASE)
        self.assertTrue(cfg.facts_enabled)
        self.assertEqual(cfg.facts_idle_minutes, 30)
        self.assertEqual(cfg.facts_max, 200)
        self.assertEqual(cfg.facts_prompt_max, 15)

    def test_paths_derive_from_data_dir(self):
        cfg = load_config({**BASE, "DATA_DIR": "elsewhere"})
        self.assertEqual(cfg.facts_path, Path("elsewhere") / "facts.json")
        self.assertEqual(cfg.facts_state_path, Path("elsewhere") / "facts_state.json")

    def test_overrides_round_trip(self):
        cfg = load_config({**BASE,
                            "FACTS_ENABLED": "false",
                            "FACTS_IDLE_MINUTES": "45",
                            "FACTS_MAX": "50",
                            "FACTS_PROMPT_MAX": "5"})
        self.assertFalse(cfg.facts_enabled)
        self.assertEqual(cfg.facts_idle_minutes, 45)
        self.assertEqual(cfg.facts_max, 50)
        self.assertEqual(cfg.facts_prompt_max, 5)

    def test_bool_garbage_raises(self):
        with self.assertRaises(ConfigError):
            load_config({**BASE, "FACTS_ENABLED": "maybe"})

    def test_non_positive_ints_raise(self):
        for key in ("FACTS_IDLE_MINUTES", "FACTS_MAX", "FACTS_PROMPT_MAX"):
            for bad in ("0", "-5"):
                with self.subTest(key=key, bad=bad):
                    with self.assertRaises(ConfigError):
                        load_config({**BASE, key: bad})

    def test_env_example_documents_facts_vars(self):
        # Sibling of TestSchedulerKnobs.test_env_example_documents_scheduler_vars,
        # same shape: guards .env.example's 1:1 catalog for the facts knobs.
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for var in ("FACTS_ENABLED", "FACTS_IDLE_MINUTES", "FACTS_MAX", "FACTS_PROMPT_MAX"):
            with self.subTest(var=var):
                self.assertIn(var, text)


class TestLoginWatchKnobs(unittest.TestCase):
    def test_login_watch_defaults(self):
        cfg = load_config({"BOT_TOKEN": "t", "AUTHORIZED_USER_ID": "1"})
        self.assertTrue(cfg.login_watch_enabled)
        self.assertEqual(cfg.login_warn_days, 3)

    def test_login_watch_overrides(self):
        cfg = load_config({"BOT_TOKEN": "t", "AUTHORIZED_USER_ID": "1",
                           "LOGIN_WATCH_ENABLED": "false", "LOGIN_WARN_DAYS": "0"})
        self.assertFalse(cfg.login_watch_enabled)
        self.assertEqual(cfg.login_warn_days, 0)

    def test_login_warn_days_negative_or_garbage_raises(self):
        for bad in ("-1", "abc"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_config({"BOT_TOKEN": "t", "AUTHORIZED_USER_ID": "1", "LOGIN_WARN_DAYS": bad})


if __name__ == "__main__":
    unittest.main()
