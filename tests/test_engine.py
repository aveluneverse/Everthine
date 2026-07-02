import os
import sys
import tempfile
import unittest
from pathlib import Path

from everthine.config import Config
from everthine import engine

FAKE = str(Path(__file__).resolve().parent / "fake_claude.py")


def cfg(**kw):
    base = dict(bot_token="x", authorized_user_id=1,
                claude_cmd=[sys.executable, FAKE], command_timeout_s=3)
    base.update(kw)
    return Config(**base)


def set_mode(mode):
    os.environ["FAKE_CLAUDE_MODE"] = mode


class TestBuildCmd(unittest.TestCase):
    def test_minimal(self):
        c = engine.build_cmd(cfg(), session_id=None, system_prompt=None)
        self.assertEqual(c[:2], [sys.executable, FAKE])
        self.assertIn("-p", c)
        self.assertIn("json", c[c.index("--output-format") + 1])
        self.assertNotIn("--allowedTools", c)
        self.assertNotIn("--resume", c)
        self.assertNotIn("--model", c)

    def test_full(self):
        c = engine.build_cmd(cfg(claude_model="opus"), session_id="s1", system_prompt="be kind")
        self.assertEqual(c[c.index("--model") + 1], "opus")
        self.assertEqual(c[c.index("--resume") + 1], "s1")
        self.assertEqual(c[c.index("--system-prompt") + 1], "be kind")


class TestMakeEnv(unittest.TestCase):
    def test_strips_sensitive(self):
        os.environ["ANTHROPIC_API_KEY"] = "secret"
        os.environ["CLAUDECODE"] = "1"
        try:
            env = engine.make_env()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("CLAUDECODE", env)
            self.assertIn("PATH", env)
        finally:
            del os.environ["ANTHROPIC_API_KEY"]
            del os.environ["CLAUDECODE"]


class TestRunOnce(unittest.TestCase):
    def test_ok_reply_and_session(self):
        set_mode("ok")
        r = engine.run_once(cfg(), "hello there")
        self.assertTrue(r.ok)
        self.assertTrue(r.text.startswith("echo:hello"))
        self.assertEqual(r.session_id, "fake-session-123")

    def test_resume_keeps_session(self):
        set_mode("ok")
        r = engine.run_once(cfg(), "hi", session_id="keep-me")
        self.assertEqual(r.session_id, "keep-me")

    def test_malformed_json_falls_back_to_raw(self):
        set_mode("malformed")
        r = engine.run_once(cfg(), "hi")
        self.assertTrue(r.ok)
        self.assertIn("not json", r.text)

    def test_valid_json_non_object_falls_back_to_raw(self):
        set_mode("nonobject")
        r = engine.run_once(cfg(), "hi")
        self.assertTrue(r.ok)
        self.assertIn("[1, 2, 3]", r.text)
        self.assertIsNone(r.error_kind)

    def test_auth_retry_succeeds_second_try(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["FAKE_CLAUDE_STATE"] = str(Path(td) / "state")
            set_mode("auth_once")
            r = engine.run_once(cfg(), "hi")
            self.assertTrue(r.ok)

    def test_plain_failure_reports_nonzero(self):
        set_mode("exit1")
        r = engine.run_once(cfg(), "hi")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "nonzero")

    def test_timeout(self):
        set_mode("slow")
        r = engine.run_once(cfg(command_timeout_s=1), "hi")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "timeout")

    def test_cli_missing(self):
        set_mode("ok")
        r = engine.run_once(cfg(claude_cmd=["definitely-not-a-real-binary-xyz"]), "hi")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "cli_missing")


if __name__ == "__main__":
    unittest.main()
