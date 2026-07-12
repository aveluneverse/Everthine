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

The module is built in three layers: the seven loaders above, the rendering
layer described here, and the CLI at the bottom. This layer follows
portrait_viewer.py's own mold: inline CSS and a single small inline script,
a system font stack, zero CDN, no external reference of any kind, every
dynamic string escaped, and an empty source rendered as a gentle empty state
rather than an error. Three of portrait_viewer's own renderers (content,
Positions, Notes to self) are reused verbatim rather than re-implemented --
publicized with one-line back-compat aliases, the same move load_entries got
in the loader task -- so a self-portrait card reads identically here and on
its own timeline page. The seven sections assemble into one page behind
render_page(), a top table of contents standing in for the navigation a
multi-page site would otherwise need: it sticks to the top of the viewport,
and the page-end script turns it into a true tab bar (click a heading, see
just that section); with JS off it is plain native #anchor scrolling over
the whole visible page. The CLI that calls
render_page() with a real data/ directory is main(), below -- its own
docstring names the privacy promise that the rendered page never leaves
--data-dir, the same gitignored directory every source above already reads
from.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from . import portrait_viewer

logger = logging.getLogger("everthine")

# Loader-layer constants: none are needed. Render-layer chrome (the English
# canonical constants, the per-language CHROME_EN / CHROME_ZH string tables,
# section order, the CSS block, and the inline tab script) lives just above
# render_page(), past the seven loaders below.


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
    `speaker`, or missing a usable `text` is skipped; a missing/wrong-typed
    `timestamp` on a surviving line degrades to "" instead of dropping the
    line -- the mirror image of archive.iter_entries's own tolerance,
    which is stricter about `timestamp` (missing or unparseable drops the
    whole line) and looser about `speaker` (a missing one degrades to "?"
    rather than dropping the line). Bytes that do not decode as UTF-8 are
    dropped by errors="ignore", mirroring the owner's own reader.
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


# =======================================================================
# Rendering: seven sections + a top anchor table of contents, assembled
# into one offline page. Every dynamic string below passes through
# html.escape before it reaches the page. Three of portrait_viewer's own
# renderers -- content, Positions, Notes to self -- are reused verbatim
# rather than re-implemented (see the render_content / render_positions /
# render_notes calls below); that module carries its own back-compat
# aliases for the rename that made them public.
# =======================================================================

# --- Static English chrome, this page's own (portrait_viewer keeps its own
# copy for its own page; this is a different document with a different
# job, so it gets its own canonical strings rather than reaching into the
# sibling module's constants) ---------------------------------------------
PAGE_TITLE = "Observatory"
PAGE_SUBTITLE = "A quiet window on their inner life — kept on this computer, for your eyes only."

# (section id, table-of-contents label) pairs, in the D2-decided reading
# order. The label doubles as the section's own <h2> text, so the nav and
# the heading it points at can never drift apart.
SECTION_ORDER = (
    ("portrait", "Portrait"),
    ("diary", "Diary"),
    ("reflections", "Reflections"),
    ("keepsakes", "Keepsakes"),
    ("facts", "What they know about you"),
    ("conversation", "Recent conversation"),
    ("memory", "Memory room"),
)

EMPTY_PORTRAIT = "No self-portrait yet — they haven't written their first one."
EMPTY_DIARY = "No diary pages yet — the first night window hasn't come."
EMPTY_REFLECTIONS = "No reflections yet — they haven't sat with a conversation long enough."
EMPTY_KEEPSAKES = "No keepsakes yet — react to a line with ❤ and it will be kept here."
EMPTY_FACTS = "Nothing gathered yet — the notebook fills as you talk."
EMPTY_CONVERSATION = "No conversation in this window."
MEMORY_UNAVAILABLE = "The memory index isn't available right now."

# Album direction -> the label a Keepsakes card shows in its eyebrow spot.
# Any OTHER direction string (a value this module doesn't recognize) is
# shown raw, escaped, rather than guessed at -- fail-soft, the same stance
# every loader above takes toward a shape it doesn't know.
DIRECTION_LABELS = {
    "partner_flagged": "You kept this",
    "companion_flagged": "They kept this",
}

# Conversation speaker -> the pronoun a transcript line shows. Any other
# speaker string (archive.py's own vocabulary is exactly
# {"user", "companion"}; there should never be another) is shown raw
# rather than guessed at.
SPEAKER_LABELS = {
    "user": "You",
    "companion": "Them",
}

