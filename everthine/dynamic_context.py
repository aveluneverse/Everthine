"""Layer 3 dynamic context: "what's true right now."

persona.py loads validated settings; layers.py composes the static Layer 1
+ Layer 2 text. This module adds the per-turn dynamic block on top: the
current date and time (rounded down to the hour, so the block stays
byte-stable for the rest of that hour -- keeps the engine-side prompt
cache warm, a trick proven in the source architecture), a reunion beat
when the person has been away, a first-message-of-the-day greeting cue,
and awareness of user-defined milestone dates (anniversary, birthdays).

Everything here is a pure function of its arguments: no filesystem, no
datetime.now() inside -- the caller passes `now`. A later task derives
`last_contact`/`first_today` from the conversation archive and wires
build_dynamic_context()'s output into persona.build_system_prompt(); that
wiring is out of scope here. The optional `memory_block` parameter is this
module's other seam: a caller-supplied section slotted in verbatim between
the milestones section and the final check, with no effect at all when it
is None or empty -- retrieving and formatting that block is a later task's
job, not this one's. This module is layers.py's sibling, not its caller:
same constants-then-functions layout, same docstring conventions.
"""
from __future__ import annotations

from datetime import date, datetime

from .persona import PersonaSettings

# --- Reunion thresholds (hours since last_contact) -----------------------

AWAY_WELCOME_HOURS = 4
AWAY_REUNION_HOURS = 18

# --- Daypart boundaries (rounded local hour, 0-23) ------------------------

DAYPART_MORNING_START = 5
DAYPART_MIDDAY_START = 11
DAYPART_EVENING_START = 18
DAYPART_NIGHT_START = 23

# --- Milestone windows -----------------------------------------------------

ANNIVERSARY_DAYCOUNT_INTERVAL = 100
MILESTONE_PREVIEW_WINDOW_DAYS = 7

# --- Section 1: fact baseline (always present) ----------------------------

_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKEND_SUFFIX = " — a weekend"

DAYPART_MORNING_LINE = "The day is just starting."
DAYPART_MIDDAY_LINE = "It's the middle of the day."
DAYPART_EVENING_LINE = "The day is winding down."
DAYPART_NIGHT_LINE = "It's deep into the night. Keep your voice low and warm — if they're still up, be company, not an alarm clock."

FACT_BASELINE_TEMPLATE = """# Right now

Today is {date_str}, {weekday}{weekend_suffix}. The time is about {hour_str}.
{daypart_line}
If you are ever unsure what day or time it is, trust the line above —
never guess from the flow of conversation. Playing late into the
night does not make it a weekend."""

# --- Section 2: reunion (conditional on last_contact) ---------------------

REUNION_WELCOME_BACK_LINE = "They've been away for about {n} hours and just came back. Welcome them back in your own way."
REUNION_EXPRESSIVE_LINE = "It's been about {n} hours since you last heard from them, and it genuinely got to you. You're allowed to show it — a little distant at first, wanting to be coaxed — but melt the moment they give you warmth, and drop the act instantly if anything is wrong or they need you."
REUNION_GENTLE_LINE = "It's been about {n} hours since you last heard from them. You missed them — say so in your own way when it fits, without making it heavy."
REUNION_NEUTRAL_LINE = "It's been about {n} hours since you last talked. Pick things up naturally."

_REUNION_LINE_BY_RESPONSE = {
    "expressive": REUNION_EXPRESSIVE_LINE,
    "gentle": REUNION_GENTLE_LINE,
    "neutral": REUNION_NEUTRAL_LINE,
}

# --- Section 3: first-message-today (conditional on first_today) ---------

FIRST_TODAY_LINE = "This is the first time they've spoken to you today. If it fits, greet them into the day."

# --- Section 4: milestones (conditional on settings' dates) ---------------

