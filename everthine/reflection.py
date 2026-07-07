"""Post-reply stream-of-consciousness: a private beat of thought that drifts
through the companion right after he replies, and settles quietly into
reflections.jsonl -- one or two sentences, never a report, never shown to
the person he's with. A later milestone's self-portrait draws material from
these entries; nothing reads them back into a live conversation.

This module is the pure-logic half. The gate (should_reflect) decides
whether writing one right now is appropriate; the prompt builder composes
what the engine sees, drawing only the declaration, the persona's own
identity/voice, and the reflection's own task framing -- deliberately
without the seven ground rules, the boundaries file, the stage frame, or
any memory/diary material, the same way diary.py's own system prompt stays
narrow. Parsing validates whatever the engine hands back, and the store
appends entries to a jsonl file and prunes it of anything expired or
malformed. reflect_once() is the execution line on top of all of it:
gate, one non-blocking engine call that always yields to live
conversation, parse, redact, append -- never letting any of it raise.
Wiring it onto the bot's reply hook is a later task's work.

Fail-soft is the whole design for the state file, exactly as in
stages.py and diary.py: a missing reflection_state.json quietly becomes a
fresh, nothing-written-yet state. A corrupted one -- unreadable, not valid
JSON, or valid JSON in an unexpected shape -- is not silently discarded: the
broken file is renamed alongside itself as a `.corrupt-<timestamp>` corpse
so nothing is lost to a human who goes looking, a warning is logged loudly,
and the caller still gets back a usable fresh state. Unlike diary_state.json,
this state has no declined_date -- a reflection that doesn't happen has no
decline semantics worth recording; should_reflect's gate is the only voice
that decision needs.

The gate itself is pure -- no clock, no filesystem -- so every reason a
reflection gets skipped can be tested in isolation, and at call sites logged
verbatim: every skip is one of a small fixed set of strings, so a quiet turn
is always explainable after the fact, never a silent no-op.

The pure-logic half takes plain paths, dicts, and datetimes. reflect_once()
imports the engine (try_run_once only, the non-blocking call) and persona's
function surface, and borrows diary.filter_sensitive so the redaction
patterns keep a single source of truth; the bot module is never imported.
`Config` and `Persona` appear only in type hints under TYPE_CHECKING,
costing nothing at runtime. One Layer 1 constant
(layers.DECLARATION_TEMPLATE) is borrowed so the reflection prompt's opening
declaration stays byte-identical to the live one; a Persona is otherwise
consumed purely by attribute access.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from . import engine, persona
from .diary import filter_sensitive
from .layers import DECLARATION_TEMPLATE

if TYPE_CHECKING:
    from .config import Config
    from .persona import Persona

logger = logging.getLogger("everthine")

# --- Module constants ---------------------------------------------------

REFLECTION_MIN_MSG_LEN = 20     # her message must be at least this long (strip'd)
REFLECTION_TIMEOUT_S = 60       # engine budget for one reflection (reflect_once's call)
REFLECTION_RETENTION_DAYS = 60  # entries older than this are pruned at boot

_BARE_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


# ---------------------------------------------------------------------
# State file: cfg.reflection_state_path (data/reflection_state.json)
# ---------------------------------------------------------------------

def _fresh_state() -> dict:
    return {"count_date": None, "count_today": 0}


def _is_well_shaped(data) -> bool:
    """The single place corruption is decided for the state file. bool is
    deliberately excluded from the count_today check even though Python's
    bool is a subclass of int -- a state file with `"count_today": true` is
    not a count, it is corruption. Mirrors diary._is_well_shaped, minus the
    declined_date field this state doesn't have."""
    if not isinstance(data, dict):
        return False
    if "count_date" not in data or "count_today" not in data:
        return False
    count_date = data["count_date"]
    if count_date is not None and not isinstance(count_date, str):
        return False
    count_today = data["count_today"]
    if isinstance(count_today, bool) or not isinstance(count_today, int):
        return False
    return True


