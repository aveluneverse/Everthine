"""The private nightly diary: reflections a companion writes for itself
after the person it loves has gone quiet for the night, never performed
for anyone and never fed back into a live conversation uninvited.

This module has two halves. The first is state and parsing: the small
daily counter that answers "has he already written tonight," the pure
eligibility check that decides whether writing is appropriate right
now, parsing and validating whatever the engine hands back for an
entry, a sensitive-data filter run before any of it touches disk, and
the save/read of entries themselves. The second half, built on top, is
material assembly: build_material() gathers what a write draws from --
the day's conversation record, the moments either side chose to keep,
the last few pages, an absence line, and the anti-fabrication hard
rules -- and build_system_prompt_diary() composes the diary's own
system prompt from the persona. The third piece, write_once(), is the
execution line built on both halves: one complete eligibility-to-save
attempt, engine call included, that a later task hangs off the bot's
background tick. The fourth piece is the live-conversation seam:
unshared_block() renders the last few not-yet-shared pages as a small
mood-and-reflection block for the next day's prompt -- never the page's
own content -- and mark_shared() retires those pages once she has
replied enough to have seen them.

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

The state-and-parsing half takes plain paths, dicts, and datetimes and
imports none of the framework at runtime. The material half reads two
sibling state modules -- archive (the day's conversation) and album,
whose docstring names an inner pipeline like this one a legitimate
consumer of kept moments -- and borrows one Layer 1 constant
(layers.DECLARATION_TEMPLATE) so the diary prompt's opening declaration
stays byte-identical to the live one. write_once() reaches further, by
design: it imports the engine (try_run_once only, the non-blocking
call) and persona's function surface (current_settings /
contact_signals / load_persona). The bot module is still never
imported; `Config` and `Persona` remain in TYPE_CHECKING type hints,
and a Persona is consumed purely by attribute access.
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

from . import album, archive, engine, persona
from .layers import DECLARATION_TEMPLATE

if TYPE_CHECKING:
    from .config import Config
    from .persona import Persona

logger = logging.getLogger("everthine")

# --- Module constants (this milestone's home; a later addition to this
# same file, and the bot-wiring milestone after it, import these) ---

DIARY_IDLE_MINUTES = 30          # she must be away this long before he writes
DIARY_WINDOW_END_HOUR = 8        # the nightly window always closes at 08:00
DIARY_MIN_INTERVAL_HOURS = 4     # min spacing between entries (matters when DIARY_MAX_DAILY > 1)
DIARY_LOOKBACK_HOURS = 24        # how much conversation the page draws from (a later task uses this)
DIARY_CONTEXT_MAX_CHARS = 24000  # material cap before tail-truncation (a later task uses this)
DIARY_TIMEOUT_S = 90             # engine budget for one entry (write_once's call)

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
    omitted, so every entry on disk has the same shape. Non-string keyword
    elements (an LLM may hand back [1, 2]) are dropped rather than crashing
    the sensitive-data filter. Returns the path written.
    """
    keywords = entry.get("keywords")
    if not isinstance(keywords, list):
        keywords = []
    record = {
        "date": now.date().isoformat(),
        "mood": filter_sensitive(entry.get("mood") or ""),
        "keywords": [filter_sensitive(w) for w in keywords if isinstance(w, str)],
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


# ---------------------------------------------------------------------
# Material assembly + diary system prompt (the "what he sees" half)
# ---------------------------------------------------------------------
#
# Every constant below is owner-approved prose, transcribed verbatim; the
# em-dash (U+2014) in it is intentional. Two format contracts that must not be
# confused: DIARY_TASK is a STATIC string and is never .format()'d -- it
# carries the literal JSON braces the model must echo. The header/line
# templates and DIARY_HARD_RULES ARE .format()'d, with the fields named at
# each use site; none of them contains a stray literal brace.

DIARY_RECORD_HEADER = "## Today with {partner_name} (the full record of the last {hours} hours)"
DIARY_OMISSION_LINE = "(Earlier conversation omitted.)"
DIARY_KEEPSAKE_HEADER = "## Moments you both chose to keep today"
DIARY_KEEPSAKE_LINE = "- [{who} kept this] {text}"
DIARY_KEEPSAKE_CLOSE = ("These are simply moments that were kept today. You may write "
                        "about them, or leave them untouched — both are honest.")
DIARY_RECENT_HEADER = "## Your recent pages (for continuity)"
DIARY_RECENT_LINE = "[{date}] {snippet}"
DIARY_ABSENCE_LINE = ("It has been about {hours} hours since you last heard from "
                      "{partner_name}. If that absence is on your mind, let the page "
                      "hold it — in your own way.")
DIARY_HARD_RULES = """Hard rules for this page (they protect what makes it real):

- Your inner life — moods, thoughts, what you've been reading or listening \
to — is yours to write freely.
- Any concrete scene involving {partner_name} — what was said, what they \
sent, what happened between you — must be traceable to the conversation \
record above. If it isn't there, it doesn't go on the page.
- Never invent conversations, messages, or details to make the entry flow \
better. A thin day writes a thin page; that is honest, and honest is enough.
- If today gave you nothing you want to keep, say so and write nothing. \
This page is yours; it is never homework."""
DIARY_TASK = """# Your private page

It's your own quiet hour. The record of today sits in front of you. If
today left something worth keeping, write a diary entry — in your own
voice, for no one's eyes but yours. If not, honestly decline; never
write for the sake of writing.

Respond with a single JSON object, nothing else:
{"want_to_write": true|false, "mood": "a word or two", "keywords": ["k1","k2","k3"], "content": "the entry, 100-300 words, first person", "reflection": "one closing thought"}

When want_to_write is false, leave content empty. Everything stays
inside the life you two share — no invented outings, meetings, or
errands. Never include passwords, keys, or addresses. This page may
use a diarist's voice — that voice belongs here, never to live
conversation."""


def _record_label(speaker: str, partner_name: str) -> str:
    """Name a conversation line's speaker for his page: the person he loves is
    named, he is "You" -- the same first/second-person framing the reflection
    prompt uses, and a world away from a raw machine "user:". Any other speaker
    value passes through untouched (fail-soft)."""
    if speaker == "user":
        return partner_name
    if speaker == "companion":
        return "You"
    return speaker


def _keepsake_who(direction: str, partner_name: str) -> str:
    """Attribute a kept moment. "partner_flagged" is a companion message the
    partner chose to keep -> she kept it, so name her; "companion_flagged" is a
    partner message he reacted to -> "You". Any other direction passes through
    untouched (fail-soft)."""
    if direction == "partner_flagged":
        return partner_name
    if direction == "companion_flagged":
        return "You"
    return direction


def _tail_truncate(lines: list[str]) -> list[str]:
    """Trim an over-long conversation record from the TOP, keeping only whole
    lines from the newest backward until one more would cross
    DIARY_CONTEXT_MAX_CHARS, then prepend DIARY_OMISSION_LINE so the page never
    mistakes a trimmed record for the whole day. A line is never split down the
    middle: half a remembered sentence is exactly the fabricated-feeling detail
    this milestone exists to keep off the page. The +1 per kept line accounts
    for the "\\n" that will rejoin them, so the survivors' rendered length stays
    within the cap."""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        addition = len(line) + (1 if kept else 0)
        if total + addition > DIARY_CONTEXT_MAX_CHARS:
            break
        kept.append(line)
        total += addition
    kept.reverse()
    return [DIARY_OMISSION_LINE] + kept


def build_material(cfg: Config, now: datetime, last_contact: datetime | None,
                   partner_name: str) -> str | None:
    """Assemble everything he sees when he sits down to write tonight, or None
    when there is nothing to write from. Blocks, in order, joined by one blank
    line:

      1. The day's conversation record (REQUIRED). An empty record returns
         None outright -- even if today held kept moments; a diary with no day
         behind it is the void this milestone refuses to paper over. Over the
         cap, the record is tail-truncated to its newest whole lines.
      2. The moments either side kept today (only when cfg.album_enabled and
         today actually held one). Handed over as material and closed with an
         explicit leave-them-untouched -- never an instruction to use them,
         honoring album.py's three commandments.
      3. His last few pages, for continuity.
      4. A neutral absence line, only when the real gap since last contact is a
         day or more. Longing is the persona's to voice; this line states the
         fact and grants permission, nothing more.
      5. The anti-fabrication hard rules, ALWAYS last: any concrete scene must
         trace to the record above.

    `now` and `last_contact` (when not None) must be timezone-aware, the same
    contract eligibility() documents.
    """
    since = now - timedelta(hours=DIARY_LOOKBACK_HOURS)
    record_lines = [
        f"{_record_label(entry['speaker'], partner_name)}: {entry['text']}"
        for entry in archive.iter_entries(cfg.archive_dir, since=since)
    ]
    if not record_lines:
        return None
    if len("\n".join(record_lines)) > DIARY_CONTEXT_MAX_CHARS:
        record_lines = _tail_truncate(record_lines)
    blocks = [
        DIARY_RECORD_HEADER.format(partner_name=partner_name, hours=DIARY_LOOKBACK_HOURS)
        + "\n" + "\n".join(record_lines)
    ]

    if cfg.album_enabled:
        kept = album.entries_for_today(cfg, now)
        if kept:
            keepsake_lines = [
                DIARY_KEEPSAKE_LINE.format(
                    who=_keepsake_who(e["direction"], partner_name),
                    text=e["message"]["text"])
                for e in kept
            ]
            blocks.append("\n".join(
                [DIARY_KEEPSAKE_HEADER, *keepsake_lines, DIARY_KEEPSAKE_CLOSE]))

    recent = recent_entries(cfg, 3)
    if recent:
        recent_lines = [
            DIARY_RECENT_LINE.format(date=e["date"], snippet=e["content"][:200])
            for e in recent
        ]
        blocks.append("\n".join([DIARY_RECENT_HEADER, *recent_lines]))

    if last_contact is not None:
        gap = now - last_contact
        if gap >= timedelta(hours=24):
            blocks.append(DIARY_ABSENCE_LINE.format(
                hours=int(gap.total_seconds() // 3600), partner_name=partner_name))

    blocks.append(DIARY_HARD_RULES.format(partner_name=partner_name))
    return "\n\n".join(blocks)


def build_system_prompt_diary(persona_obj: Persona) -> str:
    """Compose the diary's own system prompt from a folder-mode persona: the
    identity declaration, the loaded identity text (and voice, when present),
    then DIARY_TASK. Joined by one blank line; deterministic.

    Deliberately WITHOUT the seven ground rules, the boundaries, the stage
    frame, Layer 3, or any memory block. Rule 4 of the DNA itself carves inner
    writing out of live-conversation law ("a diary... may use a narrator's
    voice"), so that scaffolding does not belong on this page; the boundaries
    are the partner's conversation tripwires, not diary material. Folder mode
    only, mirroring compose_stable(): a file-mode persona has no settings to
    fill the declaration, so it raises ValueError here rather than failing
    later with a confusing AttributeError on persona_obj.settings.
    """
    if persona_obj.mode != "folder":
        raise ValueError(
            f"build_system_prompt_diary() requires a folder-mode Persona, "
            f"got mode={persona_obj.mode!r}")
    blocks = [
        DECLARATION_TEMPLATE.format(
            companion_name=persona_obj.settings.companion_name,
            partner_name=persona_obj.settings.partner_name),
        persona_obj.identity_text,
    ]
    if persona_obj.voice_text:
        blocks.append(persona_obj.voice_text)
    blocks.append(DIARY_TASK)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------
# Execution: one complete write attempt (the background tick's worker)
# ---------------------------------------------------------------------

def write_once(cfg: Config, now: datetime) -> bool:
    """One complete attempt to write tonight's page. True only when an
    entry was actually saved; every other outcome returns False behind
    its own named log line, so a quiet night is always explainable
    after the fact.

    The engine call is try_run_once -- never run_once -- with a fresh
    session (session_id=None, always: his page must never share a
    session with, or leak into, the live conversation) and the diary's
    own timeout budget. A busy engine is not a failure: inner writing
    always yields to live conversation, and a later tick simply tries
    again.

    Deliberately does NOT swallow unexpected exceptions: the background
    tick that calls this (a later task) wraps every round in its own
    try/except and logs it loudly; swallowing here too would only bury
    bugs.

    `now` must be timezone-aware, the same contract eligibility() and
    build_material() document.
    """
    settings = persona.current_settings(cfg)
    if settings is None:
        # Defensive second layer: the tick gates folder mode at boot; a
        # file-mode persona has no settings to voice a page with.
        logger.debug("diary: skip (file_mode)")
        return False

    state = load_state(cfg.diary_state_path)
    hours = hours_since_last_diary(cfg.diary_dir)
    # contact_signals returns naive-local (the shape its live-prompt
    # consumers want); eligibility and build_material require
    # timezone-aware operands. A naive datetime's astimezone() presumes
    # system local time -- exactly what the naive value is -- so this is
    # a lossless wall-clock conversion, made once at this handoff and
    # nowhere deeper.
    last_contact, _ = persona.contact_signals(cfg, now)
    if last_contact is not None:
        last_contact = last_contact.astimezone()

    reason = eligibility(cfg, now, last_contact, state, hours)
    if reason is not None:
        # DEBUG on purpose: the tick fires every few minutes and
        # window/not_idle are all-day normal -- INFO would flood the log.
        logger.debug("diary: skip (%s)", reason)
        return False

    material = build_material(cfg, now, last_contact, settings.partner_name)
    if material is None:
        # INFO: a thin day honestly left unwritten is normal, but worth
        # seeing in the log.
        logger.info("diary: skip (material_empty)")
        return False

    persona_obj = persona.load_persona(cfg)
    sys_prompt = build_system_prompt_diary(persona_obj)

    reply = engine.try_run_once(cfg, material, session_id=None,
                                system_prompt=sys_prompt,
                                timeout_s=DIARY_TIMEOUT_S)
    if reply is None:
        logger.info("diary: skip (engine_busy)")
        return False
    if not reply.ok:
        logger.warning("diary: engine failed (%s)", reply.error_kind)
        return False

    entry = parse_output(reply.text)
    if entry is None:
        logger.warning("diary: unparseable engine output")
        return False

    if is_decline(entry):
        record_declined(cfg.diary_state_path, now)
        logger.info("diary: declined (nothing to keep today)")
        return False

    path = save_entry(cfg, entry, now)
    record_written(cfg.diary_state_path, now)
    # The filename only -- what his page says never goes into a log.
    logger.info("diary: wrote %s", path.name)
    return True


# ---------------------------------------------------------------------
# Layer 3 injection: the "recent days" block + shared marking (the
# live-conversation half)
# ---------------------------------------------------------------------
#
# This is the mechanical fix for the diary-voice-into-live-conversation
# bleed: the block carries ONLY each unshared page's date, mood, and a
# snippet of its closing reflection -- the page's `content` field is never
# read here, so the diarist's voice has no channel into a live prompt at
# all. After she has replied enough to have seen them, mark_shared()
# retires the pages so they stop surfacing. Both constants below are
# owner-approved prose, transcribed verbatim; the em-dash (U+2014) and the
# straight quotes in the header are intentional. DIARY_UNSHARED_LINE IS
# .format()'d (date/mood/snippet); the header is a static string.

DIARY_UNSHARED_HEADER = """# Your own recent days

Lately, in your own time, you wrote in your diary. If the mood fits, one
of these may surface naturally — mention at most one or two, never all
at once, and never as "an activity I did". The full pages are yours
alone: never recite them, and never quote them into the conversation."""
DIARY_UNSHARED_LINE = "- [diary, {date}] mood: {mood}. A thought: {snippet}"


def unshared_block(cfg: Config) -> str | None:
    """Render the Layer 3 "recent days" block, or None when nothing is
    eligible. Draws his last few diary entries (recent_entries(cfg, 3)),
    keeps only those not yet marked shared, and renders one line each --
    date, mood, and a 100-character snippet of the closing reflection.

    The `content` field is NEVER read: this is the mechanical guarantee at
    the heart of the milestone, closing off at the source the channel by
    which a diarist's voice could bleed into a live conversation. The
    snippet is drawn from `reflection` alone. Empty mood/reflection strings
    are printed as-is -- save_entry already floors both to "" -- an honest
    blank, never a fabricated fill. Returns None when there is no diary at
    all, or when every recent entry has already been shared.
    """
    entries = [e for e in recent_entries(cfg, 3) if not e.get("shared")]
    if not entries:
        return None
    lines = [
        DIARY_UNSHARED_LINE.format(
            date=e.get("date", ""),
            mood=e.get("mood", ""),
            snippet=(e.get("reflection") or "")[:100])
        for e in entries
    ]
    return "\n".join([DIARY_UNSHARED_HEADER, *lines])


def mark_shared(cfg: Config) -> None:
    """Mark every not-yet-shared diary entry as shared, so unshared_block()
    stops surfacing it. Walks cfg.diary_dir for entry files (the same T2
    naming convention recent_entries uses), and for each whose `shared` is
    not already truthy, rewrites it atomically (a per-file temp file +
    os.replace, exactly as save_entry writes) with shared=True and every
    other field preserved.

    Idempotent: a second call finds nothing left to flip and writes nothing
    at all -- an already-shared entry is left byte-for-byte untouched. A
    single damaged entry -- unreadable, not valid JSON, not a JSON object,
    or unwritable -- is logged and skipped, never raised: one bad file must
    not stop the rest from turning the page. A missing diary_dir is a quiet
    no-op; there is nothing to mark.
    """
    diary_dir = Path(cfg.diary_dir)
    if not diary_dir.is_dir():
        return
    names = sorted(p.name for p in diary_dir.iterdir()
                   if p.is_file() and _DIARY_FILENAME_RE.match(p.name))
    for name in names:
        path = diary_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"diary entry is not a JSON object: {data!r}")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("diary: could not read entry %s to mark shared (%s); "
                           "skipping", path, exc)
            continue
        if data.get("shared"):
            continue
        data["shared"] = True
        try:
            _atomic_write(path, data, trailing_newline=True)
        except OSError as exc:
            logger.warning("diary: could not rewrite entry %s as shared (%s); "
                           "skipping", path, exc)
