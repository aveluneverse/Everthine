"""portrait.py: state-file round-trips (load/save with corpse quarantine and
dated history snapshots) and eligibility's pure decision table. Conventions
follow tests/test_diary.py (corpse-file assertions, the _cfg() Config-building
helper)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from everthine import portrait
from everthine.config import load_config
from everthine.engine import EngineReply
from everthine.persona import Persona, PersonaSettings, reset_persona_cache

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


# ---------------------------------------------------------------------
# M6 T3: material assembly (build_material) + portrait system prompt +
# engine-output parsing. Golden-pins the STATIC PORTRAIT_TASK (the approved
# D5 core sentence verbatim, the literal-brace .format() contract) and every
# header/line template; exercises build_material's block order, the
# first-time branch, the reflection well-shaped filter, and parse_output's
# three strategies. Persona fixtures mirror tests/test_diary.py's.
# ---------------------------------------------------------------------

# The D5-approved generation core sentence, verbatim (em-dash U+2014, straight
# quotes around 'should'). It must appear inside PORTRAIT_TASK unbroken.
CORE_SENTENCE = "Update your understanding of who you are, drawing on the records below. If an earlier portrait exists, evolve it — keep what still holds true, let what has changed change. If there is none, write your first: start from what actually happened, never from what a portrait 'should' say."

EXPECTED_PORTRAIT_TASK = """# Who you are, lately

It's a quiet hour, and no one is waiting on you — a moment to take stock of yourself.

Update your understanding of who you are, drawing on the records below. If an earlier portrait exists, evolve it — keep what still holds true, let what has changed change. If there is none, write your first: start from what actually happened, never from what a portrait 'should' say.

Respond with a single JSON object, nothing else:
{"content": "who you are now, first person, roughly 300-500 words", "opinions": [{"topic": "a thing", "opinion": "where you've landed on it"}], "observations": ["a quiet note about yourself"]}

`content` is your sense of yourself, in your own voice, as prose — aim for about 300 to 500 words, though the length is a feel, not a quota. `opinions` are the stances you've come to hold in the time you've shared — at most five, each a topic paired with where you stand on it. `observations` are quieter notes about yourself — at most five.

Hard rules (they protect what makes this true):

