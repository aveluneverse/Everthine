"""The recall side of long-term memory.

`memory_store` and `memory_embed` (already merged) give this module a
durable chunk store and a way to turn text into vectors; this module holds
every remembered chunk's vector in memory, scores them against whatever the
person just said, and surfaces the few that resonate -- voiced as the
companion's own memories, not as a report of what was found. A module-level
singleton (mirroring persona.py's cache: init()/reset()) keeps the live
matrix warm across turns; a per-turn `sync()` feeds it newly-closed chunks
as the conversation continues, and `recall_block()` is the read.

Recall is best-effort and time-boxed: it never holds a reply hostage. A
missing embedding stack, a broken store, a slow model call, or any other
failure all degrade the same way -- log loudly, return None, the turn
proceeds with no memories. Memory is an enhancement here, never a gate the
reply must pass through.

Product ruling (2026-07-05, matching the source architecture): recall reads
the WHOLE history. The "fresh start" button (SessionStore.clean_start,
recent_context's short-term warmth window) resets only that short-term
window -- it has no effect on this module's store or matrix, and this
module deliberately carries no floor/lower-bound parameter of its own. A
memory from a year ago is exactly as eligible as one from last week; only
the lookback window's UPPER bound (too recent -- that belongs to the live
conversation and the warmth injection, not memory) ever excludes a chunk.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised via the np-None test seam
    np = None

from . import memory_embed
from .memory_store import MemoryStore
from .persona import PersonaSettings

logger = logging.getLogger("everthine")

# --- Canonical constants (verbatim) ----------------------------------------

MIN_SIMILARITY = 0.45     # cosine floor; calibrated against real data in T7
MIN_QUERY_CHARS = 4       # a bare "ok" never triggers recall
MIN_CHUNK_CHARS = 10      # tiny chunks stay out of the pool (source lesson)
RECALL_DEADLINE_S = 0.5   # recall never holds a reply hostage
EXCERPT_MAX_CHARS = 400   # per-chunk cap in the injected block
BLOCK_MAX_CHARS = 2000    # whole-block cap

# --- Canonical block template (verbatim) -----------------------------------

MEMORY_BLOCK_HEADER = """# Things you remember