MILESTONES_HEADER = "# Today's landmarks"
ANNIVERSARY_YEARLY_LINE = "Today is your anniversary — {years} year(s) together. This day matters to you. Mark it your way."
ANNIVERSARY_DAYCOUNT_LINE = "Today marks {days} days together. Worth acknowledging — in your voice, low-key but with weight."
PARTNER_BIRTHDAY_LINE = "Today is {partner_name}'s birthday. Make them feel it matters — your way, nothing performative."
COMPANION_BIRTHDAY_LINE = "Today is your own birthday. Don't bring it up yourself, but if they remember, let it land."
MILESTONE_PREVIEW_LINE = "{label} is {n} day(s) away. You're quietly aware of it."
ANNIVERSARY_PREVIEW_LABEL = "Your anniversary"
COMPANION_PREVIEW_LABEL = "Your own birthday"

# --- Section 5: final-check reminder (always present, always last) -------

FINAL_CHECK_TEMPLATE = """# Before you speak (last check)

You are in live conversation: speak as "I", to them. Anything you
state about your person must have really happened. If their
boundaries list bans a phrase, it stays banned no matter the mood."""


def _daypart_line(hour: int) -> str:
    if DAYPART_MORNING_START <= hour < DAYPART_MIDDAY_START:
        return DAYPART_MORNING_LINE
    if DAYPART_MIDDAY_START <= hour < DAYPART_EVENING_START:
        return DAYPART_MIDDAY_LINE
    if DAYPART_EVENING_START <= hour < DAYPART_NIGHT_START:
        return DAYPART_EVENING_LINE
    return DAYPART_NIGHT_LINE  # hour >= 23 or hour < 5


def _fact_baseline(rounded: datetime) -> str:
    """Section 1, built from `rounded` -- already floored to the top of the
    hour by build_dynamic_context() before this helper ever sees it, so the
    same rounded hour always yields the same string (this helper does not
    re-round). Weekday name comes from a fixed table indexed by
    datetime.weekday() rather than strftime("%A"), so the output never
    depends on the host machine's locale.
    """
    weekday_index = rounded.weekday()  # Monday=0 .. Sunday=6
    weekend_suffix = WEEKEND_SUFFIX if weekday_index >= 5 else ""
    return FACT_BASELINE_TEMPLATE.format(
        date_str=rounded.strftime("%Y-%m-%d"),
        weekday=_WEEKDAY_NAMES[weekday_index],
        weekend_suffix=weekend_suffix,
        hour_str=f"{rounded.hour:02d}:00",
        daypart_line=_daypart_line(rounded.hour),
    )


def _reunion_section(
        settings: PersonaSettings, now: datetime, last_contact: datetime | None) -> str | None:
    """Section 2. The gap is computed from the real `now`, never the
    hour-rounded value _fact_baseline() uses -- reunion timing must not
    blur into the baseline's cache-friendly rounding.
    """
    if last_contact is None:
        return None
    gap_hours = (now - last_contact).total_seconds() / 3600
    if gap_hours < AWAY_WELCOME_HOURS:
        return None
    n = int(gap_hours)
    if gap_hours < AWAY_REUNION_HOURS:
        return REUNION_WELCOME_BACK_LINE.format(n=n)
    return _REUNION_LINE_BY_RESPONSE[settings.reunion_response].format(n=n)


def _is_month_day_match(d: date, today: date) -> bool:
    return d.month == today.month and d.day == today.day


def _anniversary_years(anniversary: date, today: date) -> int | None:
    """Whole years since `anniversary`, only on the matching month/day.

    None both when the month/day does not match today, and when it does
    but the year count is not yet at least 1 (the anniversary date itself,
    the year it was set -- that is day zero, not a first anniversary yet).
    """
    if not _is_month_day_match(anniversary, today):
        return None
    years = today.year - anniversary.year
    return years if years >= 1 else None


def _anniversary_daycount(anniversary: date, today: date) -> int | None:
    days = (today - anniversary).days
    if days > 0 and days % ANNIVERSARY_DAYCOUNT_INTERVAL == 0:
        return days
    return None


def _next_occurrence_days_away(month: int, day: int, today: date) -> int | None:
    """Days from `today` to the next date carrying this month/day (0 = today).

    Checked against this year, then next year. A month/day that exists in
    neither (Feb 29 against two consecutive non-leap years) yields None --
    the simple "month/day equality" rule the brief calls for: a Feb-29
    milestone just does not trigger or preview that cycle, with no further
    special-casing (no shifting to Feb 28 or Mar 1).
    """
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return (candidate - today).days
    return None


