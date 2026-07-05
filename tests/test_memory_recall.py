"""Tests for the M3 recall module: the in-memory vector index, cosine
ranking, deadline fuse, and the injected memory block. Conventions follow
tests/test_memory_store.py (tmp archive + db dirs, addCleanup ordering so
sqlite handles close before Windows deletes the tmp dir) and
tests/test_persona_assembly_wiring.py (Config built directly, PersonaSettings
built directly, a fixed mid-afternoon NOW so date-boundary arithmetic never
flakes).

Fake-embedder strategy (dict-driven, per the brief): every text used in a
test -- the literal "warmup" probe text, each chunk's exact joined text, and
each query string -- is registered in a small dict mapping text -> a hand
-picked, already-near-unit vector. Unregistered text falls back to a fixed
orthogonal default, so a typo or an unaccounted-for embed call reads as a
score of ~0 rather than an accidental match. This makes every similarity
assertion exact: no real model, no floating-point surprises.

Two structural traps this file works around, both load-bearing for
correctness (not just tidiness):

1. memory_recall.py owns process-global state (the module singleton) plus a
   SHARED, never-recreated ThreadPoolExecutor(max_workers=1). reset() clears
   the singleton's fields but never touches the executor, so a straggler
   worker thread left running past its test (the deadline-fuse case) would
   otherwise sit in the executor's single slot and delay -- or, worse,
   spuriously starve -- the next test's own recall_block() call. The deadline
   test sleeps past its own straggler before finishing, so the executor is
   guaranteed idle before the next test claims it.

2. MemoryStore's sync cursor only ever advances forward. Seeding an "old"
   conversation AFTER a "recent" one has already been synced would land the
   old entry's timestamp behind the cursor and get silently skipped. Every
   multi-conversation test therefore seeds in chronological order (oldest
   sync call first), never the reverse.
"""
import contextlib
import dataclasses
import io
import math
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from everthine import archive, memory_embed, memory_recall
from everthine.config import Config
from everthine.memory_store import MemoryStore
from everthine.persona import PersonaSettings

# Aware local, fixed date, mid-afternoon -- subtracting days/hours below never
# straddles a local-midnight boundary, so every "date" computed from these
# fixtures is unambiguous (mirrors test_persona_assembly_wiring.py's NOW).
NOW = datetime(2026, 7, 5, 14, 30, 0).astimezone()

MODEL = "fake-model"
DEFAULT_VECTOR = [0.0, 1.0]  # orthogonal fallback for any unregistered text


def _to_naive_local(ts: datetime) -> datetime:
    """Test-side mirror of the module's own normalization, used only to
    compute the expected "date" string for assertions."""
    if ts.tzinfo is not None and ts.tzinfo.utcoffset(ts) is not None:
        return ts.astimezone().replace(tzinfo=None)
    return ts


def _date_str(ts: datetime) -> str:
    return _to_naive_local(ts).isoformat()[:10]


def _rounds_text(rounds) -> str:
    """Mirror memory_store._chunk_text: how chunk_entries joins a
    single-chunk conversation's rounds. Every fixture below stays well
    under the gap/round/size limits, so it always collapses to one chunk
    whose text is exactly this join."""
    return "\n".join(f"{speaker}: {text}" for speaker, text in rounds)


def _make_fake(mapping: dict):
    """A plain (non-recording) dict-driven fake embed function."""
    def fake(text):
        return list(mapping.get(text, DEFAULT_VECTOR))
    return fake


class _RecordingFake:
    """Dict-driven fake that also records every text it was called with, so
    a gate test can assert the embed step was never reached."""

    def __init__(self, mapping: dict):
        self.mapping = mapping
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        return list(self.mapping.get(text, DEFAULT_VECTOR))


def _unit(cos_val: float):
    """An exact 2D unit vector at the given cosine relative to [1.0, 0.0]."""
    return [cos_val, math.sqrt(1 - cos_val ** 2)]


