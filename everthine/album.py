"""Keepsake album: a private, append-only record of moments either side of
the relationship chose to keep.

Three design principles, in the same spirit as this framework's other
state modules:

1. Authenticity. An entry stores the flagged message's raw text and
   nothing else -- this module never reasons about, summarizes, or
   editorializes a kept moment. What went in is what comes back out.

2. Natural overflow, never an instruction. A kept moment can only ever
   reach the companion through a future inner pipeline (a diary entry, an
   evolving self-portrait -- later milestones); it never enters a live
   conversation prompt. This is a project ruling (D1, 2026-07-06), not
   just a convention: no function in this module builds prompt text, and
   persona.py / layers.py / dynamic_context.py must never import or even
   mention this module. A guard test in tests/test_persona_assembly.py
   pins that boundary mechanically -- if it ever turns red, something was
   wired into the prompt chain that should not have been.

3. Keeping a moment is a right, not a privilege. Writing a keepsake is
   never conditioned on where the relationship currently stands: there is
   no stage check anywhere in this module (it does not import the stages
   module, or persona, or anything else that could gate a write on
   relationship progress). The only gate on writing at all is the feature
   being enabled (cfg.album_enabled); once it is, either side can always
   keep a moment.

Storage is a single JSON file, `{"version": 1, "entries": [...]}`,
written atomically (tempfile + os.replace, so a reader never observes a
half-written file) and read back fail-soft: a missing file is quietly a
fresh, empty album. A corrupt one -- unreadable, not valid JSON, or valid
JSON in an unexpected shape -- is never silently discarded: the broken
file is preserved alongside itself as a `.corrupt-<timestamp>` corpse, a
warning is logged loudly, and the caller gets back a fresh, empty album.
A person's kept moments are never overwritten in silence. This module
copies stages.py's atomic-write and corpse-quarantine idiom exactly, down
to the same fail-soft philosophy.

`entries_for_today`/`entries_for_days` are the M5/M6 seam: the read APIs
a future inner pipeline pulls material from. See their docstrings for the
three commandments any consumer that turns this material into prompt
framing must follow.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config

logger = logging.getLogger("everthine")


def _fresh_album() -> dict:
    return {"version": 1, "entries": []}


def _is_well_shaped(data) -> bool:
    """The single place corruption is decided. Every entry is validated
    down to its keys, and its timestamp's parseability, here -- so
    everything downstream (all_entries, entries_for_today,
    entries_for_days) can work with a loaded entry without a defensive
    check of its own, the same contract stages.py's _is_well_shaped
    documents for stage_block()."""
    if not isinstance(data, dict):
        return False
    if "version" not in data or "entries" not in data:
        return False
    if data["version"] != 1:
        return False
    entries = data["entries"]
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("id"), str):
            return False
        if not isinstance(entry.get("direction"), str):
            return False
        if not isinstance(entry.get("message_id"), int):
            return False
        message = entry.get("message")
        if not isinstance(message, dict):
            return False
        if not isinstance(message.get("speaker"), str):
            return False
        if not isinstance(message.get("text"), str):
            return False
        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str):
            return False
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            return False
    return True


def _quarantine_corpse(path: Path, reason: Exception) -> None:
    """Rename a broken album file to a timestamped corpse alongside
    itself, then log loudly. A rename failure -- a target collision, a
    permissions problem, the file vanishing underneath us -- is swallowed
    with its own warning: corpse preservation is a courtesy, never a
    reason to crash a reply. Verbatim copy of stages.py's
    _quarantine_corpse, retargeted at the album's own vocabulary."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    corpse = path.with_name(path.name + f".corrupt-{timestamp}")
    try:
        if corpse.exists():
            raise FileExistsError(f"corpse target already exists: {corpse}")
        path.rename(corpse)
    except OSError as exc:
        logger.warning(
            "album: could not preserve broken album file %s as %s (%s); "
            "leaving the broken file in place", path, corpse, exc)
    logger.warning(
        "album: %s is corrupt (%s); degrading to a fresh, empty album", path, reason)


