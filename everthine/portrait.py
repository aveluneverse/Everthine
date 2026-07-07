"""The weekly self-portrait: a snapshot of self-knowledge the companion
composes every so often by digesting recent diary pages and reflections,
then layers into its own persona underneath everything else -- not a new
fact to recite, but something closer to "who I've noticed I am lately."

This task builds only the state core: the module's constants, load/save of
the single current snapshot (with a parallel dated history so nothing
written is ever lost to the next week's overwrite), and the pure
eligibility check that decides whether a new snapshot is due right now.
Composing what a portrait actually reads from -- diary excerpts, reflection
lines, the prompt that turns them into prose -- is a later task's (T3's)
work, and wiring a write into the engine and the bot's background tick is
later still (T4/T6). Nothing here calls the engine or imports bot.py.

Fail-soft mirrors diary.py and reflection.py: a missing portrait.json
quietly means "nothing written yet". Unlike those two, though, there is no
fresh-but-existing state to hand back -- a portrait either exists or it
doesn't -- so load_portrait returns None outright rather than a placeholder
dict, and eligibility()'s "no previous version" branch is what actually
consumes that None. A corrupted file -- unreadable, not valid JSON, or JSON
that isn't even an object -- is preserved as a `.corrupt-<timestamp>` corpse
alongside itself (see _quarantine_corpse, a verbatim retargeting of
diary.py's own), logged loudly, and load_portrait still hands back None.

Saving keeps exactly four fields (updated/content/opinions/observations)
regardless of what a caller's entry dict carries beyond them, filters prose
through diary.filter_sensitive before it ever touches disk exactly as
diary.save_entry does, and writes twice: the live file at cfg.portrait_path,
and a same-content snapshot under cfg.portrait_history_dir named by that
day's date, so a second save on the same calendar day overwrites that day's
snapshot rather than piling up duplicates -- at most one per day, ever.

The eligibility check is pure -- no clock, no filesystem -- so every reason
a portrait write is skipped can be tested in isolation and, at call sites,
logged verbatim: "disabled", "interval_not_reached", or "no_new_material".
A first-ever portrait (portrait is None) is a special case throughout:
there is no interval to have not reached yet, and any diary entry at all
counts as new material. A previous portrait whose own `updated` field has
been hand-corrupted or is otherwise unreadable is never allowed to crash a
caller -- it degrades to being treated exactly like "no previous portrait",
loudly logged, rather than raising.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .diary import filter_sensitive

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("everthine")

# --- Module constants ---------------------------------------------------
# Several of these have no consumer yet -- build_material and the prompt
# that reads opinions/observations back out arrive in T3, the engine call's
# own timeout in T4. Declaring them here, ahead of their first use, is by
# design: the values are part of this milestone's contract, not dead code.

PORTRAIT_RECENT_DIARY = 7           # how many recent diary entries a snapshot digests (T3)
PORTRAIT_RECENT_REFLECTIONS = 15    # how many recent reflection lines a snapshot digests (T3)
PORTRAIT_TIMEOUT_S = 120            # engine budget for composing one snapshot (T4)
PORTRAIT_CONTENT_MAX_CHARS = 4000   # save_portrait's tail-truncation cap on `content`
PORTRAIT_DIARY_SNIPPET_CHARS = 200  # per-entry diary snippet length fed into material (T3)
PORTRAIT_OPINIONS_STORED_CAP = 10   # save_portrait's cap on stored `opinions`
PORTRAIT_OBSERVATIONS_STORED_CAP = 10  # save_portrait's cap on stored `observations`
PORTRAIT_OPINIONS_PROMPT_CAP = 5    # how many stored opinions resurface in a live prompt (T3)


# ---------------------------------------------------------------------
# State file: cfg.portrait_path (data/portrait.json), with a dated
# history snapshot alongside it under cfg.portrait_history_dir
# ---------------------------------------------------------------------

def _quarantine_corpse(path: Path, reason: Exception) -> None:
    """Rename a broken portrait file to a timestamped corpse alongside
    itself, then log loudly. A rename failure -- a target collision, a
    permissions problem, the file vanishing underneath us -- is swallowed
    with its own warning: corpse preservation is a courtesy, never a reason
    to crash a reply. Verbatim copy of diary.py's and reflection.py's
    _quarantine_corpse, retargeted at this module's own vocabulary."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    corpse = path.with_name(path.name + f".corrupt-{timestamp}")
    try:
        if corpse.exists():
            raise FileExistsError(f"corpse target already exists: {corpse}")
        path.rename(corpse)
    except OSError as exc:
        logger.warning(
            "portrait: could not preserve broken file %s as %s (%s); "
            "leaving the broken file in place", path, corpse, exc)
    logger.warning(
        "portrait: %s is corrupt (%s); degrading to no previous portrait", path, reason)