# The six categories facts_extract.py's own extraction prompt fixes as its
# closed vocabulary, in the group order the brief pins. A category outside
# this set -- hand-edited data, or a future prompt revision this page
# hasn't caught up with -- still renders, grouped by its own exact value
# and placed after all six known groups (oldest-seen-unknown category
# first); it is never dropped and never folded into a guessed bucket.
FACT_CATEGORY_ORDER = ("interest", "mood", "stress", "follow_up", "life_event", "conflict")

# --- i18n chrome tables (T5 r1) -----------------------------------------
# Every localizable string above, bundled per language. CHROME_EN reuses the
# canonical English constants verbatim, so `--lang en` stays byte-identical to
# the page every prior task pinned; CHROME_ZH carries the Traditional Chinese
# pack the product now defaults to (see main()'s --lang, default "zh"). The
# render layer -- render_page() and every _render_*_section() below -- takes
# one of these dicts; they default to CHROME_EN so their unit tests keep
# pinning the English canonical while the CLI selects the user's language.
#
# Line-shaped values are str.format templates with named fields. Every
# substituted value is html-escaped by the caller BEFORE it reaches .format(),
# so the template string is the only place a brace is ever interpreted -- a
# substituted value carrying a stray brace is inserted literally, never
# re-parsed. Label maps (direction/speaker/fact category) are looked up with a
# raw-escaped fallback for any value not in the map, the same fail-soft stance
# every loader takes toward a shape it does not know.
CHROME_EN = {
    "html_lang": "en",
    "page_title": PAGE_TITLE,
    "page_subtitle": PAGE_SUBTITLE,
    # Section id -> localized heading; the id order still lives in
    # SECTION_ORDER, so nav order and section order stay language-independent.
    "section_titles": dict(SECTION_ORDER),
    "empty_portrait": EMPTY_PORTRAIT,
    "empty_diary": EMPTY_DIARY,
    "empty_reflections": EMPTY_REFLECTIONS,
    "empty_keepsakes": EMPTY_KEEPSAKES,
    "empty_facts": EMPTY_FACTS,
    "empty_conversation": EMPTY_CONVERSATION,
    "memory_unavailable": MEMORY_UNAVAILABLE,
    "direction_labels": DIRECTION_LABELS,
    "speaker_labels": SPEAKER_LABELS,
    # English shows the raw category value, exactly as every prior version did;
    # an empty map sends every category through the raw-escaped fallback.
    "fact_categories": {},
    "positions_title": portrait_viewer.SECTION_POSITIONS,
    "notes_title": portrait_viewer.SECTION_NOTES,
    "version_eyebrow": "Version {n} · {updated}",
    "history_line": "{n} earlier version(s) — run python -m everthine.portrait_viewer for the full timeline.",
    "mood_line": "Mood: {mood}",
    "keywords_line": "Keywords: {keywords}",
    "last_gathered_line": "Last gathered: {cursor}",
    "earlier_notice": "Earlier: {days} more day(s), {lines} more line(s) — not shown here.",
    "remembered_line": "Remembered fragments: {n}",
    "covering_line": "Covering: {a} → {b}",
    "index_size_line": "Index size: {n:,} bytes",
}

CHROME_ZH = {
    "html_lang": "zh-Hant",
    "page_title": "觀景窗",
    "page_subtitle": "一扇安靜的窗——他的內在世界，只在這台電腦上，只給你看。",
    "section_titles": {
        "portrait": "自畫像",
        "diary": "日記",
        "reflections": "反思",
        "keepsakes": "珍藏冊",
        "facts": "他記得你的事",
        "conversation": "近期對話",
        "memory": "記憶書房",
    },
    "empty_portrait": "還沒有自畫像——他還沒寫下第一張。",
    "empty_diary": "還沒有日記——第一個夜晚還沒到來。",
    "empty_reflections": "還沒有反思——他還沒對哪段對話坐得夠久。",
    "empty_keepsakes": "還沒有珍藏——對某句話按下 ❤，它就會被收在這裡。",
    "empty_facts": "還沒記下什麼——聊著聊著，小冊子就會滿起來。",
    "empty_conversation": "這段時間沒有對話。",
    "memory_unavailable": "記憶索引現在讀不到。",
    "direction_labels": {
        "partner_flagged": "你珍藏的",
        "companion_flagged": "他珍藏的",
    },
    "speaker_labels": {
        "user": "你",
        "companion": "他",
    },
    "fact_categories": {
        "interest": "興趣",
        "mood": "心情",
        "stress": "壓力",
        "follow_up": "掛心的事",
        "life_event": "生活大事",
        "conflict": "摩擦",
    },
    "positions_title": "立場",
    "notes_title": "自我筆記",
    "version_eyebrow": "第 {n} 版 · {updated}",
    "history_line": "還有 {n} 個更早的版本——跑 python -m everthine.portrait_viewer 看完整時間軸。",
    "mood_line": "心情：{mood}",
    "keywords_line": "關鍵詞：{keywords}",
    "last_gathered_line": "最近一次整理：{cursor}",
    "earlier_notice": "更早：還有 {days} 天、{lines} 句——沒有顯示在這裡。",
    "remembered_line": "記憶片段：{n}",
    "covering_line": "涵蓋：{a} → {b}",
    "index_size_line": "索引大小：{n:,} 位元組",
}


