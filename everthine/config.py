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

    @property
    def engine_home(self) -> Path:
        return self.data_dir / "engine"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session.json"


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
    )
