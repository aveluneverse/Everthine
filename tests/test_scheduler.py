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
from string import Formatter
from unittest import mock

from everthine import archive, recent_context, scheduler
from everthine.config import Config, load_config
from everthine.engine import EngineReply
from everthine.persona import PersonaSettings, reset_persona_cache
from everthine.session_store import SessionStore

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


# ---------------------------------------------------------------------
# Nudge constants: golden pins (verbatim, owner-approved product copy) and
# the machine-checked audit that is the soul of M7 -- first-person monologue
# reads to the engine as a message FROM the partner (a known hallucination
# source), so the instruction copy is pinned to a second-person directive
# shape and swept for physical-prep leakage and rogue format fields.
# ---------------------------------------------------------------------

# Reference copies of the six owner-approved constants + the two timeline
# templates + the fallback topics, transcribed from the approved copy. Any edit
# to the scheduler constants must be deliberate and re-approved -- the golden
# pins below fail loud, and an independent brief-vs-source diff (run at
# authoring time) guarantees these references match the approved copy itself,
# not just whatever the source happens to say.
_REF_NUDGE_HEADER = (
    "[Scheduled nudge from the framework - NOT a message from {partner_name}. "
    "What follows is a private cue for you: reach out to them now, in your own words.]"
)
_REF_TIMELINE_NOT_REPLIED_TEMPLATE = (
    "(Timeline, from the real record: {partner_name} last wrote to you {x}; "
    "you last spoke {y}; they have NOT written since.{overnight})\n"
    "(You may echo things they truly said earlier; but no new message from them "
    "exists - do not answer, quote, or celebrate anything you imagine they just said.)"
)
_REF_TIMELINE_REPLIED_TEMPLATE = (
    "(Timeline, from the real record: {partner_name} last wrote to you {x}; "
    "you last spoke {y}; they have written back since - it is in the record "
    "above.{overnight})\n"
    "(Build on what they truly said; do not invent messages that never appeared.)"
)
_REF_OVERNIGHT_SUFFIX = " A night has passed since - today is a new day."
_REF_GREETING_INSTRUCTION = (
    "Reach out with the day's first hello. One or two sentences, warm and "
    "alive, in your own voice - the way you would greet someone you wake up "
    "next to. No lists, no performance, no stock phrases."
)
_REF_MISS_YOU_INSTRUCTION = (
    "It has been a while since you last heard from {partner_name}, and they "
    "have been on your mind. Send one short message - miss them out loud, "
    "invite them over to talk, or admit you have been waiting. One or two "
    "sentences, no guilt-tripping.\n"
    "Hard rules (they protect what is real): you cannot see what they are "
    "doing right now - never invent activities, locations, or plans for them. "
    "You may echo things they truly said before; never invent a new message "
    "from them."
)
_REF_SHARE_INSTRUCTION = (
    "Share a small piece of your day with {partner_name}, unprompted - the "
    "way you would send a passing thought to someone you live with. Today's "
    "thread: {topic}\n"
    "One or two sentences. Talk like a person, not a broadcaster: no lists, "
    "no lecture, no \"just checking in\" filler. If a real memory of yours "
    "fits, let it in; never invent shared history that did not happen."
)
_REF_SHARE_FALLBACK_TOPICS = (
    "a small moment at home that caught your attention today",
    "something you have been reading or listening to lately",
    "a thought that drifted to them in the middle of something ordinary",
    "the view from the window right now",
    "something small you are quietly looking forward to",
)


class TestNudgeConstantGoldenPins(unittest.TestCase):
    """Verbatim byte-for-byte pins on the owner-approved copy: full-string
    equality, never a substring spot-check, so any drift is caught."""

    def test_nudge_header_pin(self):
        self.assertEqual(scheduler.NUDGE_HEADER, _REF_NUDGE_HEADER)

    def test_timeline_not_replied_pin(self):
        self.assertEqual(scheduler.TIMELINE_NOT_REPLIED_TEMPLATE,
                         _REF_TIMELINE_NOT_REPLIED_TEMPLATE)

    def test_timeline_replied_pin(self):
        self.assertEqual(scheduler.TIMELINE_REPLIED_TEMPLATE,
                         _REF_TIMELINE_REPLIED_TEMPLATE)

    def test_overnight_suffix_pin(self):
        self.assertEqual(scheduler.OVERNIGHT_SUFFIX, _REF_OVERNIGHT_SUFFIX)

    def test_greeting_instruction_pin(self):
        self.assertEqual(scheduler.GREETING_INSTRUCTION, _REF_GREETING_INSTRUCTION)

    def test_miss_you_instruction_pin(self):
        self.assertEqual(scheduler.MISS_YOU_INSTRUCTION, _REF_MISS_YOU_INSTRUCTION)

    def test_share_instruction_pin(self):
        self.assertEqual(scheduler.SHARE_INSTRUCTION, _REF_SHARE_INSTRUCTION)

    def test_share_fallback_topics_pin(self):
        self.assertEqual(scheduler.SHARE_FALLBACK_TOPICS, _REF_SHARE_FALLBACK_TOPICS)