# Warm-paper theme: the same :root tokens portrait_viewer.CSS defines, at
# the same values, so a self-portrait card looks identical on both pages.
# The component classes portrait_viewer's own render_content /
# render_positions / render_notes emit -- .content, .block, .block-title,
# .topic -- are restyled here too: this page never imports that CSS block,
# so it needs its own rules for the very class names those reused
# functions emit. New, page-specific: .toc (the D2 anchor directory),
# .eyebrow/.meta (the small-print lines every section's cards use),
# .fact-group/.day (the two sections that group by heading instead of
# stacking cards), and .stats (the Memory room's three-line <dl>).
CSS = """\
:root {
  --paper: #f4efe4;
  --card: #fbf8f1;
  --ink: #33302a;
  --ink-soft: #736b5d;
  --rule: #e4dccd;
  --accent: #b07d4b;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC",
    "Microsoft JhengHei", "PingFang TC", "Hiragino Sans", sans-serif;
  font-size: 17px;
  line-height: 1.75;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
.page {
  max-width: 680px;
  margin: 0 auto;
  padding: 72px 28px 96px;
}
.masthead { margin-bottom: 40px; }
.masthead h1 {
  margin: 0;
  font-size: 1.55rem;
  font-weight: 600;
  letter-spacing: 0.01em;
}
.masthead .subtitle {
  margin: 12px 0 0;
  font-size: 0.98rem;
  font-weight: 400;
  color: var(--ink-soft);
}

/* Top table of contents (T5 r1): the page's navigation. It sticks to the
   top of the viewport (opaque card background, so scrolled content passes
   behind it, never through it) and, once the page-end script runs, doubles
   as a tab bar -- clicking an item shows just that section. With JS off it
   is plain native #anchor scrolling over the full, all-sections-visible
   page, so nothing here depends on the script. */
.toc {
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 10px;
  padding: 18px 22px;
  margin-bottom: 48px;
  position: sticky;
  top: 0;
  z-index: 10;
}
.toc ul {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
}
.toc a {
  color: var(--accent);
  text-decoration: none;
  font-weight: 600;
  font-size: 0.92rem;
}
.toc a:hover, .toc a:focus { text-decoration: underline; }
/* The active tab, marked by the page-end script; never set without JS. */
.toc a.toc-active {
  color: var(--ink);
  text-decoration: underline;
  text-underline-offset: 3px;
}

section {
  margin-bottom: 56px;
  scroll-margin-top: 24px;
}
section:last-child { margin-bottom: 0; }
section h2 {
  margin: 0 0 20px;
  font-size: 1.05rem;
  font-weight: 600;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--rule);
}

/* Chronological card list: Portrait / Diary / Reflections / Keepsakes all
   stack their entries this way -- the exact timeline-with-dot visual
   portrait_viewer.py established for the standalone page, reused here so
   the two documents read as one family. */
.timeline { position: relative; margin: 0; padding: 0; }
.timeline::before {
  content: "";
  position: absolute;
  left: 6px;
  top: 12px;
  bottom: 12px;
  width: 2px;
  background: var(--rule);
}
.entry { position: relative; padding-left: 40px; margin: 0 0 32px; }
.entry:last-child { margin-bottom: 0; }
.entry::before {
  content: "";
  position: absolute;
  left: 0;
  top: 7px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--card);
  border: 3px solid var(--accent);
  box-shadow: 0 0 0 4px var(--paper);
}
.card {
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 10px;
  padding: 22px 26px 24px;
  box-shadow: 0 1px 2px rgba(60, 50, 30, 0.05);
}
.eyebrow {
  margin: 0 0 12px;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--accent);
}
.meta {
  margin: 10px 0 0;
  font-size: 0.88rem;
  color: var(--ink-soft);
}

/* portrait_viewer's own render_content / render_positions / render_notes
   emit exactly these class names; this page reuses those functions
   verbatim, so it must style their output, not just borrow their look. */
.content { margin: 0; }
.content p { margin: 0 0 1.05em; }
.content p:last-child { margin-bottom: 0; }
.block {
  margin-top: 20px;
  padding-top: 18px;
  border-top: 1px solid var(--rule);
}
.block-title {
  margin: 0 0 10px;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-soft);
}
.block ul { margin: 0; padding: 0; list-style: none; }
.block li { margin: 0 0 8px; }
.block li:last-child { margin-bottom: 0; }
.topic { font-weight: 600; }

/* Facts: a grouped notebook list, not a card stack -- category headings
   over plain <li> lines read faster than seven more timeline cards would. */
.fact-group { margin: 0 0 26px; }
.fact-group:last-child { margin-bottom: 0; }
.fact-group h3 {
  margin: 0 0 10px;
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--ink-soft);
}
.fact-group ul { margin: 0; padding: 0; list-style: none; }
.fact-group li { margin: 0 0 8px; }
.fact-date { color: var(--ink-soft); font-size: 0.88rem; }

/* Recent conversation: a transcript, grouped by day heading. */
.day { margin: 0 0 28px; }
.day:last-child { margin-bottom: 0; }
.day h3 {
  margin: 0 0 10px;
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--ink-soft);
}
.line { margin: 0 0 8px; }
.who { font-weight: 600; color: var(--accent); margin-right: 4px; }

/* Memory room: a three-line <dl>. Wrapped in an overflow-x:auto container
   on principle (the RWD precedent this page follows asks any table-like
   content to sit in one) even though a three-line label/value list is in
   practice never wide enough to need it. */
.stats-wrap { overflow-x: auto; }
.stats { margin: 0; }
.stats dt {
  margin-top: 10px;
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--ink-soft);
}
.stats dt:first-child { margin-top: 0; }

.empty { padding: 6px 0 2px; }
.empty-lead { margin: 0; font-size: 1rem; color: var(--ink-soft); }

@media (max-width: 520px) {
  .page { padding: 48px 18px 64px; }
  .card { padding: 20px 18px 22px; }
  .entry { padding-left: 34px; }
  .toc ul { gap: 6px 14px; }
}"""


