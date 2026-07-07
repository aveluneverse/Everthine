"""Tests for M3 Task 5: bot.py's long-term memory wiring.

Covers: recall threaded into the prompt on both reply paths (produce_reply
and stream_reply), the flag-off / file-mode-persona gates that keep today's
behavior byte-identical (the L1 rollback), the post-reply sync that only
fires after a successful reply, stream_reply's prepare_exchange/
build_system_prompt calls running off the event loop, and make_app's boot
wiring (init then a boot-time sync, both fail-soft).

Conventions follow tests/test_bot_core.py (a fake engine module that
captures the system_prompt kwarg it receives), tests/test_bot_stream.py
(ScriptedEngine + FakeDisplay streaming harness), tests/test_memory_recall.py
(dict-driven fake embedder via memory_embed.set_embed_fn, a real archive via
archive.write_entry, memory_recall.reset() + set_embed_fn(None) in
addCleanup -- including the Windows-safe cleanup ORDER: the tmp-dir cleanup
is registered first so it runs last, after memory_recall.reset() has already
closed the sqlite handle living inside that same tmp dir), and
tests/test_bot_persona_wiring.py (a tmp persona-folder fixture, and
resetting the persona cache / message overrides around any make_app call
since both are process-global).
"""
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from everthine import archive, bot, memory_embed, memory_recall, messages, persona
from everthine.config import Config
from everthine.engine import EngineReply
from everthine.session_store import SessionStore

# Aware local, fixed date, mid-afternoon -- mirrors test_memory_recall.py's
# NOW so date-boundary arithmetic never flakes.
NOW = datetime(2026, 7, 5, 14, 30, 0).astimezone()

DEFAULT_VECTOR = [0.0, 1.0]  # orthogonal fallback for any unregistered text

IDENTITY_TEXT = "I am Theo: warm, steady, and endlessly attentive to Wren.\n"
SETTINGS_YAML = """\
companion:
  name: Theo
partner:
  name: Wren
"""


def _write_persona_folder(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "identity.md").write_text(IDENTITY_TEXT, encoding="utf-8")
    (root / "settings.yaml").write_text(SETTINGS_YAML, encoding="utf-8")
    return root