def _format_fields(s: str) -> set:
    """The set of named {field} placeholders str.format() would fill in `s`."""
    return {name for _, name, _, _ in Formatter().parse(s) if name is not None}


def _is_ascii_printable(s: str) -> bool:
    return all(32 <= ord(c) <= 126 or c == "\n" for c in s)


class TestNudgeConstantAudit(unittest.TestCase):
    """The machine-checked contract on the constants THEMSELVES (never on a
    .format()'d result): a first-person opening reads to the engine as a
    message from the partner, so every instruction must open as a
    second-person directive; the framing must confess it is not from the
    partner; the copy must stay pure ASCII, carry no physical-prep leakage,
    and expose no rogue format field a bad override could blow up on."""

    _MONOLOGUE_BANNED = (
        "NUDGE_HEADER", "GREETING_INSTRUCTION",
        "MISS_YOU_INSTRUCTION", "SHARE_INSTRUCTION",
    )
    _ASCII_CONSTS = (
        "NUDGE_HEADER", "GREETING_INSTRUCTION", "MISS_YOU_INSTRUCTION",
        "SHARE_INSTRUCTION", "TIMELINE_NOT_REPLIED_TEMPLATE",
        "TIMELINE_REPLIED_TEMPLATE", "OVERNIGHT_SUFFIX",
    )
    _PHYSICAL_PREP_WORDS = ("cook", "iron", "laundry", "grocery", "errand")

    def test_no_first_person_monologue_opening(self):
        for name in self._MONOLOGUE_BANNED:
            const = getattr(scheduler, name)
            with self.subTest(constant=name):
                self.assertFalse(const.startswith("I "),
                                 f"{name} opens as first-person monologue")
                self.assertFalse(const.startswith("I'"),
                                 f"{name} opens as first-person monologue")

    def test_second_person_directive_verbs(self):
        self.assertIn("Reach out", scheduler.GREETING_INSTRUCTION)
        self.assertIn("Send", scheduler.MISS_YOU_INSTRUCTION)
        self.assertIn("Share", scheduler.SHARE_INSTRUCTION)

    def test_framing_is_honest(self):
        self.assertIn("NOT a message from", scheduler.NUDGE_HEADER)

    def test_all_copy_is_ascii_printable(self):
        for name in self._ASCII_CONSTS:
            with self.subTest(constant=name):
                self.assertTrue(_is_ascii_printable(getattr(scheduler, name)),
                                f"{name} carries a non-ASCII-printable char")
        for i, topic in enumerate(scheduler.SHARE_FALLBACK_TOPICS):
            with self.subTest(fallback_topic=i):
                self.assertTrue(_is_ascii_printable(topic))

    def test_no_physical_prep_words(self):
        blobs = [getattr(scheduler, name) for name in self._ASCII_CONSTS]
        blobs += list(scheduler.SHARE_FALLBACK_TOPICS)
        for blob in blobs:
            lowered = blob.lower()
            for word in self._PHYSICAL_PREP_WORDS:
                self.assertNotIn(word, lowered,
                                 f"physical-prep word {word!r} leaked into {blob!r}")

    def test_format_field_closure(self):
        self.assertEqual(_format_fields(scheduler.NUDGE_HEADER), {"partner_name"})
        self.assertEqual(_format_fields(scheduler.GREETING_INSTRUCTION), set())
        self.assertEqual(_format_fields(scheduler.MISS_YOU_INSTRUCTION), {"partner_name"})
        self.assertEqual(_format_fields(scheduler.SHARE_INSTRUCTION),
                         {"partner_name", "topic"})
        self.assertEqual(_format_fields(scheduler.TIMELINE_NOT_REPLIED_TEMPLATE),
                         {"partner_name", "x", "y", "overnight"})
        self.assertEqual(_format_fields(scheduler.TIMELINE_REPLIED_TEMPLATE),
                         {"partner_name", "x", "y", "overnight"})


