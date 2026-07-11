"""scheduler.py's background inner-life tick: start_tick (the arm-time gate
and its log lines) and tick_loop (the heartbeat that hands the diary, the
self-portrait, -- new in M7 T6 -- one proactive reach-out, and -- new in D1
T5 -- one facts extraction, one attempt each per round).

M7 T6 moved this tick out of bot.py (where M5 T6 first built it for the diary
and M6 T6 widened it to the self-portrait) into scheduler.py, its permanent
home next to nudge_once/deliver, and added the proactive third segment; D1 T5
inserted facts extraction as a new third segment, pushing proactive to
fourth, and a fourth arm-gate flag (facts_enabled) alongside its own armed
log line. The tick mounting and loop-survival assertions below are migrated
verbatim in semantics from tests/test_bot_inner_life.py's old
TestInnerTickMounting / TestInnerTickLoopSurvives (which drove
bot._start_inner_tick / bot._inner_tick_loop); the proactive segment, the
three-flag arm gate, and the L1 pins were new in M7 T6, and the facts
segment, the fourth flag, and its own isolation/arm pins are new here.

Conventions follow tests/test_bot_inner_life.py (the tmp-dir + persona-cache
reset harness, the folder/file persona cfg builders, the done-event +
cancel-the-real-task loop-driving idiom) and tests/test_scheduler.py (the
"everthine.scheduler.engine.try_run_once" seam, _seed_contact, and the
FakeApp whose .bot.send_message is an AsyncMock). No real engine, no real
model, no real sleep: TICK_INTERVAL_S is patched to 0 to spin the loop and to
100 to pin cancel-during-sleep.
"""
import asyncio
import itertools
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from everthine import archive, diary, engine, facts_extract, persona, portrait, scheduler
from everthine.config import Config
from everthine.engine import EngineReply
from everthine.session_store import SessionStore


# --- shared fixtures --------------------------------------------------------

def _install_resets(tc):
    """Reset every process-global a start_tick / tick_loop / nudge_once call
    can touch, in Windows-safe LIFO order (tmp-dir cleanup registered first so
    it runs last). The tick never opens the memory store, so unlike the bot
    tests this needs no memory_recall/embed seam -- only the persona cache
    that current_settings() and the nudge pipeline load into."""
    tc._td = tempfile.TemporaryDirectory()
    tc.addCleanup(tc._td.cleanup)
    tc.addCleanup(persona.reset_persona_cache)
    persona.reset_persona_cache()
    tc.root = Path(tc._td.name)


def _folder_cfg(root, **overrides):
    """A folder-mode persona (settings present -> current_settings != None, so
    start_tick arms rather than refusing on file mode) with memory off."""
    folder = root / "persona"
    if not (folder / "identity.md").exists():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "identity.md").write_text(
            "I am Theo, warm and steady.\n", encoding="utf-8")
        (folder / "settings.yaml").write_text(
            "companion:\n  name: Theo\npartner:\n  name: Wren\n", encoding="utf-8")
    kwargs = dict(bot_token="x", authorized_user_id=1,
                  data_dir=root / "data", persona_path=folder,
                  memory_enabled=False, streaming_enabled=False)
    kwargs.update(overrides)
    return Config(**kwargs)


def _file_cfg(root, **overrides):
    """A single-file persona (current_settings -> None): the file-mode / L1
    persona-rollback state in which the tick refuses to arm."""
    persona_file = root / "persona.md"
    if not persona_file.exists():
        persona_file.write_text("You are Testbot, warm and steady.",
                                encoding="utf-8")
    kwargs = dict(bot_token="x", authorized_user_id=1,
                  data_dir=root / "data-file", persona_path=persona_file,
                  memory_enabled=False, streaming_enabled=False)
    kwargs.update(overrides)
    return Config(**kwargs)


def _aware(hour, minute=0, day=6):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def _seed_contact(cfg, now, minutes_ago=60):
    """One 'user' archive entry `minutes_ago` before `now`: old enough to
    clear partner_active, recent enough to be a real last_contact."""
    archive.write_entry(cfg.archive_dir, "user", "hi there",
                        ts=now - timedelta(minutes=minutes_ago))


class FakeApp:
    """Minimal stand-in for the PTB Application start_tick parks its task on
    and deliver() sends through: a bot_data dict and a .bot whose send_message
    is an AsyncMock (so a proactive send is observable, per-call scriptable)."""

    def __init__(self):
        self.bot_data = {}
        self.bot = mock.Mock()
        self.bot.send_message = mock.AsyncMock()