# The one inline script this page carries (T5 r1): a tiny tab switcher that
# turns the seven-section stack into true tabs once JS runs. It reads only the
# page's own toc links and sections -- no src, no URL of any kind, no external
# reference whatsoever, keeping the offline/self-contained promise intact. It
# is inert without JS: nothing below runs, no section is ever marked hidden,
# and the default CSS shows all seven sections, so a no-JS viewer gets the
# whole page as one long readable document (the graceful-degradation
# fallback). On load it hides every section but the first and highlights the
# matching toc item; a click switches which section shows.
TAB_SCRIPT = """\
(function () {
  var links = document.querySelectorAll('.toc a[href^="#"]');
  var sections = document.querySelectorAll('section[id]');
  if (!links.length || !sections.length) { return; }
  function activate(id) {
    var i;
    for (i = 0; i < sections.length; i++) {
      sections[i].hidden = sections[i].id !== id;
    }
    for (i = 0; i < links.length; i++) {
      var on = links[i].getAttribute('href') === '#' + id;
      if (on) { links[i].classList.add('toc-active'); }
      else { links[i].classList.remove('toc-active'); }
    }
  }
  for (var j = 0; j < links.length; j++) {
    links[j].addEventListener('click', function (event) {
      event.preventDefault();
      activate(this.getAttribute('href').slice(1));
    });
  }
  activate(sections[0].id);
})();"""


# ---------------------------------------------------------------------
# Small shared markup helpers (this page's own _wrap_block-equivalent)
# ---------------------------------------------------------------------