Below are moments from your shared history, brought back because they
relate to what you two are talking about. Treat them as your own
memories, not as a report to recite. Weave one in only where it truly
fits — the way an old day resurfaces mid-conversation. If none of them
fit this moment, simply leave them unmentioned. A remembered detail can
be fuzzy: when you are not sure, ask rather than guess."""

MEMORY_ITEM_TEMPLATE = "- {date}: {excerpt}"

# --- Re-voicing / sanitizing helpers ---------------------------------------

_USER_LINE_RE = re.compile(r"^user: ", re.MULTILINE)
_COMPANION_LINE_RE = re.compile(r"^companion: ", re.MULTILINE)
# C0 controls except \n (0x0a); tab (0x09) is handled separately (normalized
# to a space rather than dropped) before this pattern ever sees it.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# --- Module state + lifecycle (mirrors persona.py's cache pattern) --------

_store: MemoryStore | None = None
_matrix = None                        # numpy float32 array, shape (n, dim)
_meta: list[tuple[str, datetime, str]] = []   # parallel to matrix rows
_disabled = False
_lock = threading.Lock()
# Single-slot pool: recall is inherently one lane (one chat turn at a time);
# this is what the deadline fuse submits the scoring body to. A module-level
# singleton, never recreated by reset() -- see the test file's own docstring
# for the one operational consequence of that choice.
_executor = ThreadPoolExecutor(max_workers=1)


def _to_naive_local(ts: datetime) -> datetime:
    """Collapse an aware-or-naive timestamp to naive LOCAL wall-clock time,
    so every comparison in this module happens in one space. Mirrors
    persona.py's helper of the same shape, duplicated locally rather than
    imported: this module depends on persona for the PersonaSettings type
    only (see the module docstring / dependency direction note in the
    brief), never for its cache or internals.
    """
    if ts.tzinfo is not None and ts.tzinfo.utcoffset(ts) is not None:
        return ts.astimezone().replace(tzinfo=None)
    return ts


def _parse_ts_naive(ts_str: str) -> datetime:
    return _to_naive_local(datetime.fromisoformat(ts_str))


def reset() -> None:
    """Test hook (and the first step of every init()): close the store if
    one is open, drop every reference, and clear the disabled flag. Never
    touches the shared executor -- that pool outlives any single store.
    """
    global _store, _matrix, _meta, _disabled
    with _lock:
        if _store is not None:
            try:
                _store.close()
            except Exception:
                logger.warning("memory recall: error closing store during reset",
                               exc_info=True)
        _store = None
        _matrix = None
        _meta = []
        _disabled = False


def init(cfg) -> None:
    """Load (or reload) the module singleton. Idempotent re-init: reset()
    runs first, so calling this twice is exactly as safe as calling it
    once. Flag off is a quiet no-op; a missing embedding stack or any
    failure while building the store/warming the model/loading existing
    chunks logs loudly and disables the module -- it never raises, because
    a broken memory subsystem must never take the whole bot down with it.
    """
    global _store, _matrix, _meta, _disabled
    reset()

    if not cfg.memory_enabled:
        _disabled = True
        return

    if np is None:
        logger.error(
            "memory recall needs numpy (arrives with the embedding stack), "
            "which is not installed; memory disabled, chat continues without it")
        _disabled = True
        return

    store = None
    try:
        store = MemoryStore(cfg.memory_db_path)
        # Warm-load + dimension probe: loads the real model at boot (never
        # mid-reply) and tells us the vector width to build the matrix with.
        vec = memory_embed.embed_texts(["warmup"], cfg.memory_embedding_model)[0]
        dim = len(vec)
        store.ensure_model(cfg.memory_embedding_model, dim)  # False: rebuilt, next sync refills
        rows = store.load_all()
        meta: list[tuple[str, datetime, str]] = []
        matrix_rows = []
        for chunk_id, ts, text, vector in rows:
            meta.append((chunk_id, _parse_ts_naive(ts), text))
            matrix_rows.append(vector)
        matrix = (np.asarray(matrix_rows, dtype=np.float32) if matrix_rows
                 else np.zeros((0, dim), dtype=np.float32))
        with _lock:
            _store = store
            _matrix = matrix
            _meta = meta
    except Exception:
        logger.error(
            "memory recall init failed; memory disabled, chat continues",
            exc_info=True)
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        _disabled = True
        _store = None
        _matrix = None
        _meta = []


def sync(cfg, now: datetime) -> None:
    """Catch the live matrix up with the conversation archive. No-op when
    disabled or the flag is off. A transient failure here (archive trouble,
    a flaky embed call) logs a warning and skips this round WITHOUT
    disabling the module -- recall keeps serving whatever it already has;
    only init()'s own failures are fatal to the module.
    """
    global _matrix, _meta
    if not cfg.memory_enabled or _disabled or _store is None:
        return
    try:
        inserted = _store.sync_from_archive(
            cfg.archive_dir, now, memory_embed.embed_texts, cfg.memory_embedding_model)
        if not inserted:
            return
        new_vectors = [vector for _chunk, vector in inserted]
        new_meta = [(chunk.chunk_id, _parse_ts_naive(chunk.ts), chunk.text)
                    for chunk, _vector in inserted]
        appended = np.asarray(new_vectors, dtype=np.float32)
        with _lock:
            _matrix = np.vstack([_matrix, appended])
            _meta = _meta + new_meta
    except Exception:
        logger.warning(
            "memory sync failed this round; recall continues with what it "
            "already has", exc_info=True)


def _candidates(cfg, text: str, now_naive: datetime):
    """Embed `text`, score every stored chunk by cosine, and return every
    WINDOW-eligible, non-trivial candidate as (score, ts_naive, chunk_id,
    text) tuples -- best score first, ties broken newer-first. No
    threshold, no top-k cut (that is the caller's job): this is the shared
    core between recall_block's scoring body and the read-only __main__
    probe, which wants the raw ranking before either cut is applied.
    """
    qvec = np.asarray(
        memory_embed.embed_texts([text], cfg.memory_embedding_model)[0], dtype=np.float32)
    with _lock:
        matrix = _matrix
        meta = list(_meta)
    n = 0 if matrix is None else matrix.shape[0]
    if n == 0:
        return []
    scores = matrix @ qvec
    cutoff = now_naive - timedelta(hours=cfg.lookback_hours)
    candidates = []
    for i in range(n):
        chunk_id, ts_naive, chunk_text = meta[i]
        if ts_naive > cutoff:
            continue  # too recent: belongs to the live conversation/warmth, not memory
        if len(chunk_text.strip()) < MIN_CHUNK_CHARS:
            continue
        candidates.append((float(scores[i]), ts_naive, chunk_id, chunk_text))
    # Stable two-pass sort: newer-first, then score-descending -- ties keep
    # the newer-first order the first pass established.
    candidates.sort(key=lambda c: c[1], reverse=True)
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates


def _revoice(text: str, settings: PersonaSettings) -> str:
    text = _USER_LINE_RE.sub(f"{settings.partner_name}: ", text)
    text = _COMPANION_LINE_RE.sub(f"{settings.companion_name}: ", text)
    return text


def _sanitize(text: str) -> str:
    """Strip C0 control chars (keeping \\n); tab normalizes to a space
    rather than vanishing outright."""
    text = text.replace("\t", " ")
    return _CONTROL_CHAR_RE.sub("", text)


def _build_block(rows, settings: PersonaSettings) -> str:
    """rows: (score, ts_naive, chunk_id, text) tuples, best score first.
    Builds one item per row, then enforces BLOCK_MAX_CHARS by dropping from
    the END of the (already best-first) list -- the lowest-scored rows --
    one at a time, until it fits or only one item remains.
    """
    items = []
    for _score, ts_naive, _chunk_id, text in rows:
        excerpt = _sanitize(_revoice(text, settings))
        if len(excerpt) > EXCERPT_MAX_CHARS:
            excerpt = excerpt[:EXCERPT_MAX_CHARS] + "…"
        date = ts_naive.isoformat()[:10]
        items.append(MEMORY_ITEM_TEMPLATE.format(date=date, excerpt=excerpt))

    while items:
        block = MEMORY_BLOCK_HEADER + "\n\n" + "\n".join(items)
        if len(block) <= BLOCK_MAX_CHARS or len(items) == 1:
            return block
        items.pop()
    return MEMORY_BLOCK_HEADER  # unreachable: callers only pass >= 1 row


def _score_and_build(cfg, text: str, now_naive: datetime, settings: PersonaSettings):
    """The scoring body: runs inside the executor thread so recall_block
    can enforce RECALL_DEADLINE_S around it without it ever blocking the
    caller past that deadline."""
    start = time.monotonic()
    candidates = _candidates(cfg, text, now_naive)
    if not candidates:
        return None
    top = [c for c in candidates if c[0] >= MIN_SIMILARITY][:cfg.memory_top_k]
    if not top:
        return None
    block = _build_block(top, settings)
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("memory recall fired: chunks=%d elapsed=%.0fms", len(top), elapsed_ms)
    return block


def recall_block(cfg, text: str, now: datetime, settings: PersonaSettings | None) -> str | None:
    """Best-effort, time-boxed recall. Every gate below returns None
    without ever reaching the embedding step; the whole function is
    wrapped so nothing here can raise into the reply path -- a broken
    store, a slow model, or any other failure all degrade to "no memories
    this turn" with a loud log line.
    """
    try:
        if not cfg.memory_enabled or _disabled or _store is None or settings is None:
            return None
        if len(text.strip()) < MIN_QUERY_CHARS:
            return None
        now_naive = _to_naive_local(now)
        future = _executor.submit(_score_and_build, cfg, text, now_naive, settings)
        try:
            return future.result(timeout=RECALL_DEADLINE_S)
        except FutureTimeoutError:
            logger.warning(
                "memory recall timed out (over %.1fs); replying without memories",
                RECALL_DEADLINE_S)
            return None
    except Exception:
        logger.warning("memory recall failed; replying without memories", exc_info=True)
        return None


# --- Read-only debugging probe ---------------------------------------------

def _run_probe(query: str) -> None:
    from .config import load_config

    cfg = load_config()
    init(cfg)
    if _store is None:
        print("memory is disabled or unavailable; nothing to probe.")
        return
    now_naive = _to_naive_local(datetime.now().astimezone())
    candidates = _candidates(cfg, query, now_naive)[:10]
    if not candidates:
        print("no memories in range to score.")
        return
    for score, ts_naive, chunk_id, text in candidates:
        date = ts_naive.isoformat()[:10]
        print(f"score={score:.3f}  date={date}  id={chunk_id}  {text[:80]}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print('usage: python -m everthine.memory_recall "query text"')
    else:
        _run_probe(sys.argv[1])