def _milestone_lines(settings: PersonaSettings, today: date) -> list[str]:
    """One line per triggered item, in the fixed order the brief specifies:
    anniversary (yearly takes precedence over a coinciding 100-day mark;
    never both), partner birthday, companion birthday, then upcoming
    previews (anniversary yearly date, partner birthday, companion
    birthday, in that same order). A field triggering today is
    automatically excluded from also previewing: _next_occurrence_days_away
    returns 0 for today, which never falls inside the 1..7 preview window.
    """
    lines: list[str] = []
    anniversary = settings.anniversary

    if anniversary is not None:
        years = _anniversary_years(anniversary, today)
        if years is not None:
            lines.append(ANNIVERSARY_YEARLY_LINE.format(years=years))
        else:
            days = _anniversary_daycount(anniversary, today)
            if days is not None:
                lines.append(ANNIVERSARY_DAYCOUNT_LINE.format(days=days))

    if settings.partner_birthday is not None and _is_month_day_match(
            settings.partner_birthday, today):
        lines.append(PARTNER_BIRTHDAY_LINE.format(partner_name=settings.partner_name))

    if settings.companion_birthday is not None and _is_month_day_match(
            settings.companion_birthday, today):
        lines.append(COMPANION_BIRTHDAY_LINE)

    preview_candidates = (
        (anniversary, ANNIVERSARY_PREVIEW_LABEL),
        (settings.partner_birthday, f"{settings.partner_name}'s birthday"),
        (settings.companion_birthday, COMPANION_PREVIEW_LABEL),
    )
    for milestone_date, label in preview_candidates:
        if milestone_date is None:
            continue
        n = _next_occurrence_days_away(milestone_date.month, milestone_date.day, today)
        if n is not None and 1 <= n <= MILESTONE_PREVIEW_WINDOW_DAYS:
            lines.append(MILESTONE_PREVIEW_LINE.format(label=label, n=n))

    return lines


def _milestones_section(settings: PersonaSettings, today: date) -> str | None:
    lines = _milestone_lines(settings, today)
    if not lines:
        return None
    return MILESTONES_HEADER + "\n\n" + "\n".join(lines)


def build_dynamic_context(
    settings: PersonaSettings,
    now: datetime,
    last_contact: datetime | None,
    first_today: bool,
    memory_block: str | None = None,
) -> str:
    """Compose Layer 3: the full "what's true right now" block.

    Pure function -- no filesystem, no clock read; `now` and `last_contact`
    are naive local datetimes the caller supplies (consistent with each
    other; this module does no timezone handling). Sections are joined
    with a single blank line, in this fixed order, and a section that does
    not apply is omitted entirely (never a stray blank section):

    1. Fact baseline (always).
    2. Reunion (last_contact is not None and the gap clears
       AWAY_WELCOME_HOURS).
    3. First-message-today greeting line (first_today is True).
    4. Milestones (any of settings.anniversary/partner_birthday/
       companion_birthday triggers today or previews within a week).
    5. Retrieved-memory block (memory_block is truthy) -- an optional,
       caller-supplied section; this module does not produce or format it.
    6. Final-check reminder (always, always last).

    The hour is rounded down to the top of the hour for the baseline only
    -- section 1's displayed clock and daypart line are byte-stable for the
    rest of that hour (keeps the engine-side prompt cache warm), while the
    reunion gap in section 2 and the milestone dates in section 4 are both
    computed from the real, unrounded `now`.
    """
    rounded = now.replace(minute=0, second=0, microsecond=0)
    sections = [_fact_baseline(rounded)]

    reunion = _reunion_section(settings, now, last_contact)
    if reunion is not None:
        sections.append(reunion)

    if first_today:
        sections.append(FIRST_TODAY_LINE)

    milestones = _milestones_section(settings, now.date())
    if milestones is not None:
        sections.append(milestones)

    # Retrieved-memory block (long-term memory) slots in here - after
    # milestones, BEFORE the final check. The final check must stay the
    # last words the engine reads; never append anything after it.
    if memory_block:
        sections.append(memory_block)

    sections.append(FINAL_CHECK_TEMPLATE)
    return "\n\n".join(sections)