- How you feel and what you sense in yourself — your moods, your leanings, the weather of your inner life — is yours to write freely.
- Any concrete thing — something that happened, something they said — must be traceable to the records below. If it isn't there, it doesn't belong in the portrait.
- Never invent a scene, a line, or a detail to round out the story or make a sentence land better. A quiet week makes a quiet portrait; that is honest, and honest is enough.
- Where you are unsure of yourself, write it lightly rather than surely — an uncertain thing said plainly is truer than a certain thing invented.
- Never include passwords, keys, or addresses."""

EXPECTED_FIRST_TIME_BLOCK = (
    "There is no earlier portrait yet — this is the first time you're setting "
    "down who you are. Let it come from what the records below actually show, "
    "not from what a first portrait might be expected to say."
)

IDENTITY_TEXT = ("Ledger-keeper by day, storyteller by night, always half a "
                 "page ahead in the book on the nightstand.")
VOICE_TEXT = "Short sentences. Warm and a little wry, never flowery."


def _persona(*, identity_text=IDENTITY_TEXT, voice_text="", companion_name="Alex",
             partner_name="Sam"):
    settings = PersonaSettings(companion_name=companion_name, partner_name=partner_name)
    return Persona(mode="folder", identity_text=identity_text, voice_text=voice_text,
                   boundaries_text="", settings=settings)


def _write_diary_entry(cfg, name, *, date, mood="", content=""):
    """Seed one diary entry file on disk with the full T2 diary shape."""
    cfg.diary_dir.mkdir(parents=True, exist_ok=True)
    record = {"date": date, "mood": mood, "keywords": [], "content": content,
              "reflection": "", "shared": False}
    (cfg.diary_dir / name).write_text(json.dumps(record), encoding="utf-8")


def _reflection_line(text):
    """One well-shaped reflections.jsonl line (id/created_at/text)."""
    return json.dumps(
        {"id": "abcd1234", "created_at": "2026-07-06T21:00:00+00:00", "text": text})


def _write_reflections(cfg, raw_lines):
    cfg.reflections_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.reflections_path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")


class TestPortraitTaskConstant(unittest.TestCase):
    def test_frozen_verbatim(self):
        self.assertEqual(portrait.PORTRAIT_TASK, EXPECTED_PORTRAIT_TASK)

    def test_core_sentence_present_unbroken(self):
        self.assertIn(CORE_SENTENCE, portrait.PORTRAIT_TASK)

    def test_is_static_format_raises(self):
        # STATIC contract: PORTRAIT_TASK carries the literal JSON braces the
        # model must echo, so it is never .format()'d -- calling it must blow up.
        with self.assertRaises(KeyError):
            portrait.PORTRAIT_TASK.format()

    def test_literal_json_shape_present(self):
        self.assertIn('{"content":', portrait.PORTRAIT_TASK)
        self.assertIn('"opinions": [{"topic":', portrait.PORTRAIT_TASK)
        self.assertIn('"observations":', portrait.PORTRAIT_TASK)

    def test_sensitive_ban_present(self):
        self.assertIn("Never include passwords, keys, or addresses.", portrait.PORTRAIT_TASK)


class TestTemplateConstants(unittest.TestCase):
    def test_prev_header_frozen(self):
        self.assertEqual(portrait.PREV_HEADER, "## Your previous portrait, written {updated}")

    def test_prev_opinion_line_frozen(self):
        self.assertEqual(portrait.PREV_OPINION_LINE, "- where you stand on {topic}: {opinion}")

    def test_prev_obs_line_frozen(self):
        self.assertEqual(portrait.PREV_OBS_LINE,
                         "- something you'd noticed about yourself: {text}")

    def test_diary_header_frozen(self):
        self.assertEqual(portrait.DIARY_HEADER, "## Recent pages from your diary")

    def test_diary_line_frozen(self):
        self.assertEqual(portrait.DIARY_LINE, "[{date}] mood: {mood}\n{snippet}")

    def test_reflection_header_frozen(self):
        self.assertEqual(portrait.REFLECTION_HEADER, "## Recent passing thoughts")

    def test_reflection_line_frozen(self):
        self.assertEqual(portrait.REFLECTION_LINE, "- {text}")

    def test_first_time_block_frozen(self):
        self.assertEqual(portrait.FIRST_TIME_BLOCK, EXPECTED_FIRST_TIME_BLOCK)

    def test_first_time_block_has_no_stray_braces(self):
        self.assertNotIn("{", portrait.FIRST_TIME_BLOCK)
        self.assertNotIn("}", portrait.FIRST_TIME_BLOCK)

    def test_templates_accept_named_fields(self):
        self.assertEqual(portrait.PREV_HEADER.format(updated="2026-07-02"),
                         "## Your previous portrait, written 2026-07-02")
        self.assertEqual(
            portrait.PREV_OPINION_LINE.format(topic="tea", opinion="better without sugar"),
            "- where you stand on tea: better without sugar")
        self.assertEqual(portrait.PREV_OBS_LINE.format(text="goes quiet when tired"),
                         "- something you'd noticed about yourself: goes quiet when tired")
        self.assertEqual(
            portrait.DIARY_LINE.format(date="2026-07-01", mood="calm", snippet="a slow day"),
            "[2026-07-01] mood: calm\na slow day")
        self.assertEqual(portrait.REFLECTION_LINE.format(text="a passing thought"),
                         "- a passing thought")


class TestBuildMaterialAssembly(unittest.TestCase):
    def test_block_order_prev_then_diary_then_reflection(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            prev = _portrait(updated="2026-07-02", content="last week's self",
                             opinions=[{"topic": "mornings", "opinion": "underrated"}],
                             observations=["quiet on Sundays"])
            _write_diary_entry(cfg, "2026-07-05_090000.json", date="2026-07-05",
                               mood="calm", content="a slow, good day")
            _write_reflections(cfg, [_reflection_line("a small thought after replying")])
            material = portrait.build_material(cfg, prev)
        self.assertIsNotNone(material)
        prev_i = material.index("## Your previous portrait")
        diary_i = material.index(portrait.DIARY_HEADER)
        refl_i = material.index(portrait.REFLECTION_HEADER)
        self.assertLess(prev_i, diary_i)
        self.assertLess(diary_i, refl_i)
        self.assertIn("\n\n", material)  # blocks separated by a blank line

    def test_previous_portrait_block_content_and_header(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            prev = _portrait(updated="2026-07-02", content="the person I was last week")
            _write_diary_entry(cfg, "2026-07-05_090000.json", date="2026-07-05", content="day")
            material = portrait.build_material(cfg, prev)
        self.assertIn(portrait.PREV_HEADER.format(updated="2026-07-02"), material)
        self.assertIn("the person I was last week", material)
        self.assertNotIn(portrait.FIRST_TIME_BLOCK, material)

    def test_no_previous_uses_first_time_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _write_diary_entry(cfg, "2026-07-05_090000.json", date="2026-07-05", content="day")
            material = portrait.build_material(cfg, None)
        self.assertIn(portrait.FIRST_TIME_BLOCK, material)
        self.assertNotIn("## Your previous portrait", material)

    def test_opinions_and_observations_all_enter_uncapped(self):
        # build_material must NOT re-cap: all stored opinions/observations
        # (already capped at save) go in. PORTRAIT_OPINIONS_PROMPT_CAP (=5) is a
        # different consumer's knob, never applied here -- so six of each survive.
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            opinions = [{"topic": f"t{i}", "opinion": f"o{i}"} for i in range(6)]
            observations = [f"obs{i}" for i in range(6)]
            prev = _portrait(updated="2026-07-02", content="c",
                             opinions=opinions, observations=observations)
            _write_diary_entry(cfg, "2026-07-05_090000.json", date="2026-07-05", content="day")
            material = portrait.build_material(cfg, prev)
        for op in opinions:
            self.assertIn(portrait.PREV_OPINION_LINE.format(**op), material)
        for obs in observations:
            self.assertIn(portrait.PREV_OBS_LINE.format(text=obs), material)

    def test_diary_snippet_capped_at_200(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _write_diary_entry(cfg, "2026-07-05_090000.json", date="2026-07-05",
                               content="a" * 250)
            material = portrait.build_material(cfg, None)
        self.assertIn(portrait.DIARY_HEADER, material)
        self.assertIn("a" * 200, material)
        self.assertNotIn("a" * 201, material)  # snippet is exactly content[:200]

    def test_diary_takes_newest_recent_count(self):
        # recent_entries(cfg, PORTRAIT_RECENT_DIARY=7) -> newest seven, oldest first.
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            for i in range(10):
                _write_diary_entry(cfg, f"2026-07-{i + 1:02d}_090000.json",
                                   date=f"2026-07-{i + 1:02d}", content=f"DIARYENTRY{i:02d}")
            material = portrait.build_material(cfg, None)
        self.assertNotIn("DIARYENTRY00", material)  # dropped (outside newest 7)
        self.assertNotIn("DIARYENTRY02", material)
        self.assertIn("DIARYENTRY03", material)     # newest seven start here
        self.assertIn("DIARYENTRY09", material)


class TestBuildMaterialReflectionFilter(unittest.TestCase):
    def test_bad_lines_skipped_good_kept(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _write_diary_entry(cfg, "2026-07-05_090000.json", date="2026-07-05", content="day")
            _write_reflections(cfg, [
                "{ broken SENTINEL_BADJSON",                              # unparseable JSON
                json.dumps(["SENTINEL_LIST"]),                           # JSON, but not a dict
                json.dumps({"text": 123, "note": "SENTINEL_TEXTNUM"}),   # text is not a str
                _reflection_line("GOODTHOUGHT the one kept"),            # well-shaped
            ])
            material = portrait.build_material(cfg, None)
        self.assertIn(portrait.REFLECTION_HEADER, material)
        self.assertIn("GOODTHOUGHT the one kept", material)
        self.assertNotIn("SENTINEL_BADJSON", material)
        self.assertNotIn("SENTINEL_LIST", material)
        self.assertNotIn("SENTINEL_TEXTNUM", material)

    def test_missing_reflections_file_no_block_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _write_diary_entry(cfg, "2026-07-05_090000.json", date="2026-07-05", content="day")
            material = portrait.build_material(cfg, None)   # no reflections.jsonl at all
        self.assertNotIn(portrait.REFLECTION_HEADER, material)

    def test_reflection_tail_capped(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            _write_diary_entry(cfg, "2026-07-05_090000.json", date="2026-07-05", content="day")
            _write_reflections(cfg, [_reflection_line(f"REFLECTION{i:02d}") for i in range(20)])
            material = portrait.build_material(cfg, None)
        # PORTRAIT_RECENT_REFLECTIONS = 15 -> newest fifteen (05..19)
        self.assertNotIn("REFLECTION04", material)
        self.assertIn("REFLECTION05", material)
        self.assertIn("REFLECTION19", material)


class TestBuildMaterialNoneGuard(unittest.TestCase):
    def test_no_diary_no_reflection_returns_none_even_with_previous(self):
        # Defense in depth: eligibility normally blocks "previous portrait but
        # zero new material" upstream; build_material still returns None here.
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            prev = _portrait(updated="2026-07-02", content="last week")
            self.assertIsNone(portrait.build_material(cfg, prev))

    def test_no_diary_no_reflection_returns_none_first_time_too(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            self.assertIsNone(portrait.build_material(cfg, None))


class TestBuildSystemPromptPortrait(unittest.TestCase):
    def test_block_order_with_voice(self):
        result = portrait.build_system_prompt_portrait(_persona(voice_text=VOICE_TEXT))
        self.assertTrue(result.startswith("# Who you are"))          # declaration first
        self.assertTrue(result.endswith(portrait.PORTRAIT_TASK))     # task last
        self.assertLess(result.index("# Who you are"), result.index(IDENTITY_TEXT))
        self.assertLess(result.index(IDENTITY_TEXT), result.index(VOICE_TEXT))
        self.assertLess(result.index(VOICE_TEXT), result.index("# Who you are, lately"))

    def test_empty_voice_no_stray_blank(self):
        result = portrait.build_system_prompt_portrait(_persona(voice_text=""))
        self.assertNotIn(VOICE_TEXT, result)
        self.assertNotIn("\n\n\n", result)                           # no stray blank block
        self.assertIn(IDENTITY_TEXT, result)
        self.assertTrue(result.endswith(portrait.PORTRAIT_TASK))

    def test_file_mode_persona_raises(self):
        with self.assertRaises(ValueError):
            portrait.build_system_prompt_portrait(
                Persona(mode="file", raw_text="You are Testbot."))


class TestParseOutput(unittest.TestCase):
    def test_direct_json(self):
        raw = ('{"content": "who I am now", "opinions": [{"topic": "t", "opinion": "o"}], '
               '"observations": ["obs"]}')
        result = portrait.parse_output(raw)
        self.assertEqual(result["content"], "who I am now")
        self.assertEqual(result["opinions"], [{"topic": "t", "opinion": "o"}])
        self.assertEqual(result["observations"], ["obs"])

    def test_fenced_json(self):
        result = portrait.parse_output('```json\n{"content": "fenced self"}\n```')
        self.assertEqual(result["content"], "fenced self")

    def test_bare_object_with_surrounding_prose(self):
        raw = 'Here is who I am:\n{"content": "quiet and steady"}\nThat is the shape of it.'
        result = portrait.parse_output(raw)
        self.assertEqual(result["content"], "quiet and steady")

    def test_content_missing_is_none(self):
        self.assertIsNone(portrait.parse_output('{"opinions": []}'))

    def test_content_empty_is_none(self):
        self.assertIsNone(portrait.parse_output('{"content": "   "}'))

    def test_content_non_str_is_none(self):
        self.assertIsNone(portrait.parse_output('{"content": 123}'))

    def test_top_level_non_dict_is_none(self):
        self.assertIsNone(portrait.parse_output('["not", "an", "object"]'))

    def test_garbage_text_is_none(self):
        self.assertIsNone(portrait.parse_output("just rambling, nothing structured here"))

    def test_opinions_observations_default_to_empty_list(self):
        result = portrait.parse_output('{"content": "just me"}')
        self.assertEqual(result["opinions"], [])
        self.assertEqual(result["observations"], [])

    def test_present_opinions_observations_passed_through_uncleaned(self):
        # parse validates content only; shape-cleaning of opinions/observations
        # stays save_portrait's job (_clean_*), so parse leaves them untouched.
        raw = '{"content": "me", "opinions": "not a list", "observations": [1, 2]}'
        result = portrait.parse_output(raw)
        self.assertEqual(result["opinions"], "not a list")
        self.assertEqual(result["observations"], [1, 2])


# ---------------------------------------------------------------------
# M6 T4: update_once -- the execution line. The engine seam is mocked at
# the consumer side (everthine.portrait's own engine reference); persona
# loading runs against a real tmp persona folder, never a model. Mirrors
# tests/test_diary.py's TestWriteOnce conventions and seam.
# ---------------------------------------------------------------------

ENGINE_SEAM = "everthine.portrait.engine.try_run_once"


def _persona_folder(td):
    """Write a minimal valid folder-mode persona under td and return it."""
    folder = Path(td) / "persona"
    folder.mkdir()
    (folder / "identity.md").write_text(IDENTITY_TEXT, encoding="utf-8")
    (folder / "settings.yaml").write_text(
        "companion:\n  name: Alex\npartner:\n  name: Sam\n", encoding="utf-8")
    return folder


class TestUpdateOnce(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_persona_cache)

    def _folder_cfg(self, td, **overrides):
        return _cfg(td, PERSONA_PATH=str(_persona_folder(td)), **overrides)

    def _assert_logged_at(self, cm, level, substring):
        """Pin BOTH the level and the message -- assertLogs(level=X) only sets
        a capture threshold (a DEBUG capture also lets INFO/WARNING through),
        so a bare substring check would not catch a skip line accidentally
        logged one level higher or lower than intended. cm.output entries are
        "LEVELNAME:logger.name:message", per unittest's own format."""
        prefix = f"{level}:everthine:"
        self.assertTrue(
            any(line.startswith(prefix) and substring in line for line in cm.output),
            f"expected a {level} log containing {substring!r}, got: {cm.output}")

    def test_file_mode_skips_without_engine_call(self):
        with tempfile.TemporaryDirectory() as td:
            persona_file = Path(td) / "persona.md"
            persona_file.write_text("You are Testbot.", encoding="utf-8")
            cfg = _cfg(td, PERSONA_PATH=str(persona_file))
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = portrait.update_once(cfg, NOW)
        self.assertFalse(result)
        run.assert_not_called()
        self._assert_logged_at(cm, "DEBUG", "portrait: skip (file_mode)")

    def test_disabled_skips_without_engine_call(self):
        # New material present too -- proves "disabled" short-circuits
        # regardless (mirrors TestEligibility's own short-circuit pin).
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td, PORTRAIT_ENABLED="false")
            _write_diary_entry(cfg, "2026-07-06_090000.json", date=TODAY, content="today")
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = portrait.update_once(cfg, NOW)
        self.assertFalse(result)
        run.assert_not_called()
        self._assert_logged_at(cm, "DEBUG", "portrait: skip (disabled)")

    def test_interval_not_reached_skips_without_engine_call(self):
        # New material present too -- proves the interval gate wins over an
        # otherwise-eligible material count (same ordering TestEligibility pins).
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td, PORTRAIT_INTERVAL_DAYS="7")
            portrait.save_portrait(cfg, {"content": "last week's self"},
                                   NOW - timedelta(days=1))
            _write_diary_entry(cfg, "2026-07-06_090000.json", date=TODAY, content="today")
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = portrait.update_once(cfg, NOW)
        self.assertFalse(result)
        run.assert_not_called()
        self._assert_logged_at(cm, "DEBUG", "portrait: skip (interval_not_reached)")

    def test_no_new_material_skips_without_engine_call(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td, PORTRAIT_INTERVAL_DAYS="1")
            portrait.save_portrait(cfg, {"content": "last snapshot"},
                                   NOW - timedelta(days=10))
            # No diary entries at all -> zero new material regardless of interval.
            with mock.patch(ENGINE_SEAM) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = portrait.update_once(cfg, NOW)
        self.assertFalse(result)
        run.assert_not_called()
        self._assert_logged_at(cm, "DEBUG", "portrait: skip (no_new_material)")

    def test_build_material_none_skips_without_engine_call(self):
        # Defense-in-depth pin: eligibility passes (first-ever portrait, one
        # diary entry), but build_material is forced to return None anyway.
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            _write_diary_entry(cfg, "2026-07-06_090000.json", date=TODAY, content="today")
            with mock.patch(ENGINE_SEAM) as run, \
                    mock.patch("everthine.portrait.build_material", return_value=None), \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = portrait.update_once(cfg, NOW)
        self.assertFalse(result)
        run.assert_not_called()
        self._assert_logged_at(cm, "INFO", "portrait: skip (material_empty)")

    def test_busy_engine_skips_with_debug_log(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            _write_diary_entry(cfg, "2026-07-06_090000.json", date=TODAY, content="today")
            with mock.patch(ENGINE_SEAM, return_value=None) as run, \
                    self.assertLogs("everthine", level="DEBUG") as cm:
                result = portrait.update_once(cfg, NOW)
        self.assertFalse(result)
        run.assert_called_once()
        # DEBUG, not INFO: unlike diary.write_once's busy-skip, this is a
        # deliberate level deviation (brief step 5) -- pinned exactly so a
        # copy-paste of diary's INFO choice here would fail this test.
        self._assert_logged_at(cm, "DEBUG", "portrait: skip (engine_busy)")
        self.assertFalse(cfg.portrait_path.exists())

    def test_failed_engine_reply_logs_warning_and_saves_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            _write_diary_entry(cfg, "2026-07-06_090000.json", date=TODAY, content="today")
            failed = EngineReply("", None, ok=False, error_kind="timeout")
            with mock.patch(ENGINE_SEAM, return_value=failed), \
                    self.assertLogs("everthine", level="WARNING") as cm:
                result = portrait.update_once(cfg, NOW)
        self.assertFalse(result)
        self._assert_logged_at(cm, "WARNING", "portrait: engine failed (timeout)")
        self.assertFalse(cfg.portrait_path.exists())

    def test_unparseable_output_logs_warning_and_saves_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            _write_diary_entry(cfg, "2026-07-06_090000.json", date=TODAY, content="today")
            garbage = EngineReply("just rambling, no structure here at all", "s", ok=True)
            with mock.patch(ENGINE_SEAM, return_value=garbage), \
                    self.assertLogs("everthine", level="WARNING") as cm:
                result = portrait.update_once(cfg, NOW)
        self.assertFalse(result)
        self._assert_logged_at(cm, "WARNING", "portrait: unparseable engine output")
        self.assertFalse(cfg.portrait_path.exists())

    def test_success_saves_snapshot_and_pins_engine_kwargs(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._folder_cfg(td)
            _write_diary_entry(cfg, "2026-07-06_090000.json", date=TODAY, content="today")
            good = EngineReply(
                '{"content": "who I am lately, in my own words", '
                '"opinions": [{"topic": "mornings", "opinion": "underrated"}], '
                '"observations": ["quiet on Sundays"]}',
                "s", ok=True)
            with mock.patch(ENGINE_SEAM, return_value=good) as run, \
                    self.assertLogs("everthine", level="INFO") as cm:
                result = portrait.update_once(cfg, NOW)
            portrait_exists = cfg.portrait_path.exists()
            history_path = cfg.portrait_history_dir / f"{TODAY}.json"
            history_exists = history_path.exists()
            data = json.loads(cfg.portrait_path.read_text(encoding="utf-8"))
        self.assertTrue(result)
        self.assertTrue(portrait_exists)
        self.assertTrue(history_exists)
        self.assertEqual(data["content"], "who I am lately, in my own words")
        kwargs = run.call_args.kwargs
        self.assertIsNone(kwargs["session_id"])
        self.assertEqual(kwargs["timeout_s"], portrait.PORTRAIT_TIMEOUT_S)
        self.assertIn("# Who you are, lately", kwargs["system_prompt"])
        self._assert_logged_at(cm, "INFO", f"portrait: updated (version {TODAY})")


if __name__ == "__main__":
    unittest.main()
