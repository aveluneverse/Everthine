"""Daily JSONL conversation archive.

A flat, dependency-free log: one file per local day, one JSON object per
line. This is NOT the semantic memory system (that arrives in a later
milestone) - it exists so the warmth injection can quote recent turns.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("everthine")


def write_entry(archive_dir: Path, speaker: str, text: str,
                ts: datetime | None = None) -> bool:
    ts = ts or datetime.now().astimezone()
    archive_dir = Path(archive_dir)
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"timestamp": ts.isoformat(), "speaker": speaker,
                           "text": text}, ensure_ascii=False)
        with (archive_dir / f"{ts.date().isoformat()}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except OSError as exc:
        # The bool return is widely ignored (archiving is fire-and-forget), so
        # a bare `return False` would swallow a real disk problem in silence.
        # Surface it -- but log the path, speaker, and exception ONLY. The
        # conversation text never enters a log; two people's words are not a
        # place for a warning line (same spirit as the token mask).
        logger.warning("archive write failed for %s (speaker=%s): %s",
                       archive_dir, speaker, exc)
        return False


def iter_entries(archive_dir: Path, since: datetime | None = None) -> Iterator[dict]:
    archive_dir = Path(archive_dir)
    if not archive_dir.is_dir():
        return
    for day_file in sorted(archive_dir.glob("*.jsonl")):
        for raw in day_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                entry = json.loads(raw)
                ts = datetime.fromisoformat(entry["timestamp"])
                text = entry["text"]
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
            if since is not None and ts < since:
                continue
            yield {"timestamp": ts, "speaker": entry.get("speaker", "?"), "text": text}
