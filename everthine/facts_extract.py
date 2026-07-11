"""D1 extraction pipeline: the extractor that reads the conversation archive
and distils durable facts into facts.py's book.

facts.py is the pure, dependency-light core -- the store, the extraction
cursor, the bigram ranking/dedup, and the eligibility gate -- and its own
docstring promises to STAY that way: "Both sit on top of what is written here
and add the wiring; this file stays dependency-light and testable on its own
... it imports only the standard library at runtime, with Config appearing
solely in a TYPE_CHECKING hint." This module is that wiring, kept deliberately
in a sibling so the core's promise holds: here is where the archive, the
engine, and the persona are imported, where the extractor's system prompt
lives, and where one background tick's worth of work -- eligibility -> material
-> engine -> parse -> store -- is stitched into extract_once, the proactive
twin of diary.write_once.

Four moving parts, mirroring the diary's shape:

  * EXTRACTOR_SYSTEM_PROMPT -- the memory-extraction assistant's instructions,
    owner-approved prose transcribed verbatim; .format()'d with the partner's
    name and today's date (hence the doubled braces around its JSON examples).
  * build_material -- the since-cursor slice of the archive, rendered one line
    per turn in the archive's own speaker labels and tail-truncated to newest
    whole lines, returned alongside the timestamp that becomes the next cursor.
  * parse_extractor_output / normalise_facts -- pure functions turning the
    engine's reply into clean fact dicts. parse deliberately distinguishes a
    genuine empty answer ([]) from a parse failure (None), so extract_once can
    advance the cursor on the first and retry the same material on the second.
  * extract_once -- one complete attempt, engine call included, that a later
    task hangs off the background tick. try_run_once (never run_once) with a
    fresh session, so extraction always yields to live conversation.

Every skip returns False behind its own named "everthine" log line, exactly as
diary.write_once does, so a quiet tick is always explainable after the fact.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from . import archive, engine, facts, persona

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("everthine")


# ---------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------

FACTS_MATERIAL_MAX_CHARS = 12000   # material cap before build_material tail-truncates
FACTS_TIMEOUT_S = 60               # engine budget for one extraction (extract_once's call)
FACT_TEXT_MAX_CHARS = 200          # each fact's text is truncated to this in normalise_facts

# The user-prompt instruction line that leads the material. Concatenated with
# the material, never .format()'d, so archive text carrying stray braces can
# never break interpolation.
EXTRACT_INSTRUCTION = "Extract memorable facts from this conversation:"

# Owner-approved prose, transcribed verbatim (SDD); the em-dash (U+2014) in
# rule 1 is intentional. Two real placeholders, {partner_name} and {today};
# every brace in the JSON examples is DOUBLED so .format() renders it as a
# literal brace. Never add a stray single brace here -- it would raise KeyError
# at format time.
EXTRACTOR_SYSTEM_PROMPT = """You are a memory extraction assistant. Your only job is to extract facts
worth remembering about {partner_name} from the conversation below.

Rules:
1. Output ONLY a JSON array — no markdown fences, no commentary.
2. Never extract intimate, sexual, or bodily content of any kind.
3. Only record things of real substance. Skip greetings, filler, and
   small talk that says nothing about who they are or what is going on
   in their life.
4. Each entry looks like:
   {{"date": "YYYY-MM-DD", "content": "concrete statement", "category": "one of the six"}}
5. category must be exactly one of:
   interest / mood / stress / follow_up / life_event / conflict
6. Write each fact in the same language the conversation is in.
7. If nothing is worth keeping, output [].
8. Today's date: {today}

