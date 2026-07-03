"""Claude Code CLI wrapper.

One blocking call per user message: prompt goes in via stdin (avoids OS
argv length limits), the reply comes back as a single JSON document.
A reply lock serializes calls; an auth-race is retried once.
stream_once() streams one reply as queue events for progressive display.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass

from .config import Config

logger = logging.getLogger("everthine")

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


def build_cmd(cfg: Config, session_id: str | None, system_prompt: str | None,
              streaming: bool = False) -> list:
    cmd = list(cfg.claude_cmd) + ["-p"]
    if streaming:
        cmd += ["--output-format", "stream-json",
                "--include-partial-messages", "--verbose"]
    else:
        cmd += ["--output-format", "json"]
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
    except OSError:
        return EngineReply("", session_id, ok=False, error_kind="nonzero")

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
        if data.get("is_error"):
            blob = str(data.get("result", ""))
            kind = "auth" if _looks_like_auth_error(blob) else "nonzero"
            return EngineReply("", session_id, ok=False, error_kind=kind)
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


def _stream_attempt(cfg: Config, prompt: str, session_id: str | None,
                    system_prompt: str | None, events, cancel) -> tuple:
    """One streaming subprocess run. Returns (reply, emitted_text, saw_auth)."""
    cfg.engine_home.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(cfg, session_id, system_prompt, streaming=True)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="ignore", env=make_env(), cwd=str(cfg.engine_home))
    except FileNotFoundError:
        return EngineReply("", session_id, ok=False, error_kind="cli_missing"), False, False
    except OSError:
        return EngineReply("", session_id, ok=False, error_kind="nonzero"), False, False

    try:
        state = {"last_activity": time.monotonic(),
                 "deadline": time.monotonic() + cfg.command_timeout_s,
                 "timed_out": False, "cancelled": False}

        def _guard():
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    state["cancelled"] = True
                    proc.kill()
                    return
                now = time.monotonic()
                if (now - state["last_activity"] > cfg.stream_stall_timeout_s
                        or now > state["deadline"]):
                    state["timed_out"] = True
                    proc.kill()
                    return
                time.sleep(0.25)

        threading.Thread(target=_guard, daemon=True).start()

        # The CLI reads the whole prompt before it starts generating, so writing
        # then closing stdin up front cannot deadlock at our prompt sizes.
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            proc.kill()
            proc.wait()
            return EngineReply("", session_id, ok=False, error_kind="nonzero"), False, False

        text_parts = []
        final_session = session_id
        result_error = False
        for line in proc.stdout:
            state["last_activity"] = time.monotonic()
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "stream_event":
                inner = event.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        text_parts.append(delta["text"])
                        events.put({"type": "text", "text": delta["text"]})
            elif event.get("type") == "result":
                final_session = event.get("session_id", final_session)
                if event.get("is_error"):
                    result_error = True

        stderr_tail = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        emitted = bool(text_parts)
        full = "".join(text_parts)

        if state["cancelled"]:
            return EngineReply(full, session_id, ok=False, error_kind="nonzero"), emitted, False
        if state["timed_out"]:
            return EngineReply(full, session_id, ok=False, error_kind="timeout"), emitted, False
        if proc.returncode != 0 or result_error:
            auth = _looks_like_auth_error(full + (stderr_tail or ""))
            kind = "auth" if auth else "nonzero"
            return EngineReply(full, session_id, ok=False, error_kind=kind), emitted, auth
        if not emitted:
            return EngineReply("", session_id, ok=False, error_kind="nonzero"), False, False
        return EngineReply(full, final_session, ok=True), True, False
    finally:
        # Close the pipes deterministically on every exit path: a long-running
        # process must not rely on GC to reclaim subprocess fds after kills.
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass


def stream_once(cfg: Config, prompt: str, session_id: str | None = None,
                system_prompt: str | None = None, events=None,
                cancel=None) -> None:
    """Thread target: stream one reply, pushing events onto `events`.

    Emits {"type": "text", "text": str} per text delta, then exactly one
    {"type": "done", "reply": EngineReply}. An auth failure is retried once,
    but only while no text has been emitted - the user never sees a rerun.
    The done event is guaranteed even if an attempt crashes unexpectedly.
    """
    try:
        with _reply_lock:
            for attempt in range(2):
                reply, emitted, saw_auth = _stream_attempt(
                    cfg, prompt, session_id, system_prompt, events, cancel)
                if saw_auth and not emitted and attempt == 0:
                    time.sleep(1.5)
                    continue
                break
    except Exception:
        # Never strand the consumer waiting on the queue: log the crash and
        # still deliver the terminal event (same style as the bot's handler).
        logger.error("streaming attempt crashed unexpectedly", exc_info=True)
        reply = EngineReply("", session_id, ok=False, error_kind="nonzero")
    events.put({"type": "done", "reply": reply})