ENGINE_SEAM = "everthine.scheduler.engine.try_run_once"


# --- 1. start_tick mounting: armed for a folder persona whenever ANY of the
#        four organ flags (diary / portrait / scheduler / facts) is on;
#        refused when all are off or the persona is file-mode ---------------

class TestStartTickMounting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _install_resets(self)

    async def _cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_tick_armed_all_flags_on(self):
        cfg = _folder_cfg(self.root)  # diary/portrait/scheduler/facts default True
        app = FakeApp()
        store = SessionStore(cfg.session_path)
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, store)
        # Byte-identical literal pins: the deployment SOP greps for all four --
        # the first two predate M7 T6, the third is M7 T6's proactive arm
        # line, the fourth is D1 T5's facts arm line.
        self.assertTrue(any("diary: inner-life tick started" in m for m in cm.output))
        self.assertTrue(any("portrait: armed (interval 7d)" in m for m in cm.output))
        self.assertTrue(any("facts: extraction armed (idle 30m)" in m for m in cm.output))
        self.assertTrue(any(
            "scheduler: proactive armed (greeting=True miss_you=True share=True)" in m
            for m in cm.output))
        task = app.bot_data.get("_inner_tick_task")
        # Held in bot_data on purpose: a bare create_task result nobody keeps
        # can be garbage-collected mid-flight (asyncio keeps only a weak ref).
        self.assertIsInstance(task, asyncio.Task)
        self.assertFalse(task.done())
        await self._cancel(task)

    async def test_tick_armed_diary_only_no_other_lines(self):
        cfg = _folder_cfg(self.root, portrait_enabled=False,
                          scheduler_enabled=False, facts_enabled=False)
        app = FakeApp()
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertTrue(any("diary: inner-life tick started" in m for m in cm.output))
        self.assertFalse(any("portrait: armed" in m for m in cm.output))
        self.assertFalse(any("facts: extraction armed" in m for m in cm.output))
        self.assertFalse(any("scheduler: proactive armed" in m for m in cm.output))
        await self._cancel(app.bot_data["_inner_tick_task"])

    async def test_tick_armed_portrait_only_no_other_lines(self):
        # A non-default interval on purpose: proves the armed line's {n} reads
        # cfg.portrait_interval_days, not a hardcoded 7.
        cfg = _folder_cfg(self.root, diary_enabled=False,
                          scheduler_enabled=False, facts_enabled=False,
                          portrait_interval_days=3)
        app = FakeApp()
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertFalse(any("tick started" in m for m in cm.output))
        self.assertTrue(any("portrait: armed (interval 3d)" in m for m in cm.output))
        self.assertFalse(any("facts: extraction armed" in m for m in cm.output))
        self.assertFalse(any("scheduler: proactive armed" in m for m in cm.output))
        await self._cancel(app.bot_data["_inner_tick_task"])

    async def test_tick_armed_scheduler_only_no_other_lines(self):
        """New M7 arm source: scheduler_enabled alone arms the tick even with
        diary, portrait, and facts all off -- the proactive segment is reason
        enough to keep the heartbeat running."""
        cfg = _folder_cfg(self.root, diary_enabled=False, portrait_enabled=False,
                          facts_enabled=False)
        app = FakeApp()
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertFalse(any("tick started" in m for m in cm.output))
        self.assertFalse(any("portrait: armed" in m for m in cm.output))
        self.assertFalse(any("facts: extraction armed" in m for m in cm.output))
        self.assertTrue(any(
            "scheduler: proactive armed (greeting=True miss_you=True share=True)" in m
            for m in cm.output))
        self.assertIsInstance(app.bot_data.get("_inner_tick_task"), asyncio.Task)
        await self._cancel(app.bot_data["_inner_tick_task"])

    async def test_tick_armed_facts_only_no_other_lines(self):
        """New D1 T5 arm source: facts_enabled alone arms the tick even with
        diary, portrait, and scheduler all off -- extraction is reason enough
        to keep the heartbeat running."""
        cfg = _folder_cfg(self.root, diary_enabled=False, portrait_enabled=False,
                          scheduler_enabled=False)
        app = FakeApp()
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertFalse(any("tick started" in m for m in cm.output))
        self.assertFalse(any("portrait: armed" in m for m in cm.output))
        self.assertFalse(any("scheduler: proactive armed" in m for m in cm.output))
        self.assertTrue(any("facts: extraction armed (idle 30m)" in m for m in cm.output))
        self.assertIsInstance(app.bot_data.get("_inner_tick_task"), asyncio.Task)
        await self._cancel(app.bot_data["_inner_tick_task"])

    async def test_scheduler_armed_line_shows_subflag_bools(self):
        """The proactive arm line reports each per-job flag's real bool, so a
        greeting-off run says so at boot rather than claiming all three."""
        cfg = _folder_cfg(self.root, diary_enabled=False, portrait_enabled=False,
                          greeting_enabled=False)
        app = FakeApp()
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertTrue(any(
            "scheduler: proactive armed (greeting=False miss_you=True share=True)" in m
            for m in cm.output))
        await self._cancel(app.bot_data["_inner_tick_task"])

    async def test_no_scheduler_armed_line_when_scheduler_off(self):
        """L1 pin (part): with scheduler_enabled off but diary/portrait on, the
        tick still arms for them, but no 'scheduler: proactive armed' line is
        ever emitted."""
        cfg = _folder_cfg(self.root, scheduler_enabled=False)
        app = FakeApp()
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertTrue(any("diary: inner-life tick started" in m for m in cm.output))
        self.assertFalse(any("scheduler: proactive armed" in m for m in cm.output))
        await self._cancel(app.bot_data["_inner_tick_task"])

    async def test_tick_not_armed_when_all_four_disabled(self):
        """L1 pin, standalone: the tick is structurally absent (no task,
        nothing to cancel) the moment ALL FOUR organ flags are off, even
        though any one alone is enough to arm it. Byte-identical to the old
        three-flag claim: no facts log lines either, since start_tick returns
        before any per-organ line is ever reached."""
        cfg = _folder_cfg(self.root, diary_enabled=False, portrait_enabled=False,
                          scheduler_enabled=False, facts_enabled=False)
        app = FakeApp()
        scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertNotIn("_inner_tick_task", app.bot_data)

    async def test_tick_not_armed_in_file_mode_persona_and_says_so(self):
        cfg = _file_cfg(self.root)  # all flags default on, but file-mode persona
        app = FakeApp()
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertNotIn("_inner_tick_task", app.bot_data)
        self.assertTrue(any("file-mode persona" in m for m in cm.output))

    async def test_tick_gate_matrix(self):
        """The full thirty-two-cell gate truth table: diary_enabled x
        portrait_enabled x scheduler_enabled x facts_enabled x persona mode.
        Folder mode arms whenever ANY flag is on; file mode never arms,
        regardless of the four flags. The load-bearing corners (any-one-on,
        all-off, file mode) also have dedicated log-line tests above; this
        sweep is the systematic cross-check that no cell was missed."""
        cases = []
        for d, p, s, f in itertools.product([True, False], repeat=4):
            cases.append(("folder", d, p, s, f, d or p or s or f))
            cases.append(("file", d, p, s, f, False))

        for mode, d, p, s, f, expect_armed in cases:
            with self.subTest(mode=mode, diary=d, portrait=p, scheduler=s, facts=f):
                cfg_fn = _folder_cfg if mode == "folder" else _file_cfg
                cfg = cfg_fn(self.root, diary_enabled=d, portrait_enabled=p,
                             scheduler_enabled=s, facts_enabled=f)
                app = FakeApp()
                scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
                task = app.bot_data.get("_inner_tick_task")
                if expect_armed:
                    self.assertIsInstance(task, asyncio.Task)
                    await self._cancel(task)
                else:
                    self.assertIsNone(task)