def _card(eyebrow_html: str, body_html: str) -> str:
    """Wrap one chronological entry in the shared card shell: an eyebrow
    line over a body. `eyebrow_html`/`body_html` are already-escaped,
    already-assembled HTML -- this function adds no escaping of its own,
    mirroring portrait_viewer._render_entry's division of labor (callers
    escape; wrappers just wrap)."""
    return (
        '<article class="entry">\n<div class="card">\n'
        f'<p class="eyebrow">{eyebrow_html}</p>\n'
        f"{body_html}\n"
        "</div>\n</article>"
    )


def _timeline(cards: list) -> str:
    """Wrap a list of already-rendered `_card` articles in the shared
    timeline rail (the vertical rule + dot decoration)."""
    return '<div class="timeline">\n' + "\n".join(cards) + "\n</div>"


def _empty_state(message: str) -> str:
    """The shared empty-state shell every section falls back to. `message`
    is always one of this module's own canonical constants above, never
    user data, so it is not re-escaped here."""
    return f'<div class="empty">\n<p class="empty-lead">{message}</p>\n</div>'


# ---------------------------------------------------------------------
# 1. Portrait section: the latest snapshot, full card + history count
# ---------------------------------------------------------------------

def _render_portrait_section(portraits: list, chrome: dict = CHROME_EN) -> str:
    """The latest self-portrait (the last entry -- load_portraits returns
    oldest-first, portrait_viewer.load_entries' own contract), rendered
    through portrait_viewer's own render_content / render_positions /
    render_notes so this card is identical to the one on the standalone
    timeline page (the Positions/Notes headings come from `chrome`, the only
    localized part of that reuse). A history line ("N earlier version(s)...")
    follows when older snapshots exist, and is omitted outright when this is
    the only one (n == 1, so earlier == 0)."""
    if not portraits:
        return _empty_state(chrome["empty_portrait"])
    n = len(portraits)
    latest = portraits[-1]
    eyebrow = chrome["version_eyebrow"].format(n=n, updated=html.escape(latest['updated']))
    body_parts = [f'<div class="content">{portrait_viewer.render_content(latest["content"])}</div>']
    for block in (portrait_viewer.render_positions(latest["opinions"], chrome["positions_title"]),
                  portrait_viewer.render_notes(latest["observations"], chrome["notes_title"])):
        if block:
            body_parts.append(block)
    parts = [_timeline([_card(eyebrow, "\n".join(body_parts))])]
    earlier = n - 1
    if earlier > 0:
        parts.append(f'<p class="meta">{chrome["history_line"].format(n=earlier)}</p>')
    return "\n".join(parts)


# ---------------------------------------------------------------------
# 2. Diary section: every page, date-ordered cards
# ---------------------------------------------------------------------

def _render_diary_section(entries: list, chrome: dict = CHROME_EN) -> str:
    """All diary pages, in the order load_diary_entries already returns
    them (filename order = chronological). Each card: a date eyebrow, an
    optional "Mood: ..." line, an optional "Keywords: a · b · c" line,
    then the page's prose through render_content -- in that order."""
    if not entries:
        return _empty_state(chrome["empty_diary"])
    cards = []
    for entry in entries:
        eyebrow = html.escape(entry["date"])
        body_parts = []
        if entry["mood"]:
            body_parts.append(
                f'<p class="meta">{chrome["mood_line"].format(mood=html.escape(entry["mood"]))}</p>')
        if entry["keywords"]:
            joined = " · ".join(html.escape(word) for word in entry["keywords"])
            body_parts.append(
                f'<p class="meta">{chrome["keywords_line"].format(keywords=joined)}</p>')
        body_parts.append(f'<div class="content">{portrait_viewer.render_content(entry["content"])}</div>')
        cards.append(_card(eyebrow, "\n".join(body_parts)))
    return _timeline(cards)


# ---------------------------------------------------------------------
# 3. Reflections section: chronological list, date-prefix eyebrow
# ---------------------------------------------------------------------

def _render_reflections_section(entries: list, chrome: dict = CHROME_EN) -> str:
    """Every reflection, in file order (already chronological -- appends
    are chronological, per load_reflections). Each card's eyebrow is the
    first 10 characters of created_at (its date part; a short or blank
    timestamp is kept as-is, never padded or guessed at)."""
    if not entries:
        return _empty_state(chrome["empty_reflections"])
    cards = []
    for entry in entries:
        eyebrow = html.escape(entry["created_at"][:10])
        body = f'<div class="content">{portrait_viewer.render_content(entry["text"])}</div>'
        cards.append(_card(eyebrow, body))
    return _timeline(cards)