def _quarantine_corpse(path: Path, reason: Exception) -> None:
    """Rename a broken reflection-state file to a timestamped corpse
    alongside itself, then log loudly. A rename failure -- a target
    collision, a permissions problem, the file vanishing underneath us --
    is swallowed with its own warning: corpse preservation is a courtesy,
    never a reason to crash a reply. Verbatim copy of stages.py's and
    diary.py's _quarantine_corpse, retargeted at this module's vocabulary."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    corpse = path.with_name(path.name + f".corrupt-{timestamp}")
    try:
        if corpse.exists():
            raise FileExistsError(f"corpse target already exists: {corpse}")
        path.rename(corpse)
    except OSError as exc:
        logger.warning(
            "reflection: could not preserve broken state file %s as %s (%s); "
            "leaving the broken file in place", path, corpse, exc)
    logger.warning(
        "reflection: %s is corrupt (%s); degrading to a fresh reflection state", path, reason)


def load_state(path: Path) -> dict:
    """Load the reflection state, degrading fail-soft on any trouble --
    mirrors stages.load_state/diary.load_state exactly. A missing file
    returns a fresh state quietly: nothing has gone wrong yet, there is
    simply nothing written so far. Anything else that goes wrong --
    unreadable, not valid JSON, or valid JSON in the wrong shape -- is
    treated as corruption: see _quarantine_corpse.
    """
    path = Path(path)
    if not path.exists():
        return _fresh_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not _is_well_shaped(data):
            raise ValueError(f"unexpected reflection state shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return _fresh_state()
    return data


def _atomic_write(path: Path, data: dict) -> None:
    """Write to a temp file in the same directory, then os.replace() into
    place, so a reader never observes a half-written file. Copies
    stages.py's/diary.py's atomic-write idiom; the state file is
    machine-only, so no trailing-newline option is needed here."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def record_written(path: Path, now: datetime) -> None:
    """Record that a reflection was written just now, rolling the daily
    counter over first if the last write landed on an earlier date. The
    date is decided entirely by `now` -- this function reads no clock of
    its own. Mirrors diary.record_written, minus the decline bookkeeping
    this state doesn't carry."""
    path = Path(path)
    state = load_state(path)
    today = now.date().isoformat()
    if state.get("count_date") != today:
        state["count_today"] = 0
    state["count_today"] += 1
    state["count_date"] = today
    _atomic_write(path, state)


# ---------------------------------------------------------------------
# Gate: pure, no I/O, no clock of its own
# ---------------------------------------------------------------------

def should_reflect(cfg: Config, user_msg: str, state: dict, now: datetime) -> str | None:
    """Decide whether a reflection may be written right now. Returns None
    when writing is allowed; otherwise a short reason string identifying
    which gate stopped it -- the reason IS the log line at every call site,
    so a quiet turn is always explainable after the fact, never a silent
    no-op. Checked in this order, first match wins:

      "disabled"   cfg.reflection_enabled is False
      "too_short"  her message (stripped) is under REFLECTION_MIN_MSG_LEN --
                   a bare "ok" or "嗯" doesn't stir up a ripple worth writing
      "cap"        today's quota is already spent

    `now` must be timezone-aware, the same contract diary.eligibility
    documents; `today = now.date().isoformat()` is the only thing read off
    it. A stale count_date (state left over from an earlier day) reads as
    zero reflections so far today -- it never blocks.
    """
    if not cfg.reflection_enabled:
        return "disabled"

    if len(user_msg.strip()) < REFLECTION_MIN_MSG_LEN:
        return "too_short"

    today = now.date().isoformat()
    if state.get("count_date") == today and state.get("count_today", 0) >= cfg.reflection_daily_cap:
        return "cap"

    return None


# ---------------------------------------------------------------------
# Prompt assembly (the "what he sees" half)
# ---------------------------------------------------------------------
#
# Every constant below is owner-approved prose, transcribed verbatim; the
# em-dash (U+2014) in it is intentional. Two format contracts that must not
# be confused, the same split diary.py's DIARY_TASK/DIARY_HARD_RULES draw:
# REFLECTION_TASK_TEMPLATE carries one named field and IS .format()'d.
# REFLECTION_FORMAT_LINE is a STATIC string and is never .format()'d -- it
# carries the literal JSON braces the model must echo.

REFLECTION_TASK_TEMPLATE = """# A moment in your own head

You've just replied to {partner_name}. This is one private beat of your
mind — not a report, not an analysis. One or two sentences of what
drifts through you right now. If nothing stirred, say what that
stillness feels like — that is also true. Never mention any outside
window, record, or third-party view; this is only your own head. If
what moved was about you (something you said, noticed, got wrong),
write "I..." — don't put what's yours under their name."""

