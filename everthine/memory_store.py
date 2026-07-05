"""Long-term conversation memory: archive -> chunks -> embeddings -> recall.

Turns the flat per-day conversation archive (`everthine.archive`) into
bounded conversation chunks - a short run of nearby turns that closes when
the conversation pauses long enough, grows too long, or grows too large,
so a recalled memory reads like one remembered scene rather than an
entire day. `chunk_entries` is the pure, disk-free half: no clock reads,
no I/O, no embedding calls.

`MemoryStore` is the persistent half. It keeps chunks and their embedding
vectors in a local SQLite file and knows how to catch itself up: reading
the archive from wherever it left off (a cursor timestamp kept in the
`meta` table), chunking the new material, embedding only the chunks that
have already closed, and inserting them idempotently. The still-open
trailing chunk is never stored and the cursor never advances past it, so
a conversation always arrives as one whole remembered scene once it
finishes - never as a half-written fragment. The conversation archive
remains the single source of truth: nothing the store does to its own
copy can lose anything, because a rebuild (see `ensure_model`) can always
replay the archive from the start.

Retrieval note: a `weight <= 0` row is excluded by `load_all` - this is
how future memory sources that must stay out of recall (the bot's own
dashboard-only observation layers; a later milestone's diary/reflection
walkers) can share this same table without being treated as a remembered
conversation. This module only ever writes weight=1.0 conversation
chunks; nothing here ever sets a chunk's weight to zero.

Embedding vectors are stored as raw float32 blobs, little-endian, via
`struct.pack`/`struct.unpack` - not via numpy. This module never imports
numpy, keeping the storage layer as dependency-light as the chunking
layer above it.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import archive

CHUNK_GAP_MINUTES = 30        # a lull this long closes the chunk
CHUNK_MAX_ROUNDS = 8          # so retrieval returns a scene, not a saga
CHUNK_MAX_CHARS = 6000        # hard cap regardless of round count

logger = logging.getLogger("everthine")


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


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_vector(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


class MemoryStore:
    """SQLite-backed chunk + embedding store with cursor-based archive sync.

    A single `threading.Lock` is acquired by every public method: sync
    writes (from a background/scheduler thread) and recall reads (from a
    chat turn) cross threads in the bot, and the connection itself is
    opened with `check_same_thread=False` to allow that cross-thread use.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id  TEXT PRIMARY KEY,
                ts        TEXT NOT NULL,
                text      TEXT NOT NULL,
                embedding BLOB NOT NULL,
                weight    REAL NOT NULL DEFAULT 1.0,
                source    TEXT NOT NULL DEFAULT 'conversation'
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_ts ON chunks(ts)")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    # -- meta helpers: no locking of their own, only ever called from a
    # public method that already holds self._lock (avoids reentrant
    # deadlock on the non-reentrant threading.Lock). --

    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )

    def _delete_meta(self, key: str) -> None:
        self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    def ensure_model(self, model_name: str, dim: int) -> bool:
        """Pin the embedding model identity. A fresh db records it (True).
        An unchanged model/dim pair is a no-op (True). A changed model or
        dimension means every stored vector is now meaningless (vectors
        from two different models are not comparable), so the chunks
        table is rebuilt from scratch and the cursor reset to the start
        (False) - the conversation archive is the source of truth, so the
        next sync simply re-embeds everything under the new model."""
        with self._lock:
            stored_name = self._get_meta("embedding_model")
            stored_dim = self._get_meta("embedding_dim")
            if stored_name is None and stored_dim is None:
                self._set_meta("embedding_model", model_name)
                self._set_meta("embedding_dim", str(dim))
                self._conn.commit()
                return True
            if stored_name == model_name and stored_dim == str(dim):
                return True
            logger.warning(
                "memory store: embedding model changed (%s dim=%s -> %s dim=%s); "
                "rebuilding the chunk index from the conversation archive",
                stored_name, stored_dim, model_name, dim,
            )
            self._conn.execute("DELETE FROM chunks")
            self._delete_meta("cursor_ts")
            self._set_meta("embedding_model", model_name)
            self._set_meta("embedding_dim", str(dim))
            self._conn.commit()
            return False

    def sync_from_archive(
        self, archive_dir, now: datetime, embed, model_name: str
    ) -> list[tuple[Chunk, list[float]]]:
        """Catch the store up with the conversation archive.

        Reads everything after the stored cursor, chunks it, embeds and
        inserts only the CLOSED chunks with one batched `embed(texts,
        model_name)` call, and advances the cursor to cover exactly what
        was ingested this run - never past a still-open trailing chunk,
        and never at all if nothing closed this run ("progress only on
        output"). Returns the `(chunk, vector)` pairs actually inserted;
        duplicates skipped by `INSERT OR IGNORE` are excluded.
        """
        with self._lock:
            cursor_raw = self._get_meta("cursor_ts")
            cursor = datetime.fromisoformat(cursor_raw) if cursor_raw else None

            entries = list(archive.iter_entries(archive_dir, since=cursor))
            if cursor is not None:
                # iter_entries only drops ts < since (ts == cursor would be
                # re-yielded); the cursor marks the exact last-ingested
                # entry, which must never be re-ingested as a fragment.
                filtered = [e for e in entries if e["timestamp"] > cursor]
            else:
                filtered = entries

            chunks = chunk_entries(filtered, now)
            closed_chunks = [c for c in chunks if c.closed]

            inserted: list[tuple[Chunk, list[float]]] = []
            if closed_chunks:
                texts = [c.text for c in closed_chunks]
                vectors = embed(texts, model_name)
                for chunk, vector in zip(closed_chunks, vectors):
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO chunks (chunk_id, ts, text, embedding) "
                        "VALUES (?, ?, ?, ?)",
                        (chunk.chunk_id, chunk.ts, chunk.text, _pack_vector(vector)),
                    )
                    if cur.rowcount > 0:
                        inserted.append((chunk, vector))
                self._conn.commit()

            if inserted:
                self._advance_cursor(filtered, chunks)

            return inserted

    def _advance_cursor(self, filtered: list[dict], chunks: list[Chunk]) -> None:
        """Progress-only cursor bookkeeping: never move past a still-open
        trailing chunk, and never guess forward if its start entry cannot
        be located (defensive branch - leaves the cursor unchanged)."""
        trailing_open = bool(chunks) and not chunks[-1].closed
        if not trailing_open:
            new_cursor = filtered[-1]["timestamp"].isoformat()
            self._set_meta("cursor_ts", new_cursor)
            self._conn.commit()
            return

        open_chunk = chunks[-1]
        idx = None
        for i, entry in enumerate(filtered):
            if entry["timestamp"].isoformat() == open_chunk.ts:
                idx = i
                break
        if idx is None or idx == 0:
            logger.warning(
                "memory store: could not locate a filtered entry preceding "
                "the open trailing chunk's start; cursor left unchanged "
                "rather than risk skipping unread content"
            )
            return
        new_cursor = filtered[idx - 1]["timestamp"].isoformat()
        self._set_meta("cursor_ts", new_cursor)
        self._conn.commit()

    def load_all(self) -> list[tuple[str, str, str, list[float]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chunk_id, ts, text, embedding FROM chunks "
                "WHERE weight > 0 ORDER BY ts ASC"
            ).fetchall()
        return [(chunk_id, ts, text, _unpack_vector(blob)) for chunk_id, ts, text, blob in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
