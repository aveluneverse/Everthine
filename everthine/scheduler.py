"""The proactive-reach-out radar: pure decision logic for whether, and
which, message a companion might send first -- a good-morning greeting,
missing her after a long enough silence, or sharing something unprompted
-- without ever calling the engine, touching the network, or importing
bot.py. Wiring this onto the bot's background tick is a later milestone
task; consuming these decisions to actually generate and send a message
is the task after that. This module only answers "is now an appropriate
moment, and for what" -- never "what should he say."

Three layers sit on top of each other. The gates decide eligibility:
common_gate() is the shared "may he even consider reaching out at all"
check everything else sits behind, and greeting_due() / miss_you_due() /
share_due() are the three per-message due-checks layered on top of it.
pick_job() composes both layers into one call: the common gate first,
then the three per-message checks in a fixed priority order, returning
exactly one job to run or one reason nothing ran. truth_timeline() is a
separate, single-source read of the conversation archive -- the one
place "how long has it actually been quiet" gets computed, so nothing
downstream keeps its own competing guess. record_nudge() closes the
loop: the accounting call made once a message has been generated, so the
same silence never gets nudged into twice.

Fail-soft is the whole design for the state file, exactly as in
stages.py, diary.py, and reflection.py: a missing scheduler_state.json
quietly becomes a fresh, nothing-recorded-yet state. A corrupted one --
unreadable, not valid JSON, or valid JSON in an unexpected shape -- is
not silently discarded: the broken file is renamed alongside itself as a
`.corrupt-<timestamp>` corpse so nothing is lost to a human who goes
looking, a warning is logged loudly, and the caller still gets back a
usable fresh state. This schema is plain str/int throughout -- unlike
diary's Optional[str] date fields, no field here ever legitimately holds
None; an empty string is the unset sentinel everywhere in it.

Every gate and due-check is pure -- no clock, no filesystem -- so every
reason a message gets skipped can be tested in isolation, and at call
sites logged verbatim: every quiet tick is one of a small fixed set of
named reasons, never a silent no-op. share_due's dice roll is a plain
float argument the caller supplies, never rolled inside this module, so
tests can hand it any value they like.

Takes plain paths, dicts, and datetimes throughout; imports nothing from
the framework at runtime beyond archive (truth_timeline's one data
source). `Config` appears only in TYPE_CHECKING type hints, costing
nothing at runtime.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import archive

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("everthine")

# --- Module constants ---------------------------------------------------

TICK_INTERVAL_S = 300            # this module's own copy of the tick cadence --
                                  # bot.py's identically-named constant is left
                                  # untouched by this task; a later task retires it
RECENT_INTERACT_MINUTES = 3      # "she's on the line right now" -- never interrupt
PROACTIVE_COOLDOWN_MINUTES = 90  # minimum spacing enforced between any two nudges
PROACTIVE_TIMEOUT_S = 120        # engine budget for one proactive message; a later
                                  # task consumes this, this task only defines it

# Roughly 180 ticks/day fall outside the default quiet window: at
# TICK_INTERVAL_S=300s (12 ticks/hour) and the default 23-08 quiet window
# (15 open hours/day), 15 * 12 = 180. 180 * 0.02 ~= 3.6 expected share
# rolls a day, in practice capped by share_max_daily (2 by default). The
# chance of at least one hit within the first 4 open hours (48 ticks) is
# 1 - (1 - 0.02)**48 ~= 63%: the distribution is naturally front-loaded,
# but a chance to share persists all day long.
SHARE_CHANCE_PER_TICK = 0.02


# ---------------------------------------------------------------------
# State file: cfg.scheduler_state_path (data/scheduler_state.json)
# ---------------------------------------------------------------------

def _fresh_state() -> dict:
    return {
        "greeting_date": "",
        "miss_you_anchor": "",
        "share_date": "",
        "share_count": 0,
        "budget_date": "",
        "budget_used": 0,
        "last_nudge_at": "",
    }


_STR_FIELDS = ("greeting_date", "miss_you_anchor", "share_date", "budget_date", "last_nudge_at")
_INT_FIELDS = ("share_count", "budget_used")


def _is_well_shaped(data) -> bool:
    """The single place corruption is decided for the state file. Every
    field is required and, unlike diary's Optional[str] date fields,
    never legitimately None -- an empty string is this schema's unset
    sentinel throughout, so a string field holding None is corruption,
    not a fresh value. bool is deliberately excluded from the int fields
    even though Python's bool is a subclass of int -- a state file with
    `"share_count": true` is not a count, it is corruption."""
    if not isinstance(data, dict):
        return False
    for key in _STR_FIELDS + _INT_FIELDS:
        if key not in data:
            return False
    for key in _STR_FIELDS:
        if not isinstance(data[key], str):
            return False
    for key in _INT_FIELDS:
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return False
    return True


def _quarantine_corpse(path: Path, reason: Exception) -> None:
    """Rename a broken scheduler-state file to a timestamped corpse
    alongside itself, then log loudly. A rename failure -- a target
    collision, a permissions problem, the file vanishing underneath us --
    is swallowed with its own warning: corpse preservation is a courtesy,
    never a reason to crash a reply. Verbatim copy of stages.py's,
    diary.py's, and reflection.py's _quarantine_corpse, retargeted at
    this module's own vocabulary."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    corpse = path.with_name(path.name + f".corrupt-{timestamp}")
    try:
        if corpse.exists():
            raise FileExistsError(f"corpse target already exists: {corpse}")
        path.rename(corpse)
    except OSError as exc:
        logger.warning(
            "scheduler: could not preserve broken state file %s as %s (%s); "
            "leaving the broken file in place", path, corpse, exc)
    logger.warning(
        "scheduler: %s is corrupt (%s); degrading to a fresh scheduler state", path, reason)


