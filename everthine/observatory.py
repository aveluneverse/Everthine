"""The observatory: one command that renders the companion's whole inner
life -- diary pages, post-reply reflections, self-portrait snapshots, kept
album moments, gathered facts, the recent conversation, and the memory
store's vital signs -- out of data/ into a single, self-contained, offline
HTML page the user can double-click open. It is portrait_viewer.py's
seven-source sibling: where that page shows who the companion has noticed
itself becoming, this one shows the whole inner life at a glance.

This module is a deliberate island, exactly as portrait_viewer.py is. It
imports neither config nor bot nor engine nor persona nor facts: every
loader takes a plain Path (plus, for the conversation window, a
caller-supplied date) that the caller composes, so a future CLI can invoke
it with nothing but a data directory and no BOT_TOKEN in sight. The
companion does not know the observatory exists -- there is no bot-side
wiring in either direction, nothing here ever writes into the data it
reads (memory.db is opened through a read-only sqlite URI; every other
source is plain file reading), and nothing it produces is ever fed back
into a prompt.

The island has one deliberate cost: this module re-reads, with its own
small readers, files whose schemas are owned elsewhere. facts.json's
schema source of truth is facts.py ({"facts": [...]}, a dict wrapper,
never a bare list); album.json's is album.py (each entry's message is a
nested {"speaker", "text"} dict); diary entries' is diary.save_entry;
reflections.jsonl's is reflection.append_entry; the archive's is
archive.py; the chunks table's is memory_store.MemoryStore. Two places
reading one file is the price of never importing the modules that own
them; each loader's docstring names its owner so a schema change there
knows where to look. The one exception is the self-portrait history,
whose owner (portrait_viewer.load_entries) is itself an island and is
imported directly rather than re-implemented.

Fail-soft throughout, mirroring the rest of the codebase, with the try
boundary at file/line granularity: a broken JSON file is logged and
skipped; a broken line or mis-shaped record inside an otherwise-healthy
file is silently dropped (routine line-level noise would flood a log);
a missing file or directory is quietly an empty source -- nothing has
gone wrong, there is simply nothing written yet. No loader ever raises.

This task ships the module skeleton and the seven loaders. Rendering and
the CLI arrive in the next tasks, in portrait_viewer.py's mold: inline
CSS only, a system font stack, zero CDN, zero JS, every dynamic string
escaped, and an empty source rendered as a gentle empty state rather
than an error.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import portrait_viewer

logger = logging.getLogger("everthine")

# Loader-layer constants would live here; none are needed yet. The page
# chrome (title, section labels, CSS) arrives with the render task.


# ---------------------------------------------------------------------
# 1. Diary: {diary_dir}/*.json, filename-ordered, fail-soft per file
# ---------------------------------------------------------------------

def load_diary_entries(diary_dir: Path) -> list[dict]:
    """Read every {diary_dir}/*.json diary entry, oldest first -- the entry
    naming convention ("YYYY-MM-DD_HHMMSS.json"; schema owner:
    diary.save_entry) makes a lexical filename sort the chronological
    order. Fail-soft per file, in portrait_viewer.load_entries' exact
    mold: an unreadable or invalid-JSON file, a non-object payload, or an
    entry with no usable `content` is logged and skipped. A missing/blank
    `date` falls back to the filename stem; a missing/wrong-typed `mood`
    degrades to ""; `keywords` keeps only its string elements and degrades
    to [] when it is not a list at all. Returns {date, content, mood,
    keywords} dicts in filename order.
    """
    entries: list[dict] = []
    diary_dir = Path(diary_dir)
    if not diary_dir.is_dir():
        return entries
    for path in sorted(diary_dir.glob("*.json"), key=lambda p: p.name):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError subsumes both json.JSONDecodeError and the
            # UnicodeDecodeError a corrupt-encoding file raises out of
            # read_text -- the same tuple portrait_viewer.load_entries uses.
            logger.warning("observatory: skipping unreadable diary file %s (%s)", path, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("observatory: skipping non-object diary file %s", path)
            continue
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.warning("observatory: skipping diary file %s (no content)", path)
            continue
        entry_date = data.get("date")
        if not isinstance(entry_date, str) or not entry_date.strip():
            entry_date = path.stem
        mood = data.get("mood")
        if not isinstance(mood, str):
            mood = ""
        keywords = data.get("keywords")
        if isinstance(keywords, list):
            keywords = [word for word in keywords if isinstance(word, str)]
        else:
            keywords = []
        entries.append({
            "date": entry_date,
            "content": content,
            "mood": mood,
            "keywords": keywords,
        })
    return entries


# ---------------------------------------------------------------------
# 2. Reflections: reflections.jsonl, file order, fail-soft per line
# ---------------------------------------------------------------------

def load_reflections(path: Path) -> list[dict]:
    """Read the reflections file (schema owner: reflection.append_entry --
    one {id, created_at, text} object per line), keeping file order:
    appends are chronological, so the file order IS the time order.
    Fail-soft per line: a line that is not valid JSON, not an object, or
    has no usable `text` is silently skipped without breaking the read; a
    missing/wrong-typed `created_at` degrades to "". A missing file is
    quietly an empty list (nothing written yet); an unreadable one is
    logged and reads as empty. Returns {created_at, text} dicts.
    """
    path = Path(path)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as exc:
        logger.warning("observatory: could not read reflections %s (%s)", path, exc)
        return []
    entries: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        created_at = data.get("created_at")
        if not isinstance(created_at, str):
            created_at = ""
        entries.append({"created_at": created_at, "text": text})
    return entries


# ---------------------------------------------------------------------
# 3. Portraits: a direct reuse of portrait_viewer's own loader
# ---------------------------------------------------------------------

def load_portraits(history_dir: Path) -> list[dict]:
    """Every self-portrait snapshot, oldest first -- a direct reuse of
    portrait_viewer.load_entries (the schema owner), which already carries
    the full fail-soft contract this module needs: unreadable, invalid, or
    contentless snapshots are logged and skipped; `updated` falls back to
    the filename stem; opinions/observations degrade to []. Importing the
    sibling island keeps one reader for this format instead of two, and
    costs nothing the island rule protects: portrait_viewer imports none
    of the modules this one refuses.
    """
    return portrait_viewer.load_entries(Path(history_dir))


# ---------------------------------------------------------------------
# 4. Album: album.json, storage order, fail-soft per entry
# ---------------------------------------------------------------------

def load_album(path: Path) -> list[dict]:
    """Read the keepsake album (schema owner: album.py -- a
    {"version": 1, "entries": [...]} dict whose every entry carries a
    NESTED message dict {"speaker": str, "text": str}, never a bare
    string). A missing file is quietly an empty list; an unreadable or
    invalid file, a non-dict top level, or a non-list `entries` is logged
    and reads as empty. Per entry, silently: a non-dict entry, a non-dict
    `message`, or a missing/blank message text is skipped; `direction`,
    `speaker` (from message["speaker"]), and `timestamp` degrade to ""
    when missing or mistyped. Returns {direction, speaker, message,
    timestamp} dicts in storage order (chronological by when each moment
    was kept), where `message` is the kept line's text.
    """
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("observatory: could not read album %s (%s)", path, exc)
        return []
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        logger.warning("observatory: skipping mis-shaped album %s", path)
        return []
    kept: list[dict] = []
    for entry in data["entries"]:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        direction = entry.get("direction")
        if not isinstance(direction, str):
            direction = ""
        speaker = message.get("speaker")
        if not isinstance(speaker, str):
            speaker = ""
        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str):
            timestamp = ""
        kept.append({
            "direction": direction,
            "speaker": speaker,
            "message": text,
            "timestamp": timestamp,
        })
    return kept


# ---------------------------------------------------------------------
# 5. Facts: facts.json + the extraction cursor, fail-soft per fact
# ---------------------------------------------------------------------

def load_facts(path: Path) -> list[dict]:
    """Read the fact book (schema owner: facts.py -- {"facts": [...]}, a
    DICT wrapper around the list, never a bare list; the wrapper exists so
    the on-disk shape can grow a sibling key without a migration). This
    module reads the file itself rather than importing everthine.facts:
    that module is bound into the config vocabulary, and the island rule
    outranks reuse here -- the two readers are reconciled by naming the
    owner. A missing file is quietly an empty list; an unreadable or
    invalid file, a non-dict top level (including a bare-list file, which
    no shipped version ever wrote), or a missing/non-list `facts` key is
    logged and reads as empty. Per fact, silently: a non-dict entry or a
    missing/blank `text` is skipped; `category` and `date` degrade to "".
    Returns {category, date, text} dicts in storage order (chronological,
    newest at the back, as the owner appends).
    """
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("observatory: could not read facts %s (%s)", path, exc)
        return []
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        logger.warning("observatory: skipping mis-shaped facts file %s", path)
        return []
    facts: list[dict] = []
    for fact in data["facts"]:
        if not isinstance(fact, dict):
            continue
        text = fact.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        category = fact.get("category")
        if not isinstance(category, str):
            category = ""
        fact_date = fact.get("date")
        if not isinstance(fact_date, str):
            fact_date = ""
        facts.append({"category": category, "date": fact_date, "text": text})
    return facts


def load_facts_cursor(state_path: Path) -> str | None:
    """The extraction cursor out of facts_state.json (schema owner:
    facts.py -- {"last_extracted_ts": str}, where "" is the never-extracted
    sentinel and passes through as-is: it is a successful read, not a
    failure). Any failure at all is None: a missing file, unreadable
    bytes, invalid JSON, a non-dict top level, a missing or non-string
    value. Only the missing-file case is quiet (nothing extracted yet is
    a normal day); every other failure logs a warning first.
    """
    state_path = Path(state_path)
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("observatory: could not read facts state %s (%s)", state_path, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("last_extracted_ts"), str):
        logger.warning("observatory: skipping mis-shaped facts state %s", state_path)
        return None
    return data["last_extracted_ts"]


# ---------------------------------------------------------------------
# 6. Conversation window: archive/*.jsonl split at a caller-chosen horizon
# ---------------------------------------------------------------------

def load_conversation_window(archive_dir: Path, days: int,
                             today: date) -> tuple[list[dict], int, int]:
    """Split the conversation archive (schema owner: archive.py -- one
    YYYY-MM-DD.jsonl file per local day, one {timestamp, speaker, text}
    object per line) at a horizon: the last `days` days of messages come
    back whole, everything older comes back as two honest counts. Returns
    (window_entries, earlier_day_count, earlier_message_count), where
    window_entries are {date, speaker, text, timestamp} dicts in
    chronological order -- filename order, then line order, since appends
    within a day are already chronological.

    "The last `days` days" means file dates >= today - (days - 1), with
    `today` supplied by the caller (a plain datetime.date), so the split
    is a pure function of its arguments -- no clock is read here. The day
    count counts FILES whose stem parses as a date (one file per day is
    the archive's contract); the message count counts only lines that
    would have survived the window's own per-line rules, so the "N more
    messages" figure never inflates itself with junk lines.

    Fail-soft: a missing directory is quietly ([], 0, 0). A file whose
    stem does not parse as a date, or that cannot be read at all, is
    logged and contributes nothing anywhere. Within a file, silently: a
    line that is not valid JSON, not an object, missing a string
    `speaker`, or missing a usable `text` is skipped (the same tolerance
    archive.iter_entries shows the same file); a missing/wrong-typed
    `timestamp` on a surviving line degrades to "". Bytes that do not
    decode as UTF-8 are dropped by errors="ignore", mirroring the owner's
    own reader.
    """
    archive_dir = Path(archive_dir)
    window: list[dict] = []
    earlier_days = 0
    earlier_messages = 0
    if not archive_dir.is_dir():
        return window, earlier_days, earlier_messages
    cutoff = today - timedelta(days=days - 1)
    for path in sorted(archive_dir.glob("*.jsonl"), key=lambda p: p.name):
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            logger.warning("observatory: skipping archive file %s (name is not a date)", path)
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError as exc:
            logger.warning("observatory: could not read archive file %s (%s)", path, exc)
            continue
        in_window = file_date >= cutoff
        if not in_window:
            earlier_days += 1
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            speaker = data.get("speaker")
            text = data.get("text")
            if not isinstance(speaker, str) or not isinstance(text, str) or not text.strip():
                continue
            if not in_window:
                earlier_messages += 1
                continue
            timestamp = data.get("timestamp")
            if not isinstance(timestamp, str):
                timestamp = ""
            window.append({
                "date": path.stem,
                "speaker": speaker,
                "text": text,
                "timestamp": timestamp,
            })
    return window, earlier_days, earlier_messages


# ---------------------------------------------------------------------
# 7. Memory stats: memory.db opened READ-ONLY, one aggregate query
# ---------------------------------------------------------------------

def _memory_db_uri(db_path: Path) -> str:
    """The read-only sqlite URI for memory.db. as_posix() because sqlite
    URIs take forward slashes on every platform (a Windows path's
    backslashes would otherwise need escaping games); mode=ro is the whole
    point -- the observatory must be physically incapable of writing into
    the companion's memory, not merely polite about it. Kept as its own
    tiny helper so the test that proves an INSERT down this URI raises is
    exercising the loader's exact connection string, not a re-derivation.
    """
    return f"file:{Path(db_path).as_posix()}?mode=ro"


def load_memory_stats(db_path: Path) -> dict | None:
    """The memory store's vital signs, without ever being able to touch
    it: chunk count, first and last chunk timestamps, and the database
    file's size in bytes (schema owner: memory_store.MemoryStore -- the
    `chunks` table's chunk_id/ts columns). The connection is opened
    read-only via a sqlite URI (mode=ro, uri=True), so a write statement
    down this handle raises inside sqlite itself -- a test pins that.
    None, with a warning, on any failure at all: a missing file, a
    connection that will not open, a file that is not a database, a
    database without the table. An empty chunks table is NOT a failure:
    it returns chunk_count 0 with None timestamps (SQL MIN/MAX of no
    rows), an honest zero rather than a missing gauge. The connection is
    closed in a finally: an open sqlite handle blocks directory deletion
    on Windows, and a viewer must never pin down the data directory it
    read.
    """
    db_path = Path(db_path)
    conn = None
    try:
        size = db_path.stat().st_size
        conn = sqlite3.connect(_memory_db_uri(db_path), uri=True)
        row = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM chunks").fetchone()
        return {
            "chunk_count": row[0],
            "earliest_ts": row[1],
            "latest_ts": row[2],
            "db_size_bytes": size,
        }
    except (OSError, sqlite3.Error) as exc:
        logger.warning("observatory: could not read memory stats from %s (%s)", db_path, exc)
        return None
    finally:
        if conn is not None:
            conn.close()
