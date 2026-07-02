"""Cross-session warmth injection.

When a fresh CLI session starts, the companion would normally wake up
blank. This module quotes the recent archive into the prompt prefix so
the first replies still carry yesterday's warmth. The window decays to
nothing as the current session accumulates its own native memory.

Window: since = max(now - lookback, floor);  entries with ts >= upper
(the current session's start) are skipped - the CLI already remembers
those natively.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from . import archive
from .config import Config

HEADER = "[Context: recent conversation from before this session - treat it as your own memory]"
USER_TURN_MARK = "[The user says now]"


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def build_block(cfg: Config, store_data: dict, archive_dir: Path,
                now: datetime) -> str | None:
    if not cfg.injection_enabled:
        return None
    since = now - timedelta(hours=cfg.lookback_hours)
    floor = _parse(store_data.get("recent_context_floor"))
    if floor is not None and floor > since:
        since = floor
    upper = _parse(store_data.get("session_started_at"))

    lines = []
    for entry in archive.iter_entries(archive_dir, since=since):
        if upper is not None and entry["timestamp"] >= upper:
            continue
        lines.append(f'{entry["speaker"]}: {entry["text"]}')
    if not lines:
        return None

    body = "\n".join(lines)
    if len(body) > cfg.injection_max_chars:
        body = body[-cfg.injection_max_chars:]
        cut = body.find("\n")
        if cut != -1:
            body = body[cut + 1:]
    return f"{HEADER}\n{body}"


def prepend(block: str | None, body: str) -> str:
    if not block:
        return body
    return f"{block}\n\n{USER_TURN_MARK}\n{body}"