def load_state(path: Path) -> dict:
    """Load the scheduler state, degrading fail-soft on any trouble --
    mirrors diary.load_state exactly. A missing file returns a fresh
    state quietly: nothing has been recorded yet. Anything else that
    goes wrong -- unreadable, not valid JSON, or valid JSON in the wrong
    shape -- is treated as corruption: see _quarantine_corpse.
    """
    path = Path(path)
    if not path.exists():
        return _fresh_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not _is_well_shaped(data):
            raise ValueError(f"unexpected scheduler state shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return _fresh_state()
    return data


def _atomic_write(path: Path, state: dict) -> None:
    """Write to a temp file in the same directory, then os.replace() into
    place, so a reader never observes a half-written file. Verbatim copy
    of diary.py's atomic-write idiom; unlike diary's entry files, nothing
    this module writes is meant to be opened and read by a human, so
    there is no trailing-newline option here."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------
# The common gate: shared eligibility every proactive message sits behind
# ---------------------------------------------------------------------

def common_gate(cfg: Config, now: datetime, last_contact: datetime | None,
                 state: dict) -> str | None:
    """Decide whether ANY proactive message may even be considered right
    now -- independent of which one. None means it is fine to go on and
    check the individual jobs; otherwise the first reason that stopped
    it, checked in this order (first match wins):

      "disabled"        cfg.scheduler_enabled is False -- the master switch
      "never_met"       last_contact is None -- there has never been a
                         conversation to reach out from. Every proactive
                         message stays silent rather than risk sounding
                         like it remembers a relationship that has not
                         started yet.
      "quiet"           now falls inside the quiet-hours window (below)
      "partner_active"  she is on the line right now -- last contact was
                         under RECENT_INTERACT_MINUTES ago -- so nothing
                         proactive should talk over her
      "cooldown"        a nudge went out too recently: state's
                         last_nudge_at is under PROACTIVE_COOLDOWN_MINUTES
                         old
      "budget"          today's allowance is already spent: state's
                         budget_date is today AND budget_used has reached
                         cfg.proactive_daily_max. A budget_date from
                         yesterday or earlier never blocks -- see below.

    Quiet-hours window, decided on now.hour alone (minutes never matter,
    the same granularity every other hour-gate in this codebase uses):

      start < end   ->  quiet iff start <= now.hour < end
      start > end   ->  quiet iff now.hour >= start or now.hour < end
                         (wraps past midnight; the shipped default, 23-8,
                         reads as quiet from 23:00 through 07:59)
      start == end  ->  quiet hours are disabled outright -- this hour
                         never matches, by design, rather than "all day"

    The budget's "today" rolls over for free on the read side: budget_date
    is only ever compared against now's calendar date here, never mutated
    (that is record_nudge's job) -- a stale budget_date from an earlier
    day simply fails the comparison and stops blocking, with nothing to
    reset.

    Contract, not enforced here: `now` and `last_contact` (when not None)
    must both be timezone-aware, exactly as diary.eligibility documents.
    "Today" means now.date().isoformat() -- the calendar day of the aware
    `now`, in whatever tzinfo the caller normalized it to.
    """
    if not cfg.scheduler_enabled:
        return "disabled"

    if last_contact is None:
        return "never_met"

    start, end = cfg.quiet_start_hour, cfg.quiet_end_hour
    if start < end:
        in_quiet = start <= now.hour < end
    elif start > end:
        in_quiet = now.hour >= start or now.hour < end
    else:
        in_quiet = False
    if in_quiet:
        return "quiet"

    if (now - last_contact) < timedelta(minutes=RECENT_INTERACT_MINUTES):
        return "partner_active"

    last_nudge_at = state.get("last_nudge_at") or ""
    if last_nudge_at:
        last_nudge = datetime.fromisoformat(last_nudge_at)
        if (now - last_nudge) < timedelta(minutes=PROACTIVE_COOLDOWN_MINUTES):
            return "cooldown"

    today = now.date().isoformat()
    if (state.get("budget_date") == today
            and state.get("budget_used", 0) >= cfg.proactive_daily_max):
        return "budget"

    return None


# ---------------------------------------------------------------------
# Per-job due checks
# ---------------------------------------------------------------------

def greeting_due(cfg: Config, now: datetime, state: dict) -> str | None:
    """Is a good-morning greeting due? None means yes; otherwise, in
    order: "disabled" (cfg.greeting_enabled is False), "before_hour"
    (now.hour hasn't reached cfg.greeting_hour yet), "already_today"
    (state's greeting_date is already today -- one per day, always)."""
    if not cfg.greeting_enabled:
        return "disabled"
    if now.hour < cfg.greeting_hour:
        return "before_hour"
    if state.get("greeting_date") == now.date().isoformat():
        return "already_today"
    return None


def miss_you_due(cfg: Config, now: datetime, last_contact: datetime | None,
                  state: dict) -> str | None:
    """Is a missing-her message due? None means yes; otherwise, in order:

      "disabled"       cfg.miss_you_enabled is False
      "not_away"       last_contact is None, or the gap since it is under
                        cfg.miss_you_after_hours -- either way, she has
                        not been gone long enough (or at all) to miss yet
      "already_fired"  state's miss_you_anchor already equals
                        last_contact.isoformat() -- this exact away
                        period already got its one message

    The anchor is the whole dedup mechanism: compared by plain string
    equality against last_contact's own isoformat, never parsed or
    diffed against `now`. The moment she says anything at all,
    last_contact moves forward and the stored anchor -- pinned to the
    previous, now-stale last_contact -- stops matching on its own. No
    module-level or process-lifetime state is involved, so this survives
    a restart exactly as well as the file on disk does.

    By the time pick_job() reaches this check, common_gate's "never_met"
    gate has already guaranteed last_contact is not None -- but this
    function is defensive on its own account (returns "not_away" rather
    than raising on a None last_contact) so it stays safe to call
    directly, as this module's own tests do.
    """
    if not cfg.miss_you_enabled:
        return "disabled"
    if last_contact is None:
        return "not_away"
    if (now - last_contact) < timedelta(hours=cfg.miss_you_after_hours):
        return "not_away"
    if state.get("miss_you_anchor") == last_contact.isoformat():
        return "already_fired"
    return None


def share_due(cfg: Config, now: datetime, state: dict, roll: float) -> str | None:
    """Is an unprompted share due? None means yes; otherwise, in order:

      "disabled"  cfg.share_enabled is False
      "cap"       today's share_count already reached cfg.share_max_daily
                  (only when state's share_date is today -- a share_date
                  from an earlier day never caps; record_nudge is what
                  rolls it over, not this read-only check)
      "dice"      the caller-supplied `roll` did not beat
                  SHARE_CHANCE_PER_TICK; `roll >= SHARE_CHANCE_PER_TICK`
                  is the miss, so a roll exactly equal to the threshold
                  counts as a miss

    `roll` is a deliberate seam: nothing in this module calls random()
    itself, so a test can hand it any float it likes and get a fully
    deterministic answer.
    """
    if not cfg.share_enabled:
        return "disabled"
    if (state.get("share_date") == now.date().isoformat()
            and state.get("share_count", 0) >= cfg.share_max_daily):
        return "cap"
    if roll >= SHARE_CHANCE_PER_TICK:
        return "dice"
    return None


# ---------------------------------------------------------------------
# pick_job: the common gate + all three due-checks, composed into one call
# ---------------------------------------------------------------------

def pick_job(cfg: Config, now: datetime, last_contact: datetime | None,
             state: dict, roll: float) -> tuple[str | None, str | None]:
    """One call, one decision: which proactive job (if any) should run
    this tick. Returns a mutually exclusive pair -- exactly one of the
    two is None:

      due:   (job_name, None)     job_name is one of "greeting",
                                   "miss_you", "share"
      skip:  (None, skip_reason)  skip_reason is always a non-empty str

    common_gate() is checked first; a block there short-circuits straight
    to (None, reason) without even looking at the individual jobs. Past
    the common gate, greeting_due() / miss_you_due() / share_due() are
    tried in that fixed order and the first one that comes back due
    wins -- greeting outranks miss_you outranks share. When none of the
    three is due, the reason returned is share_due's (the last one
    checked); it is meant for a log line, not a perfect diagnosis of all
    three at once.

    At most one job per call, by construction: there is no mechanism
    here for returning two. A morning where both a greeting and a
    missing-her message would qualify still sends only the greeting this
    tick; the second waits for a later tick, and by then common_gate's
    cooldown gate (PROACTIVE_COOLDOWN_MINUTES, once record_nudge has
    logged the first one) enforces at least that much space between the
    two. This spacing is a deliberate design choice, not an accident of
    the priority order.
    """
    gate_reason = common_gate(cfg, now, last_contact, state)
    if gate_reason is not None:
        return None, gate_reason

    greeting_reason = greeting_due(cfg, now, state)
    if greeting_reason is None:
        return "greeting", None

    miss_you_reason = miss_you_due(cfg, now, last_contact, state)
    if miss_you_reason is None:
        return "miss_you", None

    share_reason = share_due(cfg, now, state, roll)
    if share_reason is None:
        return "share", None

    return None, share_reason


# ---------------------------------------------------------------------
# truth_timeline: single-source read of the conversation archive
# ---------------------------------------------------------------------

def truth_timeline(cfg: Config, now: datetime) -> tuple[float, float, bool] | None:
    """Scan the conversation archive once and return the three numbers
    every proactive message's sense of "how long has it actually been"
    should be built from -- never a separately tracked guess:

      partner_hours          hours since her (speaker "user") newest entry
      companion_hours        hours since his (speaker "companion") newest entry
      partner_replied_since  True iff her newest entry is more recent than
                             his newest entry -- she spoke last

    None when either speaker has no entry at all in the archive -- a
    brand-new relationship, or one where only one side has ever written
    anything, has no timeline to report yet. That is the fallback the
    caller relies on: no timeline block gets built at all, and whatever
    guards "what really happened" fall back to plain instruction text
    instead of a fabricated pair of numbers.

    A single pass over archive.iter_entries(cfg.archive_dir) (this
    module's only data source -- no separate parsing of the archive's
    file format, and no `since` bound: the whole history is read every
    time), tracking each speaker's maximum timestamp seen so far rather
    than trusting iteration order to already be chronological.

    `now` must be timezone-aware; each archive entry's timestamp already
    carries its own UTC offset (archive.py's own format), so nothing here
    normalizes timezones beyond ordinary aware-aware subtraction.
    """
    partner_last: datetime | None = None
    companion_last: datetime | None = None
    for entry in archive.iter_entries(cfg.archive_dir):
        ts = entry["timestamp"]
        if entry["speaker"] == "user":
            if partner_last is None or ts > partner_last:
                partner_last = ts
        elif entry["speaker"] == "companion":
            if companion_last is None or ts > companion_last:
                companion_last = ts

    if partner_last is None or companion_last is None:
        return None

    partner_hours = (now - partner_last).total_seconds() / 3600
    companion_hours = (now - companion_last).total_seconds() / 3600
    partner_replied_since = partner_last > companion_last
    return partner_hours, companion_hours, partner_replied_since


# ---------------------------------------------------------------------
# record_nudge: the accounting call, made once a message has been conceived
# ---------------------------------------------------------------------

def record_nudge(path: Path, job: str, now: datetime,
                  last_contact: datetime | None) -> None:
    """Record that a proactive nudge was just conceived: load -> mutate
    -> _atomic_write, in one call. Always updates the shared bookkeeping,
    regardless of which job:

      - budget_date != today -> budget_date = today, budget_used = 0
        (the free rollover on common_gate's read side becomes an actual
        reset here); then budget_used += 1 unconditionally.
      - last_nudge_at = now.isoformat() -- feeds common_gate's cooldown
        gate on the next tick.

    Then exactly one job-specific field, keyed by `job`:

      "greeting"   greeting_date = today
      "miss_you"   miss_you_anchor = last_contact.isoformat() -- pins the
                   anchor miss_you_due() dedupes against for this away
                   period. Only this job reads last_contact; it must not
                   be None when job == "miss_you" (pick_job's own gating
                   guarantees that in practice).
      "share"      share_date != today -> share_date = today, share_count
                   = 0 (the same free-rollover-becomes-reset as budget,
                   above); then share_count += 1 unconditionally

    Nudge semantics, deliberately: this is called after a message has
    been successfully generated but before it is known to have been
    delivered (a later task's wiring). Accounting counts the attempt at
    reaching out, not receipt -- a send that fails after this point still
    used up its slot in the budget; that failure is a delivery problem to
    log elsewhere, not a reason to let the same silence be nudged into
    twice.
    """
    path = Path(path)
    state = load_state(path)
    today = now.date().isoformat()

    if state.get("budget_date") != today:
        state["budget_date"] = today
        state["budget_used"] = 0
    state["budget_used"] = state.get("budget_used", 0) + 1
    state["last_nudge_at"] = now.isoformat()

    if job == "greeting":
        state["greeting_date"] = today
    elif job == "miss_you":
        state["miss_you_anchor"] = last_contact.isoformat()
    elif job == "share":
        if state.get("share_date") != today:
            state["share_date"] = today
            state["share_count"] = 0
        state["share_count"] = state.get("share_count", 0) + 1

    _atomic_write(path, state)
