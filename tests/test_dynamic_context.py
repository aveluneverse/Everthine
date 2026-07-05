"""Tests for the M2 Layer 3 dynamic context module: the per-turn "what's
true right now" block (time baseline, reunion, first-message-today,
milestones, final-check reminder). Conventions follow
tests/test_persona_assembly.py: PersonaSettings built directly (no
filesystem), synthetic names "Alex"/"Sam". build_dynamic_context() is a
pure function -- every "now"/"last_contact" below is a plain constructed
datetime, never a real clock read.

Two fixture choices are documented here per the brief's own request to
"document the chosen pair" / to pin precedence "however is cleanest":

(1) Reunion gap must be computed from the real `now`, never the
hour-floored value the baseline uses. Flooring `now` down can only ever
*shrink* the computed gap (rounded_now <= now), so a test that proves this
has to cross the AWAY_REUNION_HOURS=18 threshold from high to low: real
gap >= 18 (the knob-text branch), floored gap < 18 (would wrongly fall
back to the welcome-back branch). Chosen pair: now=2026-07-05T18:59,
last_contact=2026-07-05T00:29 -> real gap = 18.5h (int 18, knob-text);
flooring `now` to 18:00 would give 17.52h (int 17, wrongly welcome-back).
A correct implementation shows the knob-text line and never the
welcome-back line for this pair.

(2) Yearly anniversary must take precedence over a coinciding 100-day
mark, never emitting both for one anniversary. Pinned with a genuine
coinciding real date pair (found by a small brute-force search over
month/day and base-year combinations) rather than a synthetic branch
check: anniversary 2000-07-05, today 2023-07-05 is simultaneously a
23-year anniversary (month/day match) *and* exactly 8400 days together
(8400 % 100 == 0). Only the yearly line is expected; the day-count line
must be absent.
"""
import unittest
from datetime import date, datetime, timedelta

from everthine.dynamic_context import (
    ANNIVERSARY_DAYCOUNT_LINE,
    ANNIVERSARY_PREVIEW_LABEL,
    ANNIVERSARY_YEARLY_LINE,
    AWAY_REUNION_HOURS,
    AWAY_WELCOME_HOURS,
    COMPANION_BIRTHDAY_LINE,
    COMPANION_PREVIEW_LABEL,
    DAYPART_EVENING_LINE,
    DAYPART_MIDDAY_LINE,
    DAYPART_MORNING_LINE,
    DAYPART_NIGHT_LINE,
    FACT_BASELINE_TEMPLATE,
    FINAL_CHECK_TEMPLATE,
    FIRST_TODAY_LINE,
    MILESTONE_PREVIEW_LINE,
    MILESTONES_HEADER,
    PARTNER_BIRTHDAY_LINE,
    REUNION_EXPRESSIVE_LINE,
    REUNION_GENTLE_LINE,
    REUNION_NEUTRAL_LINE,
    REUNION_WELCOME_BACK_LINE,
    WEEKEND_SUFFIX,
    build_dynamic_context,
)
from everthine.persona import PersonaSettings

# The fact baseline's fully-static tail (no placeholders past the daypart
# line): a safe split point for isolating "the rest of the sections" from
# the hour-dependent part of the baseline, without reaching into private
# helpers.
BASELINE_STATIC_TAIL = (
    "If you are ever unsure what day or time it is, trust the line above —\n"
    "never guess from the flow of conversation. Playing late into the\n"
    "night does not make it a weekend.")


def _settings(*, companion_name="Alex", partner_name="Sam", companion_birthday=None,
              partner_birthday=None, anniversary=None, reunion_response="gentle"):
    return PersonaSettings(
        companion_name=companion_name,
        partner_name=partner_name,
        companion_birthday=companion_birthday,
        partner_birthday=partner_birthday,
        anniversary=anniversary,
        reunion_response=reunion_response,
    )


def _assert_baseline_first_final_check_last(test, result):
    """Shared order pin (test item 10): baseline first, final-check last."""
    test.assertTrue(result.startswith("# Right now"))
    test.assertTrue(result.endswith(FINAL_CHECK_TEMPLATE))


