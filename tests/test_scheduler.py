"""scheduler.py: state-file round-trips and fail-soft parsing, the common
gate's pure decision table (including priority order and the quiet-hours
window's three cross-midnight shapes), the three per-job due-checks
(including the miss-you anchor's dedup cycle across a simulated restart),
pick_job's composition and mutual-exclusivity, truth_timeline's
single-source archive scan, and record_nudge's accounting. Conventions
follow tests/test_diary.py: the _cfg() Config-building helper, corpse-file
assertions, and tz-aware datetime fixtures.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from everthine import archive, scheduler
from everthine.config import load_config

BASE_ENV = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}

TODAY = "2026-07-06"
YESTERDAY = "2026-07-05"

FRESH_STATE = {
    "greeting_date": "",
    "miss_you_anchor": "",
    "share_date": "",
    "share_count": 0,
    "budget_date": "",
    "budget_used": 0,
    "last_nudge_at": "",
}


def _cfg(td, **overrides):
    env = {**BASE_ENV, "DATA_DIR": str(td), **overrides}
    return load_config(env)


def _aware(hour, minute=0, day=6):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------
# State file: load_state / _atomic_write / _quarantine_corpse
# ---------------------------------------------------------------------

class TestLoadState(unittest.TestCase):
    def test_missing_file_is_fresh_state(self):
        with tempfile.TemporaryDirectory() as td:
            state = scheduler.load_state(Path(td) / "scheduler_state.json")
        self.assertEqual(state, FRESH_STATE)

    def test_corrupt_json_degrades_and_keeps_corpse(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scheduler_state.json"
            p.write_text("{not json", encoding="utf-8")
            state = scheduler.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            corpses = list(Path(td).glob("scheduler_state.json.corrupt-*"))
            self.assertEqual(len(corpses), 1)

    def test_wrong_shape_list_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scheduler_state.json"
            p.write_text('["not", "a", "dict"]', encoding="utf-8")
            state = scheduler.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("scheduler_state.json.corrupt-*"))), 1)

    def test_missing_key_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scheduler_state.json"
            incomplete = {k: v for k, v in FRESH_STATE.items() if k != "budget_used"}
            p.write_text(json.dumps(incomplete), encoding="utf-8")
            state = scheduler.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("scheduler_state.json.corrupt-*"))), 1)

    def test_string_field_none_is_corrupt(self):
        # Unlike diary's Optional[str] date fields, every string field here
        # has "" as its unset sentinel -- None is never a legitimate value.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scheduler_state.json"
            bad = {**FRESH_STATE, "greeting_date": None}
            p.write_text(json.dumps(bad), encoding="utf-8")
            state = scheduler.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("scheduler_state.json.corrupt-*"))), 1)

    def test_share_count_as_string_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scheduler_state.json"
            bad = {**FRESH_STATE, "share_count": "1"}
            p.write_text(json.dumps(bad), encoding="utf-8")
            state = scheduler.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("scheduler_state.json.corrupt-*"))), 1)

    def test_budget_used_as_bool_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scheduler_state.json"
            bad = {**FRESH_STATE, "budget_used": True}
            p.write_text(json.dumps(bad), encoding="utf-8")
            state = scheduler.load_state(p)
            self.assertEqual(state, FRESH_STATE)
            self.assertEqual(len(list(Path(td).glob("scheduler_state.json.corrupt-*"))), 1)

    def test_well_shaped_state_round_trips_without_corpse(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scheduler_state.json"
            valid = {
                "greeting_date": "2026-07-06",
                "miss_you_anchor": "2026-07-06T10:00:00+00:00",
                "share_date": "2026-07-06",
                "share_count": 1,
                "budget_date": "2026-07-06",
                "budget_used": 2,
                "last_nudge_at": "2026-07-06T10:05:00+00:00",
            }
            p.write_text(json.dumps(valid), encoding="utf-8")
            state = scheduler.load_state(p)
            corpses = list(Path(td).glob("*.corrupt-*"))
        self.assertEqual(state, valid)
        self.assertEqual(corpses, [])


# ---------------------------------------------------------------------
# common_gate: each reason in isolation, then priority order
# ---------------------------------------------------------------------

class TestCommonGate(unittest.TestCase):
    def test_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, SCHEDULER_ENABLED="false")
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertEqual(reason, "disabled")

    def test_never_met(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            reason = scheduler.common_gate(cfg, now, None, dict(FRESH_STATE))
        self.assertEqual(reason, "never_met")

    def test_quiet(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)  # default quiet window 23-8
            now = _aware(23)
            last_contact = now - timedelta(hours=1)
            reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertEqual(reason, "quiet")

    def test_partner_active(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            last_contact = now - timedelta(minutes=1)  # under RECENT_INTERACT_MINUTES
            reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertEqual(reason, "partner_active")

    def test_partner_active_boundary_exactly_at_threshold_not_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            last_contact = now - timedelta(minutes=scheduler.RECENT_INTERACT_MINUTES)
            reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertIsNone(reason)  # "< 3" is false at exactly 3 minutes

    def test_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            state = {**FRESH_STATE, "last_nudge_at": (now - timedelta(minutes=10)).isoformat()}
            reason = scheduler.common_gate(cfg, now, last_contact, state)
        self.assertEqual(reason, "cooldown")

    def test_cooldown_boundary_exactly_at_threshold_clears(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            state = {**FRESH_STATE, "last_nudge_at": (
                now - timedelta(minutes=scheduler.PROACTIVE_COOLDOWN_MINUTES)).isoformat()}
            reason = scheduler.common_gate(cfg, now, last_contact, state)
        self.assertIsNone(reason)  # "< 90" is false at exactly 90 minutes

    def test_budget(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PROACTIVE_DAILY_MAX="4")
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            state = {**FRESH_STATE, "budget_date": TODAY, "budget_used": 4}
            reason = scheduler.common_gate(cfg, now, last_contact, state)
        self.assertEqual(reason, "budget")

    def test_all_clear_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertIsNone(reason)


class TestCommonGatePriorityOrder(unittest.TestCase):
    """Every condition below is engineered so more than one reason would
    fire; the assertion pins which one wins, i.e. the checked order."""

    def test_disabled_beats_never_met_and_quiet(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, SCHEDULER_ENABLED="false")
            now = _aware(23)  # inside quiet window too
            reason = scheduler.common_gate(cfg, now, None, dict(FRESH_STATE))  # never_met too
        self.assertEqual(reason, "disabled")

    def test_never_met_beats_quiet(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(23)  # inside quiet window
            reason = scheduler.common_gate(cfg, now, None, dict(FRESH_STATE))
        self.assertEqual(reason, "never_met")

    def test_quiet_beats_partner_active(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(23)
            last_contact = now - timedelta(minutes=1)  # would be partner_active outside quiet
            reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertEqual(reason, "quiet")

    def test_partner_active_beats_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            last_contact = now - timedelta(minutes=1)  # partner_active
            state = {**FRESH_STATE,
                     "last_nudge_at": (now - timedelta(minutes=10)).isoformat()}  # cooldown too
            reason = scheduler.common_gate(cfg, now, last_contact, state)
        self.assertEqual(reason, "partner_active")

    def test_cooldown_beats_budget(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PROACTIVE_DAILY_MAX="4")
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            state = {**FRESH_STATE,
                     "last_nudge_at": (now - timedelta(minutes=10)).isoformat(),  # cooldown
                     "budget_date": TODAY, "budget_used": 4}                      # budget too
            reason = scheduler.common_gate(cfg, now, last_contact, state)
        self.assertEqual(reason, "cooldown")


class TestQuietWindow(unittest.TestCase):
    def test_start_before_end_window(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, QUIET_START_HOUR="1", QUIET_END_HOUR="6")
            cases = (
                (0, None),      # before the window opens
                (1, "quiet"),   # window opens
                (5, "quiet"),   # inside
                (6, None),      # window closes (exclusive)
            )
            for hour, expected in cases:
                with self.subTest(hour=hour):
                    now = _aware(hour)
                    last_contact = now - timedelta(hours=1)
                    reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
                    self.assertEqual(reason, expected)

    def test_start_after_end_default_window_wraps_midnight(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)  # default quiet 23-8
            cases = (
                (22, 59, None),      # not yet quiet
                (23, 0, "quiet"),    # quiet opens
                (7, 59, "quiet"),    # still quiet
                (8, 0, None),        # quiet closes
            )
            for hour, minute, expected in cases:
                with self.subTest(hour=hour, minute=minute):
                    now = _aware(hour, minute)
                    last_contact = now - timedelta(hours=1)
                    reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
                    self.assertEqual(reason, expected)

    def test_start_equals_end_disables_quiet_hours(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, QUIET_START_HOUR="10", QUIET_END_HOUR="10")
            now = _aware(10)  # the one hour that would match either interpretation
            last_contact = now - timedelta(hours=1)
            reason = scheduler.common_gate(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertIsNone(reason)


class TestBudgetRollover(unittest.TestCase):
    def test_stale_budget_date_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PROACTIVE_DAILY_MAX="4")
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            state = {**FRESH_STATE, "budget_date": YESTERDAY, "budget_used": 99}
            reason = scheduler.common_gate(cfg, now, last_contact, state)
        self.assertIsNone(reason)

    def test_empty_budget_date_never_blocks_even_if_used_meets_max(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PROACTIVE_DAILY_MAX="1")
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            state = {**FRESH_STATE, "budget_used": 1}  # budget_date stays "" (never set)
            reason = scheduler.common_gate(cfg, now, last_contact, state)
        self.assertIsNone(reason)

    def test_todays_budget_date_at_max_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PROACTIVE_DAILY_MAX="1")
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            state = {**FRESH_STATE, "budget_date": TODAY, "budget_used": 1}
            reason = scheduler.common_gate(cfg, now, last_contact, state)
        self.assertEqual(reason, "budget")


# ---------------------------------------------------------------------
# greeting_due
# ---------------------------------------------------------------------

class TestGreetingDue(unittest.TestCase):
    def test_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_ENABLED="false")
            now = _aware(9)
            reason = scheduler.greeting_due(cfg, now, dict(FRESH_STATE))
        self.assertEqual(reason, "disabled")

    def test_before_hour(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_HOUR="8")
            now = _aware(7, 59)
            reason = scheduler.greeting_due(cfg, now, dict(FRESH_STATE))
        self.assertEqual(reason, "before_hour")

    def test_due_at_exact_hour(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_HOUR="8")
            now = _aware(8, 0)
            reason = scheduler.greeting_due(cfg, now, dict(FRESH_STATE))
        self.assertIsNone(reason)

    def test_already_today_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_HOUR="8")
            now = _aware(9)
            state = {**FRESH_STATE, "greeting_date": TODAY}
            reason = scheduler.greeting_due(cfg, now, state)
        self.assertEqual(reason, "already_today")

    def test_yesterday_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_HOUR="8")
            now = _aware(9)
            state = {**FRESH_STATE, "greeting_date": YESTERDAY}
            reason = scheduler.greeting_due(cfg, now, state)
        self.assertIsNone(reason)


# ---------------------------------------------------------------------
# miss_you_due + the anchor's dedup cycle
# ---------------------------------------------------------------------

class TestMissYouDue(unittest.TestCase):
    def test_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, MISS_YOU_ENABLED="false")
            now = _aware(12)
            last_contact = now - timedelta(hours=10)
            reason = scheduler.miss_you_due(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertEqual(reason, "disabled")

    def test_not_away_when_last_contact_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            reason = scheduler.miss_you_due(cfg, now, None, dict(FRESH_STATE))
        self.assertEqual(reason, "not_away")

    def test_not_away_recent_contact(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=5)
            reason = scheduler.miss_you_due(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertEqual(reason, "not_away")

    def test_boundary_exactly_after_hours_is_due(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=6)
            reason = scheduler.miss_you_due(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertIsNone(reason)  # "< 6" is false at exactly 6 hours

    def test_due_when_away_long_enough_and_no_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=7)
            reason = scheduler.miss_you_due(cfg, now, last_contact, dict(FRESH_STATE))
        self.assertIsNone(reason)

    def test_already_fired_blocks_same_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=7)
            state = {**FRESH_STATE, "miss_you_anchor": last_contact.isoformat()}
            reason = scheduler.miss_you_due(cfg, now, last_contact, state)
        self.assertEqual(reason, "already_fired")

    def test_different_anchor_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=7)
            stale_anchor = (last_contact - timedelta(hours=1)).isoformat()
            state = {**FRESH_STATE, "miss_you_anchor": stale_anchor}
            reason = scheduler.miss_you_due(cfg, now, last_contact, state)
        self.assertIsNone(reason)


class TestMissYouAnchorDedupCycle(unittest.TestCase):
    def test_fire_record_refire_then_reply_invalidates_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, MISS_YOU_AFTER_HOURS="6")
            path = cfg.scheduler_state_path
            now = _aware(12)
            last_contact = now - timedelta(hours=7)

            state = scheduler.load_state(path)
            self.assertIsNone(scheduler.miss_you_due(cfg, now, last_contact, state))

            scheduler.record_nudge(path, "miss_you", now, last_contact)

            state = scheduler.load_state(path)
            self.assertEqual(
                scheduler.miss_you_due(cfg, now, last_contact, state), "already_fired")

            # She replies: last_contact advances, and the anchor -- pinned
            # to the previous, now-stale last_contact -- stops matching.
            new_last_contact = now + timedelta(minutes=5)
            later = new_last_contact + timedelta(hours=7)
            self.assertIsNone(
                scheduler.miss_you_due(cfg, later, new_last_contact, state))


# ---------------------------------------------------------------------
# share_due + the dice seam
# ---------------------------------------------------------------------

class TestShareDue(unittest.TestCase):
    def test_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, SHARE_ENABLED="false")
            now = _aware(12)
            reason = scheduler.share_due(cfg, now, dict(FRESH_STATE), 0.001)
        self.assertEqual(reason, "disabled")

    def test_cap_blocks_regardless_of_dice(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, SHARE_MAX_DAILY="2")
            now = _aware(12)
            state = {**FRESH_STATE, "share_date": TODAY, "share_count": 2}
            reason = scheduler.share_due(cfg, now, state, 0.001)  # dice alone would be due
        self.assertEqual(reason, "cap")

    def test_below_cap_falls_through_to_dice(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, SHARE_MAX_DAILY="2")
            now = _aware(12)
            state = {**FRESH_STATE, "share_date": TODAY, "share_count": 1}
            reason = scheduler.share_due(cfg, now, state, 0.001)
        self.assertIsNone(reason)

    def test_stale_share_date_does_not_cap(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, SHARE_MAX_DAILY="2")
            now = _aware(12)
            state = {**FRESH_STATE, "share_date": YESTERDAY, "share_count": 99}
            reason = scheduler.share_due(cfg, now, state, 0.99)  # dice misses on purpose
        self.assertEqual(reason, "dice")  # not "cap": yesterday's count never carries over

    def test_dice_seam_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            cases = (
                (0.01, None),
                (0.02, "dice"),
                (0.99, "dice"),
            )
            for roll, expected in cases:
                with self.subTest(roll=roll):
                    reason = scheduler.share_due(cfg, now, dict(FRESH_STATE), roll)
                    self.assertEqual(reason, expected)


# ---------------------------------------------------------------------
# pick_job: common gate + priority order + mutual exclusivity
# ---------------------------------------------------------------------

class TestPickJob(unittest.TestCase):
    def test_common_gate_block_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, SCHEDULER_ENABLED="false")
            now = _aware(12)
            last_contact = now - timedelta(hours=7)
            job, reason = scheduler.pick_job(cfg, now, last_contact, dict(FRESH_STATE), 0.5)
        self.assertIsNone(job)
        self.assertEqual(reason, "disabled")

    def test_greeting_wins_when_greeting_and_miss_you_both_due(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_HOUR="8", MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=7)  # away long enough for miss_you too
            job, reason = scheduler.pick_job(cfg, now, last_contact, dict(FRESH_STATE), 0.5)
        self.assertEqual(job, "greeting")
        self.assertIsNone(reason)

    def test_miss_you_wins_when_greeting_already_done(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_HOUR="8", MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=7)
            state = {**FRESH_STATE, "greeting_date": TODAY}
            job, reason = scheduler.pick_job(cfg, now, last_contact, state, 0.5)
        self.assertEqual(job, "miss_you")
        self.assertIsNone(reason)

    def test_share_wins_when_greeting_and_miss_you_not_due(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_HOUR="8", MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=1)  # not away long enough for miss_you
            state = {**FRESH_STATE, "greeting_date": TODAY}
            job, reason = scheduler.pick_job(cfg, now, last_contact, state, 0.001)
        self.assertEqual(job, "share")
        self.assertIsNone(reason)

    def test_nothing_due_returns_shares_reason(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, GREETING_HOUR="8", MISS_YOU_AFTER_HOURS="6")
            now = _aware(12)
            last_contact = now - timedelta(hours=1)
            state = {**FRESH_STATE, "greeting_date": TODAY}
            job, reason = scheduler.pick_job(cfg, now, last_contact, state, 0.99)
        self.assertIsNone(job)
        self.assertEqual(reason, "dice")


# ---------------------------------------------------------------------
# truth_timeline: single-source archive scan
# ---------------------------------------------------------------------

class TestTruthTimeline(unittest.TestCase):
    def test_both_speakers_present_values_and_replied_since_true(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            archive.write_entry(cfg.archive_dir, "companion", "hey", ts=now - timedelta(hours=5))
            archive.write_entry(cfg.archive_dir, "user", "hi", ts=now - timedelta(hours=2))
            result = scheduler.truth_timeline(cfg, now)
        self.assertIsNotNone(result)
        partner_hours, companion_hours, replied_since = result
        self.assertAlmostEqual(partner_hours, 2.0, delta=0.01)
        self.assertAlmostEqual(companion_hours, 5.0, delta=0.01)
        self.assertTrue(replied_since)  # her more recent entry is later than his

    def test_replied_since_false_when_companion_spoke_last(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            archive.write_entry(cfg.archive_dir, "user", "hi", ts=now - timedelta(hours=5))
            archive.write_entry(cfg.archive_dir, "companion", "hey", ts=now - timedelta(hours=2))
            result = scheduler.truth_timeline(cfg, now)
        partner_hours, companion_hours, replied_since = result
        self.assertAlmostEqual(partner_hours, 5.0, delta=0.01)
        self.assertAlmostEqual(companion_hours, 2.0, delta=0.01)
        self.assertFalse(replied_since)

    def test_takes_latest_per_speaker_not_last_iterated(self):
        # Written out of chronological order on purpose: the newest "user"
        # entry is written FIRST, an older one SECOND. If the scan kept
        # whichever entry it saw last (instead of tracking a true max), it
        # would wrongly report the older one as most recent.
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            archive.write_entry(cfg.archive_dir, "user", "later", ts=now - timedelta(hours=1))
            archive.write_entry(cfg.archive_dir, "user", "written second, but older",
                                ts=now - timedelta(hours=10))
            archive.write_entry(cfg.archive_dir, "companion", "hey", ts=now - timedelta(hours=3))
            result = scheduler.truth_timeline(cfg, now)
        self.assertAlmostEqual(result[0], 1.0, delta=0.01)

    def test_reads_full_history_no_lookback_window(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            archive.write_entry(cfg.archive_dir, "user", "long ago",
                                ts=now - timedelta(days=100))
            archive.write_entry(cfg.archive_dir, "companion", "hey", ts=now - timedelta(hours=1))
            result = scheduler.truth_timeline(cfg, now)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], 100 * 24, delta=0.1)

    def test_single_speaker_only_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            archive.write_entry(cfg.archive_dir, "user", "hi", ts=now - timedelta(hours=1))
            result = scheduler.truth_timeline(cfg, now)
        self.assertIsNone(result)

    def test_empty_archive_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            now = _aware(12)
            result = scheduler.truth_timeline(cfg, now)
        self.assertIsNone(result)


# ---------------------------------------------------------------------
# record_nudge: the accounting call
# ---------------------------------------------------------------------

class TestRecordNudge(unittest.TestCase):
    def test_greeting_sets_date_and_bumps_budget(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler_state.json"
            now = _aware(9)
            scheduler.record_nudge(path, "greeting", now, None)
            state = scheduler.load_state(path)
        self.assertEqual(state["greeting_date"], TODAY)
        self.assertEqual(state["budget_date"], TODAY)
        self.assertEqual(state["budget_used"], 1)
        self.assertEqual(state["last_nudge_at"], now.isoformat())

    def test_miss_you_sets_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler_state.json"
            now = _aware(9)
            last_contact = now - timedelta(hours=7)
            scheduler.record_nudge(path, "miss_you", now, last_contact)
            state = scheduler.load_state(path)
        self.assertEqual(state["miss_you_anchor"], last_contact.isoformat())
        self.assertEqual(state["budget_used"], 1)

    def test_share_sets_date_and_count(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler_state.json"
            now = _aware(9)
            scheduler.record_nudge(path, "share", now, None)
            state = scheduler.load_state(path)
        self.assertEqual(state["share_date"], TODAY)
        self.assertEqual(state["share_count"], 1)

    def test_share_count_accumulates_same_day(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler_state.json"
            now = _aware(9)
            scheduler.record_nudge(path, "share", now, None)
            scheduler.record_nudge(path, "share", now, None)
            state = scheduler.load_state(path)
        self.assertEqual(state["share_count"], 2)
        self.assertEqual(state["budget_used"], 2)

    def test_share_count_resets_next_day(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler_state.json"
            yesterday = _aware(9, day=5)
            today = _aware(9, day=6)
            scheduler.record_nudge(path, "share", yesterday, None)
            scheduler.record_nudge(path, "share", yesterday, None)
            scheduler.record_nudge(path, "share", today, None)
            state = scheduler.load_state(path)
        self.assertEqual(state["share_count"], 1)
        self.assertEqual(state["share_date"], TODAY)

    def test_budget_accumulates_across_different_jobs_same_day(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler_state.json"
            now = _aware(9)
            scheduler.record_nudge(path, "greeting", now, None)
            scheduler.record_nudge(path, "share", now, None)
            state = scheduler.load_state(path)
        self.assertEqual(state["budget_used"], 2)

    def test_budget_resets_next_day(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler_state.json"
            yesterday = _aware(9, day=5)
            today = _aware(9, day=6)
            scheduler.record_nudge(path, "greeting", yesterday, None)
            scheduler.record_nudge(path, "greeting", today, None)
            state = scheduler.load_state(path)
        self.assertEqual(state["budget_used"], 1)
        self.assertEqual(state["budget_date"], TODAY)

    def test_last_nudge_at_updates_each_call(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler_state.json"
            first = _aware(9)
            second = _aware(10)
            scheduler.record_nudge(path, "greeting", first, None)
            scheduler.record_nudge(path, "share", second, None)
            state = scheduler.load_state(path)
        self.assertEqual(state["last_nudge_at"], second.isoformat())


if __name__ == "__main__":
    unittest.main()