class _MemoryRecallTestCase(unittest.TestCase):
    """Base for every recall test: an isolated tmp dir plus the two required
    cleanups (module reset, embed seam restore). Registration order matters
    on Windows -- the tmp-dir cleanup is registered FIRST so it runs LAST
    (addCleanup is LIFO), after memory_recall.reset() has already closed the
    sqlite handle living inside that same tmp dir.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        self.addCleanup(memory_embed.set_embed_fn, None)
        memory_recall.reset()
        memory_embed.set_embed_fn(None)
        self.data_dir = Path(self._td.name)
        self.settings = PersonaSettings(companion_name="Theo", partner_name="Wren")

    def _cfg(self, **overrides) -> Config:
        kwargs = dict(
            bot_token="x", authorized_user_id=1, data_dir=self.data_dir,
            memory_enabled=True, memory_top_k=3, memory_embedding_model=MODEL,
            lookback_hours=36,
        )
        kwargs.update(overrides)
        return Config(**kwargs)

    def _archive_and_sync(self, cfg: Config, rounds, start_ts: datetime, sync_now=None):
        """Write `rounds` (speaker, text) pairs one minute apart starting at
        start_ts, then sync so the resulting (single, closed) chunk is live
        -recallable. Returns the last entry's timestamp."""
        ts = start_ts
        for speaker, text in rounds:
            archive.write_entry(cfg.archive_dir, speaker, text, ts=ts)
            ts = ts + timedelta(minutes=1)
        last_ts = ts - timedelta(minutes=1)
        memory_recall.sync(cfg, sync_now or (last_ts + timedelta(hours=1)))
        return last_ts


# --- 1. End-to-end hit ----------------------------------------------------

class TestEndToEndHit(_MemoryRecallTestCase):
    def test_old_matching_conversation_surfaces_revoiced(self):
        cfg = self._cfg()
        rounds = [
            ("user", "we watched the sunset from the rooftop"),
            ("companion", "that was one of my favorite nights"),
        ]
        query = "remember that rooftop evening?"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [0.98, 0.199],  # cos ~ 0.98, well above MIN_SIMILARITY
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        old_ts = NOW - timedelta(days=5)
        self._archive_and_sync(cfg, rounds, old_ts)

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)

        self.assertIsNotNone(result)
        self.assertIn(memory_recall.MEMORY_BLOCK_HEADER, result)
        self.assertIn(f"- {_date_str(old_ts)}:", result)
        self.assertIn("Wren: we watched the sunset from the rooftop", result)
        self.assertIn("Theo: that was one of my favorite nights", result)
        self.assertNotIn("user:", result)
        self.assertNotIn("companion:", result)


# --- 2. Miss ----------------------------------------------------------------

class TestMiss(_MemoryRecallTestCase):
    def test_orthogonal_query_returns_none(self):
        cfg = self._cfg()
        rounds = [("user", "we watched the sunset from the rooftop together")]
        query = "what is the capital of France"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [0.0, 1.0],  # cos = 0
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        self._archive_and_sync(cfg, rounds, NOW - timedelta(days=5))

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)
        self.assertIsNone(result)


# --- 3. Gates (each without touching the executor) -------------------------

class TestGates(_MemoryRecallTestCase):
    def _seeded_cfg_and_recorder(self):
        cfg = self._cfg()
        rounds = [("user", "an old, perfectly matchable conversation right here")]
        recorder = _RecordingFake({
            _rounds_text(rounds): [1.0, 0.0],
            "a real enough question": [1.0, 0.0],
        })
        memory_embed.set_embed_fn(recorder)
        memory_recall.init(cfg)
        self._archive_and_sync(cfg, rounds, NOW - timedelta(days=5))
        return cfg, recorder

    def test_flag_off_returns_none_without_embed_call(self):
        cfg, recorder = self._seeded_cfg_and_recorder()
        calls_before = len(recorder.calls)
        cfg_off = dataclasses.replace(cfg, memory_enabled=False)

        result = memory_recall.recall_block(cfg_off, "a real enough question", NOW, self.settings)

        self.assertIsNone(result)
        self.assertEqual(len(recorder.calls), calls_before)

    def test_settings_none_returns_none_without_embed_call(self):
        cfg, recorder = self._seeded_cfg_and_recorder()
        calls_before = len(recorder.calls)

        result = memory_recall.recall_block(cfg, "a real enough question", NOW, None)

        self.assertIsNone(result)
        self.assertEqual(len(recorder.calls), calls_before)

    def test_short_query_returns_none_without_embed_call(self):
        cfg, recorder = self._seeded_cfg_and_recorder()
        calls_before = len(recorder.calls)

        result = memory_recall.recall_block(cfg, "ok", NOW, self.settings)

        self.assertIsNone(result)
        self.assertEqual(len(recorder.calls), calls_before)


# --- 4. Window upper bound (warmth/no-overlap pin) --------------------------

