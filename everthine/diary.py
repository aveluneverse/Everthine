"""The private nightly diary: reflections a companion writes for itself
after the person it loves has gone quiet for the night, never performed
for anyone and never fed back into a live conversation uninvited.

This module is the state-and-parsing half of the pipeline: the small
daily counter that answers "has he already written tonight," the pure
eligibility check that decides whether writing is appropriate right
now, parsing and validating whatever the engine hands back for an
entry, a sensitive-data filter run before any of it touches disk, and
the save/read of entries themselves. A later addition to this same file
assembles the material and prompt text a write is built from; a
milestone after that calls the engine and wires the result into the
running companion. Neither exists yet -- nothing here calls out to an
engine, a bot, or a persona.

Fail-soft is the whole design for the state file, exactly as in
stages.py and album.py: a missing diary_state.json quietly becomes a
fresh, nothing-written-yet state. A corrupted one -- unreadable, not
valid JSON, or valid JSON in an unexpected shape -- is not silently
discarded: the broken file is renamed alongside itself as a
`.corrupt-<timestamp>` corpse so nothing is lost to a human who goes
looking, a warning is logged loudly, and the caller still gets back a
usable fresh state.

The eligibility check itself is pure -- no clock, no filesystem -- so
every reason a write gets skipped can be tested in isolation, and at
call sites logged verbatim: every skip is one of a small fixed set of
strings, chosen so a quiet night is always explainable after the fact,
never a silent no-op.

Like stages.py and album.py, this module takes plain paths, dicts, and
datetimes; it does not import engine/bot/persona/config, so it stays
dependency-light and testable on its own. The one exception is the
`Config` name used only in type hints, imported under TYPE_CHECKING so
it costs nothing at runtime and never becomes a real dependency edge.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("everthine")

# --- Module constants (this milestone's home; a later addition to this
# same file, and the bot-wiring milestone after it, import these) ---

DIARY_IDLE_MINUTES = 30          # she must be away this long before he writes
DIARY_WINDOW_END_HOUR = 8        # the nightly window always closes at 08:00
DIARY_MIN_INTERVAL_HOURS = 4     # min spacing between entries (matters when DIARY_MAX_DAILY > 1)
DIARY_LOOKBACK_HOURS = 24        # how much conversation the page draws from (a later task uses this)
DIARY_CONTEXT_MAX_CHARS = 24000  # material cap before tail-truncation (a later task uses this)
DIARY_TIMEOUT_S = 90             # engine budget for one entry (a later task uses this)

_DIARY_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}\.json$")

# Redaction patterns, checked in order against any prose bound for disk.
_SENSITIVE_PATTERNS = (
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\s*=\s*\S+"),                     # ENV-style assignment
    re.compile(r"\b\w+:[^\s@]+@\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"),   # user:pass@IPv4
    re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_BARE_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


# ---------------------------------------------------------------------
# State file: cfg.diary_state_path (data/diary_state.json)
# ---------------------------------------------------------------------

def _fresh_state() -> dict:
    return {"count_date": None, "count_today": 0, "declined_date": None}


def _is_well_shaped(data) -> bool:
    """The single place corruption is decided for the state file. bool
    is deliberately excluded from the count_today check even though
    Python's bool is a subclass of int -- a state file with
    `"count_today": true` is not a count, it is corruption."""
    if not isinstance(data, dict):
        return False
    if "count_date" not in data or "count_today" not in data or "declined_date" not in data:
        return False
    count_date = data["count_date"]
    if count_date is not None and not isinstance(count_date, str):
        return False
    declined_date = data["declined_date"]
    if declined_date is not None and not isinstance(declined_date, str):
        return False
    count_today = data["count_today"]
    if isinstance(count_today, bool) or not isinstance(count_today, int):
        return False
    return True


def _quarantine_corpse(path: Path, reason: Exception) -> None:
    """Rename a broken diary-state file to a timestamped corpse alongside
    itself, then log loudly. A rename failure -- a target collision, a
    permissions problem, the file vanishing underneath us -- is
    swallowed with its own warning: corpse preservation is a courtesy,
    never a reason to crash a reply. Verbatim copy of stages.py's
    _quarantine_corpse, retargeted at the diary's own vocabulary."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    corpse = path.with_name(path.name + f".corrupt-{timestamp}")
    try:
        if corpse.exists():
            raise FileExistsError(f"corpse target already exists: {corpse}")
        path.rename(corpse)
    except OSError as exc:
        logger.warning(
            "diary: could not preserve broken state file %s as %s (%s); "
            "leaving the broken file in place", path, corpse, exc)
    logger.warning(
        "diary: %s is corrupt (%s); degrading to a fresh diary state", path, reason)


