"""The proactive-reach-out radar: pure decision logic for whether, and
which, message a companion might send first -- a good-morning greeting,
missing her after a long enough silence, or sharing something unprompted
-- without ever calling the engine, touching the network, or importing
bot.py. Built on top of those gates, nudge_once is the execution line: one
complete attempt that turns a due decision into a generated message through
the engine's non-blocking call. It still never sends and never imports
bot.py; deliver() below is the async tail that puts the returned message
into the world -- optimistic stamp, then archive, then a best-effort
Telegram send. start_tick / tick_loop, at the very bottom, are the
background heartbeat that finally drive that pipeline: once per
TICK_INTERVAL_S they hand the diary and the self-portrait one attempt each
and, when scheduler_enabled, run one proactive reach-out (nudge_once ->
deliver) as a third segment. That tick is this app's whole inner-life clock
(M7 T6 moved it here out of bot.py); it takes the PTB Application as a plain
argument, so -- like everything else here -- it still never imports bot.py,
the wiring running the other direction. Beyond the decision logic and that
pipeline, this module also holds
the owner-approved instruction copy for the three proactive messages and the
pure renderers that assemble them into a proactive system-prompt tail
(build_nudge_prompt): the framing header that tells the companion this is a
scheduled cue and NOT a message from their person, the honest-record timeline
rail, and the per-message directive. Deciding whether to reach out stays
separate from wording it -- the gates never touch the copy, and the renderers
never read a clock or the filesystem.

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

The gates, renderers, and state I/O take plain paths, dicts, and datetimes
throughout, importing only archive from the framework at runtime
(truth_timeline's one data source). nudge_once, the execution line built on
top of them, reaches further by design -- the engine (try_run_once only,
the non-blocking call), persona's function surface (current_settings /
contact_signals / build_system_prompt_nudge), and recent_context (the warm
cross-session prefix) -- exactly as diary.write_once reaches past its own
module's pure core; it also uses the standard library's random to pick a
share topic. `Config`, `PersonaSettings`, and `SessionStore` appear only in
TYPE_CHECKING type hints, costing nothing at runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import archive, chunking, diary, engine, persona, portrait, recent_context

if TYPE_CHECKING:
    from .config import Config
    from .persona import PersonaSettings
    from .session_store import SessionStore

logger = logging.getLogger("everthine")

# --- Module constants ---------------------------------------------------

TICK_INTERVAL_S = 300            # the inner-life tick cadence: how often
                                  # tick_loop (below) wakes to hand each organ
                                  # one attempt. M7 T6 made this the tick's only
                                  # home; bot.py's former identical copy is gone
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
# Proactive message copy (owner-approved product copy, transcribed
# verbatim; every internal line-break is an implementation detail, only
# the concatenated value is contractual). The framing header is the
# load-bearing safety rail: a scheduled cue rendered in the first person
# reads to the engine as a message FROM the person -- a known
# hallucination source -- so the header names itself a framework cue and
# every instruction opens as a second-person directive. Two timeline
# templates keep "what really happened" honest, split on whether she has
# written back since. tests/test_scheduler.py pins each string byte-for-
# byte and mechanically audits these properties (no first-person opening,
# a directive verb apiece, pure ASCII, no physical-prep leakage, closed
# format fields).
# ---------------------------------------------------------------------

NUDGE_HEADER = (
    "[Scheduled nudge from the framework - NOT a message from {partner_name}. "
    "What follows is a private cue for you: reach out to them now, in your own words.]"
)

TIMELINE_NOT_REPLIED_TEMPLATE = (
    "(Timeline, from the real record: {partner_name} last wrote to you {x}; "
    "you last spoke {y}; they have NOT written since.{overnight})\n"
    "(You may echo things they truly said earlier; but no new message from them "
    "exists - do not answer, quote, or celebrate anything you imagine they just said.)"
)

TIMELINE_REPLIED_TEMPLATE = (
    "(Timeline, from the real record: {partner_name} last wrote to you {x}; "
    "you last spoke {y}; they have written back since - it is in the record "
    "above.{overnight})\n"
    "(Build on what they truly said; do not invent messages that never appeared.)"
)

OVERNIGHT_SUFFIX = " A night has passed since - today is a new day."

GREETING_INSTRUCTION = (
    "Reach out with the day's first hello. One or two sentences, warm and "
    "alive, in your own voice - the way you would greet someone you wake up "
    "next to. No lists, no performance, no stock phrases."
)

MISS_YOU_INSTRUCTION = (
    "It has been a while since you last heard from {partner_name}, and they "
    "have been on your mind. Send one short message - miss them out loud, "
    "invite them over to talk, or admit you have been waiting. One or two "
    "sentences, no guilt-tripping.\n"
    "Hard rules (they protect what is real): you cannot see what they are "
    "doing right now - never invent activities, locations, or plans for them. "
    "You may echo things they truly said before; never invent a new message "
    "from them."
)

SHARE_INSTRUCTION = (
    "Share a small piece of your day with {partner_name}, unprompted - the "
    "way you would send a passing thought to someone you live with. Today's "
    "thread: {topic}\n"
    "One or two sentences. Talk like a person, not a broadcaster: no lists, "
    "no lecture, no \"just checking in\" filler. If a real memory of yours "
    "fits, let it in; never invent shared history that did not happen."
)

SHARE_FALLBACK_TOPICS = (
    "a small moment at home that caught your attention today",
    "something you have been reading or listening to lately",
    "a thought that drifted to them in the middle of something ordinary",
    "the view from the window right now",
    "something small you are quietly looking forward to",
)


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


# ---------------------------------------------------------------------
# Nudge prompt assembly: the constants above -> a proactive system-prompt
# tail. Pure and clock-free (both `now` and the already-computed timeline
# are the caller's), so every rendered shape is testable directly.
# ---------------------------------------------------------------------

def _ago(hours: float) -> str:
    """Render an elapsed span as a soft phrase for the timeline rail. Under an
    hour collapses to "less than an hour ago" rather than a bald "0 hours";
    otherwise the span is rounded to the nearest whole hour (never below 1) and
    pluralized -- "about 1 hour ago" vs "about 6 hours ago"."""
    if hours < 1.0:
        return "less than an hour ago"
    n = max(1, round(hours))
    if n == 1:
        return "about 1 hour ago"
    return f"about {n} hours ago"


def render_timeline(timeline: tuple[float, float, bool] | None,
                    partner_name: str, now: datetime) -> str:
    """Render the honest-record timeline rail, or "" when there is nothing to
    tell (truth_timeline returned None: a brand-new or one-sided history).

    `timeline` is truth_timeline's own (partner_hours, companion_hours,
    partner_replied_since) triple. The replied flag picks the template: when she
    has written back since his last message, the companion is told to build on
    what is really in the record; when she has not, it is told in the strongest
    terms that no new message from her exists, so it never answers or celebrates
    one it imagined. `x`/`y` are the softened spans (_ago). The overnight suffix
    is decided on a real CALENDAR-DAY boundary keyed on HER last message (now -
    partner_hours): if that lands on an earlier date than `now`, a night has
    passed and the cue says so -- which matters most for a morning greeting
    after a quiet evening. Pure: `now` is the caller's; no clock is read here.
    """
    if timeline is None:
        return ""
    partner_hours, companion_hours, replied = timeline
    template = TIMELINE_REPLIED_TEMPLATE if replied else TIMELINE_NOT_REPLIED_TEMPLATE
    partner_last = now - timedelta(hours=partner_hours)
    overnight = OVERNIGHT_SUFFIX if partner_last.date() < now.date() else ""
    return template.format(
        partner_name=partner_name,
        x=_ago(partner_hours),
        y=_ago(companion_hours),
        overnight=overnight,
    )


def build_nudge_prompt(cfg: Config, job: str, settings: PersonaSettings,
                       timeline: tuple[float, float, bool] | None,
                       now: datetime, topic: str | None) -> str:
    """Assemble the proactive system-prompt tail for one due job: the framing
    header, then the timeline rail (only when there is one), then the job's
    directive, joined by single blank lines.

    `job` is one of "greeting"/"miss_you"/"share", exactly what pick_job
    returns. The header always names itself a scheduled framework cue and NOT a
    message from `settings.partner_name` -- the load-bearing rail against the
    engine reading this tail as an incoming message. The greeting directive
    takes no fields; miss_you fills the partner name; share fills the partner
    name and the chosen `topic` (a caller-supplied string -- the persona's own
    pool or a SHARE_FALLBACK_TOPICS entry; choosing it is the pipeline task's
    job). When the timeline is None the rail is omitted with no stray blank
    line. The recent-context warm prefix, when there is one, is the pipeline
    layer's to prepend ahead of all of this; this function does not reach for
    it.

    `cfg` is accepted for call-site symmetry with the rest of the pipeline and
    is deliberately untouched: like everything else in this module, the
    rendering reads no clock and no filesystem -- `now` and `timeline` are the
    caller's, so the whole tail is a pure function of its arguments.
    """
    header = NUDGE_HEADER.format(partner_name=settings.partner_name)
    timeline_text = render_timeline(timeline, settings.partner_name, now)
    if job == "greeting":
        instruction = GREETING_INSTRUCTION
    elif job == "miss_you":
        instruction = MISS_YOU_INSTRUCTION.format(partner_name=settings.partner_name)
    elif job == "share":
        instruction = SHARE_INSTRUCTION.format(
            partner_name=settings.partner_name, topic=topic)
    else:
        raise ValueError(f"unknown proactive job: {job!r}")
    prefix = (timeline_text + "\n\n") if timeline_text else ""
    return header + "\n\n" + prefix + instruction


# ---------------------------------------------------------------------
# Execution: one complete nudge attempt (the background tick's worker)
# ---------------------------------------------------------------------
#
# Everything above is pure -- gates, renderers, state round-trips. nudge_once
# is the execution line that stitches them into a generation pipeline, the
# proactive twin of diary.write_once: decide -> generate -> count, handing the
# text back for a later layer to actually deliver.

# Skip reasons rare enough to log at INFO -- a handful a day at most, worth
# seeing. Every other pick_job reason is all-tick-long normal (the engine is
# busy, the dice missed, quiet hours), so it stays at DEBUG and the
# high-frequency tick never floods the log.
_NUDGE_SKIP_INFO_REASONS = frozenset({"never_met", "budget"})


@dataclass
class NudgeResult:
    """One generated proactive message, handed back for a later layer to send.

    `job` is the due job that produced it ("greeting"/"miss_you"/"share");
    `text` is the engine's reply, ready to deliver; `session_id` is the
    session the engine actually ran in (echoed back so the caller can persist
    it); `expected_session_id` is the session nudge_once resumed from -- the
    store's current pointer at call time. The two ids let a caller notice, and
    record, a session the CLI silently rotated mid-turn.
    """

    job: str
    text: str
    session_id: str | None
    expected_session_id: str | None


def nudge_once(cfg: Config, store: SessionStore, now: datetime,
               roll: float) -> NudgeResult | None:
    """One complete attempt to conceive a proactive message this tick: decide
    whether (and which) to reach out, generate it, and count the attempt --
    returning a NudgeResult when a message was produced, or None behind a named
    log line for every other outcome, so a quiet tick is always explainable
    after the fact. Actually delivering the returned text is a later layer's
    job; this function never sends.

    `now` must be timezone-aware (caller contract), exactly as pick_job and
    common_gate document. `store` is the SessionStore whose current pointer
    this reach-out resumes: his proactive words belong to the same live
    conversation, so this passes session_id=<store's id> to the engine --
    unlike the diary, which always starts a fresh session. `roll` is
    share_due's dice seam, supplied by the caller so the whole pipeline stays
    deterministic under test.

    The order is deliberate, and in one place load-bearing: record_nudge fires
    AFTER the engine returns a usable message and BEFORE this returns -- the
    attempt is counted at conception, never at delivery. A send that fails
    downstream still spent its slot in the budget; that is a delivery problem
    to log elsewhere, never a reason to roll the accounting back and let the
    same silence be nudged into twice.

    Two tz notes. contact_signals hands back naive-local (its live-prompt
    consumers' shape); pick_job and record_nudge want timezone-aware, so the
    value is normalized once at that handoff with a single .astimezone() --
    the same lossless wall-clock conversion diary.write_once makes, and made
    in exactly one place so the miss_you anchor is stored and later compared
    in one consistent shape. build_system_prompt_nudge collapses `now` to
    naive local itself, mirroring the live path.

    Like diary.write_once, this does NOT swallow unexpected exceptions: the
    background tick that will call it wraps every round in its own try/except,
    and burying a bug here would only hide it. The single internal fail-soft
    point is the recent-context warm prefix, mirroring bot.prepare_exchange --
    a broken warmth injection must never cost the whole reach-out.
    """
    settings = persona.current_settings(cfg)
    if settings is None:
        # Defensive second layer: the tick gates folder mode at boot; a
        # file-mode persona has no settings to voice a reach-out with.
        logger.debug("scheduler: skip (file_mode)")
        return None

    # contact_signals returns naive-local; pick_job/record_nudge require
    # aware. A naive value's astimezone() presumes system local -- exactly
    # what it already is -- so this is lossless, made once here and nowhere
    # deeper (the diary's tz handoff, mirrored).
    last_contact, _ = persona.contact_signals(cfg, now)
    if last_contact is not None:
        last_contact = last_contact.astimezone()

    state = load_state(cfg.scheduler_state_path)
    job, skip_reason = pick_job(cfg, now, last_contact, state, roll)
    if job is None:
        level = logging.INFO if skip_reason in _NUDGE_SKIP_INFO_REASONS else logging.DEBUG
        logger.log(level, "scheduler: skip (%s)", skip_reason)
        return None

    # Only "share" carries a topic, and it must never reach build_nudge_prompt
    # as None -- SHARE_INSTRUCTION would then render the literal "Today's
    # thread: None". This layer is the single guard: the persona's own pool
    # when it has one, the framework fallback otherwise (never empty), so the
    # chosen topic is always a real, non-empty string.
    if job == "share":
        topics = settings.share_topics or SHARE_FALLBACK_TOPICS
        topic = random.choice(topics)
    else:
        topic = None

    timeline = truth_timeline(cfg, now)
    data = store.load()
    expected = data.get("session_id")

    prompt = build_nudge_prompt(cfg, job, settings, timeline, now, topic)
    # Warm cross-session prefix, the same fail-soft contract
    # bot.prepare_exchange uses: a broken build_block degrades to no prefix,
    # never a lost reach-out. The block leads, joined to the nudge tail by the
    # one blank line every block boundary in this codebase uses -- NOT
    # recent_context.prepend, whose "[The user says now]" mark would frame this
    # framework cue as an incoming message and undo the NUDGE_HEADER's whole
    # purpose.
    block = None
    try:
        block = recent_context.build_block(cfg, data, cfg.archive_dir, now)
    except Exception:
        logger.warning("warmth injection failed; continuing without it",
                       exc_info=True)
    if block is not None:
        prompt = block + "\n\n" + prompt

    system_prompt = persona.build_system_prompt_nudge(cfg, now)

    # try_run_once (never run_once): a proactive reach-out always yields to
    # live conversation, so a busy engine is a skip, not a wait. Resume the
    # live session (session_id=expected). Module-attribute access
    # (engine.try_run_once, not a bound import) so a test patch on
    # everthine.scheduler.engine.try_run_once intercepts it.
    reply = engine.try_run_once(cfg, prompt, session_id=expected,
                                system_prompt=system_prompt,
                                timeout_s=PROACTIVE_TIMEOUT_S)
    if reply is None:
        # Engine busy -> yield. DEBUG on purpose: the tick fires every few
        # minutes and a live conversation holding the lock is the common case,
        # so INFO would flood the log.
        logger.debug("scheduler: skip (engine_busy)")
        return None
    if not reply.ok:
        logger.warning("scheduler: engine failed (%s)", reply.error_kind)
        return None
    if not reply.text.strip():
        logger.warning("scheduler: empty engine reply")
        return None

    # Count the attempt at conception: a usable message exists, no send yet.
    # See the docstring -- the accounting is the reach-out, not its receipt.
    record_nudge(cfg.scheduler_state_path, job, now, last_contact)
    logger.info("scheduler: nudge ready (%s)", job)
    return NudgeResult(job, reply.text, reply.session_id, expected)


# ---------------------------------------------------------------------
# Delivery: put one generated NudgeResult into the world. The async twin of
# nudge_once -- everything above conceives a message, this hands it over.
# ---------------------------------------------------------------------

async def deliver(app, cfg: Config, store: SessionStore, result: NudgeResult,
                  now: datetime) -> None:
    """Put one generated proactive message (T4's NudgeResult) into the world,
    in the single order this task's integrity rests on: account first (stamp,
    then archive), send best-effort afterwards.

    1. Optimistic stamp. Re-read the store's CURRENT session pointer -- never
       the snapshot T4 took -- and stamp the session this reach-out actually
       ran in (result.session_id) forward only while that pointer is still
       where nudge_once left it (result.expected_session_id). If she pressed
       "clean start" or "warm restart" mid-generation the pointer has already
       moved, and stamping would silently resurrect a notebook she chose to
       zero; skip it, and log that the session changed hands.

    2. Archive -- whether or not the stamp was skipped, behind the same
       repo-wide cfg.archive_enabled gate every conversation-path write sits
       behind (archiving off means: speak, but do not record). He generated
       these words, so they are an unforgeable fact; the archive is the single
       record every downstream consumer (the warm recent-context prefix,
       memory sync, the diary's raw material) reads as "what he really said".
       The speaker is a clean "companion", never decorated with "(proactive)":
       to those consumers a proactive line simply IS something he said, which
       is the correct semantics. The framework instruction is never archived
       -- it is nobody's speech. The write is disk I/O, kept off the event
       loop with asyncio.to_thread.

    3. Send, best-effort. Chunk for Telegram's length limit and send each part
       as plain text (no parse_mode, the M1 convention). A failing chunk is
       retried exactly once; a second failure logs a warning (naming the job)
       and ABANDONS the remaining chunks, returning without raising -- a send
       failure is a delivery problem, never a pipeline failure, and by here the
       record is already safe. The worst a lost send can cost is a line she
       never received (true); it can never make him insist he sent a line that
       never existed (false), because the archive was written first.

    Deliberately absent: this never calls record_nudge (T4 counted the attempt
    at conception, not at receipt) and never runs memory sync -- her next
    reply's sync sweeps the archive forward from its incremental cursor and
    ingests this companion entry for free, with no new mechanism and one fewer
    embed. It touches neither the conversation-log nor any live-reply function.
    """
    current = store.load().get("session_id")
    if current == result.expected_session_id:
        store.stamp_session_started(result.session_id, now)
    else:
        logger.info(
            "scheduler: session changed hands mid-generation; not stamping "
            "the proactive message (%s)", result.job)

    # File IO off the event loop (the M4 T7b convention). Runs even when the
    # stamp above was skipped -- the archive records what he said, and he said
    # it regardless of which notebook it lands in -- but honors the repo-wide
    # archive gate exactly as every conversation-path write does.
    if cfg.archive_enabled:
        await asyncio.to_thread(archive.write_entry, cfg.archive_dir,
                                "companion", result.text, now)

    for chunk in chunking.split_message(result.text):
        for attempt in range(2):  # the original send, then one retry
            try:
                await app.bot.send_message(
                    chat_id=cfg.authorized_user_id, text=chunk)
                break
            except Exception:
                if attempt == 1:
                    logger.warning(
                        "scheduler: could not deliver a proactive message "
                        "chunk (%s); abandoning the rest", result.job,
                        exc_info=True)
                    return

    logger.info("scheduler: sent (%s)", result.job)


# ---------------------------------------------------------------------
# The background inner-life tick: arm it (start_tick), then run it forever
# (tick_loop). This is the app's whole inner-life clock. M7 T6 moved it here
# out of bot.py -- its permanent home, next to the pipeline it drives -- and
# added the proactive third segment. It takes the PTB Application as a plain
# argument (never importing bot.py) and the session store as an explicit
# parameter (never a bot_data side channel).
# ---------------------------------------------------------------------

def start_tick(app, cfg: Config, store: SessionStore) -> None:
    """Arm the background inner-life tick, if this run wants any of its three
    organs: diary_enabled, portrait_enabled, and (M7 T6) scheduler_enabled all
    off means no task at all (the L1 rollback -- nothing ticking, nothing to
    cancel), and a file-mode persona has no settings any organ could voice a
    page, a portrait, or a reach-out with, said once here at boot (INFO: it is
    the L1 persona-rollback state, worth one line) rather than rediscovered
    every few minutes by each organ's own defensive gate.

    Once armed, each organ logs its own INFO line, gated on its own flag alone
    (not on whether the tick as a whole started): "diary: inner-life tick
    started" and "portrait: armed (interval {n}d)" are byte-identical on
    purpose -- the deployment SOP greps for them -- and each prints only when
    its own flag is on, even if a different organ is what actually armed the
    task; "scheduler: proactive armed (greeting=... miss_you=... share=...)"
    reports each per-job flag's real bool, so a single-organ run never logs a
    line that isn't true of it. `store` reaches the loop as an explicit
    argument, never fished back out of bot_data: an argument that cannot be
    supplied fails loudly at once (TypeError), where a silent side-channel miss
    would arm a tick wired to nothing that no one would notice.

    asyncio.create_task, not Application.create_task, on two measured PTB 22.6
    facts (read from the installed package): the startup hook is awaited via
    run_until_complete BEFORE Application.start() flips `running`, so
    Application.create_task here would warn ("won't be automatically awaited")
    and skip its own tracking anyway; and stop() awaits every task that
    tracking DOES hold (asyncio.gather, no cancellation), which for an infinite
    loop would hang shutdown forever. The loop the startup hook runs on is
    already the application's own, so plain asyncio.create_task is both
    sufficient and honest. The Task reference is parked in bot_data because
    asyncio holds only a weak reference to running tasks -- an unparked tick
    could be garbage-collected mid-flight.
    """
    if not (cfg.diary_enabled or cfg.portrait_enabled or cfg.scheduler_enabled):
        logger.debug("inner-life: tick not started (all-disabled)")
        return
    if persona.current_settings(cfg) is None:
        logger.info("inner-life: disabled (file-mode persona)")
        return
    app.bot_data["_inner_tick_task"] = asyncio.create_task(
        tick_loop(cfg, store, app))
    if cfg.diary_enabled:
        logger.info("diary: inner-life tick started")
    if cfg.portrait_enabled:
        logger.info("portrait: armed (interval %sd)", cfg.portrait_interval_days)
    if cfg.scheduler_enabled:
        logger.info("scheduler: proactive armed (greeting=%s miss_you=%s share=%s)",
                    cfg.greeting_enabled, cfg.miss_you_enabled, cfg.share_enabled)


async def tick_loop(cfg: Config, store: SessionStore, app) -> None:
    """The inner-life heartbeat: sleep first (the moment of boot is never
    writing time), take one shared timestamp for the round, then run three
    segments in a fixed order -- diary, self-portrait, proactive reach-out --
    each on its own worker thread where it does disk/engine work
    (asyncio.to_thread), forever.

    The diary and portrait segments NEVER send a Telegram message: whatever
    either produces stays on disk. The proactive segment (M7 T6) is the one
    that DOES send -- but only through deliver, and only when scheduler_enabled
    is on: with that flag off the segment is not entered at all, so nudge_once
    is never even called (the most geometric form of the L1 rollback -- zero
    engine work, zero send, no state file). When on, nudge_once conceives one
    reach-out (its own dice roll cast fresh here, via random.random(), on the
    same seam its tests patch) and, when it returns a message, deliver puts it
    into the world.

    The three-segment order is deliberate: the diary's night window gets first
    crack at the shared engine lock, and the proactive reach-out naturally
    yields to it (nudge_once uses try_run_once, so a busy engine is a skip, not
    a wait). Unkillable by design, per segment: each sits in its own
    try/except (write_once, update_once, nudge_once, and deliver all propagate
    unexpected exceptions on purpose, deferring the catch to this loop), so a
    raising diary round never skips that round's portrait or proactive attempt,
    a raising portrait round never skips the proactive one, and a proactive
    round that blows up (even inside deliver's send tail) never skips next
    round's diary -- one organ's bug is never another's outage. Every except
    logs by name, and only CancelledError -- the one exit anyone ever means,
    including one raised while asyncio.sleep is still waiting (sleep sits
    outside all three try/excepts, so its own cancellation is never caught
    here) -- passes through uncaught. Known and accepted: on a conversation-free
    day, every in-window round logs "diary: skip (material_empty)" at INFO;
    that visibility is deliberate (the skip line is the observable proof the
    tick is alive), not duplication to suppress. When more than one segment
    wants the engine in the same round, engine.try_run_once's natural
    serialization (busy returns None) is enough on its own: whoever gets there
    first holds it, the others yield and try again next round -- no extra
    coordination lives here.
    """
    while True:
        await asyncio.sleep(TICK_INTERVAL_S)
        now = datetime.now().astimezone()
        try:
            await asyncio.to_thread(diary.write_once, cfg, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("diary: tick iteration failed", exc_info=True)
        try:
            await asyncio.to_thread(portrait.update_once, cfg, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("portrait: tick iteration failed", exc_info=True)
        if cfg.scheduler_enabled:
            try:
                result = await asyncio.to_thread(
                    nudge_once, cfg, store, now, random.random())
                if result is not None:
                    await deliver(app, cfg, store, result, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("scheduler: proactive tick iteration failed",
                               exc_info=True)
