"""portrait.py: state-file round-trips (load/save with corpse quarantine and
dated history snapshots) and eligibility's pure decision table. Conventions
follow tests/test_diary.py (corpse-file assertions, the _cfg() Config-building
helper)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from everthine import portrait
from everthine.config import load_config

BASE_ENV = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}

TODAY = "2026-07-06"
NOW = datetime(2026, 7, 6, 21, 30, tzinfo=timezone.utc)


def _cfg(td, **overrides):
    env = {**BASE_ENV, "DATA_DIR": str(td), **overrides}
    return load_config(env)


def _portrait(updated=TODAY, content="", opinions=None, observations=None):
    return {"updated": updated, "content": content,
            "opinions": opinions or [], "observations": observations or []}


def _diary_entry(d):
    return {"date": d}


class TestLoadPortrait(unittest.TestCase):
    def test_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            self.assertIsNone(portrait.load_portrait(cfg))

    def test_corrupt_json_degrades_and_keeps_corpse(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            cfg.portrait_path.write_text("{not json", encoding="utf-8")
            result = portrait.load_portrait(cfg)
            self.assertIsNone(result)
            corpses = list(cfg.portrait_path.parent.glob("portrait.json.corrupt-*"))
            self.assertEqual(len(corpses), 1)

    def test_wrong_shape_list_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            cfg.portrait_path.write_text('["not", "a", "dict"]', encoding="utf-8")
            result = portrait.load_portrait(cfg)
            self.assertIsNone(result)
            corpses = list(cfg.portrait_path.parent.glob("portrait.json.corrupt-*"))
            self.assertEqual(len(corpses), 1)

    def test_well_formed_file_loads_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            data = _portrait(content="a quiet week", opinions=[{"topic": "tea", "opinion": "good"}],
                             observations=["reads before bed"])
            cfg.portrait_path.write_text(json.dumps(data), encoding="utf-8")
            result = portrait.load_portrait(cfg)
        self.assertEqual(result, data)


class TestSavePortrait(unittest.TestCase):
    def test_writes_exactly_four_fields(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = portrait.save_portrait(cfg, {"content": "a quiet week"}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {"updated", "content", "opinions", "observations"})

    def test_updated_is_now_local_date(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = portrait.save_portrait(cfg, {"content": "x"}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["updated"], TODAY)

    def test_missing_optional_fields_default_empty(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = portrait.save_portrait(cfg, {"content": "just this"}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["opinions"], [])
        self.assertEqual(data["observations"], [])

    def test_content_sensitive_data_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = portrait.save_portrait(cfg, {"content": "note: api_key=abc123 done"}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("abc123", data["content"])
        self.assertIn("[REDACTED]", data["content"])

    def test_content_truncated_over_cap_with_warning(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            long_content = "x" * (portrait.PORTRAIT_CONTENT_MAX_CHARS + 500)
            with self.assertLogs("everthine", level="WARNING") as cm:
                path = portrait.save_portrait(cfg, {"content": long_content}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["content"]), portrait.PORTRAIT_CONTENT_MAX_CHARS)
        self.assertTrue(any("truncat" in line.lower() for line in cm.output))

    def test_content_under_cap_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            short = "short content, nowhere near the cap"
            path = portrait.save_portrait(cfg, {"content": short}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["content"], short)

    def test_opinions_malformed_dropped_wellformed_kept_extra_keys_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            opinions = [
                {"topic": "coffee", "opinion": "too bitter"},
                {"topic": "only missing opinion"},
                {"opinion": "only missing topic"},
                "not a dict",
                123,
                {"topic": "tea", "opinion": "better", "extra": "junk"},
            ]
            path = portrait.save_portrait(cfg, {"opinions": opinions}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["opinions"], [
            {"topic": "coffee", "opinion": "too bitter"},
            {"topic": "tea", "opinion": "better"},
        ])

    def test_opinions_capped(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            opinions = [{"topic": f"t{i}", "opinion": f"o{i}"} for i in range(20)]
            path = portrait.save_portrait(cfg, {"opinions": opinions}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["opinions"]), portrait.PORTRAIT_OPINIONS_STORED_CAP)
        self.assertEqual(data["opinions"][0], {"topic": "t0", "opinion": "o0"})

    def test_observations_malformed_dropped_strings_kept(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            observations = ["she prefers tea", 123, None, "quiet on Sundays", {"x": 1}, ["nested"]]
            path = portrait.save_portrait(cfg, {"observations": observations}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["observations"], ["she prefers tea", "quiet on Sundays"])

    def test_observations_capped(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            observations = [f"obs{i}" for i in range(20)]
            path = portrait.save_portrait(cfg, {"observations": observations}, NOW)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["observations"]), portrait.PORTRAIT_OBSERVATIONS_STORED_CAP)
        self.assertEqual(data["observations"][0], "obs0")

    def test_history_snapshot_matches_main_file_content(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = portrait.save_portrait(
                cfg, {"content": "week one", "opinions": [{"topic": "a", "opinion": "b"}],
                      "observations": ["obs"]}, NOW)
            main_data = json.loads(path.read_text(encoding="utf-8"))
            history_path = cfg.portrait_history_dir / f"{TODAY}.json"
            history_data = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(main_data, history_data)

    def test_same_day_rerun_overwrites_single_history_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            portrait.save_portrait(cfg, {"content": "first pass"}, NOW)
            later_same_day = NOW + timedelta(hours=2)
            portrait.save_portrait(cfg, {"content": "second pass"}, later_same_day)
            snapshots = list(cfg.portrait_history_dir.glob("*.json"))
            data = json.loads(
                (cfg.portrait_history_dir / f"{TODAY}.json").read_text(encoding="utf-8"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(data["content"], "second pass")

    def test_different_day_adds_second_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            portrait.save_portrait(cfg, {"content": "week one"}, NOW)
            next_week = NOW + timedelta(days=7)
            portrait.save_portrait(cfg, {"content": "week two"}, next_week)
            snapshots = list(cfg.portrait_history_dir.glob("*.json"))
        self.assertEqual(len(snapshots), 2)

    def test_returns_main_file_path(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = portrait.save_portrait(cfg, {"content": "x"}, NOW)
        self.assertEqual(path, cfg.portrait_path)

    def test_history_dir_created_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            self.assertFalse(cfg.portrait_history_dir.exists())
            portrait.save_portrait(cfg, {"content": "x"}, NOW)
            self.assertTrue(cfg.portrait_history_dir.is_dir())

    def test_file_is_valid_json_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            path = portrait.save_portrait(cfg, {"content": "hello"}, NOW)
            raw = path.read_bytes().decode("utf-8")
        json.loads(raw)  # must not raise


class TestEligibility(unittest.TestCase):
    def test_disabled_short_circuits_even_with_other_conditions_met(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PORTRAIT_ENABLED="false")
            now = datetime(2026, 7, 20, tzinfo=timezone.utc)  # interval long past
            prev = _portrait(updated="2026-07-01")
            entries = [_diary_entry("2026-07-15")]            # new material present too
            reason = portrait.eligibility(cfg, prev, entries, now)
        self.assertEqual(reason, "disabled")

    def test_no_previous_version_zero_diary_is_no_new_material(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            reason = portrait.eligibility(cfg, None, [], NOW)
        self.assertEqual(reason, "no_new_material")

    def test_no_previous_version_with_diary_is_none_first_version_special_case(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            entries = [_diary_entry("2026-07-01")]
            reason = portrait.eligibility(cfg, None, entries, NOW)
        self.assertIsNone(reason)

    def test_previous_version_interval_not_reached(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PORTRAIT_INTERVAL_DAYS="7")
            now = datetime(2026, 7, 6, tzinfo=timezone.utc)
            prev = _portrait(updated="2026-07-02")            # 4 days ago, < 7
            entries = [_diary_entry("2026-07-05")]
            reason = portrait.eligibility(cfg, prev, entries, now)
        self.assertEqual(reason, "interval_not_reached")

    def test_previous_version_interval_reached_no_new_diary(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PORTRAIT_INTERVAL_DAYS="7")
            now = datetime(2026, 7, 9, tzinfo=timezone.utc)   # exactly 7 days later
            prev = _portrait(updated="2026-07-02")
            entries = [_diary_entry("2026-07-01"), _diary_entry("2026-07-02")]  # all <= updated
            reason = portrait.eligibility(cfg, prev, entries, now)
        self.assertEqual(reason, "no_new_material")

    def test_previous_version_interval_reached_with_new_diary_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PORTRAIT_INTERVAL_DAYS="7")
            now = datetime(2026, 7, 9, tzinfo=timezone.utc)
            prev = _portrait(updated="2026-07-02")
            entries = [_diary_entry("2026-07-02"), _diary_entry("2026-07-03")]  # one new
            reason = portrait.eligibility(cfg, prev, entries, now)
        self.assertIsNone(reason)

    def test_exact_interval_boundary_is_due_not_interval_not_reached(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PORTRAIT_INTERVAL_DAYS="7")
            now = datetime(2026, 7, 9, tzinfo=timezone.utc)   # (now.date()-updated).days == 7
            prev = _portrait(updated="2026-07-02")
            entries = [_diary_entry("2026-07-05")]            # new material, so None means "due"
            reason = portrait.eligibility(cfg, prev, entries, now)
        self.assertIsNone(reason)

    def test_one_day_before_boundary_still_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PORTRAIT_INTERVAL_DAYS="7")
            now = datetime(2026, 7, 8, tzinfo=timezone.utc)   # 6 days later, one short
            prev = _portrait(updated="2026-07-02")
            entries = [_diary_entry("2026-07-05")]
            reason = portrait.eligibility(cfg, prev, entries, now)
        self.assertEqual(reason, "interval_not_reached")

    def test_updated_malformed_variants_treated_as_no_previous_version(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, PORTRAIT_INTERVAL_DAYS="7")
            # now is only 1 day after "updated" in each case -- if the malformed
            # field were (wrongly) used for the interval check, this would wrongly
            # read as "interval_not_reached" instead of bypassing it.
            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            variants = (
                {"content": "", "opinions": [], "observations": []},  # missing "updated" key
                _portrait(updated=20260702),                          # wrong type (int)
                _portrait(updated="not-a-date"),                      # unparsable string
            )
            for prev in variants:
                with self.subTest(prev=prev):
                    entries = [_diary_entry("2026-07-01")]
                    with self.assertLogs("everthine", level="WARNING") as cm:
                        reason = portrait.eligibility(cfg, prev, entries, now)
                    self.assertIsNone(reason)
                    self.assertTrue(any("updated" in line.lower() for line in cm.output))

    def test_malformed_updated_still_applies_material_gate(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            prev = _portrait(updated="not-a-date")
            with self.assertLogs("everthine", level="WARNING"):
                reason = portrait.eligibility(cfg, prev, [], NOW)
        self.assertEqual(reason, "no_new_material")


if __name__ == "__main__":
    unittest.main()