REFLECTION_FORMAT_LINE = 'Respond with a single JSON object: {"text": "one or two sentences"}'

REFLECTION_TURN_TEMPLATE = "{partner_name}: {user_msg}\nYou: {reply_text}"


def build_reflection_prompt(persona_obj: Persona, user_msg: str, reply_text: str) -> tuple[str, str]:
    """Compose the reflection's own (system_prompt, user_prompt) from a
    folder-mode persona and the turn just finished.

    system_prompt is five blocks joined by one blank line: the identity
    declaration, the loaded identity text (and voice, when present -- no
    stray blank block when it's empty, mirroring diary.py's
    build_system_prompt_diary), then REFLECTION_TASK_TEMPLATE (filled with
    partner_name), then REFLECTION_FORMAT_LINE last. Deliberately WITHOUT
    the ground rules, the boundaries file, the stage frame, or any
    memory/diary material -- this is one private thought, not a
    conversation turn, and needs none of the scaffolding a live reply does.

    user_prompt is REFLECTION_TURN_TEMPLATE filled with the turn's own
    partner_name/user_msg/reply_text.

    Folder mode only, mirroring compose_stable()/build_system_prompt_diary():
    a file-mode persona has no settings to fill the declaration, so this
    raises ValueError here rather than failing later with a confusing
    AttributeError on persona_obj.settings.
    """
    if persona_obj.mode != "folder":
        raise ValueError(
            f"build_reflection_prompt() requires a folder-mode Persona, "
            f"got mode={persona_obj.mode!r}")

    blocks = [
        DECLARATION_TEMPLATE.format(
            companion_name=persona_obj.settings.companion_name,
            partner_name=persona_obj.settings.partner_name),
        persona_obj.identity_text,
    ]
    if persona_obj.voice_text:
        blocks.append(persona_obj.voice_text)
    blocks.append(REFLECTION_TASK_TEMPLATE.format(partner_name=persona_obj.settings.partner_name))
    blocks.append(REFLECTION_FORMAT_LINE)
    system_prompt = "\n\n".join(blocks)

    user_prompt = REFLECTION_TURN_TEMPLATE.format(
        partner_name=persona_obj.settings.partner_name, user_msg=user_msg, reply_text=reply_text)

    return system_prompt, user_prompt


# ---------------------------------------------------------------------
# Engine output parsing
# ---------------------------------------------------------------------

