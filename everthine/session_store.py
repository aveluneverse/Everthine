"""Session pointer persistence.

The Claude Code CLI keeps the full transcript itself (under
~/.claude/projects/<slug>/<session_id>.jsonl); we only remember which
session id is current, plus the two timestamps the warmth-injection
window needs. Atomic writes; a hostname guard drops session ids that
were minted on another machine (their transcripts do not exist here).
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
from datetime import datetime
from pathlib import Path

from .config import Config

_DEFAULTS = {
    "session_id": None,
    "hostname": None,
    "session_started_at": None,
    "recent_context_floor": None,
}


def slug_for(path: Path) -> str:
    """Mirror the Claude CLI project-slug convention (separators and spaces -> '-')."""
    base = str(path)
    slug = (base.replace(os.sep, "-").replace("/", "-")
                .replace(":", "-").replace(" ", "-"))
    if not slug.startswith("-") and not slug[0].isalnum():
        slug = "-" + slug
    return slug


class SessionStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        data = dict(_DEFAULTS)
        try:
            data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return data
        if data.get("hostname") and data["hostname"] != socket.gethostname():
            data["session_id"] = None
            data["session_started_at"] = None
        return data

    def save(self, **fields) -> None:
        data = self.load()
        data.update(fields)
        data["hostname"] = socket.gethostname()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def warm_restart(self) -> None:
        """New notebook, keep the warmth: the injection window stays open,
        and any explicit clean-start floor is preserved."""
        self.save(session_id=None, session_started_at=None)

    def clean_start(self, now: datetime) -> None:
        """New notebook AND slam the injection floor: a true blank page."""
        self.save(session_id=None, session_started_at=None,
                  recent_context_floor=now.isoformat())

    def stamp_session_started(self, new_id: str | None, now: datetime) -> None:
        if not new_id:
            return
        data = self.load()
        # The CLI's -p --resume keeps the same session id across turns (verified
        # empirically), so "id changed" reliably means "a new conversation began".
        if data.get("session_id") == new_id:
            return
        self.save(session_id=new_id, session_started_at=now.isoformat())

    def detect_bloat(self, cfg: Config, session_id: str | None, home: Path | None = None) -> bool:
        if not session_id:
            return False
        home = home or Path.home()
        slug = slug_for(cfg.engine_home.resolve())
        # The CLI may lowercase the drive letter in its cwd string; try both.
        candidates = [slug, slug[:1].lower() + slug[1:]] if slug else [slug]
        jsonl = None
        for cand in candidates:
            probe = home / ".claude" / "projects" / cand / f"{session_id}.jsonl"
            if probe.exists():
                jsonl = probe
                break
        if jsonl is None:
            return False
        try:
            stat = jsonl.stat()
        except OSError:
            return False
        if stat.st_size > cfg.session_bloat_mb * 1024 * 1024:
            return True
        try:
            with jsonl.open(encoding="utf-8", errors="ignore") as fh:
                for count, _ in enumerate(fh, 1):
                    if count > cfg.session_bloat_lines:
                        return True
        except OSError:
            return False
        return False