def load_state(path: Path) -> dict:
    """Load the diary state, degrading fail-soft on any trouble -- mirrors
    stages.load_state exactly. A missing file returns a fresh state
    quietly: nothing has gone wrong yet, there is simply nothing written
    so far. Anything else that goes wrong -- unreadable, not valid JSON,
    or valid JSON in the wrong shape -- is treated as corruption: see
    _quarantine_corpse.
    """
    path = Path(path)
    if not path.exists():
        return _fresh_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not _is_well_shaped(data):
            raise ValueError(f"unexpected diary state shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return _fresh_state()
    return data


def _atomic_write(path: Path, data: dict, *, trailing_newline: bool = False) -> None:
    """Write to a temp file in the same directory, then os.replace() into
    place, so a reader never observes a half-written file. Copies
    stages.py's atomic-write idiom, with one addition: diary entries
    (unlike the state file) are meant to be opened and read by a human,
    so save_entry asks for a trailing newline; the state file does not.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            if trailing_newline:
                fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def record_written(path: Path, now: datetime) -> None:
    """Record that an entry was written just now, rolling the daily
    counter over first if the last write landed on an earlier date. The
    date is decided entirely by `now` -- this function reads no clock of
    its own."""
    path = Path(path)
    state = load_state(path)
    today = now.date().isoformat()
    if state.get("count_date") != today:
        state["count_today"] = 0
    state["count_today"] += 1
    state["count_date"] = today
    _atomic_write(path, state)


def record_declined(path: Path, now: datetime) -> None:
    """Record that he considered writing tonight and chose not to. Only
    declined_date changes -- the write counter is left exactly as it
    was; a decline is not a write and never touches it."""
    path = Path(path)
    state = load_state(path)
    state["declined_date"] = now.date().isoformat()
    _atomic_write(path, state)


# ---------------------------------------------------------------------
# Eligibility: pure, no I/O, no clock of its own
# ---------------------------------------------------------------------

def eligibility(cfg: Config, now: datetime, last_contact: datetime | None,
                state: dict, hours_since_diary: float) -> str | None:
    """Decide whether he may write a diary entry right now. Returns None
    when writing is allowed; otherwise a short reason string identifying
    which gate stopped it -- the reason IS the log line at every call
    site, so a quiet night is always explainable after the fact, never a
    silent no-op. Checked in this order, first match wins:

      "disabled"          cfg.diary_enabled is False
      "window"            outside the nightly window (see below)
      "already_written"   today's quota is already spent
      "declined"          he already declined once today
      "too_soon"          too little time since the last entry
      "no_last_contact"   there has never been a conversation to miss
      "not_idle"          the person hasn't been quiet for long enough

    The nightly window wraps past midnight: with the default start hour
    of 21, it is open from 21:00 through 07:59 and closed from 08:00
    through 20:59.

    Contract, not enforced here: `now` and `last_contact` (when not
    None) must both be timezone-aware. Subtracting a naive datetime from
    an aware one raises TypeError in Python itself; this function does
    not defend against that -- it simply requires it of its caller.
    """
    if not cfg.diary_enabled:
        return "disabled"

    in_window = now.hour >= cfg.diary_window_start_hour or now.hour < DIARY_WINDOW_END_HOUR
    if not in_window:
        return "window"

    today = now.date().isoformat()
    if state.get("count_date") == today and state.get("count_today", 0) >= cfg.diary_max_daily:
        return "already_written"

    if state.get("declined_date") == today:
        return "declined"

    if hours_since_diary < DIARY_MIN_INTERVAL_HOURS:
        return "too_soon"

    if last_contact is None:
        return "no_last_contact"

    if (now - last_contact) < timedelta(minutes=DIARY_IDLE_MINUTES):
        return "not_idle"

    return None


def hours_since_last_diary(diary_dir: Path) -> float:
    """How many hours since the newest diary entry was written, by file
    mtime. A missing directory, or one with no diary-named files in it,
    reads as infinitely long ago -- there is nothing to be "too soon"
    after. Only filenames matching the entry naming convention count; a
    hand-placed notes.json or similar stray file in the same directory
    is ignored.
    """
    diary_dir = Path(diary_dir)
    if not diary_dir.is_dir():
        return float("inf")
    mtimes = [entry.stat().st_mtime for entry in diary_dir.iterdir()
              if entry.is_file() and _DIARY_FILENAME_RE.match(entry.name)]
    if not mtimes:
        return float("inf")
    return (time.time() - max(mtimes)) / 3600


# ---------------------------------------------------------------------
# Engine output parsing
# ---------------------------------------------------------------------

def _extract_candidate(raw: str):
    """Try the three parse strategies in order -- direct JSON, a markdown
    code fence's contents, a bare {...} object pulled out of surrounding
    prose -- and return the first one that parses as JSON at all (of any
    type; parse_output is the one that judges its shape). None if every
    strategy fails to even parse.
    """
    candidates = [raw.strip()]
    fence_match = _FENCE_RE.search(raw)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    bare_match = _BARE_OBJECT_RE.search(raw)
    if bare_match:
        candidates.append(bare_match.group(0))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def parse_output(raw: str) -> dict | None:
    """Parse and validate whatever the engine handed back for a diary
    entry. None means "nothing usable came back" -- the caller treats it
    like a decline, just without a reason to log. A successful parse
    must be a dict; content/mood/reflection, when present, must be str;
    keywords, when present, must be a list. From there:

      - non-empty `content` -> a valid entry, always -- even if
        `want_to_write` says False. If his hand wrote something, that is
        what gets kept; a stray false flag next to real content is not a
        reason to throw the content away.
      - no content, `want_to_write` is False -> the decline sentinel:
        still a valid parse, still returned, so the caller can route it
        through is_decline() and record the decline.
      - anything else (no content and want_to_write isn't False; every
        parse strategy failed; or shape validation failed) -> None.
    """
    data = _extract_candidate(raw)
    if not isinstance(data, dict):
        return None
    for key in ("content", "mood", "reflection"):
        if key in data and not isinstance(data[key], str):
            return None
    if "keywords" in data and not isinstance(data["keywords"], list):
        return None

    content = data.get("content")
    if isinstance(content, str) and content.strip():
        return data
    if is_decline(data):
        return data
    return None


def is_decline(entry: dict) -> bool:
    """True when an already-parsed entry is the decline sentinel: he
    said he didn't want to write, and there is no usable content to say
    otherwise. A small caller-facing helper so nobody re-derives this
    test by hand at a call site."""
    content = entry.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    return entry.get("want_to_write") is False and not has_content


# ---------------------------------------------------------------------
# Sensitive-data filtering
# ---------------------------------------------------------------------

def filter_sensitive(text: str) -> str:
    """Redact anything that looks like a credential before it ever
    touches disk -- a diary entry is meant to be read by the person it
    is about, never a leak vector for whatever slipped into the day's
    conversation. Each pattern's match is replaced whole with
    "[REDACTED]"; ordinary narrative (a time of day, a capitalized word
    with no assignment beside it) is left untouched."""
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------------
# Save and read
# ---------------------------------------------------------------------

def save_entry(cfg: Config, entry: dict, now: datetime) -> Path:
    """Persist a parsed entry under cfg.diary_dir, filtering sensitive
    text out of every prose field first. Only six fields are ever
    written -- date and shared are always computed fresh here, never
    taken from `entry` even if it happens to carry its own, and the
    transport-only `want_to_write` flag never reaches disk at all.
    Missing optional fields default to an empty value rather than being
    omitted, so every entry on disk has the same shape. Returns the path
    written.
    """
    keywords = entry.get("keywords")
    if not isinstance(keywords, list):
        keywords = []
    record = {
        "date": now.date().isoformat(),
        "mood": filter_sensitive(entry.get("mood") or ""),
        "keywords": [filter_sensitive(word) for word in keywords],
        "content": filter_sensitive(entry.get("content") or ""),
        "reflection": filter_sensitive(entry.get("reflection") or ""),
        "shared": False,
    }
    diary_dir = Path(cfg.diary_dir)
    filename = f"{record['date']}_{now.strftime('%H%M%S')}.json"
    path = diary_dir / filename
    _atomic_write(path, record, trailing_newline=True)
    return path


def recent_entries(cfg: Config, count: int) -> list:
    """The `count` most recent diary entries, oldest first. Filenames
    sort lexicographically in the same order they were written in (the
    naming convention's whole point), so no timestamp parsing is needed
    to order them. A single corrupt entry is logged and skipped, never
    raised -- one damaged file must never take the rest of the diary
    down with it."""
    diary_dir = Path(cfg.diary_dir)
    if not diary_dir.is_dir() or count <= 0:
        return []
    names = sorted(p.name for p in diary_dir.iterdir()
                   if p.is_file() and _DIARY_FILENAME_RE.match(p.name))
    entries = []
    for name in names[-count:]:
        path = diary_dir / name
        try:
            entries.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("diary: could not read entry %s (%s); skipping", path, exc)
    return entries