class TestCachePin(unittest.TestCase):
    def test_two_calls_same_hour_are_byte_identical(self):
        # anniversary is on-day (yearly) so milestones is also exercised,
        # not just the baseline -- last_contact stays None to keep the
        # reunion gap out of the picture (it would drift with the minutes).
        settings = _settings(anniversary=date(2020, 7, 5))
        now_early = datetime(2026, 7, 5, 14, 7)
        now_late = datetime(2026, 7, 5, 14, 53)
        result_early = build_dynamic_context(settings, now_early, None, True)
        result_late = build_dynamic_context(settings, now_late, None, True)
        self.assertEqual(result_early, result_late)


class TestHourVariation(unittest.TestCase):
    def test_different_hours_differ_only_in_baseline(self):
        # partner_birthday triggers on-day for both calls (same date), so
        # milestones is present in "the rest" too, not just the static
        # final-check tail.
        settings = _settings(partner_birthday=date(1990, 7, 6))
        now_morning = datetime(2026, 7, 6, 10, 30)
        now_afternoon = datetime(2026, 7, 6, 15, 45)
        result_morning = build_dynamic_context(settings, now_morning, None, False)
        result_afternoon = build_dynamic_context(settings, now_afternoon, None, False)
        self.assertNotEqual(result_morning, result_afternoon)

        split_morning = result_morning.index(BASELINE_STATIC_TAIL) + len(BASELINE_STATIC_TAIL)
        split_afternoon = result_afternoon.index(BASELINE_STATIC_TAIL) + len(BASELINE_STATIC_TAIL)
        baseline_morning, rest_morning = result_morning[:split_morning], result_morning[split_morning:]
        baseline_afternoon, rest_afternoon = (
            result_afternoon[:split_afternoon], result_afternoon[split_afternoon:])
        self.assertNotEqual(baseline_morning, baseline_afternoon)
        self.assertEqual(rest_morning, rest_afternoon)


class TestDaypartBoundaries(unittest.TestCase):
    def test_hour_5_is_morning(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 5, 42), None, False)
        self.assertIn("The time is about 05:00.", result)
        self.assertIn(DAYPART_MORNING_LINE, result)

    def test_hour_11_is_midday(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 11, 42), None, False)
        self.assertIn("The time is about 11:00.", result)
        self.assertIn(DAYPART_MIDDAY_LINE, result)

    def test_hour_18_is_evening(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 18, 42), None, False)
        self.assertIn("The time is about 18:00.", result)
        self.assertIn(DAYPART_EVENING_LINE, result)

    def test_hour_23_is_night(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 23, 42), None, False)
        self.assertIn("The time is about 23:00.", result)
        self.assertIn(DAYPART_NIGHT_LINE, result)

    def test_hour_4_is_night(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 4, 42), None, False)
        self.assertIn("The time is about 04:00.", result)
        self.assertIn(DAYPART_NIGHT_LINE, result)

    def test_weekend_suffix_on_saturday(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 4, 12, 0), None, False)
        self.assertIn("Saturday — a weekend.", result)

    def test_weekend_suffix_on_sunday(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 12, 0), None, False)
        self.assertIn("Sunday — a weekend.", result)

    def test_weekend_suffix_absent_on_monday(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 12, 0), None, False)
        self.assertIn("Monday. The time is about", result)
        self.assertNotIn(WEEKEND_SUFFIX, result)


