"""Claude Code CLI wrapper.

One blocking call per user message: prompt goes in via stdin (avoids OS
argv length limits), the reply comes back as a single JSON document.
A reply lock serializes calls; an auth-race is retried once.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass

from .config import Config

_reply_lock = threading.Lock()

_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_EFFORT_LEVEL",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

_AUTH_FINGERPRINTS = ("API Error: 401", '"type":"authentication_error"',
                      "Invalid authentication credentials")


@dataclass
class EngineReply:
    text: str
    session_id: str | None
    ok: bool
    error_kind: str | None = None


def make_env() -> dict:
    env = os.environ.copy()
    for key in _STRIP_ENV:
        env.pop(key, None)
    return env


def build_cmd(cfg: Config, session_id: str | None, system_prompt: str | None) -> list:
    cmd = list(cfg.claude_cmd) + ["-p", "--output-format", "json"]
    if cfg.claude_model:
        cmd += ["--model", cfg.claude_model]
    if session_id:
        cmd += ["--resume", session_id]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    return cmd


def _looks_like_auth_error(text: str) -> bool:
    return any(fp in text for fp in _AUTH_FINGERPRINTS)


def check_claude_available(cfg: Config) -> bool:
    try:
        r = subprocess.run(list(cfg.claude_cmd) + ["--version"], env=make_env(),
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_attempt(cfg: Config, prompt: str, session_id: str | None,
                 system_prompt: str | None) -> EngineReply:
    cfg.engine_home.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(cfg, session_id, system_prompt)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="ignore", env=make_env(), cwd=str(cfg.engine_home))
    except FileNotFoundError:
        return EngineReply("", session_id, ok=False, error_kind="cli_missing")

    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=cfg.command_timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return EngineReply("", session_id, ok=False, error_kind="timeout")

    combined = (stdout or "") + (stderr or "")
    if proc.returncode != 0:
        kind = "auth" if _looks_like_auth_error(combined) else "nonzero"
        return EngineReply("", session_id, ok=False, error_kind=kind)

    try:
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise TypeError("engine output is not a JSON object")
        return EngineReply(str(data.get("result", "")).strip() or stdout.strip(),
                           data.get("session_id", session_id), ok=True)
    except (json.JSONDecodeError, TypeError):
        text = (stdout or stderr or "").strip()
        return EngineReply(text, session_id, ok=bool(text),
                           error_kind=None if text else "nonzero")


def run_once(cfg: Config, prompt: str, session_id: str | None = None,
             system_prompt: str | None = None) -> EngineReply:
    with _reply_lock:
        for attempt in range(2):
            reply = _run_attempt(cfg, prompt, session_id, system_prompt)
            if reply.error_kind == "auth" and attempt == 0:
                time.sleep(1.5)
                continue
            return reply
    return reply
