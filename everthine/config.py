"""Configuration loading for Everthine.

All runtime knobs come from environment variables (or a local .env file).
Modules receive a Config object; nothing else reads os.environ directly.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


def _get_bool(env: Mapping, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean (true/false/yes/no/on/off/1/0), got: {raw!r}")


def _get_int(env: Mapping, key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got: {raw!r}") from exc


def _get_positive_int(env: Mapping, key: str, default: int) -> int:
    value = _get_int(env, key, default)
    if value <= 0:
        raise ConfigError(f"{key} must be a positive integer, got: {value}")
    return value


def _get_hour(env: Mapping, key: str, default: int) -> int:
    value = _get_int(env, key, default)
    if not 0 <= value <= 23:
        raise ConfigError(f"{key} must be between 0 and 23, got: {value}")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    authorized_user_id: int
    claude_cmd: list = field(default_factory=lambda: ["claude"])
    claude_model: str | None = None
    command_timeout_s: int = 300
    data_dir: Path = Path("data")
    persona_path: Path = Path("personas/default.md")
    archive_enabled: bool = True
    injection_enabled: bool = True
    lookback_hours: int = 36
    injection_max_chars: int = 12000
    streaming_enabled: bool = True
    stream_stall_timeout_s: int = 60
    stream_total_timeout_s: int = 1200
    session_bloat_mb: float = 1.5
    session_bloat_lines: int = 700
    memory_enabled: bool = True
    memory_top_k: int = 3
    memory_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    stages_enabled: bool = True
    album_enabled: bool = True
    diary_enabled: bool = True
    reflection_enabled: bool = True
    diary_window_start_hour: int = 21
    diary_max_daily: int = 1
    reflection_daily_cap: int = 12
    portrait_enabled: bool = True
    portrait_interval_days: int = 7
    scheduler_enabled: bool = True
    greeting_enabled: bool = True
    greeting_hour: int = 8
    miss_you_enabled: bool = True
    miss_you_after_hours: int = 6
    share_enabled: bool = True
    share_max_daily: int = 2
    quiet_start_hour: int = 23
    quiet_end_hour: int = 8
    proactive_daily_max: int = 4

    @property
    def engine_home(self) -> Path:
        return self.data_dir / "engine"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session.json"

    @property
    def memory_db_path(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def stage_path(self) -> Path:
        return self.data_dir / "stage.json"

    @property
    def album_path(self) -> Path:
        return self.data_dir / "album.json"

    @property
    def diary_dir(self) -> Path:
        return self.data_dir / "diary"

    @property
    def diary_state_path(self) -> Path:
        return self.data_dir / "diary_state.json"

    @property
    def reflections_path(self) -> Path:
        return self.data_dir / "reflections.jsonl"

    @property
    def reflection_state_path(self) -> Path:
        return self.data_dir / "reflection_state.json"

    @property
    def portrait_path(self) -> Path:
        return self.data_dir / "portrait.json"

    @property
    def portrait_history_dir(self) -> Path:
        return self.data_dir / "portrait_history"

    @property
    def scheduler_state_path(self) -> Path:
        return self.data_dir / "scheduler_state.json"

    @property
    def expression_tag_taught(self) -> bool:
        """Whether the persona gets taught the [react:emoji] reply syntax.
        A neutral alias: the prompt layer reads this name and stays
        ignorant of which feature backs it."""
        return self.album_enabled


def load_config(env: Mapping | None = None) -> Config:
    if env is None:
        import os

        from dotenv import load_dotenv

        load_dotenv()
        env = os.environ

    token = str(env.get("BOT_TOKEN", "")).strip()
    if not token:
        raise ConfigError("BOT_TOKEN is required (get one from @BotFather).")

    raw_uid = str(env.get("AUTHORIZED_USER_ID", "")).strip()
    if not raw_uid:
        raise ConfigError("AUTHORIZED_USER_ID is required (your numeric Telegram user id).")
    try:
        uid = int(raw_uid)
    except ValueError as exc:
        raise ConfigError(f"AUTHORIZED_USER_ID must be an integer, got: {raw_uid!r}") from exc

    model = str(env.get("CLAUDE_MODEL", "")).strip() or None
    raw_cmd = str(env.get("CLAUDE_CMD", "claude")).strip() or "claude"
    try:
        cmd = shlex.split(raw_cmd)
    except ValueError as exc:
        raise ConfigError(f"CLAUDE_CMD must be a valid command line, got: {raw_cmd!r}") from exc

    return Config(
        bot_token=token,
        authorized_user_id=uid,
        claude_cmd=cmd,
        claude_model=model,
        command_timeout_s=_get_positive_int(env, "COMMAND_TIMEOUT_S", 300),
        data_dir=Path(str(env.get("DATA_DIR", "data"))),
        persona_path=Path(str(env.get("PERSONA_PATH", "personas/default.md"))),
        archive_enabled=_get_bool(env, "ARCHIVE_ENABLED", True),
        injection_enabled=_get_bool(env, "INJECTION_ENABLED", True),
        lookback_hours=_get_positive_int(env, "LOOKBACK_HOURS", 36),
        injection_max_chars=_get_positive_int(env, "INJECTION_MAX_CHARS", 12000),
        streaming_enabled=_get_bool(env, "STREAMING_ENABLED", True),
        stream_stall_timeout_s=_get_positive_int(env, "STREAM_STALL_TIMEOUT_S", 60),
        stream_total_timeout_s=_get_positive_int(env, "STREAM_TOTAL_TIMEOUT_S", 1200),
        memory_enabled=_get_bool(env, "MEMORY_ENABLED", True),
        memory_top_k=_get_positive_int(env, "MEMORY_TOP_K", 3),
        memory_embedding_model=str(env.get("MEMORY_EMBEDDING_MODEL", "")).strip() or "BAAI/bge-small-zh-v1.5",
        stages_enabled=_get_bool(env, "STAGES_ENABLED", True),
        album_enabled=_get_bool(env, "ALBUM_ENABLED", True),
        diary_enabled=_get_bool(env, "DIARY_ENABLED", True),
        reflection_enabled=_get_bool(env, "REFLECTION_ENABLED", True),
        diary_window_start_hour=_get_hour(env, "DIARY_WINDOW_START_HOUR", 21),
        diary_max_daily=_get_positive_int(env, "DIARY_MAX_DAILY", 1),
        reflection_daily_cap=_get_positive_int(env, "REFLECTION_DAILY_CAP", 12),
        portrait_enabled=_get_bool(env, "PORTRAIT_ENABLED", True),
        portrait_interval_days=_get_positive_int(env, "PORTRAIT_INTERVAL_DAYS", 7),
        scheduler_enabled=_get_bool(env, "SCHEDULER_ENABLED", True),
        greeting_enabled=_get_bool(env, "GREETING_ENABLED", True),
        greeting_hour=_get_hour(env, "GREETING_HOUR", 8),
        miss_you_enabled=_get_bool(env, "MISS_YOU_ENABLED", True),
        miss_you_after_hours=_get_positive_int(env, "MISS_YOU_AFTER_HOURS", 6),
        share_enabled=_get_bool(env, "SHARE_ENABLED", True),
        share_max_daily=_get_positive_int(env, "SHARE_MAX_DAILY", 2),
        quiet_start_hour=_get_hour(env, "QUIET_START_HOUR", 23),
        quiet_end_hour=_get_hour(env, "QUIET_END_HOUR", 8),
        proactive_daily_max=_get_positive_int(env, "PROACTIVE_DAILY_MAX", 4),
    )