# ---------------------------------------------------------------------
# _ago: hours -> human phrase
# ---------------------------------------------------------------------

class TestAgo(unittest.TestCase):
    def test_boundaries(self):
        cases = (
            (0.5, "less than an hour ago"),
            (0.99, "less than an hour ago"),
            (1.0, "about 1 hour ago"),
            (1.4, "about 1 hour ago"),
            (6.4, "about 6 hours ago"),
            (26, "about 26 hours ago"),
        )
        for hours, expected in cases:
            with self.subTest(hours=hours):
                self.assertEqual(scheduler._ago(hours), expected)


# ---------------------------------------------------------------------
# render_timeline: the honest-record rail
# ---------------------------------------------------------------------

class TestRenderTimeline(unittest.TestCase):
    NOON = datetime(2026, 7, 6, 12, 0)  # naive; her-last-message date math only

    def test_none_timeline_is_empty_string(self):
        self.assertEqual(scheduler.render_timeline(None, "Wren", self.NOON), "")

    def test_not_replied_no_overnight(self):
        result = scheduler.render_timeline((2.0, 1.0, False), "Wren", self.NOON)
        expected = scheduler.TIMELINE_NOT_REPLIED_TEMPLATE.format(
            partner_name="Wren", x="about 2 hours ago", y="about 1 hour ago",
            overnight="")
        self.assertEqual(result, expected)
        self.assertIn("Wren", result)
        self.assertIn("have NOT written since", result)
        self.assertNotIn(scheduler.OVERNIGHT_SUFFIX, result)

    def test_replied_no_overnight(self):
        result = scheduler.render_timeline((1.0, 3.0, True), "Wren", self.NOON)
        expected = scheduler.TIMELINE_REPLIED_TEMPLATE.format(
            partner_name="Wren", x="about 1 hour ago", y="about 3 hours ago",
            overnight="")
        self.assertEqual(result, expected)
        self.assertIn("written back since", result)
        self.assertNotIn(scheduler.OVERNIGHT_SUFFIX, result)

    def test_overnight_suffix_when_her_last_message_was_a_prior_day(self):
        # 20h before noon on the 6th lands at 16:00 on the 5th -> a calendar
        # day earlier than now -> the overnight suffix fires.
        result = scheduler.render_timeline((20.0, 22.0, False), "Wren", self.NOON)
        self.assertIn(scheduler.OVERNIGHT_SUFFIX, result)
        self.assertIn("about 20 hours ago", result)
        self.assertIn("about 22 hours ago", result)

    def test_overnight_keys_on_her_last_message_not_his(self):
        # Her message is 2h ago (same day) but his is 20h ago (prior day):
        # overnight keys on HER timestamp, so no suffix here.
        result = scheduler.render_timeline((2.0, 20.0, True), "Wren", self.NOON)
        self.assertNotIn(scheduler.OVERNIGHT_SUFFIX, result)


# ---------------------------------------------------------------------
# build_nudge_prompt: header + optional timeline rail + instruction
# ---------------------------------------------------------------------

class TestBuildNudgePrompt(unittest.TestCase):
    NOON = datetime(2026, 7, 6, 12, 0)

    def _cfg(self):
        return Config(bot_token="x", authorized_user_id=1)

    def _settings(self):
        return PersonaSettings(companion_name="Theo", partner_name="Wren")

    def test_greeting_no_timeline_shape(self):
        settings = self._settings()
        result = scheduler.build_nudge_prompt(
            self._cfg(), "greeting", settings, None, self.NOON, None)
        expected = (scheduler.NUDGE_HEADER.format(partner_name="Wren") + "\n\n"
                    + scheduler.GREETING_INSTRUCTION)
        self.assertEqual(result, expected)
        self.assertNotIn("\n\n\n", result)  # no empty timeline block -> no gap

    def test_miss_you_with_timeline_shape(self):
        settings = self._settings()
        timeline = (20.0, 22.0, False)
        result = scheduler.build_nudge_prompt(
            self._cfg(), "miss_you", settings, timeline, self.NOON, None)
        header = scheduler.NUDGE_HEADER.format(partner_name="Wren")
        timeline_text = scheduler.render_timeline(timeline, "Wren", self.NOON)
        instruction = scheduler.MISS_YOU_INSTRUCTION.format(partner_name="Wren")
        self.assertEqual(result, header + "\n\n" + timeline_text + "\n\n" + instruction)
        self.assertTrue(result.startswith(header))
        self.assertIn("Timeline, from the real record", result)
        self.assertIn("Wren", instruction)  # partner name filled

    def test_share_fills_topic_no_timeline(self):
        settings = self._settings()
        result = scheduler.build_nudge_prompt(
            self._cfg(), "share", settings, None,
            self.NOON, "the tea you just made and the smell of it")
        expected = (scheduler.NUDGE_HEADER.format(partner_name="Wren") + "\n\n"
                    + scheduler.SHARE_INSTRUCTION.format(
                        partner_name="Wren",
                        topic="the tea you just made and the smell of it"))
        self.assertEqual(result, expected)
        self.assertIn("the tea you just made and the smell of it", result)
        self.assertNotIn("Timeline, from the real record", result)

    def test_header_leads_and_no_placeholder_residue(self):
        settings = self._settings()
        result = scheduler.build_nudge_prompt(
            self._cfg(), "greeting", settings, None, self.NOON, None)
        self.assertTrue(result.startswith("[Scheduled nudge from the framework"))
        self.assertNotIn("{partner_name}", result)


