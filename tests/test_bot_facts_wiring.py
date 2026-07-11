"""Tests for D1 Task 4: bot.py's structured-facts wiring.

Covers: the facts_block threaded into the prompt on both reply paths
(produce_reply and stream_reply), the flag-off gate that keeps today's
behavior byte-identical (the L1 rollback), and the fail-soft production in
prepare_exchange (a raising prompt_block must never take the reply down).

Conventions follow tests/test_bot_memory_wiring.py: a tmp persona folder,
a fake engine that captures the system_prompt kwarg it receives, and the
process-global resets (persona cache, message overrides) around any call
that loads a persona. Facts are seeded straight onto disk via
facts.append_facts -- no engine, no extractor.
"""
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from everthine import archive, bot, facts, memory_recall, messages, persona
from everthine.config import Config
from everthine.engine import EngineReply
from everthine.session_store import SessionStore

# Aware local, fixed date -> now.date() == 2026-07-05, so a fact dated
# "2026-07-05" reads as today (full recency).
NOW = datetime(2026, 7, 5, 14, 30, 0).astimezone()

IDENTITY_TEXT = "I am Theo: warm, steady, and endlessly attentive to Wren.\n"
SETTINGS_YAML = """\
companion:
  name: Theo
partner:
  name: Wren
"""

COFFEE_FACT = {"text": "her coffee is always black", "category": "preference",
               "date": "2026-07-05"}


def _write_persona_folder(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "identity.md").write_text(IDENTITY_TEXT, encoding="utf-8")
    (root / "settings.yaml").write_text(SETTINGS_YAML, encoding="utf-8")
    return root


def _folder_cfg(root: Path, **overrides) -> Config:
    folder = _write_persona_folder(root / "persona")
    kwargs = dict(
        bot_token="x", authorized_user_id=1, data_dir=root / "data",
        persona_path=folder, memory_enabled=False, diary_enabled=False,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


class FakeEngineOK:
    """produce_reply's run_once stand-in: captures the system_prompt kwarg."""

    def __init__(self, reply_text="nice to hear from you", session_id="sess-new"):
        self.calls = []
        self.reply_text = reply_text
        self.session_id = session_id

    def run_once(self, cfg, prompt, session_id=None, system_prompt=None):
        self.calls.append({"prompt": prompt, "session_id": session_id,
                           "system_prompt": system_prompt})
        return EngineReply(self.reply_text, self.session_id, ok=True)


class ScriptedEngine:
    """stream_once stand-in: pushes a scripted event list and captures the
    system_prompt kwarg (mirrors tests/test_bot_stream.py)."""

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

    @property
    def full_text(self):
        return "".join(self.chunks)

    @property
    def message_texts(self):
        return [self.full_text] if self.chunks else []

    async def append(self, chunk):
        self.chunks.append(chunk)

    async def finalize(self):
        return ["m"] if self.chunks else []

    async def cancel(self):
        return ["m"] if self.chunks else []


class _FactsWiringBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        self.addCleanup(persona.reset_persona_cache)
        self.addCleanup(messages.reset_overrides)
        memory_recall.reset()
        persona.reset_persona_cache()
        messages.reset_overrides()
        self.root = Path(self._td.name)


# --- 1. Facts reach the captured prompt when enabled (sync path) -----------

class TestFactsReachPrompt(_FactsWiringBase):
    def test_seeded_fact_surfaces_before_final_check(self):
        cfg = _folder_cfg(self.root, facts_enabled=True)
        facts.append_facts(cfg.facts_path, [COFFEE_FACT], cfg.facts_max)
        store = SessionStore(cfg.session_path)
        eng = FakeEngineOK()

        bot.produce_reply(cfg, store, "what about coffee", now=NOW, engine_mod=eng)

        prompt = eng.calls[0]["system_prompt"]
        self.assertIn("# What you know about Wren", prompt)
        self.assertIn("- [preference] her coffee is always black", prompt)
        self.assertLess(prompt.index("# What you know about Wren"),
                        prompt.index("# Before you speak (last check)"))


# --- 2. Flag off: no block, byte-identical to the pre-task baseline (L1) ----

class TestFactsFlagOffL1Pin(_FactsWiringBase):
    def test_disabled_flag_no_block_matches_no_facts_baseline(self):
        cfg = _folder_cfg(self.root, facts_enabled=False)
        # Data present on disk: proves the flag gate, not merely an empty book.
        facts.append_facts(cfg.facts_path, [COFFEE_FACT], cfg.facts_max)
        store = SessionStore(cfg.session_path)
        eng = FakeEngineOK()

        out = bot.produce_reply(cfg, store, "what about coffee", now=NOW, engine_mod=eng)

        prompt = eng.calls[0]["system_prompt"]
        self.assertNotIn("# What you know about", prompt)
        # L1 pin: byte-identical to a build_system_prompt call with no
        # facts_block threaded through at all.
        self.assertEqual(prompt, persona.build_system_prompt(cfg))
        self.assertEqual(out, ["nice to hear from you"])


# --- 3. Fail-soft: a raising prompt_block never breaks the reply -----------

class TestFactsBlockFailSoft(_FactsWiringBase):
    def test_prompt_block_raise_is_fail_soft_and_logged(self):
        cfg = _folder_cfg(self.root, facts_enabled=True)
        facts.append_facts(cfg.facts_path, [COFFEE_FACT], cfg.facts_max)
        store = SessionStore(cfg.session_path)

        with mock.patch.object(facts, "prompt_block",
                               side_effect=RuntimeError("boom")), \
             self.assertLogs("everthine", level="WARNING") as cm:
            result = bot.prepare_exchange(cfg, store, "what about coffee", NOW)

        self.assertEqual(len(result), 5)  # (prompt, data, memory, inner, facts)
        facts_block = result[4]
        self.assertIsNone(facts_block)  # a broken facts read never breaks the reply
        self.assertTrue(any("facts block failed" in m for m in cm.output))


# --- 4. Streaming parity: facts reach the stream prompt too ----------------

class TestStreamingFactsReachPrompt(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(memory_recall.reset)
        self.addCleanup(persona.reset_persona_cache)
        self.addCleanup(messages.reset_overrides)
        memory_recall.reset()
        persona.reset_persona_cache()
        messages.reset_overrides()
        self.root = Path(self._td.name)

    async def test_facts_block_threads_through_stream_reply(self):
        cfg = _folder_cfg(self.root, facts_enabled=True)
        facts.append_facts(cfg.facts_path, [COFFEE_FACT], cfg.facts_max)
        store = SessionStore(cfg.session_path)
        eng = ScriptedEngine(ok_script(["ok."]))
        display = FakeDisplay()
        cancel = threading.Event()

        reply = await bot.stream_reply(cfg, store, "what about coffee", display,
                                       cancel, now=NOW, engine_mod=eng)

        self.assertTrue(reply.ok)
        prompt = eng.calls[0]["system_prompt"]
        self.assertIn("# What you know about Wren", prompt)
        self.assertIn("- [preference] her coffee is always black", prompt)


if __name__ == "__main__":
    unittest.main()
