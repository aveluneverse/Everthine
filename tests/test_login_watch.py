"""login_watch: the one-number credential read, the pure decision, the
notice sender, the boot log line, and the arm/loop. No real credential file
is ever touched (every reader points at a temp path); no real sleep
(WATCH_INTERVAL_S patched to 0); sends go through a FakeApp AsyncMock."""
import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from everthine import engine, login_watch, messages
from everthine.config import Config


def _cfg(**kw):
    base = dict(bot_token="x", authorized_user_id=42, login_warn_days=3,
                quiet_start_hour=23, quiet_end_hour=8)
    base.update(kw)
    return Config(**base)


def _at(hour, day=20):
    # A fixed, aware LOCAL time: 2026-08-<day> <hour>:00 in the machine's zone
    # (quiet hours are judged on .hour of the local time, so build it local).
    return datetime(2026, 8, day, hour, 0).astimezone()


class FakeApp:
    def __init__(self):
        self.bot_data = {}
        self.bot = mock.Mock()
        self.bot.send_message = mock.AsyncMock()


class TestCredentialsPath(unittest.TestCase):
    def test_default_is_home_dot_claude(self):
        self.assertEqual(login_watch.credentials_path({}),
                         Path.home() / ".claude" / ".credentials.json")

    def test_claude_config_dir_wins(self):
        p = login_watch.credentials_path({"CLAUDE_CONFIG_DIR": "/tmp/cfg"})
        self.assertEqual(p, Path("/tmp/cfg") / ".credentials.json")