def load_portrait(cfg: Config) -> dict | None:
    """Load the current self-portrait, fail-soft on any trouble. None means
    "there is no previous portrait" -- either nothing has ever been written
    (a missing file, the ordinary case before the first snapshot) or the
    file on disk was unreadable or malformed, in which case it is preserved
    as a `.corrupt-<timestamp>` corpse (see _quarantine_corpse) and a
    warning is logged loudly. Corruption here is judged only by "is this
    valid JSON, and is its top level an object" -- narrower than diary/
    reflection's state-shape checks, because a hand-corrupted `updated`
    field inside an otherwise well-formed dict is eligibility()'s problem
    to degrade gracefully from, not load_portrait's to quarantine over.
    """
    path = Path(cfg.portrait_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"unexpected portrait shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return None
    return data


def _atomic_write(path: Path, data: dict) -> None:
    """Write to a temp file in the same directory, then os.replace() into
    place, so a reader never observes a half-written file. Copies diary.py's
    atomic-write idiom; unlike diary's, this module never needs the
    trailing-newline option -- neither the live file nor a history snapshot
    is meant to be opened and read by a human the way a diary entry is."""
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


def _clean_opinions(raw) -> list:
    """Keep only well-shaped {"topic": str, "opinion": str} elements for
    save_portrait's `opinions` field. Any extra keys an element carries
    beyond those two are dropped along with it being kept; anything not a
    dict, or a dict missing either string field, is discarded outright.
    Capped at PORTRAIT_OPINIONS_STORED_CAP, keeping the first entries in
    caller order."""
    if not isinstance(raw, list):
        return []
    cleaned = [
        {"topic": item["topic"], "opinion": item["opinion"]}
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("topic"), str)
        and isinstance(item.get("opinion"), str)
    ]
    return cleaned[:PORTRAIT_OPINIONS_STORED_CAP]


def _clean_observations(raw) -> list:
    """Keep only str elements for save_portrait's `observations` field,
    discarding anything else (None, numbers, dicts, nested lists...)
    outright. Capped at PORTRAIT_OBSERVATIONS_STORED_CAP, keeping the first
    entries in caller order."""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)][:PORTRAIT_OBSERVATIONS_STORED_CAP]


def save_portrait(cfg: Config, entry: dict, now: datetime) -> Path:
    """Persist a new self-portrait snapshot. Only four fields are ever
    written -- `updated` is always computed fresh here from `now`, never
    taken from `entry` even if it happens to carry its own.

    Contract: `now` must be timezone-aware, the same contract diary.py's
    eligibility() and build_material() document. `updated` is
    now.date().isoformat() -- the caller's own local calendar day, not a
    UTC day; whichever timezone `now` carries is what lands on disk.

    `content` is redacted through diary.filter_sensitive first (the same
    filter diary entries are saved through), then tail-truncated to
    PORTRAIT_CONTENT_MAX_CHARS with a warning logged only when truncation
    actually happens. `opinions`/`observations` are shape-filtered and
    capped -- see _clean_opinions/_clean_observations; a missing or
    wrong-typed field in `entry` defaults to an empty list rather than
    being omitted, so every portrait on disk has the same shape.

    Writes twice: the live file at cfg.portrait_path (atomic, mirroring
    diary._atomic_write) and a byte-identical history snapshot at
    cfg.portrait_history_dir / f"{updated}.json". A second save on the same
    calendar day overwrites that day's snapshot rather than adding a second
    one -- history holds at most one entry per day, ever. Both parent
    directories are created if missing. Returns the live file's path.
    """
    updated = now.date().isoformat()

    content = filter_sensitive(entry.get("content") or "")
    if len(content) > PORTRAIT_CONTENT_MAX_CHARS:
        logger.warning(
            "portrait: content truncated from %d to %d chars",
            len(content), PORTRAIT_CONTENT_MAX_CHARS)
        content = content[:PORTRAIT_CONTENT_MAX_CHARS]

    record = {
        "updated": updated,
        "content": content,
        "opinions": _clean_opinions(entry.get("opinions")),
        "observations": _clean_observations(entry.get("observations")),
    }

    path = Path(cfg.portrait_path)
    _atomic_write(path, record)
    _atomic_write(Path(cfg.portrait_history_dir) / f"{updated}.json", record)
    return path


