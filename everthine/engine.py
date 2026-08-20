"""Claude Code CLI wrapper.

One blocking call per user message: prompt goes in via stdin (avoids OS
argv length limits), the reply comes back as a single JSON document.
A reply lock serializes calls; an auth-race is retried once. Failures are
classified (auth / rate_limited / timeout / cli_missing / nonzero), carry
the CLI's own words in error_detail, and are logged at WARNING.
try_run_once() is the non-blocking twin background inner activities use:
it gives up immediately when the lock is busy, so an inner write always
yields to live conversation instead of queueing ahead of it.
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


@dataclass
class EngineReply:
    text: str
    session_id: str | None
    ok: bool
    error_kind: str | None = None
    error_detail: str = ""


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


# Phrases the Claude CLI emits (stdout JSON "result", stream "result" event,
# or stderr, depending on version and path) when the saved login is the
# problem. Lower-case; matching is case-insensitive. Sources: observed on CLI
# 2.1.206 in -p mode, plus the official error reference
# (code.claude.com/docs/en/errors, "Not logged in" / "Login expired" /
# "OAuth token revoked" / "Invalid API key" / "API Error: 401").
_AUTH_FINGERPRINTS = (
    "api error: 401", '"type":"authentication_error"',
    "invalid authentication credentials",
    "not logged in", "please run /login", "run /login", "login expired",
    "failed to authenticate", "oauth session expired",
    "could not be refreshed", "oauth token revoked", "oauth token has expired",
    "oauth token expired", "re-authenticate", "invalid api key",
)
# The subscription's rolling allowance is used up (or the API throttled):
# a condition that lasts hours, not a glitch worth "say that again".
_RATE_LIMIT_FINGERPRINTS = (
    "hit your session limit", "hit your weekly limit", "hit your opus limit",
    "hit your limit", "usage limit reached", "rate_limit_error",
    "request rejected (429)", "api error: 429", "usage_cap_reached",
)
_DETAIL_MAX = 300


def classify_failure(text: str) -> str:
    """Name the failure a failed CLI run represents, from whatever it said:
    "auth" (the login is gone: only a human can fix it, by logging in again),
    "rate_limited" (the allowance is spent: waiting fixes it), else "nonzero"
    (a one-off worth retrying). Order matters only in that auth wins ties."""
    low = (text or "").lower()
    if any(fp in low for fp in _AUTH_FINGERPRINTS):
        return "auth"
    if any(fp in low for fp in _RATE_LIMIT_FINGERPRINTS):
        return "rate_limited"
    return "nonzero"


def _looks_like_auth_error(text: str) -> bool:
    return classify_failure(text) == "auth"


def _error_detail(result_text: str, stderr: str, stdout: str = "") -> str:
    """One log-line's worth of what the CLI said: its own result message when
    it gave one (head), else the stderr tail, else the stdout tail."""
    if result_text and result_text.strip():
        text = " ".join(result_text.split())
        return text[:_DETAIL_MAX]
    text = " ".join(((stderr or "").strip() or (stdout or "").strip()).split())
    return text[-_DETAIL_MAX:] if len(text) > _DETAIL_MAX else text


def _result_text(stdout: str) -> str:
    """The "result" field of a JSON result document, or "" when stdout is
    not one (then the raw text is the detail, handled by _error_detail)."""
    try:
        data = json.loads(stdout or "")
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(data, dict) and data.get("result"):
        return str(data.get("result"))
    return ""


# --- auth state: is the login broken right now? ------------------------
# Two wall-clock stamps, written after every FINAL engine outcome (after the
# one auth retry): the last auth failure and the last success. login_watch
# reads auth_broken() to tell her, once per episode, that a human login is
# needed even when she is not chatting (a diary or reach-out hit the wall).
_auth_state = {"failed_at": None, "ok_at": None}


def auth_broken() -> bool:
    failed, ok = _auth_state["failed_at"], _auth_state["ok_at"]
    return failed is not None and (ok is None or failed > ok)


def reset_auth_state() -> None:
    _auth_state["failed_at"] = None
    _auth_state["ok_at"] = None


def _note_outcome(reply: "EngineReply") -> None:
    """Record the final outcome of one run (the retry loop's verdict, never a
    single attempt) and log every failure with the CLI's own words, so the
    console finally shows WHY a reply died instead of just that it did.
    Always called while the caller holds _reply_lock, so stamps are ordered
    like the outcomes themselves; calling this after releasing the lock would
    let a concurrent caller's later outcome be overwritten by a stale,
    out-of-order timestamp from this one."""
    now = time.time()
    if reply.ok:
        _auth_state["ok_at"] = now
        return
    if reply.error_kind == "auth":
        _auth_state["failed_at"] = now
    logger.warning("engine: reply failed (%s): %s", reply.error_kind,
                   reply.error_detail or "no detail from the CLI")


def check_claude_available(cfg: Config) -> bool:
    try:
        r = subprocess.run(list(cfg.claude_cmd) + ["--version"], env=make_env(),
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_attempt(cfg: Config, prompt: str, session_id: str | None,
                 system_prompt: str | None,
                 timeout_s: int | None = None) -> EngineReply:
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
        stdout, stderr = proc.communicate(
            input=prompt,
            timeout=timeout_s if timeout_s is not None else cfg.command_timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return EngineReply("", session_id, ok=False, error_kind="timeout",
                           error_detail="no output within the time limit")

    combined = (stdout or "") + (stderr or "")
    result_text = _result_text(stdout)
    if proc.returncode != 0:
        return EngineReply("", session_id, ok=False,
                           error_kind=classify_failure(combined),
                           error_detail=_error_detail(result_text, stderr, stdout))

    try:
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise TypeError("engine output is not a JSON object")
        if data.get("is_error"):
            blob = str(data.get("result", ""))
            return EngineReply("", session_id, ok=False,
                               error_kind=classify_failure(blob),
                               error_detail=_error_detail(blob, stderr, stdout))
        return EngineReply(str(data.get("result", "")).strip() or stdout.strip(),
                           data.get("session_id", session_id), ok=True)
    except (json.JSONDecodeError, TypeError):
        text = (stdout or stderr or "").strip()
        return EngineReply(text, session_id, ok=bool(text),
                           error_kind=None if text else "nonzero",
                           error_detail="" if text else _error_detail("", stderr, stdout))


def _locked_run(cfg: Config, prompt: str, session_id: str | None,
                system_prompt: str | None,
                timeout_s: int | None) -> EngineReply:
    """Auth-retry loop shared by run_once/try_run_once. Caller holds _reply_lock."""
    reply = None
    for attempt in range(2):
        reply = _run_attempt(cfg, prompt, session_id, system_prompt, timeout_s)
        if reply.error_kind == "auth" and attempt == 0:
            time.sleep(1.5)
            continue
        break
    _note_outcome(reply)
    return reply


def run_once(cfg: Config, prompt: str, session_id: str | None = None,
             system_prompt: str | None = None) -> EngineReply:
    with _reply_lock:
        return _locked_run(cfg, prompt, session_id, system_prompt, None)


def try_run_once(cfg: Config, prompt: str, session_id: str | None = None,
                 system_prompt: str | None = None,
                 timeout_s: int | None = None) -> EngineReply | None:
    """Non-blocking twin of run_once: returns None immediately when the
    reply lock is busy. For background inner activities that must always
    yield to live conversation."""
    if not _reply_lock.acquire(blocking=False):
        return None
    try:
        return _locked_run(cfg, prompt, session_id, system_prompt, timeout_s)
    finally:
        _reply_lock.release()


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
                 "deadline": time.monotonic() + cfg.stream_total_timeout_s,
                 "timed_out": False, "cancelled": False}

        def _guard():
            # Two independent tripwires: the stall timeout catches a dead/hung
            # process (no output at all for a while), while the deadline below
            # only caps a truly runaway generation. It is deliberately generous
            # (stream_total_timeout_s, not the blocking-path command_timeout_s)
            # because long-form creative replies legitimately keep the CLI busy
            # - thinking or writing - for many minutes without ever going stale.
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
        result_text = ""
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
                    result_text = str(event.get("result") or "")

        stderr_tail = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        emitted = bool(text_parts)
        full = "".join(text_parts)

        if state["cancelled"]:
            return EngineReply(full, session_id, ok=False, error_kind="nonzero"), emitted, False
        if state["timed_out"]:
            return EngineReply(full, session_id, ok=False, error_kind="timeout",
                               error_detail="no output within the time limit"), emitted, False
        if proc.returncode != 0 or result_error:
            # The CLI puts its reason in the result event (2.1.206, -p mode),
            # older builds wrote to stderr, and some API errors arrive as text
            # deltas -- read all three, result text first.
            kind = classify_failure("\n".join((result_text, stderr_tail or "", full)))
            detail = _error_detail(result_text, stderr_tail or "", full)
            return (EngineReply(full, session_id, ok=False, error_kind=kind,
                                error_detail=detail), emitted, kind == "auth")
        if not emitted:
            return EngineReply("", session_id, ok=False, error_kind="nonzero",
                               error_detail=_error_detail("", stderr_tail or "", "")), False, False
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
    reply = None
    try:
        with _reply_lock:
            try:
                for attempt in range(2):
                    reply, emitted, saw_auth = _stream_attempt(
                        cfg, prompt, session_id, system_prompt, events, cancel)
                    if saw_auth and not emitted and attempt == 0:
                        time.sleep(1.5)
                        continue
                    break
            except Exception:
                # Never strand the consumer waiting on the queue: log the
                # crash and still deliver the terminal event (same style as
                # the bot's handler).
                logger.error("streaming attempt crashed unexpectedly", exc_info=True)
                reply = EngineReply("", session_id, ok=False, error_kind="nonzero")
            _note_outcome(reply)  # stamped under the same lock _locked_run uses
    finally:
        events.put({"type": "done", "reply": reply})