def load_album(path: Path) -> dict:
    """Load the album, degrading fail-soft on any trouble -- mirrors
    stages.load_state exactly. A missing file returns a fresh, empty
    album quietly: nothing has gone wrong yet, there is simply nothing
    written so far. Anything else that goes wrong -- unreadable, not
    valid JSON, or valid JSON in the wrong shape -- is treated as
    corruption: see _quarantine_corpse.
    """
    path = Path(path)
    if not path.exists():
        return _fresh_album()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not _is_well_shaped(data):
            raise ValueError(f"unexpected album shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return _fresh_album()
    return data


def _atomic_write(path: Path, data: dict) -> None:
    """Verbatim copy of stages.py's atomic-write idiom: write to a temp
    file in the same directory, then os.replace() into place, so a reader
    never observes a half-written album.json."""
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


def _add_entry(cfg: Config, *, direction: str, speaker: str, text: str,
               message_id: int, now: datetime) -> bool:
    """Shared body for add_partner_flag/add_companion_flag: gate on
    cfg.album_enabled, dedup on message_id (dedup is shared across both
    directions -- a given source message is kept at most once, no matter
    which side flagged it first), then append and write. Returns False,
    writing nothing, when the flag is off or the message is already kept.
    """
    if not cfg.album_enabled:
        return False
    data = load_album(cfg.album_path)
    if any(e.get("message_id") == message_id for e in data["entries"]):
        return False
    entry = {
        "id": "keep_" + now.strftime("%Y%m%d_%H%M%S_%f"),
        "timestamp": now.isoformat(),
        "direction": direction,
        "message": {"speaker": speaker, "text": text},
        "message_id": message_id,
    }
    data["entries"].append(entry)
    _atomic_write(cfg.album_path, data)
    return True


def add_partner_flag(cfg: Config, text: str, message_id: int, now: datetime) -> bool:
    """Record a companion message the partner hearted. Keeping a moment
    is a right (design principle 3): the only gate here is
    cfg.album_enabled -- no stage or mood check, ever. Returns False
    (writing nothing) when the album feature is disabled, or when
    message_id is already kept.
    """
    return _add_entry(cfg, direction="partner_flagged", speaker="companion",
                       text=text, message_id=message_id, now=now)


def add_companion_flag(cfg: Config, text: str, message_id: int, now: datetime) -> bool:
    """Record a partner message the companion reacted to with a heart
    (a "[react:heart]" marker in its reply). Same contract as
    add_partner_flag: only cfg.album_enabled gates the
    write, and message_id dedup is shared with the partner-side flag (one
    source message, kept at most once, regardless of which side kept it
    first).
    """
    return _add_entry(cfg, direction="companion_flagged", speaker="user",
                       text=text, message_id=message_id, now=now)


def remove_by_message_id(cfg: Config, message_id: int) -> bool:
    """Unflag by the original message's id. False, writing nothing, when
    nothing matched -- including against a missing or freshly-degraded
    album: a no-op removal never conjures an empty album.json out of
    nothing."""
    data = load_album(cfg.album_path)
    kept = [e for e in data["entries"] if e.get("message_id") != message_id]
    if len(kept) == len(data["entries"]):
        return False
    data["entries"] = kept
    _atomic_write(cfg.album_path, data)
    return True


def remove_by_id(cfg: Config, entry_id: str) -> bool:
    """Unflag by the keepsake's own id (the entry's "id" field, not the
    source message_id). Same no-match/no-write contract as
    remove_by_message_id."""
    data = load_album(cfg.album_path)
    kept = [e for e in data["entries"] if e.get("id") != entry_id]
    if len(kept) == len(data["entries"]):
        return False
    data["entries"] = kept
    _atomic_write(cfg.album_path, data)
    return True


def all_entries(cfg: Config) -> list:
    """Every kept moment, in storage order (chronological by when each
    was kept). For a management UI (e.g. an /album listing) -- not part
    of the M5/M6 seam, which is entries_for_today/entries_for_days
    below."""
    return list(load_album(cfg.album_path)["entries"])


def entries_for_today(cfg: Config, now: datetime) -> list:
    """M5/M6 seam: every kept moment whose stored timestamp falls on
    `now`'s local calendar date. Storage order is already chronological,
    so results come back oldest-first with no re-sort needed.

    This is one of two read APIs (with entries_for_days) a future inner
    pipeline -- a diary entry, an evolving self-portrait -- draws
    material from. This module never builds prompt text itself (design
    principle 2 above); whoever calls this next and turns the result into
    prompt framing must hold three commandments:

      1. Material, not instruction. Present what was kept and close with
         something like "you may leave this untouched" -- never a
         directive that it must be used or reacted to.
      2. No judging or summarizing. Hand over what is there; do not grade
         it, rank it, or compress it into a verdict.
      3. No reward signal. A kept moment is never framed as something
         earned, or as praise for having been kept -- it is simply what
         happened.
    """
    today = now.date()
    return [e for e in load_album(cfg.album_path)["entries"]
            if datetime.fromisoformat(e["timestamp"]).date() == today]


def entries_for_days(cfg: Config, days: int, now: datetime) -> list:
    """M5/M6 seam, sibling of entries_for_today: every kept moment whose
    local calendar date falls within the last `days` days, inclusive of
    today (days=3 means today, yesterday, and the day before it -- three
    calendar dates total). Storage order is already chronological, so
    results come back oldest-first with no re-sort needed. The same
    three-commandment consumer obligation documented on entries_for_today
    applies to whatever framing is built from this result.
    """
    today = now.date()
    valid_dates = {today - timedelta(days=n) for n in range(days)}
    return [e for e in load_album(cfg.album_path)["entries"]
            if datetime.fromisoformat(e["timestamp"]).date() in valid_dates]
