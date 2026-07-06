import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from everthine.config import Config
from everthine import engine

FAKE = str(Path(__file__).resolve().parent / "fake_claude.py")


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._td.name) / "data"
        self.addCleanup(self._td.cleanup)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_MODE", None)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_STATE", None)

    def cfg(self, **kw):
        base = dict(bot_token="x", authorized_user_id=1,
                    claude_cmd=[sys.executable, FAKE], command_timeout_s=3,
                    data_dir=self.data_dir)
        base.update(kw)
        return Config(**base)

    def set_mode(self, mode):
        os.environ["FAKE_CLAUDE_MODE"] = mode


class TestBuildCmd(EngineTestBase):
    def test_minimal(self):
        c = engine.build_cmd(self.cfg(), session_id=None, system_prompt=None)
        self.assertEqual(c[:2], [sys.executable, FAKE])
        self.assertIn("-p", c)
        self.assertEqual(c[c.index("--output-format") + 1], "json")
        self.assertNotIn("--allowedTools", c)
        self.assertNotIn("--resume", c)
        self.assertNotIn("--model", c)

    def test_full(self):
        c = engine.build_cmd(self.cfg(claude_model="opus"), session_id="s1",
                             system_prompt="be kind")
        self.assertEqual(c[c.index("--model") + 1], "opus")
        self.assertEqual(c[c.index("--resume") + 1], "s1")
        self.assertEqual(c[c.index("--system-prompt") + 1], "be kind")


class TestMakeEnv(EngineTestBase):
    def test_strips_sensitive(self):
        os.environ["ANTHROPIC_API_KEY"] = "secret"
        os.environ["CLAUDECODE"] = "1"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        self.addCleanup(os.environ.pop, "CLAUDECODE", None)
        env = engine.make_env()
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertIn("PATH", env)


class TestRunOnce(EngineTestBase):
    def test_ok_reply_and_session(self):
        self.set_mode("ok")
        r = engine.run_once(self.cfg(), "hello there")
        self.assertTrue(r.ok)
        self.assertTrue(r.text.startswith("echo:hello"))
        self.assertEqual(r.session_id, "fake-session-123")

    def test_resume_keeps_session(self):
        self.set_mode("ok")
        r = engine.run_once(self.cfg(), "hi", session_id="keep-me")
        self.assertEqual(r.session_id, "keep-me")

    def test_malformed_json_falls_back_to_raw(self):
        self.set_mode("malformed")
        r = engine.run_once(self.cfg(), "hi")
        self.assertTrue(r.ok)
        self.assertIn("not json", r.text)

    def test_valid_json_non_object_falls_back_to_raw(self):
        self.set_mode("nonobject")
        r = engine.run_once(self.cfg(), "hi")
        self.assertTrue(r.ok)
        self.assertIn("[1, 2, 3]", r.text)
        self.assertIsNone(r.error_kind)

    def test_result_error_flag_fails_reply(self):
        self.set_mode("result_error")
        r = engine.run_once(self.cfg(), "hi")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "nonzero")

    def test_auth_retry_succeeds_second_try(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["FAKE_CLAUDE_STATE"] = str(Path(td) / "state")
            self.set_mode("auth_once")
            r = engine.run_once(self.cfg(), "hi")
            self.assertTrue(r.ok)

    def test_plain_failure_reports_nonzero(self):
        self.set_mode("exit1")
        r = engine.run_once(self.cfg(), "hi")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "nonzero")

    def test_timeout(self):
        self.set_mode("slow")
        r = engine.run_once(self.cfg(command_timeout_s=1), "hi")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "timeout")

    def test_cli_missing(self):
        self.set_mode("ok")
        r = engine.run_once(self.cfg(claude_cmd=["definitely-not-a-real-binary-xyz"]), "hi")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "cli_missing")

    def test_unlaunchable_command_reports_error_not_raise(self):
        # A directory is not executable: Popen raises an OSError subclass.
        self.set_mode("ok")
        with tempfile.TemporaryDirectory() as td:
            r = engine.run_once(self.cfg(claude_cmd=[td]), "hi")
        self.assertFalse(r.ok)
        self.assertIn(r.error_kind, ("nonzero", "cli_missing"))


class TestTryRunOnce(EngineTestBase):
    def test_busy_lock_returns_none_without_attempt(self):
        self.assertTrue(engine._reply_lock.acquire(blocking=False))
        self.addCleanup(engine._reply_lock.release)
        with mock.patch.object(engine, "_run_attempt") as attempt:
            reply = engine.try_run_once(self.cfg(), "hi")
        self.assertIsNone(reply)
        attempt.assert_not_called()

    def test_free_lock_returns_attempt_result_and_releases(self):
        sentinel = engine.EngineReply("fine", "s1", ok=True)
        with mock.patch.object(engine, "_run_attempt", return_value=sentinel) as attempt:
            reply = engine.try_run_once(self.cfg(), "hi")
        self.assertIs(reply, sentinel)
        attempt.assert_called_once()
        self.assertTrue(engine._reply_lock.acquire(blocking=False))
        engine._reply_lock.release()

    def test_lock_released_even_when_attempt_raises(self):
        with mock.patch.object(engine, "_run_attempt", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                engine.try_run_once(self.cfg(), "hi")
        self.assertTrue(engine._reply_lock.acquire(blocking=False))
        engine._reply_lock.release()

    def test_auth_failure_retried_once_without_real_sleep(self):
        auth = engine.EngineReply("", None, ok=False, error_kind="auth")
        good = engine.EngineReply("fine", "s1", ok=True)
        with mock.patch.object(engine, "_run_attempt", side_effect=[auth, good]) as attempt, \
                mock.patch.object(engine.time, "sleep") as fake_sleep:
            reply = engine.try_run_once(self.cfg(), "hi")
        self.assertIs(reply, good)
        self.assertEqual(attempt.call_count, 2)
        fake_sleep.assert_called_once_with(1.5)

    def test_timeout_s_forwarded_verbatim_to_attempt(self):
        cfg = self.cfg()
        sentinel = engine.EngineReply("fine", "s1", ok=True)
        with mock.patch.object(engine, "_run_attempt", return_value=sentinel) as attempt:
            engine.try_run_once(cfg, "hi", timeout_s=90)
        attempt.assert_called_once_with(cfg, "hi", None, None, 90)

    def test_run_once_reaches_attempt_with_none_timeout(self):
        cfg = self.cfg()
        sentinel = engine.EngineReply("fine", "s1", ok=True)
        with mock.patch.object(engine, "_run_attempt", return_value=sentinel) as attempt:
            engine.run_once(cfg, "hi")
        attempt.assert_called_once_with(cfg, "hi", None, None, None)


class TestRunAttemptTimeoutResolution(EngineTestBase):
    def _communicate_timeout_for(self, cfg, timeout_s):
        captured = {}

        class FakeProc:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                captured["timeout"] = timeout
                return '{"result": "hi", "session_id": "s"}', ""

        with mock.patch.object(engine.subprocess, "Popen", return_value=FakeProc()):
            reply = engine._run_attempt(cfg, "prompt", None, None, timeout_s)
        self.assertTrue(reply.ok)
        return captured["timeout"]

    def test_default_none_resolves_to_cfg_command_timeout(self):
        cfg = self.cfg(command_timeout_s=7)
        self.assertEqual(self._communicate_timeout_for(cfg, None), 7)

    def test_explicit_timeout_reaches_communicate_unchanged(self):
        cfg = self.cfg(command_timeout_s=7)
        self.assertEqual(self._communicate_timeout_for(cfg, 90), 90)


if __name__ == "__main__":
    unittest.main()