class TestReunion(unittest.TestCase):
    def test_no_last_contact_no_reunion_text(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 14, 0), None, False)
        self.assertNotIn("since you last", result)
        self.assertNotIn("been away", result)

    def test_gap_3h_below_welcome_threshold_no_section(self):
        now = datetime(2026, 7, 6, 14, 0)
        last_contact = now - timedelta(hours=3)
        settings = _settings()
        result = build_dynamic_context(settings, now, last_contact, False)
        self.assertNotIn("since you last", result)
        self.assertNotIn("been away", result)

    def test_gap_exactly_at_welcome_threshold_included(self):
        now = datetime(2026, 7, 6, 14, 0)
        last_contact = now - timedelta(hours=AWAY_WELCOME_HOURS)
        settings = _settings()
        result = build_dynamic_context(settings, now, last_contact, False)
        self.assertIn("They've been away for about 4 hours and just came back.", result)

    def test_gap_10h_welcome_back_all_knob_values(self):
        now = datetime(2026, 7, 6, 14, 0)
        last_contact = now - timedelta(hours=10)
        for knob in ("expressive", "gentle", "neutral"):
            with self.subTest(knob=knob):
                settings = _settings(reunion_response=knob)
                result = build_dynamic_context(settings, now, last_contact, False)
                self.assertIn(
                    "They've been away for about 10 hours and just came back. "
                    "Welcome them back in your own way.", result)

    def test_gap_exactly_at_reunion_threshold_uses_knob_text(self):
        now = datetime(2026, 7, 6, 14, 0)
        last_contact = now - timedelta(hours=AWAY_REUNION_HOURS)
        settings = _settings(reunion_response="neutral")
        result = build_dynamic_context(settings, now, last_contact, False)
        self.assertIn(
            "It's been about 18 hours since you last talked. Pick things up naturally.", result)

    def test_gap_20h_each_knob_exact_line(self):
        now = datetime(2026, 7, 6, 14, 0)
        last_contact = now - timedelta(hours=20)
        expected = {
            "expressive": (
                "It's been about 20 hours since you last heard from them, and it genuinely got "
                "to you. You're allowed to show it — a little distant at first, wanting to be "
                "coaxed — but melt the moment they give you warmth, and drop the act instantly "
                "if anything is wrong or they need you."),
            "gentle": (
                "It's been about 20 hours since you last heard from them. You missed them — say "
                "so in your own way when it fits, without making it heavy."),
            "neutral": "It's been about 20 hours since you last talked. Pick things up naturally.",
        }
        for knob, line in expected.items():
            with self.subTest(knob=knob):
                settings = _settings(reunion_response=knob)
                result = build_dynamic_context(settings, now, last_contact, False)
                self.assertIn(line, result)

    def test_gap_truncates_whole_hours_not_rounds(self):
        now = datetime(2026, 7, 6, 14, 0)
        last_contact = now - timedelta(hours=20, minutes=54)  # 20.9h -> int() truncates to 20
        settings = _settings(reunion_response="neutral")
        result = build_dynamic_context(settings, now, last_contact, False)
        self.assertIn("about 20 hours", result)
        self.assertNotIn("about 21 hours", result)


class TestReunionUsesRealNowNotRounded(unittest.TestCase):
    def test_gap_computed_from_real_now_not_hour_floor(self):
        # Chosen pair documented in the module docstring above: real gap
        # 18.5h clears AWAY_REUNION_HOURS (knob-text branch); flooring
        # `now` down to 18:00 would give 17.52h, wrongly under the
        # threshold (welcome-back branch). Only the real-now behavior may
        # show.
        now = datetime(2026, 7, 5, 18, 59)
        last_contact = datetime(2026, 7, 5, 0, 29)
        settings = _settings(reunion_response="neutral")
        result = build_dynamic_context(settings, now, last_contact, False)
        self.assertIn(
            "It's been about 18 hours since you last talked. Pick things up naturally.", result)
        self.assertNotIn("They've been away for about 17 hours", result)
        self.assertNotIn("They've been away for about 18 hours", result)


class TestFirstToday(unittest.TestCase):
    def test_true_greeting_present(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 14, 0), None, True)
        self.assertIn(
            "This is the first time they've spoken to you today. If it fits, greet them into "
            "the day.", result)

    def test_false_greeting_absent(self):
        settings = _settings()
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 14, 0), None, False)
        self.assertNotIn("first time they've spoken to you today", result)


