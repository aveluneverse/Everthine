import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