# ---------------------------------------------------------------------
# 4. Keepsakes section: the kept album, direction-labeled cards
# ---------------------------------------------------------------------

def _render_keepsakes_section(entries: list, chrome: dict = CHROME_EN) -> str:
    """Every kept moment, in storage order. The eyebrow is the direction
    label (chrome["direction_labels"], or the raw escaped string for
    anything this module doesn't recognize -- fail-soft, never guessed);
    the body is the kept message through render_content, followed by an
    optional "— speaker, timestamp" byline that is dropped whole when
    speaker is blank (a hand-edited or otherwise incomplete entry)."""
    if not entries:
        return _empty_state(chrome["empty_keepsakes"])
    cards = []
    for entry in entries:
        direction = entry["direction"]
        label = chrome["direction_labels"].get(direction, html.escape(direction))
        body_parts = [f'<div class="content">{portrait_viewer.render_content(entry["message"])}</div>']
        if entry["speaker"]:
            body_parts.append(
                f'<p class="meta">— {html.escape(entry["speaker"])}, {html.escape(entry["timestamp"])}</p>')
        cards.append(_card(label, "\n".join(body_parts)))
    return _timeline(cards)


# ---------------------------------------------------------------------
# 5. Facts section: grouped by category, fixed order then first-seen
# ---------------------------------------------------------------------

def _render_facts_section(facts: list, cursor: str | None,
                          chrome: dict = CHROME_EN) -> str:
    """Facts grouped by category: the FACT_CATEGORY_ORDER groups first
    (only the ones actually present, in that fixed order), then any other
    category value in first-seen order. Grouping and ordering are by the
    raw category value; only the heading TEXT localizes, via
    chrome["fact_categories"] (the six known categories get a localized
    label, any other value is shown raw and escaped rather than folded into
    a guessed bucket -- the same fail-soft stance in either language). Each
    group keeps its facts in storage order. A "Last gathered: ..." footer
    follows whenever cursor is truthy -- excluding both None (a read
    failure) and the never-extracted sentinel (an empty string; the
    controller's 2026-07-12 refinement) -- independent of whether any facts
    exist: the cursor describes the extractor's own state, separate from
    whether it has found anything worth keeping yet, so it is not folded
    into the empty-state branch below."""
    parts = []
    if not facts:
        parts.append(_empty_state(chrome["empty_facts"]))
    else:
        groups: dict = {}
        for fact in facts:
            groups.setdefault(fact["category"], []).append(fact)
        ordered = [c for c in FACT_CATEGORY_ORDER if c in groups]
        ordered += [c for c in groups if c not in FACT_CATEGORY_ORDER]
        group_html = []
        for category in ordered:
            heading = chrome["fact_categories"].get(category, html.escape(category))
            items = []
            for fact in groups[category]:
                text = html.escape(fact["text"])
                if fact["date"]:
                    items.append(
                        f'<li>{text} <span class="fact-date">({html.escape(fact["date"])})</span></li>')
                else:
                    items.append(f"<li>{text}</li>")
            group_html.append(
                '<div class="fact-group">\n'
                f"<h3>{heading}</h3>\n"
                "<ul>\n" + "\n".join(items) + "\n</ul>\n"
                "</div>"
            )
        parts.append("\n".join(group_html))
    if cursor:
        parts.append(
            f'<p class="meta">{chrome["last_gathered_line"].format(cursor=html.escape(cursor))}</p>')
    return "\n".join(parts)


# ---------------------------------------------------------------------
# 6. Conversation section: the recent window, grouped by day
# ---------------------------------------------------------------------