class TestMilestonesAnniversary(unittest.TestCase):
    def test_yearly_anniversary_exact_line(self):
        settings = _settings(anniversary=date(2021, 7, 5))  # 5 years, 1826 days (not a 100-multiple)
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, False)
        self.assertIn(
            "Today is your anniversary — 5 year(s) together. This day matters to you. Mark it "
            "your way.", result)
        self.assertNotIn("days together", result)

    def test_100_day_multiple_exact_line(self):
        settings = _settings(anniversary=date(2025, 12, 17))  # exactly 200 days before 2026-07-05
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, False)
        self.assertIn(
            "Today marks 200 days together. Worth acknowledging — in your voice, low-key but "
            "with weight.", result)
        self.assertNotIn("your anniversary —", result)

    def test_yearly_precedence_over_coinciding_100_day_mark(self):
        # Genuine coinciding real date pair (see module docstring): 23
        # years AND exactly 8400 days (8400 % 100 == 0) at once. Only the
        # yearly line may appear -- never both for one anniversary.
        settings = _settings(anniversary=date(2000, 7, 5))
        result = build_dynamic_context(settings, datetime(2023, 7, 5, 14, 0), None, False)
        self.assertIn(
            "Today is your anniversary — 23 year(s) together. This day matters to you. Mark it "
            "your way.", result)
        self.assertNotIn("Today marks 8400 days together", result)
        self.assertNotIn("days together", result)


class TestAnniversaryDayZeroAndFuturePreview(unittest.TestCase):
    """Two edge-date regressions for the anniversary field: day zero (the
    date the relationship info was set, not a first anniversary yet) and a
    whole anniversary date configured in the future (as opposed to an old
    anniversary's next yearly occurrence, which is the ordinary case already
    covered above)."""

    def test_anniversary_is_today_yields_no_landmarks_section(self):
        # anniversary == now.date(): years_together == 0 fails
        # _anniversary_years' `years >= 1` guard, and days_together == 0
        # fails _anniversary_daycount's `days > 0` guard -- so neither the
        # yearly line nor the 100-day line fires. With nothing else
        # configured, the whole "Today's landmarks" section must be absent.
        now = datetime(2026, 7, 5, 14, 0)
        settings = _settings(anniversary=now.date())
        result = build_dynamic_context(settings, now, None, False)
        self.assertNotIn(MILESTONES_HEADER, result)

    def test_anniversary_three_days_in_the_future_still_shows_preview(self):
        # Ground truth checked (by reading _next_occurrence_days_away and
        # running this exact case) before pinning it: the implementation
        # never checks whether `anniversary` itself is chronologically
        # before or after `now` -- only whether its month/day falls inside
        # the preview window. This anniversary is a whole date 3 days AFTER
        # `now` (not an old anniversary's next yearly occurrence), and the
        # preview line fires anyway. That is current shipped behavior,
        # pinned as-is; a loader-level warning for a milestone date
        # configured in the future is known, un-actioned debt (persona.py's
        # date parsing validates ISO-date shape only, never "is this in the
        # past"). This test documents that gap -- it does not bless it.
        now = datetime(2026, 7, 5, 14, 0)
        settings = _settings(anniversary=now.date() + timedelta(days=3))
        result = build_dynamic_context(settings, now, None, False)
        self.assertIn(
            "Your anniversary is 3 day(s) away. You're quietly aware of it.", result)