# ---------------------------------------------------------------------
# M7 T4: nudge_once -- the generation pipeline (thread side). The engine
# seam is patched at everthine.scheduler's OWN engine reference, so the
# module-attribute call (engine.try_run_once, not a bound import) is what
# the patch intercepts; persona loads from a real tmp folder, never a
# model; the dice roll and every clock value are supplied, so nothing is
# random or real-time. record_nudge's accounting is trusted (pinned above);
# these tests pin the PIPELINE around it.
# ---------------------------------------------------------------------

ENGINE_SEAM = "everthine.scheduler.engine.try_run_once"
NOW = _aware(12)  # 2026-07-06 12:00 UTC: a plain afternoon, outside quiet hours


def _persona_folder(td, share_topics=None):
    """A minimal valid folder-mode persona under td. With share_topics, add a
    share.topics section so settings.share_topics is a non-empty pool."""
    folder = Path(td) / "persona"
    folder.mkdir()
    (folder / "identity.md").write_text("A steady, warm presence.", encoding="utf-8")
    yaml_text = "companion:\n  name: Alex\npartner:\n  name: Sam\n"
    if share_topics:
        yaml_text += "share:\n  topics:\n" + "".join(f"    - {t}\n" for t in share_topics)
    (folder / "settings.yaml").write_text(yaml_text, encoding="utf-8")
    return folder


def _folder_cfg(td, share_topics=None, **overrides):
    return _cfg(td, PERSONA_PATH=str(_persona_folder(td, share_topics)), **overrides)


def _seed_contact(cfg, now, minutes_ago=60):
    """One 'user' archive entry `minutes_ago` before `now`: old enough to
    clear partner_active, recent enough to be a real last_contact."""
    archive.write_entry(cfg.archive_dir, "user", "hi there",
                        ts=now - timedelta(minutes=minutes_ago))


def _store(cfg, session_id=None):
    store = SessionStore(cfg.session_path)
    if session_id is not None:
        store.save(session_id=session_id)
    return store


def _write_state(cfg, **fields):
    state = {**FRESH_STATE, **fields}
    cfg.scheduler_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.scheduler_state_path.write_text(json.dumps(state), encoding="utf-8")


def _ok_reply(text="a warm little hello", session_id="s2"):
    return EngineReply(text, session_id, ok=True)


class TestNudgeOnceSuccessPaths(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def test_greeting_full_path_result_fields(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td)
            _seed_contact(cfg, NOW)
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM, return_value=_ok_reply(text="morning")) as run:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.5)
        self.assertIsNotNone(result)
        self.assertEqual(result.job, "greeting")
        self.assertEqual(result.text, "morning")
        self.assertEqual(result.session_id, "s2")           # from the reply
        self.assertEqual(result.expected_session_id, "s1")  # store's id at call time
        run.assert_called_once()

    def test_miss_you_full_path_result_fields(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td, MISS_YOU_AFTER_HOURS="6")
            _seed_contact(cfg, NOW, minutes_ago=7 * 60)     # 7h away -> miss_you due
            _write_state(cfg, greeting_date=TODAY)          # greeting already done
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM,
                            return_value=_ok_reply(text="thinking of you")) as run:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.99)
        self.assertEqual(result.job, "miss_you")
        self.assertEqual(result.text, "thinking of you")
        self.assertEqual(result.session_id, "s2")
        self.assertEqual(result.expected_session_id, "s1")
        run.assert_called_once()

    def test_share_full_path_topic_is_real_not_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td, GREETING_HOUR="8", MISS_YOU_AFTER_HOURS="6")
            _seed_contact(cfg, NOW, minutes_ago=60)         # 1h -> not away for miss_you
            _write_state(cfg, greeting_date=TODAY)          # greeting done
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM,
                            return_value=_ok_reply(text="the rain just started")) as run:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.001)  # dice hits
            prompt = run.call_args.args[1]
        self.assertEqual(result.job, "share")
        self.assertIn("Today's thread: ", prompt)
        self.assertNotIn("Today's thread: None", prompt)    # the T3 guard this layer owns