# ---------------------------------------------------------------------
# Eligibility: pure, no I/O, no clock of its own
# ---------------------------------------------------------------------

def eligibility(cfg: Config, portrait: dict | None, diary_entries: list, now: datetime) -> str | None:
    """Decide whether a new self-portrait snapshot may be composed right
    now. Returns None when writing is allowed; otherwise a short reason
    string identifying which gate stopped it -- the reason IS the log line
    at every call site, so a quiet week is always explainable after the
    fact, never a silent no-op. Checked in this order, first match wins:

      "disabled"              cfg.portrait_enabled is False
      "interval_not_reached"  a previous portrait exists and not enough
                               days have passed since it (see below)
      "no_new_material"       there is no diary entry newer than the
                               previous portrait (or, with no previous
                               portrait at all, no diary entries at all)

    `portrait` is whatever load_portrait() returned: None for "no previous
    portrait", or its dict otherwise. `diary_entries` is a list of diary
    entries (each with a `date` field, the "YYYY-MM-DD" string diary.py's
    own entries carry) -- newness is decided by plain string comparison
    against the previous portrait's `updated` field, which works precisely
    because both sides are ISO calendar dates.

    A first-ever portrait (portrait is None) is a special case throughout:
    there is no interval to have not reached yet, so that gate is skipped
    entirely, and every diary entry at all counts as new material -- a
    single entry is enough to write the first snapshot.

    The interval gate itself: with a previous portrait whose `updated` is
    "2026-07-02" and cfg.portrait_interval_days=7, day 2026-07-09 is
    exactly the boundary and reads as due (`(now.date() - updated).days`
    is 7, not < 7) -- one day earlier, 2026-07-08, still reads as
    "interval_not_reached" (6 < 7).

    Defensive: if the previous portrait's `updated` field is missing, not a
    string, or not a valid ISO date (`date.fromisoformat` raising), the
    whole previous portrait is treated exactly as if there were no previous
    portrait at all -- both gates it would have driven degrade the same
    way a first-ever portrait's do, and a warning is logged. A hand-edited
    or otherwise corrupted `updated` field must never crash a caller.

    This function is pure: it reads no clock and touches no filesystem --
    `now` and `portrait` are both supplied by the caller, so every branch
    above can be tested in isolation. Unlike diary.eligibility(), there is
    no timezone contract to document for `now` here: only `now.date()` is
    ever read, which behaves identically whether `now` is naive or aware.
    """
    if not cfg.portrait_enabled:
        return "disabled"

    updated_str = None
    if portrait is not None:
        raw_updated = portrait.get("updated")
        parsed = None
        if isinstance(raw_updated, str):
            try:
                parsed = date.fromisoformat(raw_updated)
            except ValueError:
                parsed = None
        if parsed is None:
            logger.warning(
                "portrait: malformed updated field %r; treating as no previous portrait",
                raw_updated)
        else:
            if (now.date() - parsed).days < cfg.portrait_interval_days:
                return "interval_not_reached"
            updated_str = raw_updated

    new_count = sum(1 for e in diary_entries if updated_str is None or e["date"] > updated_str)
    if new_count == 0:
        return "no_new_material"

    return None