def _render_conversation_section(window: list, elder_days: int, elder_msgs: int,
                                 chrome: dict = CHROME_EN) -> str:
    """The conversation window, split into per-day sub-sections in the
    order the entries already arrive (filename+line order = chronological).
    An "Earlier: ..." notice sits above the transcript whenever
    elder_days > 0 -- strictly that count, per the brief (elder_msgs never
    gates the notice on its own; load_conversation_window's own contract
    makes elder_msgs > 0 imply elder_days > 0 in practice, but this render
    layer follows the letter of the spec rather than lean on that
    invariant). The notice is independent of the window itself being
    empty: "nothing in the last N days, but M days are waiting" is the one
    case where showing both matters most."""
    parts = []
    if elder_days > 0:
        parts.append(
            f'<p class="meta">{chrome["earlier_notice"].format(days=elder_days, lines=elder_msgs)}</p>')
    if not window:
        parts.append(_empty_state(chrome["empty_conversation"]))
        return "\n".join(parts)
    days: list = []
    current_date = None
    current_lines: list = []
    for msg in window:
        if msg["date"] != current_date:
            current_date = msg["date"]
            current_lines = []
            days.append((current_date, current_lines))
        speaker = chrome["speaker_labels"].get(msg["speaker"], msg["speaker"])
        current_lines.append(
            f'<p class="line"><span class="who">{html.escape(speaker)}</span> {html.escape(msg["text"])}</p>')
    day_html = []
    for day_date, lines in days:
        day_html.append(
            '<div class="day">\n'
            f"<h3>{html.escape(day_date)}</h3>\n" + "\n".join(lines) + "\n</div>"
        )
    parts.append("\n".join(day_html))
    return "\n".join(parts)


# ---------------------------------------------------------------------
# 7. Memory room section: the memory store's vital signs
# ---------------------------------------------------------------------

def _render_memory_section(stats: dict | None, chrome: dict = CHROME_EN) -> str:
    """None means the index itself could not be read (load_memory_stats'
    own contract); anything else -- including the honest all-zero dict an
    empty-but-valid chunks table returns -- is real data. The "Covering"
    line is skipped when either timestamp is None (always true of both
    together in practice, since they come from the same MIN/MAX query, but
    each is checked independently per the brief); the other two lines
    always render, including "Remembered fragments: 0"."""
    if stats is None:
        return _empty_state(chrome["memory_unavailable"])
    lines = [f'<dt>{chrome["remembered_line"].format(n=stats["chunk_count"])}</dt>']
    if stats["earliest_ts"] is not None and stats["latest_ts"] is not None:
        lines.append(
            f'<dt>{chrome["covering_line"].format(a=html.escape(stats["earliest_ts"]), b=html.escape(stats["latest_ts"]))}</dt>')
    lines.append(f'<dt>{chrome["index_size_line"].format(n=stats["db_size_bytes"])}</dt>')
    return '<div class="stats-wrap">\n<dl class="stats">\n' + "\n".join(lines) + "\n</dl>\n</div>"


# ---------------------------------------------------------------------
# Page assembly: the seven sections + a top anchor table of contents
# ---------------------------------------------------------------------

def render_page(sections_data: dict, chrome: dict = CHROME_EN) -> str:
    """Assemble the full offline Observatory page from the seven loaders'
    output, bundled by the caller (main(), below) into one dict:

        {
            "portraits": load_portraits(...),
            "diary": load_diary_entries(...),
            "reflections": load_reflections(...),
            "album": load_album(...),
            "facts": load_facts(...),
            "facts_cursor": load_facts_cursor(...),
            "conversation": load_conversation_window(...)[0],
            "elder_days": load_conversation_window(...)[1],
            "elder_msgs": load_conversation_window(...)[2],
            "memory_stats": load_memory_stats(...),
        }

    All ten keys are required: a caller that omits one raises KeyError
    immediately rather than silently rendering a section that looks like
    an honest empty state. (The loaders stay fail-soft against bad *data*;
    this dict is a programming contract between this function and its
    caller, not a data read, so it is allowed to fail loud -- a missing
    key is a bug in the caller, not a gap in the companion's saved life.)

    `chrome` selects the language pack (CHROME_EN / CHROME_ZH); it defaults
    to the English canonical so a direct caller and every render unit test
    keep pinning that page unchanged, while the CLI passes the user's choice
    (main(), --lang, default zh). SECTION_ORDER still fixes the id order and
    reading order in both languages; only the labels come from chrome.

    Returns one self-contained HTML document: inline CSS, one tiny inline
    tab script and no external reference of any kind, every dynamic string
    escaped, a top <nav> of seven #anchors leading to seven
    <section id="...">, in the D2-decided reading order (Portrait, Diary,
    Reflections, Keepsakes, What they know about you, Recent conversation,
    Memory room). The nav doubles as a sticky tab bar once the script runs;
    with JS off it is a plain anchor list over the whole visible page.
    """
    section_html = {
        "portrait": _render_portrait_section(sections_data["portraits"], chrome),
        "diary": _render_diary_section(sections_data["diary"], chrome),
        "reflections": _render_reflections_section(sections_data["reflections"], chrome),
        "keepsakes": _render_keepsakes_section(sections_data["album"], chrome),
        "facts": _render_facts_section(
            sections_data["facts"], sections_data["facts_cursor"], chrome),
        "conversation": _render_conversation_section(
            sections_data["conversation"], sections_data["elder_days"],
            sections_data["elder_msgs"], chrome),
        "memory": _render_memory_section(sections_data["memory_stats"], chrome),
    }
    titles = chrome["section_titles"]
    toc_items = "\n".join(
        f'<li><a href="#{section_id}">{titles[section_id]}</a></li>'
        for section_id, _label in SECTION_ORDER)
    toc = f'<nav class="toc">\n<ul>\n{toc_items}\n</ul>\n</nav>'
    sections = "\n".join(
        f'<section id="{section_id}">\n<h2>{titles[section_id]}</h2>\n{section_html[section_id]}\n</section>'
        for section_id, _label in SECTION_ORDER)
    return _wrap_page(toc, sections, chrome)