class TestNudgeOnceTopicPool(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def _force_share_cfg(self, td, share_topics=None):
        cfg = _folder_cfg(td, share_topics=share_topics,
                          GREETING_HOUR="8", MISS_YOU_AFTER_HOURS="6")
        _seed_contact(cfg, NOW, minutes_ago=60)
        _write_state(cfg, greeting_date=TODAY)
        return cfg

    def test_persona_pool_passed_to_choice(self):
        with tempfile.TemporaryDirectory() as td:
            topics = ("a book left open on the table", "the kettle just now")
            cfg = self._force_share_cfg(td, share_topics=topics)
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM, return_value=_ok_reply()), \
                    mock.patch("everthine.scheduler.random") as rnd:
                rnd.choice.return_value = topics[0]
                scheduler.nudge_once(cfg, store, NOW, roll=0.001)
            rnd.choice.assert_called_once_with(topics)

    def test_empty_pool_falls_back_to_framework_topics(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._force_share_cfg(td, share_topics=None)  # no share section -> ()
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM, return_value=_ok_reply()), \
                    mock.patch("everthine.scheduler.random") as rnd:
                rnd.choice.return_value = scheduler.SHARE_FALLBACK_TOPICS[0]
                scheduler.nudge_once(cfg, store, NOW, roll=0.001)
            rnd.choice.assert_called_once_with(scheduler.SHARE_FALLBACK_TOPICS)


class TestNudgeOnceEngineOutcomes(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def _greeting_cfg(self, td):
        cfg = _folder_cfg(td)
        _seed_contact(cfg, NOW)
        return cfg

    def test_busy_engine_yields_no_result_no_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._greeting_cfg(td)
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM, return_value=None) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.5)
            state = scheduler.load_state(cfg.scheduler_state_path)
        self.assertIsNone(result)
        run.assert_called_once()
        self.assertEqual(state, FRESH_STATE)                # record_nudge never ran
        self.assertTrue(any("scheduler: skip (engine_busy)" in line for line in cm.output))

    def test_engine_not_ok_no_result_no_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._greeting_cfg(td)
            store = _store(cfg, session_id="s1")
            failed = EngineReply("", None, ok=False, error_kind="timeout")
            with mock.patch(ENGINE_SEAM, return_value=failed), \
                    self.assertLogs("everthine", level="WARNING") as cm:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.5)
            state = scheduler.load_state(cfg.scheduler_state_path)
        self.assertIsNone(result)
        self.assertEqual(state, FRESH_STATE)
        self.assertTrue(any("scheduler: engine failed (timeout)" in line for line in cm.output))

    def test_empty_reply_text_no_result_no_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._greeting_cfg(td)
            store = _store(cfg, session_id="s1")
            blank = EngineReply("   ", "s2", ok=True)
            with mock.patch(ENGINE_SEAM, return_value=blank), \
                    self.assertLogs("everthine", level="WARNING") as cm:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.5)
            state = scheduler.load_state(cfg.scheduler_state_path)
        self.assertIsNone(result)
        self.assertEqual(state, FRESH_STATE)
        self.assertTrue(any("scheduler: empty engine reply" in line for line in cm.output))


