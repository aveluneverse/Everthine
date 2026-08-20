"""Login watch: knows when the Claude CLI login will expire, and says so.

The Claude Code CLI signs in with a browser login that lasts about a month
(the CLI's own default is 30 days) and then must be renewed by a human in a
terminal (`claude auth login`, or /login inside claude); nothing automatic
can renew it. When it lapses, every engine call fails and, before
2026-08-20, the companion went silent behind a generic apology -- the
symptom a first outside user reported.

This module reads ONE number from the CLI's credential file -- the login's
expiry timestamp (claudeAiOauth.refreshTokenExpiresAt) -- and nothing else:
the tokens in that file never leave read_login_expiry(). A small background
loop (its own task, independent of the inner-life tick and of persona mode)
turns that number, plus engine.auth_broken(), into at most two kinds of plain
notice: a heads-up login_warn_days ahead (once a day), and "it has lapsed,
here is what to do" (once per episode). Both respect quiet hours. Fail-soft
everywhere: no file (macOS keeps the login in the Keychain), an unreadable
file, or a missing field all mean "unknown", never an exception; a failed
send is logged and retried next round.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from . import engine
from .config import Config
from .messages import msg

logger = logging.getLogger("everthine")

CREDENTIALS_FILENAME = ".credentials.json"
WATCH_INTERVAL_S = 300   # how often the loop looks; notices are rate-limited by state, not by this


def credentials_path(env: Mapping | None = None) -> Path:
    """Where the CLI keeps its login on Windows/Linux: $CLAUDE_CONFIG_DIR or
    ~/.claude, file .credentials.json (per the authentication docs)."""
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else Path.home() / ".claude"
    return root / CREDENTIALS_FILENAME


def read_login_expiry(path: Path | None = None) -> datetime | None:
    """The moment the saved login stops being renewable, as an aware local
    datetime, or None when unknown. Reads claudeAiOauth.refreshTokenExpiresAt
    (epoch milliseconds) and discards everything else in the file."""
    path = credentials_path() if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    ts = oauth.get("refreshTokenExpiresAt") if isinstance(oauth, dict) else None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def days_left(now: datetime, expiry: datetime) -> int:
    """Whole days until expiry, rounded up, floored at 1 while any time is
    left; 0 once it has passed. ("expires in 1 day" for anything under 24h.)"""
    remaining = (expiry - now).total_seconds()
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining / 86400))


def is_expired(now: datetime, expiry: datetime | None, broken: bool) -> bool:
    """Expired = the engine's last verdict was an auth failure with no success
    since, OR the file says the moment has passed. Either alone is enough:
    the engine catches what the file cannot (macOS, a wiped file, a refresh
    race), the file catches it before anyone asks him anything."""
    return broken or (expiry is not None and expiry <= now)


def in_quiet_hours(cfg: Config, now: datetime) -> bool:
    """Same arithmetic as scheduler.common_gate's quiet window (a window may
    wrap midnight; start == end means no window)."""
    start, end = cfg.quiet_start_hour, cfg.quiet_end_hour
    if start < end:
        return start <= now.hour < end
    if start > end:
        return now.hour >= start or now.hour < end
    return False


def new_state() -> dict:
    return {"expired_notified": False, "expiring_date": None}


def decide(cfg: Config, now: datetime, expiry: datetime | None, broken: bool,
           state: dict) -> tuple[str | None, int | None]:
    """Pure: what, if anything, to say this round.
    -> ("expired", None): say it has lapsed (once per episode, not at night)
    -> ("expiring", days): the heads-up (once a day inside the warn window)
    -> (None, None): nothing."""
    if is_expired(now, expiry, broken):
        if state.get("expired_notified") or in_quiet_hours(cfg, now):
            return None, None
        return "expired", None
    if expiry is None or cfg.login_warn_days <= 0:
        return None, None
    left = days_left(now, expiry)
    if left > cfg.login_warn_days:
        return None, None
    if state.get("expiring_date") == now.date() or in_quiet_hours(cfg, now):
        return None, None
    return "expiring", left


async def watch_once(app, cfg: Config, now: datetime, state: dict,
                     read_expiry=None, broken=None) -> str | None:
    """One round: read, decide, send at most one plain notice, update state
    only after the send succeeded. Returns the action sent, or None.

    read_expiry/broken default to None and are resolved to
    read_login_expiry/engine.auth_broken below, inside the body, rather
    than as early-bound default values -- a plain `=read_login_expiry`
    default captures that function object once, at import time, so a
    later mock.patch.object(login_watch, "read_login_expiry", ...) could
    never reach a call that relies on the default (watch_loop's does).
    Resolving the bare names here instead means each call looks them up
    fresh from this module's namespace, so a patch is always honored."""
    read_expiry = read_login_expiry if read_expiry is None else read_expiry
    broken = engine.auth_broken if broken is None else broken
    expiry = await asyncio.to_thread(read_expiry)
    broken_now = bool(broken())
    if not is_expired(now, expiry, broken_now):
        state["expired_notified"] = False      # the episode, if any, is over
    action, days = decide(cfg, now, expiry, broken_now, state)
    if action is None:
        return None
    text = msg("auth") if action == "expired" else msg("auth_expiring").format(days=days)
    try:
        await app.bot.send_message(chat_id=cfg.authorized_user_id, text=text)
    except Exception:
        logger.warning("login-watch: could not send the %s notice; will retry next round",
                       action, exc_info=True)
        return None
    if action == "expired":
        state["expired_notified"] = True
        logger.info("login-watch: told her the Claude login has lapsed (fix: claude auth login)")
    else:
        state["expiring_date"] = now.date()
        logger.info("login-watch: heads-up sent, %s day(s) left (renew: claude auth login)", days)
    return action


def log_boot_status(cfg: Config, now: datetime | None = None,
                    read_expiry=None) -> None:
    """One honest line at boot about the login's horizon.

    read_expiry defaults to None and is resolved to read_login_expiry
    below, inside the body, not as an early-bound default value -- see
    watch_once's docstring for why: it is what lets start()'s unparameterized
    log_boot_status(cfg) call still honor a test's
    mock.patch.object(login_watch, "read_login_expiry", ...)."""
    now = now or datetime.now().astimezone()
    read_expiry = read_login_expiry if read_expiry is None else read_expiry
    expiry = read_expiry()
    if expiry is None:
        logger.info("login-watch: cannot read when the Claude login expires (no credential "
                    "file here; on macOS the login lives in the Keychain). He will say so "
                    "the moment a reply fails for that reason.")
    elif expiry <= now:
        logger.warning("login-watch: the Claude login on this computer has ALREADY expired "
                       "(%s). Open a terminal and run: claude auth login", expiry.date())
    else:
        logger.info("login-watch: Claude login expires %s (%s day(s) left); renew any time "
                    "with: claude auth login", expiry.date(), days_left(now, expiry))


async def watch_loop(app, cfg: Config, state: dict | None = None) -> None:
    """Sleep first (boot is never notice time), then one watch_once per
    round, forever; a failing round is logged and never ends the loop; only
    CancelledError passes through."""
    state = new_state() if state is None else state
    while True:
        await asyncio.sleep(WATCH_INTERVAL_S)
        now = datetime.now().astimezone()
        try:
            await watch_once(app, cfg, now, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("login-watch: iteration failed", exc_info=True)


def start(app, cfg: Config) -> None:
    """Arm the watch from the app's startup hook (same asyncio.create_task
    reasoning as scheduler.start_tick: the hook runs on the application's own
    loop before Application.start(); the Task is parked in bot_data so it is
    never garbage-collected mid-flight). LOGIN_WATCH_ENABLED=false is the
    L1 rollback: nothing armed, one INFO line."""
    if not cfg.login_watch_enabled:
        logger.info("login-watch: disabled (LOGIN_WATCH_ENABLED=false)")
        return
    log_boot_status(cfg)
    app.bot_data["_login_watch_task"] = asyncio.create_task(watch_loop(app, cfg))
    logger.info("login-watch: armed (heads-up %s day(s) ahead)", cfg.login_warn_days)