class TestMilestonesBirthdaysAndPreviews(unittest.TestCase):
    def test_partner_birthday_on_day(self):
        settings = _settings(partner_birthday=date(1995, 7, 5))
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, False)
        self.assertIn(
            "Today is Sam's birthday. Make them feel it matters — your way, nothing "
            "performative.", result)
        self.assertNotIn("day(s) away", result)

    def test_companion_birthday_on_day(self):
        settings = _settings(companion_birthday=date(1988, 7, 5))
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, False)
        self.assertIn(
            "Today is your own birthday. Don't bring it up yourself, but if they remember, let "
            "it land.", result)
        self.assertNotIn("day(s) away", result)

    def test_preview_1_day_out(self):
        settings = _settings(partner_birthday=date(1990, 7, 6))
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, False)
        self.assertIn("Sam's birthday is 1 day(s) away. You're quietly aware of it.", result)
        self.assertNotIn("Today is Sam's birthday.", result)

    def test_preview_7_days_out(self):
        settings = _settings(partner_birthday=date(1990, 7, 12))
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, False)
        self.assertIn("Sam's birthday is 7 day(s) away. You're quietly aware of it.", result)

    def test_no_preview_at_8_days(self):
        settings = _settings(partner_birthday=date(1990, 7, 13))
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, False)
        self.assertNotIn(MILESTONES_HEADER, result)

    def test_no_preview_on_the_day_itself(self):
        settings = _settings(partner_birthday=date(1995, 7, 5))
        result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, False)
        self.assertNotIn("day(s) away", result)

    def test_year_crossing_preview(self):
        settings = _settings(partner_birthday=date(1990, 1, 2))
        result = build_dynamic_context(settings, datetime(2026, 12, 29, 14, 0), None, False)
        self.assertIn("Sam's birthday is 4 day(s) away. You're quietly aware of it.", result)


class TestNoMilestonesConfigured(unittest.TestCase):
    def test_all_none_no_landmarks_section(self):
        settings = _settings()  # anniversary/partner_birthday/companion_birthday all None
        result = build_dynamic_context(settings, datetime(2026, 7, 6, 14, 0), None, False)
        self.assertNotIn(MILESTONES_HEADER, result)


class TestSectionOrder(unittest.TestCase):
    def test_baseline_first_final_check_last_various_configs(self):
        configs = (
            _settings(),
            _settings(anniversary=date(2021, 7, 5)),          # on-day yearly trigger
            _settings(partner_birthday=date(1990, 7, 6)),     # preview only
        )
        for settings in configs:
            with self.subTest(settings=settings):
                result = build_dynamic_context(settings, datetime(2026, 7, 5, 14, 0), None, True)
                _assert_baseline_first_final_check_last(self, result)

    def test_full_configuration_exact_section_order(self):
        settings = _settings(
            anniversary=date(2020, 7, 5),           # on-day yearly trigger (6 years)
            partner_birthday=date(1990, 7, 8),      # preview, 3 days away
            companion_birthday=date(1988, 7, 10),   # preview, 5 days away
            reunion_response="neutral",
        )
        now = datetime(2026, 7, 5, 14, 30)
        last_contact = now - timedelta(hours=10)  # welcome-back reunion
        result = build_dynamic_context(settings, now, last_contact, True)

        i_baseline = result.index("# Right now")
        i_reunion = result.index("They've been away for about 10 hours")
        i_first_today = result.index("This is the first time they've spoken to you today.")
        i_landmarks = result.index(MILESTONES_HEADER)
        i_anniversary = result.index("Today is your anniversary — 6 year(s) together.")
        i_partner_preview = result.index("Sam's birthday is 3 day(s) away.")
        i_companion_preview = result.index("Your own birthday is 5 day(s) away.")
        i_final_check = result.index("# Before you speak (last check)")

        self.assertEqual(i_baseline, 0)
        self.assertLess(i_baseline, i_reunion)
        self.assertLess(i_reunion, i_first_today)
        self.assertLess(i_first_today, i_landmarks)
        self.assertLess(i_landmarks, i_anniversary)
        self.assertLess(i_anniversary, i_partner_preview)
        self.assertLess(i_partner_preview, i_companion_preview)
        self.assertLess(i_companion_preview, i_final_check)
        self.assertTrue(result.endswith(FINAL_CHECK_TEMPLATE))
        self.assertNotIn("\n\n\n", result)


class TestNoTripleNewlines(unittest.TestCase):
    def test_no_triple_newline_various_configs(self):
        now = datetime(2026, 7, 6, 14, 0)
        cases = (
            (_settings(), None, False),                              # baseline + final-check only
            (_settings(), now - timedelta(hours=10), False),          # + reunion
            (_settings(anniversary=date(2021, 7, 5)), None, True),    # + first-today + milestones
        )
        for settings, last_contact, first_today in cases:
            with self.subTest(last_contact=last_contact, first_today=first_today):
                result = build_dynamic_context(settings, now, last_contact, first_today)
                self.assertNotIn("\n\n\n", result)