class TestWindowUpperBound(_MemoryRecallTestCase):
    def test_recent_excluded_old_included_same_content(self):
        cfg = self._cfg()
        shared_text = "we had that lovely dinner by the window and talked for hours"
        rounds = [("user", shared_text)]
        query = "tell me about that dinner"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [1.0, 0.0],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)

        # Chronological order matters: the sync cursor only moves forward.
        old_ts = NOW - timedelta(days=5)
        self._archive_and_sync(cfg, rounds, old_ts)
        recent_ts = NOW - timedelta(hours=1)
        self._archive_and_sync(cfg, rounds, recent_ts, sync_now=NOW)

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)

        self.assertIsNotNone(result)
        self.assertIn(_date_str(old_ts), result)
        self.assertNotIn(_date_str(recent_ts), result)


# --- 5. Long-term permanence pin --------------------------------------------

class TestLongTermPermanence(_MemoryRecallTestCase):
    def test_200_day_old_matching_chunk_still_surfaces(self):
        cfg = self._cfg()
        rounds = [("user", "a memory from a very long time ago that still matters")]
        query = "does anything from back then still matter"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [0.98, 0.199],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        old_ts = NOW - timedelta(days=200)
        self._archive_and_sync(cfg, rounds, old_ts)

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)

        self.assertIsNotNone(result)
        self.assertIn(_date_str(old_ts), result)


# --- 6. top_k + threshold ----------------------------------------------------