def _wrap_page(toc_html: str, sections_html: str, chrome: dict = CHROME_EN) -> str:
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{chrome["html_lang"]}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{chrome["page_title"]}</title>\n'
        f"<style>\n{CSS}\n</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="page">\n'
        '<header class="masthead">\n'
        f'<h1>{chrome["page_title"]}</h1>\n'
        f'<p class="subtitle">{chrome["page_subtitle"]}</p>\n'
        "</header>\n"
        f"{toc_html}\n"
        f"{sections_html}\n"
        "</div>\n"
        f"<script>\n{TAB_SCRIPT}\n</script>\n"
        "</body>\n"
        "</html>\n"
    )


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m everthine.observatory",
        description="Render the companion's whole inner life into one offline HTML page.",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help='The companion data directory (diary/, reflections.jsonl, '
             'portrait_history/, album.json, facts.json, archive/, '
             'memory.db) (default: "data").',
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="How many days of recent conversation the Recent conversation "
             "section looks back over (default: 14).",
    )
    parser.add_argument(
        "--lang",
        choices=("zh", "en"),
        default="zh",
        help="Interface language for the rendered page: zh (Traditional "
             "Chinese, the default) or en (English). Only the page's own "
             "chrome is translated; the companion's saved words are never "
             'touched (default: "zh").',
    )
    return parser


def main(argv=None) -> int:
    """Render one data/ directory into observatory.html and write the page
    back into that same directory, never anywhere else -- `--data-dir` is
    the only place a real filesystem path enters this module, and
    `out_path = data_dir / "observatory.html"` below is the whole of that
    guarantee: the privacy promise data/ already keeps (gitignored, never
    committed) extends automatically to the page this writes, because it
    can only ever land inside the directory it was pointed at. `today =
    date.today()` is the only clock read anywhere in this module -- every
    loader above stays a pure function of the plain Paths (and, for
    load_conversation_window, the caller-supplied date) this function
    composes for it.
    """
    # Windows consoles often default to a legacy codepage (e.g. cp950); the
    # path we print can carry non-ASCII, so force UTF-8 with replacement
    # rather than crash. Guard hasattr for streams that lack reconfigure
    # (test capture, pipes) -- the same guard portrait_viewer.main uses.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be >= 1")

    data_dir = Path(args.data_dir)
    today = date.today()
    chrome = CHROME_ZH if args.lang == "zh" else CHROME_EN

    conversation, elder_days, elder_msgs = load_conversation_window(
        data_dir / "archive", args.days, today)

    sections_data = {
        "portraits": load_portraits(data_dir / "portrait_history"),
        "diary": load_diary_entries(data_dir / "diary"),
        "reflections": load_reflections(data_dir / "reflections.jsonl"),
        "album": load_album(data_dir / "album.json"),
        "facts": load_facts(data_dir / "facts.json"),
        "facts_cursor": load_facts_cursor(data_dir / "facts_state.json"),
        "conversation": conversation,
        "elder_days": elder_days,
        "elder_msgs": elder_msgs,
        "memory_stats": load_memory_stats(data_dir / "memory.db"),
    }
    page = render_page(sections_data, chrome)

    out_path = data_dir / "observatory.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