Again: output a bare JSON array and nothing else, e.g.
[{{"date": "2026-07-11", "content": "prefers matcha latte over coffee", "category": "interest"}}]"""


# ---------------------------------------------------------------------
# Newest-user timestamp: eligibility's "has she spoken, and how recently"
# ---------------------------------------------------------------------

def newest_user_ts(cfg: Config) -> datetime | None:
    """The timestamp of the newest archive entry from the partner (speaker
    "user"), or None when the partner has never spoken. One pass over the
    archive, tracking the MAXIMUM user timestamp rather than trusting iteration
    order to already be chronological (the same caution scheduler.truth_timeline
    takes). Returns the archive's own aware datetime.

    This is extract_once's single source for eligibility's last_user_ts, and it
    is deliberately NOT scheduler.truth_timeline: that returns elapsed HOURS
    (not a timestamp the eligibility gate can compare against the cursor) and
    returns None unless BOTH speakers have written -- but facts must still gate
    correctly when only the partner has ever spoken (a companion that never
    replied must not silence extraction forever). See the task report for the
    full trade-off.
    """
    newest: datetime | None = None
    for entry in archive.iter_entries(cfg.archive_dir):
        if entry["speaker"] == "user":
            ts = entry["timestamp"]
            if newest is None or ts > newest:
                newest = ts
    return newest


# ---------------------------------------------------------------------
# Material assembly
# ---------------------------------------------------------------------

def _tail_truncate(lines: list[str], cap: int) -> list[str]:
    """Keep newest whole lines from the tail until one more would cross `cap`,
    never splitting a line down the middle (diary's cap idiom). The +1 per kept
    line past the first accounts for the "\\n" that rejoins them, so the
    survivors' rendered length stays within the cap. The newest line is always
    kept even if it alone exceeds the cap -- an over-long single turn still
    yields material, never an empty string (unlike the diary, this module
    prepends no omission line to fall back on)."""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        addition = len(line) + (1 if kept else 0)
        if kept and total + addition > cap:
            break
        kept.append(line)
        total += addition
    kept.reverse()
    return kept


def build_material(cfg: Config, cursor_ts: datetime | None) -> tuple[str, str] | None:
    """Render the conversation since the cursor into extractor material, or
    None when there is nothing new to read.

    Collects archive.iter_entries(cfg.archive_dir, since=cursor_ts) -- a None
    cursor (never extracted) reads the whole archive; a set cursor reads every
    turn at or after it (iter_entries is inclusive at the boundary). Each entry
    becomes one "<speaker>: <text>" line in the archive's OWN speaker labels,
    joined newest-last; over FACTS_MATERIAL_MAX_CHARS the record is
    tail-truncated to its newest whole lines.

    Returns (material_text, newest_included_ts_iso): the ISO timestamp of the
    newest entry still in the material, which becomes the next cursor on
    success. Truncation only ever drops the OLDEST lines, so the newest entry is
    always included; its timestamp is taken as a max over the kept entries (not
    the last one yielded) to stay correct even if a line arrived out of order.
    No entries at all -> None.
    """
    entries = list(archive.iter_entries(cfg.archive_dir, since=cursor_ts))
    if not entries:
        return None
    lines = [f"{entry['speaker']}: {entry['text']}" for entry in entries]
    if len("\n".join(lines)) > FACTS_MATERIAL_MAX_CHARS:
        kept = _tail_truncate(lines, FACTS_MATERIAL_MAX_CHARS)
        entries = entries[len(entries) - len(kept):]
        lines = kept
    material = "\n".join(lines)
    newest_ts = max(entry["timestamp"] for entry in entries)
    return material, newest_ts.isoformat()


# ---------------------------------------------------------------------
# Parse + normalise (pure)
# ---------------------------------------------------------------------

_PARSE_FAILED = object()


def _try_json_list(text: str):
    """json.loads(text) coerced to the parse contract: a JSON array -> that
    list; valid JSON of any other shape -> [] (a real answer, just nothing to
    keep); not valid JSON at all -> the _PARSE_FAILED sentinel."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _PARSE_FAILED
    return data if isinstance(data, list) else []


def parse_extractor_output(text: str) -> list | None:
    """Parse the engine's reply into a list of raw fact dicts.

    Tries a direct json.loads first, then -- only if that fails to parse -- the
    slice between the first "[" and the last "]" (peeling markdown fences or
    surrounding prose off a bare array). The return deliberately distinguishes
    two empties the cursor MUST treat differently (see extract_once):

      * []   a genuine answer: valid JSON that was an empty array, or valid
             JSON of some non-array shape -- either way the model spoke and
             there is nothing to keep, so the material WAS examined and the
             cursor advances.
      * None a parse FAILURE: no strategy produced valid JSON. Transient model
             misbehaviour -- the cursor stays put and the same material is
             retried next tick. A truncated preview is logged at warning.
    """
    parsed = _try_json_list(text)
    if parsed is not _PARSE_FAILED:
        return parsed
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        parsed = _try_json_list(text[start:end + 1])
        if parsed is not _PARSE_FAILED:
            return parsed
    logger.warning("facts: could not parse extractor output; preview=%r", text[:200])
    return None


def normalise_facts(raw: list, today_str: str) -> list[dict]:
    """Turn parsed extractor entries into clean fact dicts ready for
    append_facts. For each entry:

      - a non-dict entry is dropped;
      - the fact text is `content` (falling back to `text`), stripped -- an
        empty result drops the entry -- and truncated to FACT_TEXT_MAX_CHARS;
      - `category` defaults to "life_event" when missing/blank; any other
        non-blank string is kept as-is (unknown categories are tolerated here,
        the UI layer is what maps an unknown one to "other");
      - `date` defaults to today_str when missing/blank.

    Pure: no clock, no I/O -- today_str is the caller's. The output shape
    ({"text", "category", "date"}) is exactly what append_facts consumes.
    """
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        source = content if isinstance(content, str) and content.strip() else entry.get("text")
        text = source.strip() if isinstance(source, str) else ""
        if not text:
            continue
        text = text[:FACT_TEXT_MAX_CHARS]
        category = entry.get("category")
        category = category.strip() if isinstance(category, str) and category.strip() else "life_event"
        date_val = entry.get("date")
        date_val = date_val.strip() if isinstance(date_val, str) and date_val.strip() else today_str
        out.append({"text": text, "category": category, "date": date_val})
    return out


# ---------------------------------------------------------------------
# extract_once: one complete attempt (the tick's worker)
# ---------------------------------------------------------------------

def extract_once(cfg: Config, now: datetime) -> bool:
    """One complete extraction attempt. True only when at least one fact was
    stored; every other outcome returns False behind its own named "everthine"
    log line, mirroring diary.write_once's discipline, so a quiet tick is always
    explainable after the fact.

    `now` must be timezone-aware (eligibility's contract). The engine call is
    try_run_once -- never run_once -- with a fresh session (session_id=None: an
    extraction must never share, or leak into, the live conversation) and the
    extractor's own timeout budget. A busy engine is a skip, not a wait:
    extraction always yields to live conversation, and a later tick tries again.

    The extraction cursor (facts_state.json's last_extracted_ts) advances to the
    material's newest timestamp on every outcome that examined the material to a
    conclusion -- stored, all-duplicates, nothing-worth-keeping -- so the same
    silence is never re-read forever. It does NOT advance on engine_busy, an
    extraction failure, or a parse failure: each of those must retry the same
    material next tick. Entries that land in the archive DURING the engine call
    stay beyond the cursor for the next round, since the cursor is the material's
    snapshot timestamp, never `now` and never a re-scan after the call.

    Deliberately does NOT swallow unexpected exceptions: the background tick that
    will call this (a later task) wraps every round in its own try/except and
    logs it loudly; swallowing here too would only bury bugs.
    """
    state = facts.load_state(cfg.facts_state_path)
    cursor_raw = state.get("last_extracted_ts", "")
    cursor_ts: datetime | None = None
    if cursor_raw:
        try:
            cursor_ts = datetime.fromisoformat(cursor_raw)
        except ValueError:
            logger.warning("facts: unparseable cursor %r; treating as unset", cursor_raw)

    last_user_ts = newest_user_ts(cfg)

    reason = facts.eligibility(cfg, now, last_user_ts, cursor_ts)
    if reason is not None:
        # DEBUG, like diary's eligibility skips: disabled/idle_not_reached/
        # no_new_material are all-tick-normal, and INFO would flood the
        # every-few-minutes tick.
        logger.debug("facts: skip (%s)", reason)
        return False

    built = build_material(cfg, cursor_ts)
    if built is None:
        # After eligibility passes there is new user material, so this is only
        # reachable on a race (an entry vanishing between the two reads); logged
        # at the same DEBUG level and with the same reason string as the gate.
        logger.debug("facts: skip (no_new_material)")
        return False
    material, material_ts = built

    settings = persona.current_settings(cfg)
    if settings is None:
        # Defensive second layer: the tick gates folder mode at boot; a
        # file-mode persona has no partner name to voice the extractor with.
        logger.debug("facts: skip (file_mode)")
        return False

    today_str = now.date().isoformat()
    system_prompt = EXTRACTOR_SYSTEM_PROMPT.format(
        partner_name=settings.partner_name, today=today_str)
    prompt = EXTRACT_INSTRUCTION + "\n\n" + material
    reply = engine.try_run_once(cfg, prompt, session_id=None,
                                system_prompt=system_prompt, timeout_s=FACTS_TIMEOUT_S)
    if reply is None:
        logger.info("facts: skip (engine_busy)")
        return False
    if not reply.ok:
        logger.warning("facts: extraction failed (%s)", reply.error_kind)
        return False

    parsed = parse_extractor_output(reply.text)
    if parsed is None:
        # Parse FAILURE, not a genuine empty answer: do NOT advance the cursor,
        # so the same material is retried next tick. parse_extractor_output
        # already logged the preview at warning.
        return False

    normalised = normalise_facts(parsed, today_str)
    if not normalised:
        # A genuine empty answer, or nothing survived normalisation: the
        # material WAS examined, so advance the cursor -- otherwise the same
        # silence gets re-extracted every tick forever.
        facts.save_state(cfg.facts_state_path, {"last_extracted_ts": material_ts})
        logger.info("facts: nothing worth keeping")
        return False

    n = facts.append_facts(cfg.facts_path, normalised, cfg.facts_max)
    facts.save_state(cfg.facts_state_path, {"last_extracted_ts": material_ts})
    if n == 0:
        logger.info("facts: all duplicates")
        return False
    logger.info("facts: stored %d fact(s)", n)
    return True