class TestTopKAndThreshold(_MemoryRecallTestCase):
    def test_best_two_kept_in_score_order_below_threshold_excluded(self):
        cfg = self._cfg(memory_top_k=2)
        rounds_hi = [("user", "MARKHI the beach trip we took that whole golden week")]
        rounds_mid = [("user", "MARKMID the apartment hunting weekend we spent together")]
        rounds_lo = [("user", "MARKLO your work project troubles you told me about")]
        rounds_below = [("user", "MARKBELOW some totally unrelated old chatter here")]
        query = "what do you remember about us"
        fake = _make_fake({
            _rounds_text(rounds_hi): _unit(0.95),
            _rounds_text(rounds_mid): _unit(0.80),
            _rounds_text(rounds_lo): _unit(0.60),
            _rounds_text(rounds_below): _unit(0.30),
            query: [1.0, 0.0],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        base = NOW - timedelta(days=10)
        self._archive_and_sync(cfg, rounds_hi, base)
        self._archive_and_sync(cfg, rounds_mid, base + timedelta(hours=1))
        self._archive_and_sync(cfg, rounds_lo, base + timedelta(hours=2))
        self._archive_and_sync(cfg, rounds_below, base + timedelta(hours=3))

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)

        self.assertIsNotNone(result)
        self.assertIn("MARKHI", result)
        self.assertIn("MARKMID", result)
        self.assertNotIn("MARKLO", result)      # rank 3, cut by top_k=2
        self.assertNotIn("MARKBELOW", result)   # below MIN_SIMILARITY
        self.assertLess(result.index("MARKHI"), result.index("MARKMID"))  # score order


# --- 7. Tie-break: newer ts first -------------------------------------------

class TestTieBreak(_MemoryRecallTestCase):
    def test_equal_scores_newer_timestamp_first(self):
        cfg = self._cfg(memory_top_k=2)
        rounds_older = [("user", "MARKOLDER a shared memory with an identical score")]
        rounds_newer = [("user", "MARKNEWER a shared memory with an identical score too")]
        query = "what do you remember"
        same_vec = _unit(0.90)
        fake = _make_fake({
            _rounds_text(rounds_older): list(same_vec),
            _rounds_text(rounds_newer): list(same_vec),
            query: [1.0, 0.0],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        older_ts = NOW - timedelta(days=10)
        newer_ts = NOW - timedelta(days=9)
        self._archive_and_sync(cfg, rounds_older, older_ts)
        self._archive_and_sync(cfg, rounds_newer, newer_ts)

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)

        self.assertIsNotNone(result)
        self.assertIn("MARKOLDER", result)
        self.assertIn("MARKNEWER", result)
        self.assertLess(result.index("MARKNEWER"), result.index("MARKOLDER"))


# --- 8. Deadline fuse --------------------------------------------------------

class TestDeadlineFuse(_MemoryRecallTestCase):
    def test_slow_embed_times_out_promptly_and_warns(self):
        cfg = self._cfg()
        rounds = [("user", "a memory the deadline fuse must never wait around for")]
        fast_fake = _make_fake({_rounds_text(rounds): [1.0, 0.0]})
        memory_embed.set_embed_fn(fast_fake)
        memory_recall.init(cfg)
        self._archive_and_sync(cfg, rounds, NOW - timedelta(days=2))

        def slow_fake(text):
            time.sleep(memory_recall.RECALL_DEADLINE_S + 0.3)
            return [1.0, 0.0]
        memory_embed.set_embed_fn(slow_fake)

        start = time.monotonic()
        with self.assertLogs("everthine", level="WARNING"):
            result = memory_recall.recall_block(cfg, "does this ever come back", NOW, self.settings)
        elapsed = time.monotonic() - start

        self.assertIsNone(result)
        self.assertLess(elapsed, 2.0)

        # Let the straggler worker thread finish sleeping before the next
        # test claims the single-worker executor (see module docstring #1).
        time.sleep(memory_recall.RECALL_DEADLINE_S + 0.5)


# --- 9. Fail-soft init --------------------------------------------------------

class TestFailSoftInit(_MemoryRecallTestCase):
    def test_warmup_embed_raises_init_does_not_raise_and_disables(self):
        cfg = self._cfg()

        def raising_fake(text):
            raise RuntimeError("embedding backend exploded")
        memory_embed.set_embed_fn(raising_fake)

        with self.assertLogs("everthine", level="ERROR") as cm:
            memory_recall.init(cfg)  # must not raise
        self.assertTrue(any("chat continues" in line for line in cm.output))

        self.assertIsNone(memory_recall.recall_block(cfg, "a real enough question", NOW, self.settings))
        memory_recall.sync(cfg, NOW)  # must not raise; no-op


# --- 10. Sync appends live ----------------------------------------------------

class TestSyncAppendsLive(_MemoryRecallTestCase):
    def test_newly_synced_chunk_is_recallable_immediately(self):
        cfg = self._cfg()
        rounds_a = [("user", "MARKA the first old closed conversation we shared")]
        rounds_b = [("user", "MARKB the second old conversation added after init")]
        query = "what do you remember"
        fake = _make_fake({
            _rounds_text(rounds_a): [1.0, 0.0],
            _rounds_text(rounds_b): [1.0, 0.0],
            query: [1.0, 0.0],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        base = NOW - timedelta(days=10)
        self._archive_and_sync(cfg, rounds_a, base)

        result_before = memory_recall.recall_block(cfg, query, NOW, self.settings)
        self.assertIsNotNone(result_before)
        self.assertIn("MARKA", result_before)
        self.assertNotIn("MARKB", result_before)

        self._archive_and_sync(cfg, rounds_b, base + timedelta(hours=1))

        result_after = memory_recall.recall_block(cfg, query, NOW, self.settings)
        self.assertIsNotNone(result_after)
        self.assertIn("MARKA", result_after)
        self.assertIn("MARKB", result_after)


# --- 11. Excerpt cap ----------------------------------------------------------

class TestExcerptCap(_MemoryRecallTestCase):
    def test_long_excerpt_capped_control_chars_stripped_newline_survives(self):
        cfg = self._cfg()
        bell = "\x07"
        long_text = "firstline" + bell + "\nsecond line " + ("pad" * 200)
        rounds = [("user", long_text)]
        query = "tell me the long story"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [0.98, 0.199],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        old_ts = NOW - timedelta(days=5)
        self._archive_and_sync(cfg, rounds, old_ts)

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)

        self.assertIsNotNone(result)
        self.assertNotIn(bell, result)
        self.assertIn("firstline\nsecond line", result)  # embedded newline survives

        prefix = f"- {_date_str(old_ts)}: Wren: "
        self.assertIn(prefix, result)
        excerpt_part = result[result.index(prefix) + len(prefix):]
        self.assertTrue(excerpt_part.endswith("…"))
        self.assertLessEqual(len(excerpt_part), memory_recall.EXCERPT_MAX_CHARS + 1)


# --- 12. Block cap --------------------------------------------------------

class TestBlockCap(_MemoryRecallTestCase):
    def test_block_capped_lowest_scored_items_dropped(self):
        cfg = self._cfg(memory_top_k=6)
        query = "what do you remember about all of it"
        mapping = {query: [1.0, 0.0]}
        rounds_by_marker = {}
        cosines = [0.95, 0.90, 0.85, 0.80, 0.75, 0.70]
        for i, cos_val in enumerate(cosines):
            marker = f"MARK{i}"
            body = f"{marker} " + ("x" * 390)
            rounds = [("user", body)]
            rounds_by_marker[marker] = rounds
            mapping[_rounds_text(rounds)] = _unit(cos_val)
        fake = _make_fake(mapping)
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        base = NOW - timedelta(days=10)
        for i, marker in enumerate(rounds_by_marker):
            self._archive_and_sync(cfg, rounds_by_marker[marker], base + timedelta(hours=i))

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)

        self.assertIsNotNone(result)
        self.assertLessEqual(len(result), memory_recall.BLOCK_MAX_CHARS)
        self.assertIn("MARK0", result)      # highest score always kept
        self.assertNotIn("MARK5", result)   # lowest score dropped first


# --- 13. Forbidden-words pin ------------------------------------------------

class TestForbiddenWords(unittest.TestCase):
    def test_templates_contain_no_banned_words(self):
        combined = (memory_recall.MEMORY_BLOCK_HEADER + memory_recall.MEMORY_ITEM_TEMPLATE).lower()
        for banned in ("database", "search", "retrieval", "index", "lookup"):
            self.assertNotIn(banned, combined)


# --- 14. Disabled module (never inited) -------------------------------------

class TestDisabledNeverInited(_MemoryRecallTestCase):
    def test_never_inited_recall_and_sync_are_safe_no_ops(self):
        cfg = self._cfg()
        # setUp already called reset(); this is never inited in this test.
        self.assertIsNone(memory_recall.recall_block(cfg, "a real enough question", NOW, self.settings))
        memory_recall.sync(cfg, NOW)  # must not raise


# --- 15. numpy-tolerance pin --------------------------------------------------

class TestNumpyTolerance(_MemoryRecallTestCase):
    def test_np_none_fail_softs_init_without_requiring_real_numpy(self):
        cfg = self._cfg()
        with mock.patch.object(memory_recall, "np", None):
            with self.assertLogs("everthine", level="ERROR") as cm:
                memory_recall.init(cfg)  # must not raise
            self.assertTrue(any("numpy" in line.lower() for line in cm.output))
            self.assertIsNone(memory_recall._store)
            self.assertIsNone(memory_recall.recall_block(cfg, "a real enough question", NOW, self.settings))
            memory_recall.sync(cfg, NOW)  # must not raise


# --- Extra: MIN_CHUNK_CHARS floor (documented "source lesson" constant) ----

class TestMinChunkCharsFloor(_MemoryRecallTestCase):
    def test_tiny_chunk_excluded_even_with_perfect_match_vector(self):
        cfg = self._cfg()
        rounds = [("user", "hi")]  # 2 chars, well under MIN_CHUNK_CHARS=10
        query = "hi again"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [1.0, 0.0],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        self._archive_and_sync(cfg, rounds, NOW - timedelta(days=5))

        result = memory_recall.recall_block(cfg, query, NOW, self.settings)
        self.assertIsNone(result)


# --- 16. Probe read-only guards (F1) ----------------------------------------

class TestProbeReadOnlyGuards(_MemoryRecallTestCase):
    """_run_probe must be read-only in fact: a missing store is never
    created, and a store whose recorded model no longer matches cfg is
    never handed to init() (which would call ensure_model() and wipe it)."""

    def test_missing_db_prints_no_store_message_and_creates_no_file(self):
        cfg = self._cfg()
        self.assertFalse(cfg.memory_db_path.exists())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            memory_recall._run_probe("does anything match", cfg=cfg)

        self.assertIn("no memory store yet", buf.getvalue())
        self.assertFalse(cfg.memory_db_path.exists())

    def test_mismatched_stored_model_leaves_store_untouched(self):
        cfg_a = self._cfg(memory_embedding_model="model-a")
        rounds = [("user", "a chunk stored under the old embedding model")]
        fake = _make_fake({_rounds_text(rounds): [1.0, 0.0]})
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg_a)
        self._archive_and_sync(cfg_a, rounds, NOW - timedelta(days=5))
        memory_recall.reset()  # close the store, the way a bot restart would

        inspect_before = MemoryStore(cfg_a.memory_db_path)
        chunk_count_before = len(inspect_before.load_all())
        cursor_before = inspect_before._get_meta("cursor_ts")
        inspect_before.close()
        self.assertEqual(chunk_count_before, 1)
        self.assertIsNotNone(cursor_before)

        # Same db, but cfg now points at a different embedding model --
        # the .env-changed-since-last-boot scenario the probe must catch.
        cfg_b = self._cfg(memory_embedding_model="model-b")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            memory_recall._run_probe("a chunk", cfg=cfg_b)

        self.assertIn("model in .env differs from the store", buf.getvalue())

        inspect_after = MemoryStore(cfg_a.memory_db_path)
        self.assertEqual(len(inspect_after.load_all()), chunk_count_before)
        self.assertEqual(inspect_after._get_meta("cursor_ts"), cursor_before)
        inspect_after.close()


if __name__ == "__main__":
    unittest.main()
