"""Structured facts: the companion's small, growing book of plain truths
about the person it talks to -- "she takes her coffee black", "her mother's
name is Wen", "she is learning the piano" -- kept apart from the raw
conversation archive and the associative memory store so that a reply can be
handed the few facts that actually bear on what was just said, instead of a
whole transcript to re-read every turn.

This module is that book's pure core, and only its core: the file I/O, the
extraction cursor, the ranking/dedup algorithms, and the eligibility gate.
Nothing here calls the engine, reads the archive, or builds prompt text. A
later task (D1 Task 3) writes the extractor that distills facts out of a
conversation and appends them through append_facts; another (D1 Task 4)
selects a handful with select_facts and injects them into a live reply. Both
sit on top of what is written here and add the wiring; this file stays
dependency-light and testable on its own, the same way stages.py and
session_store.py do -- it imports only the standard library at runtime, with
Config appearing solely in a TYPE_CHECKING hint.

Two files back this organ, each with its own fail-soft round-trip, both
copying stages.py / album.py / scheduler.py's atomic-write and
corpse-quarantine idiom exactly:

  * facts.json           {"facts": [ {"text","category","date"}, ... ]}
                         The book itself. A DICT wrapper around the list, not a
                         bare list, so the on-disk shape can grow a sibling key
                         later without a migration. List order IS chronological
                         order -- newest at the back -- so pruning to a cap
                         means dropping from the front, never a date sort.
  * facts_state.json     {"last_extracted_ts": str}
                         The extraction cursor: the ISO-8601 timestamp (with a
                         timezone offset) of the newest conversation turn the
                         extractor has already digested, or "" for "never run".
                         A plain-str schema exactly like scheduler_state, where
                         the empty string is the unset sentinel and a field
                         holding None would be corruption, not a fresh value.

Fail-soft is the whole design for both files, the same as everywhere else in
this framework: a missing file quietly becomes a fresh default (an empty book,
an unset cursor); a corrupt one -- unreadable, not valid JSON, or valid JSON in
an unexpected shape -- is never silently discarded but renamed alongside itself
as a `.corrupt-<timestamp>` corpse, a warning is logged loudly, and the caller
still gets back a usable fresh value. A person's recorded facts are never
overwritten in silence.

The ranking and dedup rest on character-bigram overlap: split a string into
every adjacent two-character pair (after stripping whitespace, digits, and
punctuation) and compare those sets. This is language-neutral by construction
-- it works natively for Chinese, where there are no word boundaries to
tokenize on, and for English alike, with zero dependencies. It is the approach
carried over from the source architecture, its constants (the 0.7 duplicate
threshold, the 0.6/0.4 relevance/recency split, the 30-day recency horizon)
battle-tested over three months of live use rather than guessed here.

Every algorithm and the eligibility gate is pure -- no clock, no filesystem:
select_facts takes the caller's `today`, eligibility takes the caller's `now`
and the already-parsed timestamps, so every ranking and every skip reason can
be tested in isolation, and at the call site logged verbatim.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("everthine")


# ---------------------------------------------------------------------
# Ranking / dedup constants
# ---------------------------------------------------------------------

# Everything a character-bigram comparison should treat as a gap rather than
# content: whitespace, digits, and both ASCII and full-width CJK punctuation.
# Stripped before pairing so "喜, 歡" and "喜歡" yield the same bigrams and a
# string of pure punctuation yields none. Copied verbatim from the source
# architecture -- do not trim it without re-checking the three-month-tuned
# thresholds that assume this exact character class.
_PUNCTUATION = re.compile(
    r"[\s\d.,!?，。！？、；：「」『』（）\[\]【】《》“”‘’…—～·\-/\\@#$%^&*+=|<>]+"
)

# Duplicate when the symmetric bigram overlap reaches this fraction of the
# smaller fact; the relevance/recency blend and the recency horizon (days)
# select_facts scores with. Live-tuned constants, pinned by tests.
_DUPLICATE_THRESHOLD = 0.7
_RELEVANCE_WEIGHT = 0.6
_RECENCY_WEIGHT = 0.4
_RECENCY_HORIZON_DAYS = 30.0


# ---------------------------------------------------------------------
# Fresh defaults + the single corruption verdict per file
# ---------------------------------------------------------------------

def _fresh_facts() -> dict:
    """The on-disk shape of an empty book: a dict wrapper around an empty
    list, never a bare list (see the module docstring on why the shape can
    grow without a migration)."""
    return {"facts": []}


def _fresh_state() -> dict:
    """An unset extraction cursor: the empty string is the sentinel for
    "never extracted", exactly as scheduler_state uses it."""
    return {"last_extracted_ts": ""}


def _facts_well_shaped(data) -> bool:
    """The single place corruption is decided for facts.json. The wrapper
    must be a dict whose "facts" is a list; the individual entries are NOT
    validated here -- load_facts drops stray non-dict entries silently
    rather than condemning the whole book for one bad line, so only a
    broken wrapper counts as corruption."""
    return isinstance(data, dict) and isinstance(data.get("facts"), list)


def _state_well_shaped(data) -> bool:
    """The single place corruption is decided for facts_state.json. A plain
    one-field str schema: the key must be present and hold a str. As in
    scheduler_state, a field holding None (or any non-str) is corruption,
    not a fresh value -- the empty string is the unset sentinel."""
    return isinstance(data, dict) and isinstance(data.get("last_extracted_ts"), str)


def _quarantine_corpse(path: Path, reason: Exception) -> None:
    """Rename a broken facts file to a timestamped corpse alongside itself,
    then log loudly. A rename failure -- a target collision, a permissions
    problem, the file vanishing underneath us -- is swallowed with its own
    warning: corpse preservation is a courtesy, never a reason to crash a
    reply. Verbatim copy of stages.py's _quarantine_corpse, retargeted at
    this module's own vocabulary; shared by both files (facts.json and
    facts_state.json), which both degrade to a fresh default."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    corpse = path.with_name(path.name + f".corrupt-{timestamp}")
    try:
        if corpse.exists():
            raise FileExistsError(f"corpse target already exists: {corpse}")
        path.rename(corpse)
    except OSError as exc:
        logger.warning(
            "facts: could not preserve broken file %s as %s (%s); "
            "leaving the broken file in place", path, corpse, exc)
    logger.warning(
        "facts: %s is corrupt (%s); degrading to a fresh default", path, reason)