# --- 1b. start_tick's fresh-ledger stamp (merge-acceptance fix, 2026-07-10):
#         a missing scheduler-state file at arm time is created with
#         greeting_date = today, so a bot first booted in the evening never
#         "catches up" on a morning greeting that predates its own ledger ----

class TestStartTickFreshLedgerStamp(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _install_resets(self)

    async def _cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_missing_ledger_created_with_todays_greeting_taken(self):
        cfg = _folder_cfg(self.root)
        app = FakeApp()
        self.assertFalse(Path(cfg.scheduler_state_path).exists())
        with self.assertLogs("everthine", level="INFO") as cm:
            scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertTrue(Path(cfg.scheduler_state_path).exists())
        state = scheduler.load_state(cfg.scheduler_state_path)
        today = datetime.now().astimezone().date().isoformat()
        self.assertEqual(state["greeting_date"], today)
        # Every other field is still fresh: only the greeting is marked taken.
        self.assertEqual(state["miss_you_anchor"], "")
        self.assertEqual(state["share_date"], "")
        self.assertEqual(state["share_count"], 0)
        self.assertEqual(state["budget_used"], 0)
        self.assertEqual(state["last_nudge_at"], "")
        self.assertTrue(any("fresh ledger" in m for m in cm.output))
        await self._cancel(app.bot_data["_inner_tick_task"])

    async def test_existing_ledger_left_untouched(self):
        cfg = _folder_cfg(self.root)
        seeded = scheduler._fresh_state()
        seeded["greeting_date"] = "2020-01-01"
        seeded["budget_used"] = 3
        scheduler._atomic_write(Path(cfg.scheduler_state_path), seeded)
        app = FakeApp()
        scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        reread = scheduler.load_state(cfg.scheduler_state_path)
        self.assertEqual(reread["greeting_date"], "2020-01-01")
        self.assertEqual(reread["budget_used"], 3)
        await self._cancel(app.bot_data["_inner_tick_task"])

    async def test_scheduler_off_creates_no_ledger(self):
        # The stamp is the scheduler's own bootstrap; a diary/portrait-only
        # run must not leave a proactive ledger behind (L1: scheduler off
        # is byte-identical to M6, on disk included).
        cfg = _folder_cfg(self.root, scheduler_enabled=False)
        app = FakeApp()
        scheduler.start_tick(app, cfg, SessionStore(cfg.session_path))
        self.assertFalse(Path(cfg.scheduler_state_path).exists())
        await self._cancel(app.bot_data["_inner_tick_task"])


# --- 2. the tick loop is unkillable: a raising call in the diary or portrait
#        segment is logged and the loop keeps going, never blocking the other;
#        write_once/update_once always receive the same aware now. Migrated in
#        semantics from the old two-segment loop, with the proactive segment
#        turned off (scheduler_enabled=False) so these largely reproduce it
#        exactly -- one test below was extended for D1 T5 to also pin facts
#        (the new third segment); the proactive segment (now fourth) gets its
#        own isolation tests in section 3. -----------------------------------

class TestTickLoopSurvives(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _install_resets(self)

    def _bare_cfg(self, **overrides):
        # File-mode is fine: tick_loop never checks persona mode (that is
        # start_tick's gate). scheduler_enabled off keeps this the pure
        # two-segment loop the migrated assertions were written against.
        kwargs = dict(bot_token="x", authorized_user_id=1,
                      data_dir=self.root / "data", scheduler_enabled=False)
        kwargs.update(overrides)
        return Config(**kwargs)

    async def test_loop_survives_a_failing_diary_iteration_and_passes_aware_now(self):
        cfg = self._bare_cfg()
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        seen = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_write_once(cfg_arg, now):
            seen.append(now)
            if len(seen) == 1:
                raise RuntimeError("first iteration explodes")
            loop.call_soon_threadsafe(done.set)
            return False

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", fake_write_once), \
             mock.patch.object(portrait, "update_once", lambda c, n: False), \
             self.assertLogs("everthine", level="WARNING") as cm:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(len(seen), 2)  # survived the RuntimeError
        self.assertIsNotNone(seen[0].utcoffset())  # aware now, first round
        self.assertIsNotNone(seen[1].utcoffset())  # aware now, second round
        self.assertTrue(any("diary: tick iteration failed" in m for m in cm.output))

    async def test_diary_failure_does_not_block_portrait_same_round(self):
        cfg = self._bare_cfg()
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        portrait_calls = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_write_once(cfg_arg, now):
            raise RuntimeError("diary explodes every round")

        def fake_update_once(cfg_arg, now):
            portrait_calls.append(now)
            loop.call_soon_threadsafe(done.set)
            return False

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", fake_write_once), \
             mock.patch.object(portrait, "update_once", fake_update_once), \
             self.assertLogs("everthine", level="WARNING") as cm:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(len(portrait_calls), 1)
        self.assertIsNotNone(portrait_calls[0].utcoffset())
        self.assertTrue(any("diary: tick iteration failed" in m for m in cm.output))

    async def test_portrait_failure_does_not_block_diary_next_round(self):
        """Extended for D1 T5: a portrait round that always raises must not
        skip facts EITHER (facts sits right after portrait in the fixed
        order, same round) -- the symmetric half of "diary/portrait raising
        doesn't skip facts"."""
        cfg = self._bare_cfg()
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        diary_calls = []
        facts_calls = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_write_once(cfg_arg, now):
            diary_calls.append(now)
            return False

        def fake_update_once(cfg_arg, now):
            raise RuntimeError("portrait explodes every round")

        def fake_extract_once(cfg_arg, now):
            # Signal from facts, the LAST of the three segments this round --
            # not diary (the first) -- so the wake-up is never racing this
            # same round's later segments: by the time facts_calls reaches 2,
            # that round's diary call (sequenced strictly before it) is
            # already guaranteed to have happened too.
            facts_calls.append(now)
            if len(facts_calls) >= 2:
                loop.call_soon_threadsafe(done.set)
            return False

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", fake_write_once), \
             mock.patch.object(portrait, "update_once", fake_update_once), \
             mock.patch.object(facts_extract, "extract_once", fake_extract_once), \
             self.assertLogs("everthine", level="WARNING") as cm:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(len(diary_calls), 2)  # survived across rounds
        self.assertGreaterEqual(len(facts_calls), 2)  # facts kept pace too
        self.assertTrue(any("portrait: tick iteration failed" in m for m in cm.output))

    async def test_cancel_during_sleep_propagates(self):
        """Cancelling while the loop is still asleep -- before any segment's
        call has even started -- must raise CancelledError out of the task
        uncaught. Sleep sits outside every segment's try/except, so this pins
        that its own cancellation is never accidentally swallowed. All four
        segments (facts and proactive included) must be untouched."""
        cfg = self._bare_cfg(scheduler_enabled=True)
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 100), \
             mock.patch.object(diary, "write_once") as fake_diary, \
             mock.patch.object(portrait, "update_once") as fake_portrait, \
             mock.patch.object(facts_extract, "extract_once") as fake_facts, \
             mock.patch.object(scheduler, "nudge_once") as fake_nudge:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            await asyncio.sleep(0)  # let the task start and enter the sleep
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        fake_diary.assert_not_called()
        fake_portrait.assert_not_called()
        fake_facts.assert_not_called()
        fake_nudge.assert_not_called()


# --- 3. the proactive segment (M7 T6), now fourth in line behind facts (D1
#        T5): runs after diary, portrait, and facts on the same shared now,
#        is isolated from their failures and they from its (facts' own
#        order/identity/isolation pins are folded into this section's
#        shared-now and diary-failure tests below, and facts' own reciprocal
#        pin -- that ITS failure doesn't skip proactive -- gets a dedicated
#        test), and is skipped entirely when scheduler_enabled is off -------

class TestTickLoopProactiveSegment(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _install_resets(self)

    def _cfg(self, **overrides):
        kwargs = dict(bot_token="x", authorized_user_id=1,
                      data_dir=self.root / "data", scheduler_enabled=True)
        kwargs.update(overrides)
        return Config(**kwargs)

    async def test_shared_now_and_order_across_diary_portrait_facts_and_nudge(self):
        """One round hands diary.write_once, portrait.update_once,
        facts_extract.extract_once, and nudge_once the SAME now object -- the
        shared-timestamp pin, extended to the facts segment -- AND calls them
        in the fixed order diary -> portrait -> facts -> proactive (D1 T5's
        segment-order pin)."""
        cfg = self._cfg()
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        calls = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_write_once(c, now):
            calls.append(("diary", now))
            return False

        def fake_update_once(c, now):
            calls.append(("portrait", now))
            return False

        def fake_extract_once(c, now):
            calls.append(("facts", now))
            return False

        def fake_nudge(c, s, now, roll):
            calls.append(("nudge", now))
            loop.call_soon_threadsafe(done.set)
            return None

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", fake_write_once), \
             mock.patch.object(portrait, "update_once", fake_update_once), \
             mock.patch.object(facts_extract, "extract_once", fake_extract_once), \
             mock.patch.object(scheduler, "nudge_once", fake_nudge):
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(len(calls), 4)
        first_round = calls[:4]
        # Fixed order pin: facts runs after portrait, before proactive.
        self.assertEqual([name for name, _ in first_round],
                         ["diary", "portrait", "facts", "nudge"])
        # identity, not just equality: the exact same object flows to all four
        now0 = first_round[0][1]
        for _, now in first_round:
            self.assertIs(now, now0)
        self.assertIsNotNone(now0.utcoffset())  # aware

    async def test_diary_failure_does_not_block_portrait_facts_and_proactive_same_round(self):
        """Crash isolation extended to the fourth segment: a diary round that
        raises must still let portrait, facts, AND the proactive segment run
        in that same round (the "diary raising doesn't skip facts" half of
        D1 T5's isolation pin)."""
        cfg = self._cfg()
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        portrait_calls, facts_calls, nudge_calls = [], [], []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_write_once(c, now):
            raise RuntimeError("diary explodes")

        def fake_update_once(c, now):
            portrait_calls.append(now)
            return False

        def fake_extract_once(c, now):
            facts_calls.append(now)
            return False

        def fake_nudge(c, s, now, roll):
            nudge_calls.append(now)
            loop.call_soon_threadsafe(done.set)
            return None

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", fake_write_once), \
             mock.patch.object(portrait, "update_once", fake_update_once), \
             mock.patch.object(facts_extract, "extract_once", fake_extract_once), \
             mock.patch.object(scheduler, "nudge_once", fake_nudge), \
             self.assertLogs("everthine", level="WARNING") as cm:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(len(portrait_calls), 1)  # portrait still ran
        self.assertGreaterEqual(len(facts_calls), 1)     # facts still ran
        self.assertGreaterEqual(len(nudge_calls), 1)     # proactive still ran
        self.assertTrue(any("diary: tick iteration failed" in m for m in cm.output))

    async def test_facts_failure_does_not_block_proactive_same_round(self):
        """The reciprocal half of D1 T5's isolation pin: a facts round that
        raises must still let the proactive segment run in that same round --
        one organ's bug is never another's outage, including the new one."""
        cfg = self._cfg()
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        nudge_calls = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_extract_once(c, now):
            raise RuntimeError("facts explodes")

        def fake_nudge(c, s, now, roll):
            nudge_calls.append(now)
            loop.call_soon_threadsafe(done.set)
            return None

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", lambda c, n: False), \
             mock.patch.object(portrait, "update_once", lambda c, n: False), \
             mock.patch.object(facts_extract, "extract_once", fake_extract_once), \
             mock.patch.object(scheduler, "nudge_once", fake_nudge), \
             self.assertLogs("everthine", level="WARNING") as cm:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(len(nudge_calls), 1)  # proactive still ran
        self.assertTrue(any("facts: tick iteration failed" in m for m in cm.output))

    async def test_proactive_deliver_failure_does_not_block_next_round_diary(self):
        """A proactive segment that blows up INSIDE deliver (the send tail)
        must not stop the next round's diary -- one organ's bug is never
        another's outage, and deliver sits inside the segment's own
        try/except."""
        cfg = self._cfg()
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        diary_calls = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_write_once(c, now):
            diary_calls.append(now)
            if len(diary_calls) >= 2:
                loop.call_soon_threadsafe(done.set)
            return False

        def fake_nudge(c, s, now, roll):
            return scheduler.NudgeResult(job="greeting", text="hi",
                                         session_id="s2", expected_session_id="s1")

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", fake_write_once), \
             mock.patch.object(portrait, "update_once", lambda c, n: False), \
             mock.patch.object(scheduler, "nudge_once", fake_nudge), \
             mock.patch.object(scheduler, "deliver",
                               side_effect=RuntimeError("send tail explodes")), \
             self.assertLogs("everthine", level="WARNING") as cm:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(len(diary_calls), 2)  # survived across rounds
        self.assertTrue(any("proactive tick iteration failed" in m for m in cm.output))

    async def test_scheduler_disabled_skips_proactive_zero_call_no_state_no_send(self):
        """L1 pin #1 at the loop level: scheduler_enabled off means the
        proactive segment is never entered -- nudge_once is not called even
        once, no scheduler_state.json is written, nothing is sent -- while the
        diary/portrait segments keep ticking normally."""
        cfg = self._cfg(scheduler_enabled=False)
        store = SessionStore(cfg.session_path)
        app = FakeApp()
        diary_calls = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_write_once(c, now):
            diary_calls.append(now)
            if len(diary_calls) >= 2:
                loop.call_soon_threadsafe(done.set)
            return False

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", fake_write_once), \
             mock.patch.object(portrait, "update_once", lambda c, n: False), \
             mock.patch.object(scheduler, "nudge_once") as fake_nudge:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        fake_nudge.assert_not_called()                 # zero engine work
        app.bot.send_message.assert_not_called()       # zero send
        self.assertFalse(cfg.scheduler_state_path.exists())  # no state file
        self.assertGreaterEqual(len(diary_calls), 2)   # diary still ticked


# --- 4. proactive sub-flag L1, driven through the whole tick (tick-level,
#        real nudge_once + real deliver): greeting_enabled off suppresses the
#        greeting end to end; greeting on fires it end to end -----------------

class TestProactiveSubFlagThroughTick(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _install_resets(self)

    def _greeting_cfg(self, **overrides):
        # miss_you/share off so greeting is the only candidate job; greeting's
        # own flag is what these two tests flip. facts off so its own
        # engine call (the seeded contact below is old enough to clear its
        # idle gate) never muddies these proactive-only engine-call
        # assertions.
        return _folder_cfg(self.root, diary_enabled=True, portrait_enabled=True,
                           scheduler_enabled=True, miss_you_enabled=False,
                           share_enabled=False, greeting_hour=8,
                           facts_enabled=False, **overrides)

    async def test_greeting_flag_off_suppresses_greeting_through_tick(self):
        cfg = self._greeting_cfg(greeting_enabled=False)
        now = _aware(9)  # 09:00: past greeting_hour, outside the 23-8 quiet window
        _seed_contact(cfg, now, minutes_ago=60)
        store = SessionStore(cfg.session_path)
        store.save(session_id="s1")
        app = FakeApp()
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        real_nudge = scheduler.nudge_once
        results = []

        def wrapped_nudge(c, s, _now, roll):
            # Real nudge_once, but pin `now` so the whole gate chain is
            # deterministic, and signal round completion after it returns.
            r = real_nudge(c, s, now, roll)
            results.append(r)
            loop.call_soon_threadsafe(done.set)
            return r

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", lambda c, n: False), \
             mock.patch.object(portrait, "update_once", lambda c, n: False), \
             mock.patch.object(scheduler, "nudge_once", wrapped_nudge), \
             mock.patch(ENGINE_SEAM,
                        return_value=EngineReply("good morning", "s2", ok=True)) as run:
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertIsNone(results[0])                 # greeting suppressed
        run.assert_not_called()                       # never reached the engine
        app.bot.send_message.assert_not_called()      # and never sent
        self.assertFalse(cfg.scheduler_state_path.exists())

    async def test_greeting_fires_through_tick_when_enabled(self):
        """Positive control + the whole new wire in one: with greeting on and
        due, one tick round runs nudge_once -> deliver -> app.bot.send_message,
        proving the tick now DOES send through the proactive segment."""
        cfg = self._greeting_cfg(greeting_enabled=True)
        now = _aware(9)
        _seed_contact(cfg, now, minutes_ago=60)
        store = SessionStore(cfg.session_path)
        store.save(session_id="s1")
        app = FakeApp()
        done = asyncio.Event()
        real_nudge = scheduler.nudge_once

        def wrapped_nudge(c, s, _now, roll):
            return real_nudge(c, s, now, roll)  # pin now; keep the real pipeline

        app.bot.send_message.side_effect = lambda **k: done.set()

        with mock.patch.object(scheduler, "TICK_INTERVAL_S", 0), \
             mock.patch.object(diary, "write_once", lambda c, n: False), \
             mock.patch.object(portrait, "update_once", lambda c, n: False), \
             mock.patch.object(scheduler, "nudge_once", wrapped_nudge), \
             mock.patch(ENGINE_SEAM,
                        return_value=EngineReply("good morning", "s2", ok=True)):
            task = asyncio.create_task(scheduler.tick_loop(cfg, store, app))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(app.bot.send_message.call_count, 1)
        first = app.bot.send_message.call_args_list[0]
        self.assertEqual(first.kwargs["text"], "good morning")
        self.assertEqual(first.kwargs["chat_id"], cfg.authorized_user_id)
        # record_nudge fired: today's greeting is stamped so it won't repeat
        state = scheduler.load_state(cfg.scheduler_state_path)
        self.assertEqual(state["greeting_date"], now.date().isoformat())


if __name__ == "__main__":
    unittest.main()