class TestReadLoginExpiry(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = Path(self._td.name) / ".credentials.json"

    def _write(self, obj):
        self.path.write_text(json.dumps(obj), encoding="utf-8")

    def test_reads_refresh_token_expiry_as_aware_datetime(self):
        ms = 1788422371315
        self._write({"claudeAiOauth": {"accessToken": "sk-ant-oat01-FAKE",
                                       "refreshToken": "sk-ant-ort01-FAKE",
                                       "expiresAt": 1, "refreshTokenExpiresAt": ms}})
        got = login_watch.read_login_expiry(self.path)
        self.assertIsNotNone(got.tzinfo)
        self.assertEqual(got, datetime.fromtimestamp(ms / 1000, tz=timezone.utc))

    def test_missing_file_is_none(self):
        self.assertIsNone(login_watch.read_login_expiry(self.path))

    def test_malformed_json_is_none(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(login_watch.read_login_expiry(self.path))

    def test_no_oauth_block_is_none(self):
        self._write({"somethingElse": {}})
        self.assertIsNone(login_watch.read_login_expiry(self.path))

    def test_non_numeric_or_bool_or_nonpositive_is_none(self):
        for bad in ("soon", True, 0, -5, None):
            with self.subTest(bad=bad):
                self._write({"claudeAiOauth": {"refreshTokenExpiresAt": bad}})
                self.assertIsNone(login_watch.read_login_expiry(self.path))


class TestPureHelpers(unittest.TestCase):
    def test_days_left_rounds_up_and_floors_at_one(self):
        now = _at(10)
        self.assertEqual(login_watch.days_left(now, now + timedelta(days=2, hours=5)), 3)
        self.assertEqual(login_watch.days_left(now, now + timedelta(hours=6)), 1)
        self.assertEqual(login_watch.days_left(now, now - timedelta(hours=1)), 0)

    def test_is_expired(self):
        now = _at(10)
        self.assertTrue(login_watch.is_expired(now, now - timedelta(minutes=1), False))
        self.assertTrue(login_watch.is_expired(now, None, True))
        self.assertTrue(login_watch.is_expired(now, now + timedelta(days=9), True))
        self.assertFalse(login_watch.is_expired(now, now + timedelta(days=9), False))
        self.assertFalse(login_watch.is_expired(now, None, False))

    def test_in_quiet_hours_wraps_midnight(self):
        cfg = _cfg(quiet_start_hour=23, quiet_end_hour=8)
        self.assertTrue(login_watch.in_quiet_hours(cfg, _at(23)))
        self.assertTrue(login_watch.in_quiet_hours(cfg, _at(3)))
        self.assertFalse(login_watch.in_quiet_hours(cfg, _at(8)))
        self.assertFalse(login_watch.in_quiet_hours(cfg, _at(12)))
        self.assertFalse(login_watch.in_quiet_hours(_cfg(quiet_start_hour=9, quiet_end_hour=9), _at(9)))


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.now = _at(10)  # 10:00, outside quiet hours

    def test_expired_by_file_once(self):
        state = login_watch.new_state()
        self.assertEqual(login_watch.decide(self.cfg, self.now, self.now - timedelta(hours=1), False, state),
                         ("expired", None))
        state["expired_notified"] = True
        self.assertEqual(login_watch.decide(self.cfg, self.now, self.now - timedelta(hours=1), False, state),
                         (None, None))

    def test_expired_by_engine_without_file(self):
        self.assertEqual(login_watch.decide(self.cfg, self.now, None, True, login_watch.new_state()),
                         ("expired", None))

    def test_quiet_hours_hold_both_notices(self):
        night = _at(2)
        self.assertEqual(login_watch.decide(self.cfg, night, night - timedelta(hours=1), False, login_watch.new_state()),
                         (None, None))
        self.assertEqual(login_watch.decide(self.cfg, night, night + timedelta(days=1), False, login_watch.new_state()),
                         (None, None))

    def test_expiring_inside_window_once_per_day(self):
        state = login_watch.new_state()
        exp = self.now + timedelta(days=2, hours=3)   # 3 days left
        self.assertEqual(login_watch.decide(self.cfg, self.now, exp, False, state), ("expiring", 3))
        state["expiring_date"] = self.now.date()
        self.assertEqual(login_watch.decide(self.cfg, self.now, exp, False, state), (None, None))

    def test_outside_window_silent(self):
        exp = self.now + timedelta(days=3, hours=1)   # 4 days left
        self.assertEqual(login_watch.decide(self.cfg, self.now, exp, False, login_watch.new_state()), (None, None))

    def test_warn_days_zero_disables_heads_up_only(self):
        cfg = _cfg(login_warn_days=0)
        self.assertEqual(login_watch.decide(cfg, self.now, self.now + timedelta(hours=5), False, login_watch.new_state()),
                         (None, None))
        self.assertEqual(login_watch.decide(cfg, self.now, self.now - timedelta(hours=5), False, login_watch.new_state()),
                         ("expired", None))

    def test_unknown_expiry_and_healthy_engine_is_silent(self):
        self.assertEqual(login_watch.decide(self.cfg, self.now, None, False, login_watch.new_state()), (None, None))


class TestWatchOnce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        messages.reset_overrides()
        self.addCleanup(messages.reset_overrides)
        self.cfg = _cfg()
        self.app = FakeApp()
        self.now = _at(10)

    async def test_expired_sends_auth_line_once_and_marks_state(self):
        state = login_watch.new_state()
        action = await login_watch.watch_once(
            self.app, self.cfg, self.now, state,
            read_expiry=lambda: self.now - timedelta(hours=1), broken=lambda: False)
        self.assertEqual(action, "expired")
        self.app.bot.send_message.assert_awaited_once_with(chat_id=42, text=messages.msg("auth"))
        self.assertTrue(state["expired_notified"])
        again = await login_watch.watch_once(
            self.app, self.cfg, self.now, state,
            read_expiry=lambda: self.now - timedelta(hours=1), broken=lambda: False)
        self.assertIsNone(again)
        self.assertEqual(self.app.bot.send_message.await_count, 1)

    async def test_expiring_sends_formatted_heads_up(self):
        state = login_watch.new_state()
        exp = self.now + timedelta(days=1, hours=2)  # 2 days left
        action = await login_watch.watch_once(
            self.app, self.cfg, self.now, state, read_expiry=lambda: exp, broken=lambda: False)
        self.assertEqual(action, "expiring")
        sent = self.app.bot.send_message.await_args.kwargs["text"]
        self.assertEqual(sent, messages.msg("auth_expiring").format(days=2))
        self.assertNotIn("{days}", sent)
        self.assertEqual(state["expiring_date"], self.now.date())

    async def test_send_failure_logs_and_leaves_state_untouched(self):
        self.app.bot.send_message.side_effect = RuntimeError("telegram down")
        state = login_watch.new_state()
        with self.assertLogs("everthine", level="WARNING"):
            action = await login_watch.watch_once(
                self.app, self.cfg, self.now, state,
                read_expiry=lambda: self.now - timedelta(hours=1), broken=lambda: False)
        self.assertIsNone(action)
        self.assertFalse(state["expired_notified"])

    async def test_episode_resets_after_recovery(self):
        state = login_watch.new_state()
        state["expired_notified"] = True
        action = await login_watch.watch_once(
            self.app, self.cfg, self.now, state,
            read_expiry=lambda: self.now + timedelta(days=20), broken=lambda: False)
        self.assertIsNone(action)
        self.assertFalse(state["expired_notified"])

    async def test_uses_engine_auth_state_by_default(self):
        engine.reset_auth_state()
        self.addCleanup(engine.reset_auth_state)
        engine._auth_state["failed_at"] = 100.0
        state = login_watch.new_state()
        action = await login_watch.watch_once(self.app, self.cfg, self.now, state,
                                              read_expiry=lambda: None)
        self.assertEqual(action, "expired")


class TestBootStatusAndStart(unittest.IsolatedAsyncioTestCase):
    def test_boot_status_three_branches(self):
        now = _at(10)
        with self.assertLogs("everthine", level="INFO") as cm:
            login_watch.log_boot_status(_cfg(), now=now, read_expiry=lambda: None)
        self.assertTrue(any("cannot read" in line for line in cm.output))
        with self.assertLogs("everthine", level="WARNING") as cm:
            login_watch.log_boot_status(_cfg(), now=now, read_expiry=lambda: now - timedelta(days=1))
        self.assertTrue(any("ALREADY expired" in line and "claude auth login" in line for line in cm.output))
        with self.assertLogs("everthine", level="INFO") as cm:
            login_watch.log_boot_status(_cfg(), now=now, read_expiry=lambda: now + timedelta(days=12))
        self.assertTrue(any("12 day" in line for line in cm.output))

    async def test_start_disabled_arms_nothing(self):
        app = FakeApp()
        with self.assertLogs("everthine", level="INFO") as cm:
            login_watch.start(app, _cfg(login_watch_enabled=False))
        self.assertNotIn("_login_watch_task", app.bot_data)
        self.assertTrue(any("disabled" in line for line in cm.output))

    async def test_start_parks_a_task_and_logs_armed(self):
        app = FakeApp()
        # A Mock, not a lambda: proves (via assert_called()) that start()'s
        # boot-status read actually goes through this patched name rather
        # than silently falling through to the real credential file -- the
        # late-bound read_expiry in log_boot_status is what makes that true.
        reader = mock.Mock(return_value=None)
        with mock.patch.object(login_watch, "read_login_expiry", reader), \
                self.assertLogs("everthine", level="INFO") as cm:
            login_watch.start(app, _cfg())
        reader.assert_called()
        task = app.bot_data.get("_login_watch_task")
        self.assertIsNotNone(task)
        self.assertTrue(any("armed" in line for line in cm.output))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_watch_loop_honors_patched_reader(self):
        # Sibling of test_start_parks_a_task_and_logs_armed, for the loop
        # path instead of the boot path: watch_loop calls the real
        # watch_once(app, cfg, now, state) with no read_expiry override, so
        # this pins that watch_once's own late-bound default reaches a
        # module-level mock.patch.object(login_watch, "read_login_expiry",
        # ...) too -- not just an explicit read_expiry= passed by a caller.
        # read_expiry runs off the event loop thread (watch_once awaits it
        # via asyncio.to_thread), so a threading.Event -- not an
        # asyncio.Event -- is the safe way to signal back across that
        # thread boundary.
        app = FakeApp()
        called = threading.Event()

        def fake_reader():
            called.set()
            return None

        reader = mock.Mock(side_effect=fake_reader)
        with mock.patch.object(login_watch, "WATCH_INTERVAL_S", 0), \
                mock.patch.object(login_watch, "read_login_expiry", reader):
            task = asyncio.create_task(login_watch.watch_loop(app, _cfg()))
            await asyncio.to_thread(called.wait, 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(called.is_set())
        reader.assert_called()

    async def test_loop_survives_a_failing_iteration(self):
        app = FakeApp()
        calls = {"n": 0}
        done = asyncio.Event()

        async def flaky(app_, cfg, now, state, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            done.set()
            return None

        with mock.patch.object(login_watch, "WATCH_INTERVAL_S", 0), \
                mock.patch.object(login_watch, "watch_once", flaky), \
                self.assertLogs("everthine", level="WARNING"):
            task = asyncio.create_task(login_watch.watch_loop(app, _cfg()))
            await asyncio.wait_for(done.wait(), timeout=5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(calls["n"], 2)


class TestBotWiring(unittest.IsolatedAsyncioTestCase):
    """make_app's _post_init (Step 6 wiring) calls login_watch.start right
    after scheduler.start_tick. The draft in the brief patched a menu-setup
    name, _set_command_menu, that does not exist on bot.py; reading
    make_app shows the actual call awaited before start_tick is
    register_commands(app_) -- the one other thing that would otherwise
    reach the network, so that is what is patched here instead. Building
    make_app's real Application exercises the real closure rather than a
    hand-rolled stand-in; a tmp data_dir plus a faked embedding function
    keep it fast and offline, the same way tests/test_bot_stream.py's
    TestConcurrencyScope builds one for the same reason."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        from everthine import memory_embed, memory_recall
        self.addCleanup(memory_recall.reset)
        memory_embed.set_embed_fn(lambda text: [1.0, 0.0])
        self.addCleanup(memory_embed.set_embed_fn, None)

    async def test_post_init_arms_login_watch(self):
        from everthine import bot
        cfg = _cfg(bot_token="123456:ABC-DEF",   # a token the PTB validator accepts
                   data_dir=Path(self._td.name) / "data")
        with mock.patch.object(bot.scheduler, "start_tick"), \
                mock.patch.object(bot.login_watch, "start") as started, \
                mock.patch.object(bot, "register_commands", mock.AsyncMock()):
            app = bot.make_app(cfg)
            await app.post_init(app)
        started.assert_called_once()
        self.assertIs(started.call_args.args[1], cfg)


if __name__ == "__main__":
    unittest.main()