def _atomic_write(path: Path, data: dict) -> None:
    """Write to a temp file in the same directory, then os.replace() into
    place, so a reader never observes a half-written file. Verbatim copy of
    stages.py's atomic-write idiom; serves both facts.json and
    facts_state.json (both are JSON dicts)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------
# 1. Fact store
# ---------------------------------------------------------------------

def load_facts(path: Path) -> list[dict]:
    """Load the fact book as a plain list, degrading fail-soft on any
    trouble -- mirrors stages.load_state exactly. A missing file returns an
    empty list quietly: nothing has been recorded yet. A corrupt wrapper --
    unreadable, not valid JSON, top level not a dict, or "facts" not a list
    -- is treated as corruption: the broken file is quarantined (see
    _quarantine_corpse) and an empty list returned. Stray non-dict entries
    INSIDE an otherwise-valid list are not corruption; they are silently
    dropped, so one malformed line never costs the whole book.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not _facts_well_shaped(data):
            raise ValueError(f"unexpected facts shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return []
    return [fact for fact in data["facts"] if isinstance(fact, dict)]


def append_facts(path: Path, new_facts: list[dict], max_facts: int) -> int:
    """Merge freshly extracted facts into the book, dedup them, cap the
    book, write atomically, and report how many were actually added.

    Each incoming item must be a dict carrying a non-empty text (a missing,
    non-string, or blank/whitespace-only text is skipped, as is a non-dict
    item -- Task 3's extractor should never emit those, but this core never
    trusts that). A surviving candidate is skipped when is_duplicate() finds
    it a near-match of ANY fact already in the merged list -- both the facts
    loaded from disk AND the ones accepted earlier in this same batch, so a
    batch containing two near-identical lines keeps only the first.

    Survivors are appended in order, then the book is pruned to the newest
    `max_facts` by dropping from the FRONT: list order is chronological, so
    the oldest facts fall off first -- there is no date sort, and a fact's
    stored date is never consulted here (select_facts is the only place a
    date matters). The write is atomic (tempfile + os.replace). Returns the
    count that passed the text filter and the dedup gate and were appended,
    independent of whether a later fact in the same call pruned an older one
    off the front.
    """
    merged = load_facts(path)
    added = 0
    for fact in new_facts:
        if not isinstance(fact, dict):
            continue
        text = fact.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if any(is_duplicate(text, existing.get("text", "")) for existing in merged):
            continue
        merged.append(fact)
        added += 1
    if len(merged) > max_facts:
        merged = merged[len(merged) - max_facts:]
    _atomic_write(path, {"facts": merged})
    return added


# ---------------------------------------------------------------------
# 2. Extraction cursor
# ---------------------------------------------------------------------

def load_state(path: Path) -> dict:
    """Load the extraction cursor, degrading fail-soft on any trouble --
    mirrors scheduler.load_state exactly. A missing file returns a fresh,
    unset cursor quietly. A corrupt one -- unreadable, not valid JSON, or
    valid JSON without a string `last_extracted_ts` -- is quarantined (see
    _quarantine_corpse) and a fresh cursor returned.
    """
    path = Path(path)
    if not path.exists():
        return _fresh_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not _state_well_shaped(data):
            raise ValueError(f"unexpected facts state shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return _fresh_state()
    return data


def save_state(path: Path, state: dict) -> None:
    """Persist the extraction cursor atomically (tempfile + os.replace, so a
    reader never observes a half-written file). The caller owns the shape --
    load_state is the guard that reads it back fail-soft."""
    _atomic_write(path, state)


# ---------------------------------------------------------------------
# 3. Ranking / dedup algorithms (pure, no I/O)
# ---------------------------------------------------------------------

def extract_bigrams(text: str) -> set[str]:
    """Strip `text` of whitespace, digits, and punctuation (_PUNCTUATION),
    then return the set of every adjacent two-character pair in what
    remains. Fewer than two characters survive -> the empty set (a single
    char, or a string of pure punctuation, has no bigram). The bigram set is
    the atom every relevance and duplicate judgement below is built on, and
    it is deliberately language-neutral: with no word boundaries assumed, it
    reads Chinese and English the same way.
    """
    cleaned = _PUNCTUATION.sub("", text)
    if len(cleaned) < 2:
        return set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def relevance_score(message: str, fact_text: str) -> float:
    """How relevant a fact is to the current message, in [0.0, 1.0]: the
    fraction of the MESSAGE's bigrams the fact also contains
    (len(msg & fact) / len(msg)). Directional on purpose -- it asks "how
    much of what she just said does this fact touch", not the reverse, so a
    long fact is not penalized for the many bigrams a short message never
    mentions. Either side reducing to no bigrams (empty, single-char, or
    pure punctuation) -> 0.0, never a division by zero.
    """
    msg_bigrams = extract_bigrams(message)
    fact_bigrams = extract_bigrams(fact_text)
    if not msg_bigrams or not fact_bigrams:
        return 0.0
    return len(msg_bigrams & fact_bigrams) / len(msg_bigrams)


def is_duplicate(new_text: str, existing_text: str,
                 threshold: float = _DUPLICATE_THRESHOLD) -> bool:
    """Whether two fact texts say close enough to the same thing to count as
    one. Symmetric overlap: len(a & b) / min(len(a), len(b)) >= threshold,
    normalizing by the SMALLER bigram set so "she likes coffee" and "she
    likes coffee a lot" (one a near-subset of the other) read as the same
    fact rather than being split by length. The comparison is inclusive at
    the boundary (>=). Either side reducing to no bigrams -> False (nothing
    to compare, so never a duplicate), never a division by zero.
    """
    a = extract_bigrams(new_text)
    b = extract_bigrams(existing_text)
    if not a or not b:
        return False
    overlap = len(a & b) / min(len(a), len(b))
    return overlap >= threshold


def _recency_score(date_str, today: date) -> float:
    """Map a fact's stored date to a recency weight in [0.0, 1.0]: 1.0 today,
    decaying linearly to 0.0 at _RECENCY_HORIZON_DAYS old and clamped there.
    An absent, non-string, or unparseable date counts as exactly the horizon
    old (weight 0.0) -- the most conservative reading, never a guess at
    freshness the data does not support. A future date clamps to 1.0.
    """
    if isinstance(date_str, str):
        try:
            fact_date = date.fromisoformat(date_str)
            days_ago = (today - fact_date).days
        except ValueError:
            days_ago = _RECENCY_HORIZON_DAYS
    else:
        days_ago = _RECENCY_HORIZON_DAYS
    return max(0.0, min(1.0, 1.0 - days_ago / _RECENCY_HORIZON_DAYS))


def select_facts(message: str, facts: list[dict], max_n: int,
                 today: date) -> list[dict]:
    """Rank a book of facts for one incoming message and return the top
    `max_n`. Each fact scores relevance * 0.6 + recency * 0.4: relevance
    from bigram overlap with the message (relevance_score), recency from how
    many days old the fact's date is (_recency_score, an absent/unparseable
    date counting as the 30-day horizon). Sorted by score descending; ties
    keep their input order (Python's sort is stable and, even reversed, does
    not reorder equal keys -- deliberately no tiebreaker is added). `max_n`
    <= 0 or an empty book -> []. Pure: `today` is the caller's date, no clock
    is read here.
    """
    if max_n <= 0 or not facts:
        return []
    scored = []
    for fact in facts:
        relevance = relevance_score(message, fact.get("text", ""))
        recency = _recency_score(fact.get("date"), today)
        score = relevance * _RELEVANCE_WEIGHT + recency * _RECENCY_WEIGHT
        scored.append((score, fact))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [fact for _, fact in scored[:max_n]]


# ---------------------------------------------------------------------
# 4. Eligibility gate (pure, no I/O -- the reason IS the log line)
# ---------------------------------------------------------------------

def eligibility(cfg: Config, now: datetime, last_user_ts: datetime | None,
                cursor_ts: datetime | None) -> str | None:
    """Decide whether the extractor may run right now. Returns None when it
    is allowed; otherwise the first matching reason, in this order (first
    match wins), meant to be logged verbatim so a skipped run is always
    explainable:

      "disabled"          cfg.facts_enabled is False -- the master switch
      "idle_not_reached"  last_user_ts is None (no conversation yet -- there
                          is nothing to extract, and calling that
                          "no_new_material" would only confuse), OR the
                          person has been quiet for less than
                          cfg.facts_idle_minutes: extraction waits for a lull
                          so it never races a conversation still in flight
      "no_new_material"   cursor_ts is set AND last_user_ts <= cursor_ts --
                          every turn up to the cursor has already been
                          digested, so there is nothing new to read

    Engine-busy is deliberately NOT this function's concern; Task 3's
    try_run_once handles the shared-engine lock.

    Contract, not enforced here: `last_user_ts` and `cursor_ts` (when not
    None) are timezone-aware datetimes the CALLER parses -- from the
    conversation archive and from load_state()'s `last_extracted_ts` string
    respectively. `now` must be timezone-aware too. Subtracting a naive
    datetime from an aware one raises TypeError in Python itself; this
    function requires aware inputs of its caller rather than defending
    against that. Checking idle before no_new_material is what keeps the None
    last_user_ts path from ever reaching the `last_user_ts <= cursor_ts`
    comparison.
    """
    if not cfg.facts_enabled:
        return "disabled"
    if (last_user_ts is None
            or (now - last_user_ts) < timedelta(minutes=cfg.facts_idle_minutes)):
        return "idle_not_reached"
    if cursor_ts is not None and last_user_ts <= cursor_ts:
        return "no_new_material"
    return None
