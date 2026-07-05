"""Long-term conversation memory: archive -> chunks -> embeddings -> recall.

Turns the flat per-day conversation archive (`everthine.archive`) into
bounded conversation chunks - a short run of nearby turns that closes when
the conversation pauses long enough, grows too long, or grows too large,
so a recalled memory reads like one remembered scene rather than an
entire day. Each chunk is later embedded and kept in a local SQLite table
for similarity recall. Only the pure chunking function ships here so far
(no disk, no clock, no embedding calls); the SQLite store sits on top of
it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

CHUNK_GAP_MINUTES = 30        # a lull this long closes the chunk
CHUNK_MAX_ROUNDS = 8          # so retrieval returns a scene, not a saga
CHUNK_MAX_CHARS = 6000        # hard cap regardless of round count


@dataclass(frozen=True)
class Chunk:
    chunk_id: str    # "<ts-compact>-<md5(text)[:8]>"
    ts: str          # ISO8601 of the first entry (aware, as archived)
    text: str        # one line per round: "user: ..." / "companion: ..."
    closed: bool     # False only for the still-open trailing chunk


def _naive_local(dt: datetime) -> datetime:
    """Normalize an aware-or-naive timestamp to naive local wall-clock time,
    so gap arithmetic never raises on aware/naive mismatches."""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.replace(tzinfo=None)


def _chunk_text(rounds: list[tuple[str, str]]) -> str:
    return "\n".join(f"{speaker}: {text}" for speaker, text in rounds)


def _finalize(rounds: list[tuple[str, str]], first_ts: datetime, closed: bool) -> Chunk:
    text = _chunk_text(rounds)
    ts_compact = first_ts.strftime("%Y%m%d_%H%M%S")
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return Chunk(
        chunk_id=f"{ts_compact}-{digest}",
        ts=first_ts.isoformat(),
        text=text,
        closed=closed,
    )


def chunk_entries(entries: list[dict], now: datetime) -> list[Chunk]:
    valid = []
    for entry in entries:
        text = entry["text"].strip()
        if text:
            valid.append((entry["speaker"], text, entry["timestamp"]))
    if not valid:
        return []

    chunks: list[Chunk] = []
    rounds: list[tuple[str, str]] = []
    first_ts = None
    last_ts = None

    for speaker, text, ts in valid:
        if rounds:
            gap = _naive_local(ts) - _naive_local(last_ts) >= timedelta(minutes=CHUNK_GAP_MINUTES)
            rounds_full = len(rounds) >= CHUNK_MAX_ROUNDS
            overflow = len(_chunk_text(rounds + [(speaker, text)])) > CHUNK_MAX_CHARS
            if gap or rounds_full or overflow:
                chunks.append(_finalize(rounds, first_ts, closed=True))
                rounds, first_ts = [], None
        if not rounds:
            first_ts = ts
        rounds.append((speaker, text))
        last_ts = ts

    trailing_closed = _naive_local(now) - _naive_local(last_ts) >= timedelta(minutes=CHUNK_GAP_MINUTES)
    chunks.append(_finalize(rounds, first_ts, closed=trailing_closed))
    return chunks