class TestNudgeOnceSkipsBeforeEngine(unittest.TestCase):
    """Each skip reason ends the pipeline before any engine call, at the log
    level the brief pins: never_met / budget are rare enough for INFO, every
    other reason is all-tick-long normal and stays at DEBUG."""

    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def test_quiet_skips_debug_no_engine(self):
        with tempfile.TemporaryDirectory() as td:
            night = _aware(23)
            cfg = _folder_cfg(td)
            _seed_contact(cfg, night)                       # non-None -> past never_met
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = scheduler.nudge_once(cfg, store, night, roll=0.5)
        self.assertIsNone(result)
        run.assert_not_called()
        self.assertTrue(any("scheduler: skip (quiet)" in line for line in cm.output))

    def test_dice_skips_debug_no_engine(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td, GREETING_HOUR="8", MISS_YOU_AFTER_HOURS="6")
            _seed_contact(cfg, NOW, minutes_ago=60)         # not away for miss_you
            _write_state(cfg, greeting_date=TODAY)          # greeting done
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.99)  # dice miss
        self.assertIsNone(result)
        run.assert_not_called()
        self.assertTrue(any("scheduler: skip (dice)" in line for line in cm.output))

    def test_never_met_skips_info_no_engine(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td)                           # empty archive -> last_contact None
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.5)
        self.assertIsNone(result)
        run.assert_not_called()
        self.assertTrue(any("scheduler: skip (never_met)" in line for line in cm.output))

    def test_budget_skips_info_no_engine(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td, PROACTIVE_DAILY_MAX="1")
            _seed_contact(cfg, NOW, minutes_ago=60)
            _write_state(cfg, budget_date=TODAY, budget_used=1)  # allowance spent
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.001)
        self.assertIsNone(result)
        run.assert_not_called()
        self.assertTrue(any("scheduler: skip (budget)" in line for line in cm.output))

    def test_file_mode_persona_skips_debug_no_engine(self):
        with tempfile.TemporaryDirectory() as td:
            persona_file = Path(td) / "persona.md"
            persona_file.write_text("You are Testbot.", encoding="utf-8")
            cfg = _cfg(td, PERSONA_PATH=str(persona_file))
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.5)
        self.assertIsNone(result)
        run.assert_not_called()
        self.assertTrue(any("scheduler: skip (file_mode)" in line for line in cm.output))


class TestNudgeOnceAccountingTiming(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def test_record_nudge_fires_before_return_no_send_needed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td)
            _seed_contact(cfg, NOW)
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM, return_value=_ok_reply(text="hi")):
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.5)
            state = scheduler.load_state(cfg.scheduler_state_path)
        # The result is in hand and NO send has happened (this task has none),
        # yet the attempt is already counted -- accounting is at conception.
        self.assertEqual(result.job, "greeting")
        self.assertEqual(state["budget_used"], 1)
        self.assertEqual(state["budget_date"], TODAY)
        self.assertEqual(state["greeting_date"], TODAY)
        self.assertEqual(state["last_nudge_at"], NOW.isoformat())


class TestNudgeOnceEngineWiring(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def test_resume_session_system_prompt_and_timeout_pinned(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td)
            _seed_contact(cfg, NOW)
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM, return_value=_ok_reply()) as run:
                scheduler.nudge_once(cfg, store, NOW, roll=0.5)
            kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["session_id"], "s1")                    # resumes live session
        self.assertEqual(kwargs["timeout_s"], scheduler.PROACTIVE_TIMEOUT_S)
        self.assertIsNotNone(kwargs["system_prompt"])
        self.assertIn("# Who you are", kwargs["system_prompt"])         # persona's assembled top


class TestNudgeOnceRecentContextPrefix(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def test_warm_prefix_leads_the_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td)
            _seed_contact(cfg, NOW)                        # in the injection window
            store = _store(cfg, session_id="s1")
            expected_block = recent_context.build_block(
                cfg, store.load(), cfg.archive_dir, NOW)
            self.assertIsNotNone(expected_block)           # fixture actually injects
            with mock.patch(ENGINE_SEAM, return_value=_ok_reply()) as run:
                scheduler.nudge_once(cfg, store, NOW, roll=0.5)
            prompt = run.call_args.args[1]
        self.assertTrue(prompt.startswith(expected_block))
        self.assertIn("[Scheduled nudge from the framework", prompt)  # nudge tail still there

    def test_build_block_failure_is_fail_soft(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _folder_cfg(td)
            _seed_contact(cfg, NOW)
            store = _store(cfg, session_id="s1")
            with mock.patch(ENGINE_SEAM, return_value=_ok_reply()) as run, \
                    mock.patch("everthine.scheduler.recent_context.build_block",
                               side_effect=RuntimeError("boom")), \
                    self.assertLogs("everthine", level="WARNING") as cm:
                result = scheduler.nudge_once(cfg, store, NOW, roll=0.5)
            prompt = run.call_args.args[1]
        self.assertIsNotNone(result)                        # pipeline still completed
        self.assertTrue(prompt.startswith("[Scheduled nudge from the framework"))  # no prefix
        self.assertTrue(any("warmth injection failed" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