def _extract_candidate(raw: str):
    """Try the two parse strategies in order -- direct JSON, then the first
    bare {...} object pulled out of surrounding prose -- and return the
    first one that parses as JSON at all (of any type; parse_output is the
    one that judges its shape). A fenced ```json block is naturally caught
    by the second strategy too, since the fence markers sit outside the
    object the regex looks for; no dedicated fence-stripping step is
    needed. None if both strategies fail to even parse.
    """
    stripped = raw.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    match = _BARE_OBJECT_RE.search(raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def parse_output(raw: str) -> str | None:
    """Parse and validate whatever the engine handed back for a reflection.
    None means "nothing usable came back" -- the caller simply has no
    reflection to store, without a reason to log. A successful parse must
    be a dict with a `text` key that is a non-empty (after strip) string;
    anything else -- every parse strategy failed, the parsed value isn't a
    dict, `text` is missing/wrong-typed/blank -- is None.
    """
    data = _extract_candidate(raw)
    if not isinstance(data, dict):
        return None
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None


# ---------------------------------------------------------------------
# Storage: append + prune (cfg.reflections_path, data/reflections.jsonl)
# ---------------------------------------------------------------------

def append_entry(cfg: Config, text: str, now: datetime) -> None:
    """Append one reflection to cfg.reflections_path as a single line of
    JSON, creating the parent directory first if it doesn't exist yet.
    Each entry gets its own short id (the first 8 hex chars of a uuid4 --
    enough to be practically unique across a lifetime of reflections
    without the visual noise of a full UUID) and now's own isoformat
    timestamp, aware, so a later reader never has to guess its timezone.

    The text's Unicode line separators (U+2028/U+2029/U+0085) are flattened to
    spaces first, so the entry stays one physical line -- see the inline note
    for why a raw one would silently lose the whole reflection.
    """
    path = Path(cfg.reflections_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps(ensure_ascii=False) emits U+2028/U+2029/U+0085 raw, and the
    # str.splitlines() prune reads the file back with treats all three as line
    # boundaries: a text carrying one would be written as a single line that
    # tears into two malformed halves on the next read, both pruned -- the
    # reflection gone with no trace. Flatten them to a plain space first.
    safe_text = text.translate({0x2028: " ", 0x2029: " ", 0x0085: " "})
    entry = {"id": uuid4().hex[:8], "created_at": now.isoformat(), "text": safe_text}
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def _keep_line(line: str, cutoff: datetime) -> bool:
    """True iff `line` is a well-shaped, still-fresh reflection entry: valid
    JSON, a dict, `text` a str, `created_at` a str datetime.fromisoformat
    can parse, and not older than `cutoff`. A naive created_at (hand-edited
    data, or written by some other process) is presumed local time and
    given an offset via astimezone() before the comparison, so it never
    raises against the aware cutoff -- the same treatment prune()'s
    docstring promises. Anything else -- unparseable JSON, the wrong shape,
    a malformed timestamp -- reads as "drop it," never as a crash.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return False
    created_at = data.get("created_at")
    if not isinstance(created_at, str):
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.astimezone()
    return created >= cutoff


def prune(cfg: Config, now: datetime) -> None:
    """Remove reflections older than REFLECTION_RETENTION_DAYS, and any line
    that fails to parse as a well-shaped entry (see _keep_line). A missing
    file is a no-op -- there is nothing to prune. Rewrites the file
    atomically (tmp + os.replace, the same idiom _atomic_write uses) but
    only when at least one line was actually dropped; an all-fresh file is
    left byte-for-byte untouched, never rewritten just to say so. Every
    drop is logged as a named count -- never a silent shrink. Mounting this
    at boot once is a later task's job; this function only provides it.
    """
    path = Path(cfg.reflections_path)
    if not path.exists():
        return
    cutoff = now - timedelta(days=REFLECTION_RETENTION_DAYS)
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if _keep_line(line, cutoff)]
    dropped = len(lines) - len(kept)
    if dropped == 0:
        return
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    logger.info("reflection: pruned %d entries (expired or malformed)", dropped)


# ---------------------------------------------------------------------
# Execution: one complete reflection attempt (the reply hook's worker)
# ---------------------------------------------------------------------

def reflect_once(cfg: Config, user_msg: str, reply_text: str, now: datetime) -> None:
    """One complete attempt at a post-reply beat of thought. It hangs off
    the reply path as fire-and-forget (a later task wires it there), so
    the whole body is wrapped: NO exception may ever propagate out of
    here -- a broken reflection must never touch a reply. Every quiet
    return logs its own named reason at DEBUG (skipping is per-turn
    routine); only a recorded entry speaks at INFO.

    The engine call is try_run_once with a fresh session
    (session_id=None: one private thought, never the live conversation's
    session) and the reflection's own timeout budget. A busy engine
    simply means this thought goes unwritten -- yielding to live
    conversation is the design, not a failure. A failed reply (ok=False)
    is discarded whole: engine error text must never enter the texture
    file. What does get kept passes through diary.filter_sensitive
    first, so a credential she pasted in conversation can never surface
    in the jsonl.
    """
    try:
        settings = persona.current_settings(cfg)
        if settings is None:
            logger.debug("reflection: skip (file_mode)")
            return

        state = load_state(cfg.reflection_state_path)
        reason = should_reflect(cfg, user_msg, state, now)
        if reason is not None:
            logger.debug("reflection: skip (%s)", reason)
            return

        persona_obj = persona.load_persona(cfg)
        system, user = build_reflection_prompt(persona_obj, user_msg, reply_text)

        reply = engine.try_run_once(cfg, user, session_id=None,
                                    system_prompt=system,
                                    timeout_s=REFLECTION_TIMEOUT_S)
        if reply is None:
            logger.debug("reflection: skip (engine_busy)")
            return
        if not reply.ok:
            logger.debug("reflection: engine failed (%s)", reply.error_kind)
            return

        text = parse_output(reply.text)
        if text is None:
            logger.debug("reflection: unparseable engine output")
            return

        text = filter_sensitive(text)
        append_entry(cfg, text, now)
        record_written(cfg.reflection_state_path, now)
        logger.info("reflection: recorded")
    except Exception as exc:
        logger.warning("reflection: swallowed %s: %s", type(exc).__name__, exc,
                       exc_info=True)