def _folder_cfg(root: Path, **overrides) -> Config:
    folder = _write_persona_folder(root / "persona")
    kwargs = dict(
        bot_token="x", authorized_user_id=1, data_dir=root / "data",
        persona_path=folder, memory_enabled=True, memory_top_k=3,
        memory_embedding_model="fake-model", lookback_hours=36,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _rounds_text(rounds) -> str:
    """Mirror memory_store._chunk_text: how chunk_entries joins a
    single-chunk conversation's rounds."""
    return "\n".join(f"{speaker}: {text}" for speaker, text in rounds)


def _make_fake(mapping: dict):
    """A plain (non-recording) dict-driven fake embed function."""
    def fake(text):
        return list(mapping.get(text, DEFAULT_VECTOR))
    return fake


def _archive_and_boot_sync(cfg, rounds, start_ts, sync_now=None):
    """Write `rounds` one minute apart starting at start_ts, then sync so the
    resulting (single, closed) chunk is live-recallable -- mirrors the boot
    backfill make_app now performs."""
    ts = start_ts
    for speaker, text in rounds:
        archive.write_entry(cfg.archive_dir, speaker, text, ts=ts)
        ts = ts + timedelta(minutes=1)
    last_ts = ts - timedelta(minutes=1)
    memory_recall.sync(cfg, sync_now or (last_ts + timedelta(hours=1)))
    return last_ts


class FakeEngineOK:
    """produce_reply's run_once stand-in: captures the system_prompt kwarg
    (mirrors tests/test_bot_core.py's FakeEngineOK)."""

    def __init__(self, reply_text="nice to hear from you", session_id="sess-new"):
        self.calls = []
        self.reply_text = reply_text
        self.session_id = session_id

    def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
        self.calls.append({"prompt": prompt, "session_id": session_id,
                           "system_prompt": system_prompt})
        return EngineReply(self.reply_text, self.session_id, ok=True)


class FakeEngineFail:
    def __init__(self):
        self.calls = []

    def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
        self.calls.append({"prompt": prompt, "session_id": session_id,
                           "system_prompt": system_prompt})
        return EngineReply("", session_id, ok=False, error_kind="timeout")


class ScriptedEngine:
    """stream_once stand-in: pushes a scripted event list onto the queue and
    captures the system_prompt kwarg (mirrors tests/test_bot_stream.py)."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def stream_once(self, cfg, prompt, session_id=None, system_prompt=None,
                    events=None, cancel=None):
        self.calls.append({"prompt": prompt, "session_id": session_id,
                           "system_prompt": system_prompt})
        for event in self.script:
            events.put(event)


def ok_script(text_chunks, session_id="sess-stream"):
    events = [{"type": "text", "text": c} for c in text_chunks]
    full = "".join(text_chunks)
    events.append({"type": "done", "reply": EngineReply(full, session_id, ok=True)})
    return events


class FakeDisplay:
    def __init__(self):
        self.chunks = []
        self.finalized = False
        self.cancelled = False

    @property
    def full_text(self):
        return "".join(self.chunks)

    @property
    def message_texts(self):
        return [self.full_text] if self.chunks else []

    async def append(self, chunk):
        self.chunks.append(chunk)

    async def finalize(self):
        self.finalized = True
        return ["m"] if self.chunks else []

    async def cancel(self):
        self.cancelled = True
        return ["m"] if self.chunks else []


class _MemoryWiringTestCase(unittest.TestCase):
    """Base for the sync-path tests: a tmp dir plus every process-global
    reset a make_app/produce_reply call could touch. Registration order
    matters on Windows -- the tmp-dir cleanup is registered FIRST so it runs
    LAST (addCleanup is LIFO), after memory_recall.reset() has already closed
    the sqlite handle living inside that same tmp dir (test_memory_recall.py's
    own base class documents this same trap).
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        self.addCleanup(memory_embed.set_embed_fn, None)
        self.addCleanup(persona.reset_persona_cache)
        self.addCleanup(messages.reset_overrides)
        memory_recall.reset()
        memory_embed.set_embed_fn(None)
        persona.reset_persona_cache()
        messages.reset_overrides()
        self.root = Path(self._td.name)


# --- 1. End-to-end recall hit reaches the captured prompt (sync path) ------

class TestRecallHitReachesPrompt(_MemoryWiringTestCase):
    def test_old_matching_conversation_surfaces_before_final_check(self):
        cfg = _folder_cfg(self.root)
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
        _archive_and_boot_sync(cfg, rounds, NOW - timedelta(days=5))

        store = SessionStore(cfg.session_path)
        eng = FakeEngineOK()
        bot.produce_reply(cfg, store, query, now=NOW, engine_mod=eng)

        prompt = eng.calls[0]["system_prompt"]
        self.assertIn(memory_recall.MEMORY_BLOCK_HEADER, prompt)
        self.assertIn("Wren: we watched the sunset from the rooftop", prompt)
        self.assertIn("Theo: that was one of my favorite nights", prompt)
        self.assertLess(prompt.index(memory_recall.MEMORY_BLOCK_HEADER),
                        prompt.index("# Before you speak (last check)"))


# --- 2. Orthogonal query never surfaces a memory block (sync path) --------

class TestRecallMissLeavesPromptClean(_MemoryWiringTestCase):
    def test_orthogonal_query_has_no_memory_block(self):
        cfg = _folder_cfg(self.root)
        rounds = [("user", "we watched the sunset from the rooftop together")]
        query = "what is the capital of France"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [0.0, 1.0],  # cos = 0
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        _archive_and_boot_sync(cfg, rounds, NOW - timedelta(days=5))

        store = SessionStore(cfg.session_path)
        eng = FakeEngineOK()
        bot.produce_reply(cfg, store, query, now=NOW, engine_mod=eng)

        prompt = eng.calls[0]["system_prompt"]
        self.assertNotIn(memory_recall.MEMORY_BLOCK_HEADER, prompt)


# --- 3. Flag off: no block, no db file, byte-identical to the L1 baseline --

class TestFlagOffL1Pin(_MemoryWiringTestCase):
    def _cfg(self, data_dir: Path) -> Config:
        persona_file = self.root / "persona.md"
        if not persona_file.exists():
            persona_file.write_text("You are Testbot, warm and steady.",
                                    encoding="utf-8")
        return Config(bot_token="x", authorized_user_id=1, data_dir=data_dir,
                     persona_path=persona_file, memory_enabled=False)

    def test_disabled_flag_no_block_no_db_matches_no_memory_baseline(self):
        cfg = self._cfg(self.root / "data")
        store = SessionStore(cfg.session_path)
        eng = FakeEngineOK()

        out = bot.produce_reply(cfg, store, "hello", now=NOW, engine_mod=eng)

        self.assertFalse(cfg.memory_db_path.exists())
        prompt = eng.calls[0]["system_prompt"]
        self.assertNotIn(memory_recall.MEMORY_BLOCK_HEADER, prompt)
        # L1 pin: byte-identical to the pre-T5 call shape (build_system_prompt
        # with no memory_block threaded through at all).
        self.assertEqual(prompt, persona.build_system_prompt(cfg))

        # A second, wholly independent run (fresh cfg/store/engine, memory
        # never so much as touched) reproduces the exact same output --
        # proves this wiring adds no observable side effect when disabled.
        cfg2 = self._cfg(self.root / "data-baseline")
        store2 = SessionStore(cfg2.session_path)
        eng2 = FakeEngineOK()
        out2 = bot.produce_reply(cfg2, store2, "hello", now=NOW, engine_mod=eng2)
        self.assertEqual(out, out2)
        self.assertEqual(eng.calls[0]["system_prompt"], eng2.calls[0]["system_prompt"])


# --- 4. File-mode persona gates recall out even with a matching memory ----

class TestFileModePersonaNoMemoryBlock(_MemoryWiringTestCase):
    def test_file_mode_persona_gates_out_memory_block_reply_still_works(self):
        persona_file = self.root / "persona.md"
        persona_file.write_text("You are Testbot, warm and steady.", encoding="utf-8")
        cfg = Config(bot_token="x", authorized_user_id=1,
                     data_dir=self.root / "data", persona_path=persona_file,
                     memory_enabled=True, memory_embedding_model="fake-model")
        # A conversation that WOULD match if settings were available -- this
        # proves the file-mode gate (current_settings() -> None), not merely
        # an absence of matching data.
        rounds = [("user", "we watched the sunset from the rooftop")]
        query = "remember that rooftop evening?"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [0.98, 0.199],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        _archive_and_boot_sync(cfg, rounds, NOW - timedelta(days=5))

        store = SessionStore(cfg.session_path)
        eng = FakeEngineOK()
        out = bot.produce_reply(cfg, store, query, now=NOW, engine_mod=eng)

        prompt = eng.calls[0]["system_prompt"]
        self.assertNotIn(memory_recall.MEMORY_BLOCK_HEADER, prompt)
        self.assertEqual(out, ["nice to hear from you"])
        self.assertEqual(prompt, persona.build_system_prompt(cfg))


# --- 5/6. Sync fires exactly once after success, never after failure ------

class TestSyncFiresOnlyAfterSuccess(_MemoryWiringTestCase):
    def test_sync_called_once_with_now_after_successful_reply(self):
        cfg = _folder_cfg(self.root)
        store = SessionStore(cfg.session_path)

        with mock.patch.object(memory_recall, "sync") as mock_sync:
            bot.produce_reply(cfg, store, "hello", now=NOW, engine_mod=FakeEngineOK())

        mock_sync.assert_called_once_with(cfg, NOW)

    def test_sync_not_called_when_engine_fails(self):
        cfg = _folder_cfg(self.root)
        store = SessionStore(cfg.session_path)

        with mock.patch.object(memory_recall, "sync") as mock_sync:
            bot.produce_reply(cfg, store, "hello", now=NOW, engine_mod=FakeEngineFail())

        mock_sync.assert_not_called()


# --- 7. Streaming parity: memory in the prompt, prepare/build off-loop ----

class TestStreamingMemoryOffLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        self.addCleanup(memory_embed.set_embed_fn, None)
        self.addCleanup(persona.reset_persona_cache)
        self.addCleanup(messages.reset_overrides)
        memory_recall.reset()
        memory_embed.set_embed_fn(None)
        persona.reset_persona_cache()
        messages.reset_overrides()
        self.root = Path(self._td.name)

    async def test_prepare_exchange_and_build_system_prompt_run_off_loop(self):
        cfg = _folder_cfg(self.root)
        rounds = [
            ("user", "we watched the sunset from the rooftop"),
            ("companion", "that was one of my favorite nights"),
        ]
        query = "remember that rooftop evening?"
        fake = _make_fake({
            _rounds_text(rounds): [1.0, 0.0],
            query: [0.98, 0.199],
        })
        memory_embed.set_embed_fn(fake)
        memory_recall.init(cfg)
        _archive_and_boot_sync(cfg, rounds, NOW - timedelta(days=5))

        store = SessionStore(cfg.session_path)
        eng = ScriptedEngine(ok_script(["ok."]))
        display = FakeDisplay()
        cancel = threading.Event()

        thread_flags = {}
        sync_calls = []
        real_prepare_exchange = bot.prepare_exchange
        real_build_system_prompt = persona.build_system_prompt
        real_sync = memory_recall.sync

        def wrapped_prepare_exchange(cfg_, store_, text_, now_):
            thread_flags["prepare_exchange_on_main"] = (
                threading.current_thread() is threading.main_thread())
            return real_prepare_exchange(cfg_, store_, text_, now_)

        def wrapped_build_system_prompt(cfg_, memory_block=None, inner_block=None):
            # M5 T7 added build_system_prompt's inner_block param, which the
            # streaming path now threads through; this pass-through test double
            # tracks that arity (assertions below are unchanged -- it still only
            # proves the call runs off the event loop).
            thread_flags["build_system_prompt_on_main"] = (
                threading.current_thread() is threading.main_thread())
            return real_build_system_prompt(cfg_, memory_block, inner_block)

        def wrapped_sync(cfg_, now_):
            sync_calls.append((cfg_, now_))
            return real_sync(cfg_, now_)

        with mock.patch.object(bot, "prepare_exchange",
                               side_effect=wrapped_prepare_exchange), \
             mock.patch.object(persona, "build_system_prompt",
                               side_effect=wrapped_build_system_prompt), \
             mock.patch.object(memory_recall, "sync", side_effect=wrapped_sync):
            reply = await bot.stream_reply(cfg, store, query, display, cancel,
                                           now=NOW, engine_mod=eng)

        self.assertTrue(reply.ok)
        prompt = eng.calls[0]["system_prompt"]
        self.assertIn(memory_recall.MEMORY_BLOCK_HEADER, prompt)
        self.assertIn("Wren: we watched the sunset from the rooftop", prompt)
        # Structural proof both ran off the event loop, in a worker thread.
        self.assertFalse(thread_flags["prepare_exchange_on_main"])
        self.assertFalse(thread_flags["build_system_prompt_on_main"])
        self.assertEqual(len(sync_calls), 1)
        self.assertEqual(sync_calls[0], (cfg, NOW))


# --- 8. make_app boot wiring: init then sync, exactly once, fail-soft -----

class TestMakeAppBootWiring(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        self.addCleanup(memory_embed.set_embed_fn, None)
        self.addCleanup(persona.reset_persona_cache)
        self.addCleanup(messages.reset_overrides)
        memory_recall.reset()
        memory_embed.set_embed_fn(None)
        persona.reset_persona_cache()
        messages.reset_overrides()
        self.root = Path(self._td.name)

    def test_init_then_sync_exactly_once_in_order(self):
        cfg = Config(bot_token="x", authorized_user_id=1,
                     data_dir=self.root / "data",
                     memory_enabled=True, memory_embedding_model="fake-model")
        memory_embed.set_embed_fn(_make_fake({}))

        order = []
        real_init = memory_recall.init
        real_sync = memory_recall.sync

        def wrapped_init(cfg_):
            order.append("init")
            return real_init(cfg_)

        def wrapped_sync(cfg_, now_):
            order.append("sync")
            return real_sync(cfg_, now_)

        with mock.patch.object(memory_recall, "init",
                               side_effect=wrapped_init) as mock_init, \
             mock.patch.object(memory_recall, "sync",
                               side_effect=wrapped_sync) as mock_sync:
            app = bot.make_app(cfg)

        self.assertIsNotNone(app)
        self.assertEqual(mock_init.call_count, 1)
        self.assertEqual(mock_sync.call_count, 1)
        self.assertEqual(order, ["init", "sync"])

    def test_broken_memory_init_still_returns_app_fail_soft(self):
        def raising_fake(text):
            raise RuntimeError("embedding backend exploded")
        memory_embed.set_embed_fn(raising_fake)

        cfg = Config(bot_token="x", authorized_user_id=1,
                     data_dir=self.root / "data",
                     memory_enabled=True, memory_embedding_model="fake-model")

        app = bot.make_app(cfg)  # must not raise

        self.assertIsNotNone(app)
        self.assertTrue(memory_recall._disabled)

    def test_flag_off_boot_creates_no_db_and_disables_module(self):
        cfg = Config(bot_token="x", authorized_user_id=1,
                     data_dir=self.root / "data",
                     memory_enabled=False, memory_embedding_model="fake-model")
        memory_embed.set_embed_fn(_make_fake({}))

        app = bot.make_app(cfg)

        self.assertIsNotNone(app)
        self.assertFalse(cfg.memory_db_path.exists())
        self.assertTrue(memory_recall._disabled)


if __name__ == "__main__":
    unittest.main()
