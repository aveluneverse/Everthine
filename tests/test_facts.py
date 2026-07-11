"""facts.py: fact-store round-trips and dedup, the extraction cursor's
fail-soft state file, the character-bigram ranking/dedup algorithms, and the
eligibility gate. Conventions follow tests/test_stages.py (corrupt-file corpse
assertions) and tests/test_diary.py (the _cfg() minimal-env eligibility style).
No engine, no archive, plain unittest, tmp dirs throughout."""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from everthine import facts
from everthine.config import Config, load_config

BASE_ENV = {"BOT_TOKEN": "123456789:" + "A" * 35, "AUTHORIZED_USER_ID": "42"}
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 11)

# Two facts that overlap enough to be near-duplicates (favorite color, only
# the color word changes: 7 of 9 shared bigrams -> ~0.78 >= 0.7), plus a
# genuinely different fact (weekend hiking: 1 of 6 shared -> ~0.17 < 0.7).
BLUE = {"text": "我最喜歡的顏色是藍色", "category": "preference", "date": "2026-07-11"}
GREEN = {"text": "我最喜歡的顏色是綠色", "category": "preference", "date": "2026-07-11"}
HIKING = {"text": "我週末喜歡爬山", "category": "hobby", "date": "2026-07-11"}


def _cfg(td, **overrides):
    env = {**BASE_ENV, "DATA_DIR": str(td), **overrides}
    return load_config(env)


# ---------------------------------------------------------------------
# 1. Fact store: load_facts / append_facts
# ---------------------------------------------------------------------