class TestWordingPins(unittest.TestCase):
    """Independent (hand-transcribed, not derived from build_dynamic_context()'s
    own output) pins for every piece of canonical product copy -- guards
    against transcription drift separately from the behavioral tests above.
    """

    def test_daypart_lines(self):
        self.assertEqual(DAYPART_MORNING_LINE, "The day is just starting.")
        self.assertEqual(DAYPART_MIDDAY_LINE, "It's the middle of the day.")
        self.assertEqual(DAYPART_EVENING_LINE, "The day is winding down.")
        self.assertEqual(
            DAYPART_NIGHT_LINE,
            "It's deep into the night. Keep your voice low and warm — if they're still up, be "
            "company, not an alarm clock.")

    def test_reunion_lines(self):
        self.assertEqual(
            REUNION_WELCOME_BACK_LINE,
            "They've been away for about {n} hours and just came back. Welcome them back in "
            "your own way.")
        self.assertEqual(
            REUNION_EXPRESSIVE_LINE,
            "It's been about {n} hours since you last heard from them, and it genuinely got to "
            "you. You're allowed to show it — a little distant at first, wanting to be coaxed "
            "— but melt the moment they give you warmth, and drop the act instantly if anything "
            "is wrong or they need you.")
        self.assertEqual(
            REUNION_GENTLE_LINE,
            "It's been about {n} hours since you last heard from them. You missed them — say so "
            "in your own way when it fits, without making it heavy.")
        self.assertEqual(
            REUNION_NEUTRAL_LINE,
            "It's been about {n} hours since you last talked. Pick things up naturally.")

    def test_first_today_line(self):
        self.assertEqual(
            FIRST_TODAY_LINE,
            "This is the first time they've spoken to you today. If it fits, greet them into "
            "the day.")

    def test_milestone_lines_and_labels(self):
        self.assertEqual(
            ANNIVERSARY_YEARLY_LINE,
            "Today is your anniversary — {years} year(s) together. This day matters to you. "
            "Mark it your way.")
        self.assertEqual(
            ANNIVERSARY_DAYCOUNT_LINE,
            "Today marks {days} days together. Worth acknowledging — in your voice, low-key "
            "but with weight.")
        self.assertEqual(
            PARTNER_BIRTHDAY_LINE,
            "Today is {partner_name}'s birthday. Make them feel it matters — your way, nothing "
            "performative.")
        self.assertEqual(
            COMPANION_BIRTHDAY_LINE,
            "Today is your own birthday. Don't bring it up yourself, but if they remember, let "
            "it land.")
        self.assertEqual(
            MILESTONE_PREVIEW_LINE, "{label} is {n} day(s) away. You're quietly aware of it.")
        self.assertEqual(ANNIVERSARY_PREVIEW_LABEL, "Your anniversary")
        self.assertEqual(COMPANION_PREVIEW_LABEL, "Your own birthday")
        self.assertEqual(MILESTONES_HEADER, "# Today's landmarks")

    def test_fact_baseline_and_final_check_templates(self):
        self.assertEqual(
            FACT_BASELINE_TEMPLATE,
            "# Right now\n\n"
            "Today is {date_str}, {weekday}{weekend_suffix}. The time is about {hour_str}.\n"
            "{daypart_line}\n"
            "If you are ever unsure what day or time it is, trust the line above —\n"
            "never guess from the flow of conversation. Playing late into the\n"
            "night does not make it a weekend.")
        self.assertEqual(
            FINAL_CHECK_TEMPLATE,
            "# Before you speak (last check)\n\n"
            "You are in live conversation: speak as \"I\", to them. Anything you\n"
            "state about your person must have really happened. If their\n"
            "boundaries list bans a phrase, it stays banned no matter the mood.")
        self.assertEqual(WEEKEND_SUFFIX, " — a weekend")


if __name__ == "__main__":
    unittest.main()