class TestLoadFacts(unittest.TestCase):
    def test_missing_file_is_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(facts.load_facts(Path(td) / "facts.json"), [])

    def test_round_trip_preserves_fields(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            facts.append_facts(p, [BLUE], 200)
            loaded = facts.load_facts(p)
            self.assertEqual(loaded, [BLUE])

    def test_non_dict_entries_are_dropped_silently(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            p.write_text(
                '{"facts": [{"text": "a", "category": "c", "date": "2026-07-11"},'
                ' "junk", 123, {"text": "b"}]}',
                encoding="utf-8")
            loaded = facts.load_facts(p)
            self.assertEqual(loaded, [
                {"text": "a", "category": "c", "date": "2026-07-11"},
                {"text": "b"}])
            # Dropping stray non-dicts is not corruption: no corpse, file kept.
            self.assertEqual(list(Path(td).glob("facts.json.corrupt-*")), [])

    def test_corrupt_json_degrades_and_keeps_corpse(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertLogs("everthine", level="WARNING"):
                loaded = facts.load_facts(p)
            self.assertEqual(loaded, [])
            self.assertEqual(len(list(Path(td).glob("facts.json.corrupt-*"))), 1)

    def test_top_level_not_dict_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            p.write_text('["bare", "list"]', encoding="utf-8")
            self.assertEqual(facts.load_facts(p), [])
            self.assertEqual(len(list(Path(td).glob("facts.json.corrupt-*"))), 1)

    def test_facts_not_a_list_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            p.write_text('{"facts": "not a list"}', encoding="utf-8")
            self.assertEqual(facts.load_facts(p), [])
            self.assertEqual(len(list(Path(td).glob("facts.json.corrupt-*"))), 1)

    def test_missing_facts_key_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            p.write_text('{"other": 1}', encoding="utf-8")
            self.assertEqual(facts.load_facts(p), [])
            self.assertEqual(len(list(Path(td).glob("facts.json.corrupt-*"))), 1)

    def test_corpse_rename_failure_is_swallowed_and_still_degrades(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            p.write_text("{not json", encoding="utf-8")
            with mock.patch.object(Path, "rename",
                                   side_effect=OSError("rename blocked")):
                with self.assertLogs("everthine", level="WARNING") as cm:
                    loaded = facts.load_facts(p)  # must not raise
            self.assertEqual(loaded, [])
            self.assertEqual(list(Path(td).glob("facts.json.corrupt-*")), [])
            self.assertTrue(p.exists())  # broken original left in place
            self.assertTrue(any("could not preserve" in line for line in cm.output))


class TestAppendFacts(unittest.TestCase):
    def test_append_to_empty_returns_count_and_persists(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            added = facts.append_facts(p, [BLUE, HIKING], 200)
            self.assertEqual(added, 2)
            self.assertEqual([f["text"] for f in facts.load_facts(p)],
                             [BLUE["text"], HIKING["text"]])

    def test_exact_and_near_dups_are_skipped_distinct_kept(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            facts.append_facts(p, [BLUE], 200)
            # BLUE again (exact), GREEN (near dup >=0.7), HIKING (distinct <0.7)
            added = facts.append_facts(p, [BLUE, GREEN, HIKING], 200)
            self.assertEqual(added, 1)  # only HIKING survives
            self.assertEqual([f["text"] for f in facts.load_facts(p)],
                             [BLUE["text"], HIKING["text"]])

    def test_intra_batch_near_dup_keeps_only_first(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            # Into an empty store: BLUE accepted, GREEN is a near dup of the
            # just-accepted BLUE and is dropped within the same batch.
            added = facts.append_facts(p, [BLUE, GREEN], 200)
            self.assertEqual(added, 1)
            self.assertEqual([f["text"] for f in facts.load_facts(p)],
                             [BLUE["text"]])

    def test_non_dict_and_empty_text_incoming_are_filtered(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            valid = {"text": "有效資訊", "category": "x", "date": "2026-07-11"}
            added = facts.append_facts(p, [
                "not a dict", 42,
                {"text": ""}, {"category": "x"}, {"text": "   "},
                valid,
            ], 200)
            self.assertEqual(added, 1)
            self.assertEqual(facts.load_facts(p), [valid])

    def test_prune_keeps_newest_by_position_dropping_front(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            f1 = {"text": "喜歡藍色", "category": "a", "date": "2026-07-01"}
            f2 = {"text": "住在台北", "category": "a", "date": "2026-07-02"}
            f3 = {"text": "養了一隻貓", "category": "a", "date": "2026-07-03"}
            f4 = {"text": "週末去爬山", "category": "a", "date": "2026-07-04"}
            f5 = {"text": "最近在學鋼琴", "category": "a", "date": "2026-07-05"}
            self.assertEqual(facts.append_facts(p, [f1, f2, f3], 3), 3)
            # Adding two more overflows the cap; the two oldest (f1, f2) fall
            # off the FRONT -- list order is chronological, no date sort.
            self.assertEqual(facts.append_facts(p, [f4, f5], 3), 2)
            self.assertEqual([f["text"] for f in facts.load_facts(p)],
                             [f3["text"], f4["text"], f5["text"]])

    def test_prune_by_position_even_when_last_inserted_has_oldest_date(self):
        # Dates here are deliberately NOT monotonic with insertion order --
        # f5 is inserted last but carries the OLDEST date of the five. A
        # prune that (wrongly) kept the newest-by-DATE would drop f5 (and
        # keep f1/f2/f3, the three newest-dated); position-based FIFO
        # pruning keeps f5 because it is the newest-by-POSITION, dropping
        # f1/f2 off the front exactly as the plain-chronological fixture
        # above does. This is what actually distinguishes the two policies.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            f1 = {"text": "喜歡藍色", "category": "a", "date": "2026-07-08"}
            f2 = {"text": "住在台北", "category": "a", "date": "2026-07-09"}
            f3 = {"text": "養了一隻貓", "category": "a", "date": "2026-07-10"}
            f4 = {"text": "週末去爬山", "category": "a", "date": "2026-07-11"}
            f5 = {"text": "最近在學鋼琴", "category": "a", "date": "2026-01-01"}
            self.assertEqual(facts.append_facts(p, [f1, f2, f3], 3), 3)
            self.assertEqual(facts.append_facts(p, [f4, f5], 3), 2)
            self.assertEqual([f["text"] for f in facts.load_facts(p)],
                             [f3["text"], f4["text"], f5["text"]])

    def test_atomic_write_leaves_no_temp_litter(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts.json"
            facts.append_facts(p, [BLUE, HIKING], 200)
            self.assertEqual(list(Path(td).glob("*.tmp")), [])


# ---------------------------------------------------------------------
# 2. Extraction cursor: load_state / save_state
# ---------------------------------------------------------------------

class TestCursorState(unittest.TestCase):
    def test_missing_file_is_fresh_state(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(facts.load_state(Path(td) / "facts_state.json"),
                             {"last_extracted_ts": ""})

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts_state.json"
            facts.save_state(p, {"last_extracted_ts": "2026-07-11T10:00:00+08:00"})
            self.assertEqual(facts.load_state(p),
                             {"last_extracted_ts": "2026-07-11T10:00:00+08:00"})
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_corrupt_json_degrades_and_keeps_corpse(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts_state.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertLogs("everthine", level="WARNING"):
                state = facts.load_state(p)
            self.assertEqual(state, {"last_extracted_ts": ""})
            self.assertEqual(len(list(Path(td).glob("facts_state.json.corrupt-*"))), 1)

    def test_wrong_shape_list_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts_state.json"
            p.write_text('["not", "a", "dict"]', encoding="utf-8")
            self.assertEqual(facts.load_state(p), {"last_extracted_ts": ""})
            self.assertEqual(len(list(Path(td).glob("facts_state.json.corrupt-*"))), 1)

    def test_missing_key_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts_state.json"
            p.write_text('{"other": "x"}', encoding="utf-8")
            self.assertEqual(facts.load_state(p), {"last_extracted_ts": ""})
            self.assertEqual(len(list(Path(td).glob("facts_state.json.corrupt-*"))), 1)

    def test_non_str_value_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "facts_state.json"
            p.write_text('{"last_extracted_ts": 123}', encoding="utf-8")
            self.assertEqual(facts.load_state(p), {"last_extracted_ts": ""})
            self.assertEqual(len(list(Path(td).glob("facts_state.json.corrupt-*"))), 1)


# ---------------------------------------------------------------------
# 3. Ranking / dedup algorithms
# ---------------------------------------------------------------------

class TestExtractBigrams(unittest.TestCase):
    def test_cjk_text(self):
        self.assertEqual(facts.extract_bigrams("喜歡貓"), {"喜歡", "歡貓"})

    def test_ascii_text(self):
        self.assertEqual(facts.extract_bigrams("abcd"), {"ab", "bc", "cd"})

    def test_punctuation_and_whitespace_stripped_before_pairing(self):
        # The space and comma vanish, so "喜, 歡" pairs the same as "喜歡".
        self.assertEqual(facts.extract_bigrams("喜, 歡"), {"喜歡"})

    def test_punctuation_only_is_empty(self):
        self.assertEqual(facts.extract_bigrams("，。！？"), set())

    def test_digits_only_is_empty(self):
        self.assertEqual(facts.extract_bigrams("1 2 3"), set())

    def test_single_char_is_empty(self):
        self.assertEqual(facts.extract_bigrams("貓"), set())


class TestRelevanceScore(unittest.TestCase):
    def test_full_overlap_is_one(self):
        self.assertEqual(facts.relevance_score("喜歡貓", "喜歡貓"), 1.0)

    def test_partial_overlap(self):
        # msg {"喜歡","歡貓"}; fact {"喜歡","歡狗"}; intersection 1 / 2 = 0.5
        self.assertEqual(facts.relevance_score("喜歡貓", "喜歡狗"), 0.5)

    def test_empty_message_is_zero(self):
        self.assertEqual(facts.relevance_score("", "喜歡貓"), 0.0)

    def test_empty_fact_is_zero(self):
        self.assertEqual(facts.relevance_score("喜歡貓", ""), 0.0)

    def test_bounds(self):
        for msg, fact in [("喜歡貓咪", "喜歡貓咪"), ("喜歡貓", "討厭狗"),
                          ("abc", "xyz"), ("測試訊息", "完全不同")]:
            score = facts.relevance_score(msg, fact)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)


class TestIsDuplicate(unittest.TestCase):
    def test_exact_is_duplicate(self):
        self.assertTrue(facts.is_duplicate(BLUE["text"], BLUE["text"]))

    def test_near_dup_at_or_above_threshold(self):
        # BLUE vs GREEN: 7/9 ~= 0.78 >= 0.7
        self.assertTrue(facts.is_duplicate(BLUE["text"], GREEN["text"]))

    def test_distinct_below_threshold(self):
        # BLUE vs HIKING: 1/6 ~= 0.17 < 0.7
        self.assertFalse(facts.is_duplicate(BLUE["text"], HIKING["text"]))

    def test_either_empty_is_false(self):
        self.assertFalse(facts.is_duplicate("", "喜歡貓"))
        self.assertFalse(facts.is_duplicate("喜歡貓", ""))
        self.assertFalse(facts.is_duplicate("貓", "喜歡貓"))  # single char -> no bigrams

    def test_threshold_is_inclusive(self):
        # "貓狗鳥龜"={貓狗,狗鳥,鳥龜}(3); "貓狗魚"={貓狗,狗魚}(2); overlap 1;
        # ratio = 1 / min(3,2) = 0.5 -- pins the >= boundary.
        self.assertTrue(facts.is_duplicate("貓狗鳥龜", "貓狗魚", threshold=0.5))
        self.assertFalse(facts.is_duplicate("貓狗鳥龜", "貓狗魚", threshold=0.51))


class TestSelectFacts(unittest.TestCase):
    def test_empty_facts_is_empty(self):
        self.assertEqual(facts.select_facts("咖啡", [], 5, TODAY), [])

    def test_max_n_zero_is_empty(self):
        self.assertEqual(facts.select_facts("咖啡", [BLUE], 0, TODAY), [])

    def test_max_n_negative_is_empty(self):
        self.assertEqual(facts.select_facts("咖啡", [BLUE], -1, TODAY), [])

    def test_max_n_larger_than_facts_returns_all(self):
        out = facts.select_facts("咖啡", [BLUE, HIKING], 10, TODAY)
        self.assertEqual(len(out), 2)

    def test_weight_math_old_relevant_beats_fresh_irrelevant(self):
        # Fresh but irrelevant: relevance 0.0, recency 1.0 -> 0.0*0.6+1.0*0.4=0.4
        fresh_irrelevant = {"text": "天氣晴朗", "category": "x", "date": "2026-07-11"}
        # Old but fully relevant to "咖啡": relevance 1.0, recency 0.0 (30d ago)
        #   -> 1.0*0.6+0.0*0.4 = 0.6. Winner flips if the weights are swapped,
        #   so this pins the 0.6/0.4 constants, not just their sum.
        old_relevant = {"text": "我愛喝咖啡", "category": "x", "date": "2026-06-11"}
        out = facts.select_facts("咖啡", [fresh_irrelevant, old_relevant], 1, TODAY)
        self.assertEqual(out, [old_relevant])
        # And with room for both, the relevant one still leads.
        out2 = facts.select_facts("咖啡", [fresh_irrelevant, old_relevant], 2, TODAY)
        self.assertEqual(out2, [old_relevant, fresh_irrelevant])

    def test_missing_or_bad_date_counts_as_thirty_days_old(self):
        # With an irrelevant message, ranking is pure recency. A fact dated
        # today (recency 1.0) must outrank one with no date and one with a
        # garbage date (both treated as 30 days old -> recency 0.0).
        today_fact = {"text": "毫不相關甲", "category": "x", "date": "2026-07-11"}
        no_date = {"text": "毫不相關乙", "category": "x"}
        bad_date = {"text": "毫不相關丙", "category": "x", "date": "not-a-date"}
        out = facts.select_facts("咖啡", [no_date, bad_date, today_fact], 1, TODAY)
        self.assertEqual(out, [today_fact])

    def test_stable_for_ties(self):
        # Identical text and date -> identical score -> input order preserved.
        a = {"text": "同樣的事實", "category": "first", "date": "2026-07-11"}
        b = {"text": "同樣的事實", "category": "second", "date": "2026-07-11"}
        out = facts.select_facts("同樣的事實", [a, b], 2, TODAY)
        self.assertEqual([f["category"] for f in out], ["first", "second"])

    def test_empty_message_orders_by_recency_only(self):
        # The proactive nudge path (scheduler.nudge_once) has no incoming
        # message to rank against, so it calls select_facts with message="".
        # extract_bigrams("") is the empty set, which zeroes relevance for
        # every fact regardless of its text (already pinned in isolation by
        # TestRelevanceScore.test_empty_message_is_zero) -- this test pins
        # the consequence one level up, at select_facts itself: with
        # relevance out of the equation the 0.6/0.4 blend collapses to pure
        # recency, so the newest fact always leads. Fed deliberately out of
        # date order (oldest first) so a bug that just passed input order
        # through unsorted could not masquerade as a real recency sort.
        oldest = {"text": "she prefers tea over coffee", "category": "preference",
                 "date": "2026-06-11"}          # 30 days old -> recency 0.0
        middle = {"text": "her sister visited last month", "category": "family",
                 "date": "2026-07-01"}          # 10 days old -> recency ~0.667
        newest = {"text": "she started a pottery class", "category": "hobby",
                 "date": "2026-07-11"}          # today -> recency 1.0
        out = facts.select_facts("", [oldest, middle, newest], 3, TODAY)
        self.assertEqual(out, [newest, middle, oldest])


# ---------------------------------------------------------------------
# 4. Eligibility gate
# ---------------------------------------------------------------------

class TestEligibility(unittest.TestCase):
    def test_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_ENABLED="false")
            # disabled wins even when everything else would also block/allow.
            self.assertEqual(
                facts.eligibility(cfg, NOW, None, NOW), "disabled")

    def test_idle_not_reached_when_never_contacted(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_IDLE_MINUTES="30")
            self.assertEqual(
                facts.eligibility(cfg, NOW, None, None), "idle_not_reached")

    def test_idle_not_reached_when_gap_too_small(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_IDLE_MINUTES="30")
            last = NOW - timedelta(minutes=20)
            self.assertEqual(
                facts.eligibility(cfg, NOW, last, None), "idle_not_reached")

    def test_idle_beats_no_new_material_and_is_none_safe(self):
        # last_user_ts None must resolve to idle_not_reached, never crash on a
        # None <= cursor comparison, and never surface the confusing
        # no_new_material reason (brief's priority requirement).
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_IDLE_MINUTES="30")
            cursor = NOW - timedelta(minutes=5)
            self.assertEqual(
                facts.eligibility(cfg, NOW, None, cursor), "idle_not_reached")

    def test_no_new_material_when_cursor_at_or_after_last(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_IDLE_MINUTES="30")
            last = NOW - timedelta(minutes=40)  # idle gate passed
            cursor = NOW - timedelta(minutes=10)  # last <= cursor
            self.assertEqual(
                facts.eligibility(cfg, NOW, last, cursor), "no_new_material")

    def test_no_new_material_boundary_is_inclusive(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_IDLE_MINUTES="30")
            last = NOW - timedelta(minutes=40)
            self.assertEqual(
                facts.eligibility(cfg, NOW, last, last), "no_new_material")

    def test_allowed_when_new_material_exists(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_IDLE_MINUTES="30")
            last = NOW - timedelta(minutes=40)
            cursor = NOW - timedelta(minutes=50)  # last is newer than cursor
            self.assertIsNone(facts.eligibility(cfg, NOW, last, cursor))

    def test_allowed_when_cursor_never_set(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_IDLE_MINUTES="30")
            last = NOW - timedelta(minutes=40)
            self.assertIsNone(facts.eligibility(cfg, NOW, last, None))

    def test_idle_boundary_is_strict(self):
        # Exactly facts_idle_minutes elapsed passes the idle gate (< is strict).
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_IDLE_MINUTES="30")
            last = NOW - timedelta(minutes=30)
            self.assertIsNone(facts.eligibility(cfg, NOW, last, None))


# ---------------------------------------------------------------------
# 5. Prompt block (D1 Task 4): the few facts that bear on this message,
#    rendered for a live turn. Pure -- partner_name is a caller argument
#    (facts.py never imports persona), and `now` is passed in, not read.
# ---------------------------------------------------------------------

# A fact strongly relevant to a "coffee" message (its bigrams cover the
# message's), a fact dated today but irrelevant (recency only), and a fact
# both old and irrelevant (scores ~0). With prompt_max=2 the first two win.
_COFFEE = {"text": "her coffee is always black", "category": "preference",
           "date": "2026-07-11"}
_PIANO = {"text": "she is learning the piano now", "category": "hobby",
          "date": "2026-07-11"}
_MOTHER = {"text": "her mother is called Wen", "category": "family",
           "date": "2026-05-11"}  # ~61 days before TODAY -> recency 0.0


class TestPromptBlock(unittest.TestCase):
    def test_flag_off_returns_none_even_with_facts_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_ENABLED="false")
            facts.append_facts(cfg.facts_path, [_COFFEE], cfg.facts_max)
            self.assertIsNone(facts.prompt_block(cfg, "coffee", NOW, "Wren"))

    def test_empty_book_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)  # facts_enabled default True, no facts.json yet
            self.assertIsNone(facts.prompt_block(cfg, "coffee", NOW, "Wren"))

    def test_max_zero_selects_nothing_returns_none(self):
        # A non-empty book, but facts_prompt_max=0 -> select_facts returns []
        # -> None (never ""). Built directly because load_config rejects a
        # non-positive FACTS_PROMPT_MAX.
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(bot_token="x", authorized_user_id=1,
                         data_dir=Path(td), facts_prompt_max=0)
            facts.append_facts(cfg.facts_path, [_COFFEE], cfg.facts_max)
            self.assertIsNone(facts.prompt_block(cfg, "coffee", NOW, "Wren"))

    def test_anchor_lines_header_and_fact_line_render(self):
        # The template's two guard lines are byte-present, the header renders
        # partner_name, and each selected fact renders "- [category] text".
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            facts.append_facts(cfg.facts_path, [_COFFEE], cfg.facts_max)
            block = facts.prompt_block(cfg, "what about coffee", NOW, "Wren")
            self.assertIsNotNone(block)
            self.assertIn("# What you know about Wren", block)
            self.assertIn("outranks what you remember", block)
            self.assertIn("asking is warmer than guessing", block)
            self.assertIn("- [preference] her coffee is always black", block)

    def test_selection_wiring_top_two_by_score(self):
        # prompt_max=2 with a relevant + a recent + an old-irrelevant fact:
        # the block carries the relevant and the recent one, not the old
        # one. Membership only -- ordering is select_facts' own tested job.
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_PROMPT_MAX="2")
            facts.append_facts(cfg.facts_path, [_COFFEE, _PIANO, _MOTHER],
                               cfg.facts_max)
            block = facts.prompt_block(cfg, "what about coffee", NOW, "Wren")
            self.assertIsNotNone(block)
            self.assertIn("her coffee is always black", block)   # relevant
            self.assertIn("she is learning the piano now", block)  # recent
            self.assertNotIn("her mother is called Wen", block)   # old, dropped

    def test_never_returns_empty_string(self):
        # Every None path above returns None, never "" -- one consolidated
        # pin so a future refactor can't quietly start emitting "".
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, FACTS_ENABLED="false")
            self.assertIsNone(facts.prompt_block(cfg, "coffee", NOW, "Wren"))

    def test_template_wording_pin(self):
        # Hand-transcribed (not derived from prompt_block's output) so this
        # guards the canonical product copy against transcription drift,
        # mirroring test_dynamic_context.py's TestWordingPins.
        self.assertEqual(
            facts.FACTS_BLOCK_TEMPLATE,
            "# What you know about {partner_name}\n\n"
            "These are impressions you have quietly gathered over time — they live in\n"
            "you, not in any list you would ever mention out loud.\n\n"
            "- They may be out of date. What {partner_name} says right now always\n"
            "  outranks what you remember.\n"
            "- Never invent details these lines do not actually say. When you are\n"
            "  not sure, ask — asking is warmer than guessing.\n\n"
            "{facts_lines}")


if __name__ == "__main__":
    unittest.main()
