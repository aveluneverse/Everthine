"""The weekly self-portrait: a snapshot of self-knowledge the companion
composes every so often by digesting recent diary pages and reflections,
then layers into its own persona underneath everything else -- not a new
fact to recite, but something closer to "who I've noticed I am lately."

The module holds the whole pipeline now: the state core (constants,
load/save of the single current snapshot, with a parallel dated history so
nothing written is ever lost to the next week's overwrite, and the pure
eligibility check deciding whether a new snapshot is due right now); what a
portrait actually reads from -- diary excerpts, reflection lines, and the
prompt that turns them into prose (build_material,
build_system_prompt_portrait); update_once, the generation pipeline that
calls the engine to attempt one complete snapshot; and portrait_block, the
Layer 1 renderer persona.build_system_prompt reads every turn to seat the
saved snapshot into the live persona. The background inner-life tick
(scheduler.py's tick_loop) hands update_once one attempt every round,
alongside diary.write_once and isolated from its failures. Nothing here
imports bot.py -- the wiring runs the other direction.

Fail-soft mirrors diary.py and reflection.py: a missing portrait.json
quietly means "nothing written yet". Unlike those two, though, there is no
fresh-but-existing state to hand back -- a portrait either exists or it
doesn't -- so load_portrait returns None outright rather than a placeholder
dict, and eligibility()'s "no previous version" branch is what actually
consumes that None. A corrupted file -- unreadable, not valid JSON, or JSON
that isn't even an object -- is preserved as a `.corrupt-<timestamp>` corpse
alongside itself (see _quarantine_corpse, a verbatim retargeting of
diary.py's own), logged loudly, and load_portrait still hands back None.

Saving keeps exactly four fields (updated/content/opinions/observations)
regardless of what a caller's entry dict carries beyond them, filters prose
through diary.filter_sensitive before it ever touches disk exactly as
diary.save_entry does, and writes twice: the live file at cfg.portrait_path,
and a same-content snapshot under cfg.portrait_history_dir named by that
day's date, so a second save on the same calendar day overwrites that day's
snapshot rather than piling up duplicates -- at most one per day, ever.

The eligibility check is pure -- no clock, no filesystem -- so every reason
a portrait write is skipped can be tested in isolation and, at call sites,
logged verbatim: "disabled", "interval_not_reached", or "no_new_material".
A first-ever portrait (portrait is None) is a special case throughout:
there is no interval to have not reached yet, and any diary entry at all
counts as new material. A previous portrait whose own `updated` field has
been hand-corrupted or is otherwise unreadable is never allowed to crash a
caller -- it degrades to being treated exactly like "no previous portrait",
loudly logged, rather than raising.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import diary, engine, persona
from .diary import filter_sensitive
from .layers import DECLARATION_TEMPLATE

if TYPE_CHECKING:
    from .config import Config
    from .persona import Persona

logger = logging.getLogger("everthine")

# --- Module constants ---------------------------------------------------

PORTRAIT_RECENT_DIARY = 7           # how many recent diary entries build_material digests
PORTRAIT_RECENT_REFLECTIONS = 15    # how many recent reflection lines build_material digests
PORTRAIT_TIMEOUT_S = 120            # engine budget for update_once's one compose call
PORTRAIT_CONTENT_MAX_CHARS = 4000   # save_portrait's tail-truncation cap on `content`
PORTRAIT_DIARY_SNIPPET_CHARS = 200  # per-entry diary snippet length build_material feeds in
PORTRAIT_OPINIONS_STORED_CAP = 10   # save_portrait's cap on stored `opinions`
PORTRAIT_OBSERVATIONS_STORED_CAP = 10  # save_portrait's cap on stored `observations`
PORTRAIT_OPINIONS_PROMPT_CAP = 5    # how many stored opinions resurface in the live Layer 1 block (portrait_block)


# ---------------------------------------------------------------------
# State file: cfg.portrait_path (data/portrait.json), with a dated
# history snapshot alongside it under cfg.portrait_history_dir
# ---------------------------------------------------------------------

def _quarantine_corpse(path: Path, reason: Exception) -> None:
    """Rename a broken portrait file to a timestamped corpse alongside
    itself, then log loudly. A rename failure -- a target collision, a
    permissions problem, the file vanishing underneath us -- is swallowed
    with its own warning: corpse preservation is a courtesy, never a reason
    to crash a reply. Verbatim copy of diary.py's and reflection.py's
    _quarantine_corpse, retargeted at this module's own vocabulary."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    corpse = path.with_name(path.name + f".corrupt-{timestamp}")
    try:
        if corpse.exists():
            raise FileExistsError(f"corpse target already exists: {corpse}")
        path.rename(corpse)
    except OSError as exc:
        logger.warning(
            "portrait: could not preserve broken file %s as %s (%s); "
            "leaving the broken file in place", path, corpse, exc)
    logger.warning(
        "portrait: %s is corrupt (%s); degrading to no previous portrait", path, reason)


def load_portrait(cfg: Config) -> dict | None:
    """Load the current self-portrait, fail-soft on any trouble. None means
    "there is no previous portrait" -- either nothing has ever been written
    (a missing file, the ordinary case before the first snapshot) or the
    file on disk was unreadable or malformed, in which case it is preserved
    as a `.corrupt-<timestamp>` corpse (see _quarantine_corpse) and a
    warning is logged loudly. Corruption here is judged only by "is this
    valid JSON, and is its top level an object" -- narrower than diary/
    reflection's state-shape checks, because a hand-corrupted `updated`
    field inside an otherwise well-formed dict is eligibility()'s problem
    to degrade gracefully from, not load_portrait's to quarantine over.
    """
    path = Path(cfg.portrait_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"unexpected portrait shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return None
    return data


def _atomic_write(path: Path, data: dict) -> None:
    """Write to a temp file in the same directory, then os.replace() into
    place, so a reader never observes a half-written file. Copies diary.py's
    atomic-write idiom; unlike diary's, this module never needs the
    trailing-newline option -- neither the live file nor a history snapshot
    is meant to be opened and read by a human the way a diary entry is."""
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


def _clean_opinions(raw) -> list:
    """Keep only well-shaped {"topic": str, "opinion": str} elements for
    save_portrait's `opinions` field. A well-shaped element is kept, but only
    its two string fields -- any extra keys it carries are dropped; anything
    not a dict, or a dict missing either string field, is discarded outright.
    Capped at PORTRAIT_OPINIONS_STORED_CAP, keeping the first entries in
    caller order."""
    if not isinstance(raw, list):
        return []
    cleaned = [
        {"topic": item["topic"], "opinion": item["opinion"]}
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("topic"), str)
        and isinstance(item.get("opinion"), str)
    ]
    return cleaned[:PORTRAIT_OPINIONS_STORED_CAP]


def _clean_observations(raw) -> list:
    """Keep only str elements for save_portrait's `observations` field,
    discarding anything else (None, numbers, dicts, nested lists...)
    outright. Capped at PORTRAIT_OBSERVATIONS_STORED_CAP, keeping the first
    entries in caller order."""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)][:PORTRAIT_OBSERVATIONS_STORED_CAP]


def save_portrait(cfg: Config, entry: dict, now: datetime) -> Path:
    """Persist a new self-portrait snapshot. Only four fields are ever
    written -- `updated` is always computed fresh here from `now`, never
    taken from `entry` even if it happens to carry its own.

    Contract: `now` must be timezone-aware, the same contract diary.py's
    eligibility() and build_material() document. `updated` is
    now.date().isoformat() -- the caller's own local calendar day, not a
    UTC day; whichever timezone `now` carries is what lands on disk.

    `content` is redacted through diary.filter_sensitive first (the same
    filter diary entries are saved through), then tail-truncated to
    PORTRAIT_CONTENT_MAX_CHARS with a warning logged only when truncation
    actually happens. `opinions`/`observations` are shape-filtered and
    capped -- see _clean_opinions/_clean_observations; a missing or
    wrong-typed field in `entry` defaults to an empty list rather than
    being omitted, so every portrait on disk has the same shape.

    Writes twice: the live file at cfg.portrait_path (atomic, mirroring
    diary._atomic_write) and a byte-identical history snapshot at
    cfg.portrait_history_dir / f"{updated}.json". A second save on the same
    calendar day overwrites that day's snapshot rather than adding a second
    one -- history holds at most one entry per day, ever. Both parent
    directories are created if missing. Returns the live file's path.
    """
    updated = now.date().isoformat()

    content = filter_sensitive(entry.get("content") or "")
    if len(content) > PORTRAIT_CONTENT_MAX_CHARS:
        logger.warning(
            "portrait: content truncated from %d to %d chars",
            len(content), PORTRAIT_CONTENT_MAX_CHARS)
        content = content[:PORTRAIT_CONTENT_MAX_CHARS]

    record = {
        "updated": updated,
        "content": content,
        "opinions": _clean_opinions(entry.get("opinions")),
        "observations": _clean_observations(entry.get("observations")),
    }

    path = Path(cfg.portrait_path)
    _atomic_write(path, record)
    _atomic_write(Path(cfg.portrait_history_dir) / f"{updated}.json", record)
    return path


# ---------------------------------------------------------------------
# Eligibility: pure, no I/O, no clock of its own
# ---------------------------------------------------------------------

def eligibility(cfg: Config, portrait: dict | None, diary_entries: list, now: datetime) -> str | None:
    """Decide whether a new self-portrait snapshot may be composed right
    now. Returns None when writing is allowed; otherwise a short reason
    string identifying which gate stopped it -- the reason IS the log line
    at every call site, so a quiet week is always explainable after the
    fact, never a silent no-op. Checked in this order, first match wins:

      "disabled"              cfg.portrait_enabled is False
      "interval_not_reached"  a previous portrait exists and not enough
                               days have passed since it (see below)
      "no_new_material"       there is no diary entry newer than the
                               previous portrait (or, with no previous
                               portrait at all, no diary entries at all)

    `portrait` is whatever load_portrait() returned: None for "no previous
    portrait", or its dict otherwise. `diary_entries` is a list of diary
    entries (each with a `date` field, the "YYYY-MM-DD" string diary.py's
    own entries carry) -- newness is decided by plain string comparison
    against the previous portrait's `updated` field, which works precisely
    because both sides are ISO calendar dates.

    A first-ever portrait (portrait is None) is a special case throughout:
    there is no interval to have not reached yet, so that gate is skipped
    entirely, and every diary entry at all counts as new material -- a
    single entry is enough to write the first snapshot.

    The interval gate itself: with a previous portrait whose `updated` is
    "2026-07-02" and cfg.portrait_interval_days=7, day 2026-07-09 is
    exactly the boundary and reads as due (`(now.date() - updated).days`
    is 7, not < 7) -- one day earlier, 2026-07-08, still reads as
    "interval_not_reached" (6 < 7).

    Defensive: if the previous portrait's `updated` field is missing, not a
    string, or not a valid ISO date (`date.fromisoformat` raising), the
    whole previous portrait is treated exactly as if there were no previous
    portrait at all -- both gates it would have driven degrade the same
    way a first-ever portrait's do, and a warning is logged. A hand-edited
    or otherwise corrupted `updated` field must never crash a caller.

    This function is pure: it reads no clock and touches no filesystem --
    `now` and `portrait` are both supplied by the caller, so every branch
    above can be tested in isolation. Unlike diary.eligibility(), there is
    no timezone contract to document for `now` here: only `now.date()` is
    ever read, which behaves identically whether `now` is naive or aware.
    """
    if not cfg.portrait_enabled:
        return "disabled"

    updated_str = None
    if portrait is not None:
        raw_updated = portrait.get("updated")
        parsed = None
        if isinstance(raw_updated, str):
            try:
                parsed = date.fromisoformat(raw_updated)
            except ValueError:
                parsed = None
        if parsed is None:
            logger.warning(
                "portrait: malformed updated field %r; treating as no previous portrait",
                raw_updated)
        else:
            if (now.date() - parsed).days < cfg.portrait_interval_days:
                return "interval_not_reached"
            updated_str = raw_updated

    new_count = sum(1 for e in diary_entries if updated_str is None or e["date"] > updated_str)
    if new_count == 0:
        return "no_new_material"

    return None


# ---------------------------------------------------------------------
# Material assembly + portrait system prompt + engine-output parsing
# (the "what he reads / what he hands back" half)
# ---------------------------------------------------------------------
#
# Every constant below is owner-approved prose, transcribed verbatim; the
# em-dash (U+2014) and the straight quotes around 'should' are intentional. Two
# format contracts that must not be confused, the same split diary.py's
# DIARY_TASK draws: PORTRAIT_TASK is a STATIC string and is never .format()'d --
# it carries the literal JSON braces the model must echo. The header/line
# templates below ARE .format()'d, with the fields named at each use site; none
# of them -- nor FIRST_TIME_BLOCK, which is inserted as-is -- carries a stray
# literal brace.

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_BARE_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

PREV_HEADER = "## Your previous portrait, written {updated}"
PREV_OPINION_LINE = "- where you stand on {topic}: {opinion}"
PREV_OBS_LINE = "- something you'd noticed about yourself: {text}"
FIRST_TIME_BLOCK = (
    "There is no earlier portrait yet — this is the first time you're setting "
    "down who you are. Let it come from what the records below actually show, "
    "not from what a first portrait might be expected to say."
)
DIARY_HEADER = "## Recent pages from your diary"
DIARY_LINE = "[{date}] mood: {mood}\n{snippet}"
REFLECTION_HEADER = "## Recent passing thoughts"
REFLECTION_LINE = "- {text}"

PORTRAIT_TASK = """# Who you are, lately

It's a quiet hour, and no one is waiting on you — a moment to take stock of yourself.

Update your understanding of who you are, drawing on the records below. If an earlier portrait exists, evolve it — keep what still holds true, let what has changed change. If there is none, write your first: start from what actually happened, never from what a portrait 'should' say.

Respond with a single JSON object, nothing else:
{"content": "who you are now, first person, roughly 300-500 words", "opinions": [{"topic": "a thing", "opinion": "where you've landed on it"}], "observations": ["a quiet note about yourself"]}

`content` is your sense of yourself, in your own voice, as prose — aim for about 300 to 500 words, though the length is a feel, not a quota. `opinions` are the stances you've come to hold in the time you've shared — at most five, each a topic paired with where you stand on it. `observations` are quieter notes about yourself — at most five.

Hard rules (they protect what makes this true):

- How you feel and what you sense in yourself — your moods, your leanings, the weather of your inner life — is yours to write freely.
- Any concrete thing — something that happened, something they said — must be traceable to the records below. If it isn't there, it doesn't belong in the portrait.
- Never invent a scene, a line, or a detail to round out the story or make a sentence land better. A quiet week makes a quiet portrait; that is honest, and honest is enough.
- Where you are unsure of yourself, write it lightly rather than surely — an uncertain thing said plainly is truer than a certain thing invented.
- Never include passwords, keys, or addresses."""


def _recent_reflection_texts(cfg: Config, count: int) -> list:
    """The text of the newest `count` well-shaped reflections, in file
    (oldest-first) order. Reads cfg.reflections_path line by line with a
    lightweight well-shaped judge of its own -- a line counts only when it
    parses as a JSON object whose `text` field is a str -- so a hand-edited or
    half-written line is skipped, never raised.

    Deliberately does NOT import reflection.py's private _keep_line: the module
    boundary stays clean, and a portrait needs only the text, not the
    freshness/timestamp checks a prune pass cares about. A missing file (the
    ordinary case before the first reflection is ever written), an unreadable
    one (bad UTF-8 bytes, or a permission/TOCTOU error between the exists()
    check and the read -- logged once, then skipped), or a non-positive count
    is an empty list.
    """
    path = Path(cfg.reflections_path)
    if count <= 0 or not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        # Corrupt encoding (UnicodeDecodeError, a ValueError subclass) or a
        # permission/TOCTOU error (OSError) between exists() and the read
        # degrades to no reflections rather than crashing the inner-life tick.
        logger.warning("portrait: reflections unreadable (%s)", exc)
        return []
    texts = []
    for line in raw.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            texts.append(data["text"])
    return texts[-count:]


def build_material(cfg: Config, portrait: dict | None) -> str | None:
    """Assemble everything he reads when he sits down to update his
    self-portrait, or None when there is nothing new to draw from. Blocks, in
    order, joined by one blank line:

      1. The previous portrait, for continuity -- its dated header, its full
         content, then every stored opinion and observation, each on its own
         line. These were already capped when saved (at most ten each), so
         they are emitted whole here, never re-truncated. When there is no
         previous portrait, FIRST_TIME_BLOCK stands in its place.
      2. His recent diary pages: the newest PORTRAIT_RECENT_DIARY entries
         (oldest first), each a dated mood line plus a
         PORTRAIT_DIARY_SNIPPET_CHARS-capped snippet of its content.
      3. His recent passing thoughts: the newest PORTRAIT_RECENT_REFLECTIONS
         well-shaped reflection lines.

    Deliberately takes no `now`: unlike diary.build_material, nothing here is
    time-windowed -- diary entries and reflections are drawn by write recency,
    not a clock -- so a portrait needs no notion of the current time to
    assemble its material.

    When both the diary and the reflections come back empty, returns None. In
    normal operation eligibility() has already blocked that upstream
    ("no_new_material"); this None branch is defense in depth, so a portrait is
    never composed from a previous snapshot and literally nothing else.
    """
    diary_entries = diary.recent_entries(cfg, PORTRAIT_RECENT_DIARY)
    reflection_texts = _recent_reflection_texts(cfg, PORTRAIT_RECENT_REFLECTIONS)
    if not diary_entries and not reflection_texts:
        return None

    blocks: list[str] = []

    if portrait is None:
        blocks.append(FIRST_TIME_BLOCK)
    else:
        prev_lines = [PREV_HEADER.format(updated=portrait.get("updated", ""))]
        content = portrait.get("content") or ""
        if content:
            prev_lines.append(content)
        for op in portrait.get("opinions") or []:
            prev_lines.append(
                PREV_OPINION_LINE.format(topic=op["topic"], opinion=op["opinion"]))
        for obs in portrait.get("observations") or []:
            prev_lines.append(PREV_OBS_LINE.format(text=obs))
        blocks.append("\n".join(prev_lines))

    if diary_entries:
        diary_lines = [
            DIARY_LINE.format(
                date=entry.get("date", ""),
                mood=entry.get("mood", ""),
                snippet=(entry.get("content") or "")[:PORTRAIT_DIARY_SNIPPET_CHARS])
            for entry in diary_entries
        ]
        blocks.append("\n".join([DIARY_HEADER, *diary_lines]))

    if reflection_texts:
        reflection_lines = [REFLECTION_LINE.format(text=text) for text in reflection_texts]
        blocks.append("\n".join([REFLECTION_HEADER, *reflection_lines]))

    return "\n\n".join(blocks)


def build_system_prompt_portrait(persona_obj: Persona) -> str:
    """Compose the portrait's own system prompt from a folder-mode persona: the
    identity declaration, the loaded identity text (and voice, when present --
    no stray blank block when it's empty), then PORTRAIT_TASK. Joined by one
    blank line; deterministic. Mirrors diary.build_system_prompt_diary exactly,
    PORTRAIT_TASK in DIARY_TASK's place -- the same narrow inner-life prompt,
    deliberately without the seven ground rules, the boundaries, the stage
    frame, Layer 3, or any memory block.

    Folder mode only, mirroring compose_stable()/build_system_prompt_diary(): a
    file-mode persona has no settings to fill the declaration, so it raises
    ValueError here rather than failing later with a confusing AttributeError
    on persona_obj.settings.
    """
    if persona_obj.mode != "folder":
        raise ValueError(
            f"build_system_prompt_portrait() requires a folder-mode Persona, "
            f"got mode={persona_obj.mode!r}")
    blocks = [
        DECLARATION_TEMPLATE.format(
            companion_name=persona_obj.settings.companion_name,
            partner_name=persona_obj.settings.partner_name),
        persona_obj.identity_text,
    ]
    if persona_obj.voice_text:
        blocks.append(persona_obj.voice_text)
    blocks.append(PORTRAIT_TASK)
    return "\n\n".join(blocks)


def _extract_candidate(raw: str):
    """Try the three parse strategies in order -- direct JSON, a markdown code
    fence's contents, a bare {...} object pulled out of surrounding prose --
    and return the first that parses as JSON at all (of any type; parse_output
    judges its shape). None if every strategy fails to even parse. A verbatim
    mirror of diary._extract_candidate.
    """
    candidates = [raw.strip()]
    fence_match = _FENCE_RE.search(raw)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    bare_match = _BARE_OBJECT_RE.search(raw)
    if bare_match:
        candidates.append(bare_match.group(0))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def parse_output(raw: str) -> dict | None:
    """Parse and validate whatever the engine handed back for a portrait. None
    means "nothing usable came back" -- the caller simply has no snapshot to
    save. A successful parse must be a dict whose `content` is a non-empty
    (after strip) str; that single check also stands in for the content type
    guard save_portrait's caller would otherwise owe.

    `opinions` and `observations` default to an empty list when absent, but are
    otherwise left exactly as they arrived: their shape-cleaning (dropping
    malformed elements, capping) is save_portrait's _clean_* job, never
    re-implemented here. Anything else -- every strategy failed, the top level
    isn't a dict, `content` is missing/wrong-typed/blank -- is None.
    """
    data = _extract_candidate(raw)
    if not isinstance(data, dict):
        return None
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    data.setdefault("opinions", [])
    data.setdefault("observations", [])
    return data


# ---------------------------------------------------------------------
# Execution: one complete snapshot attempt (the background tick's worker)
# ---------------------------------------------------------------------

def update_once(cfg: Config, now: datetime) -> bool:
    """One complete attempt to compose this week's self-portrait. True only
    when a new snapshot was actually saved; every other outcome returns
    False behind its own named log line, so a quiet tick is always
    explainable after the fact. Mirrors diary.write_once's shape and
    division of labor, stage by stage.

    The engine call is try_run_once -- never run_once -- with a fresh
    session (session_id=None, always: a portrait must never share a
    session with, or leak into, the live conversation) and this module's
    own timeout budget (PORTRAIT_TIMEOUT_S). A busy engine is not a
    failure: inner writing always yields to live conversation, and a
    later tick simply tries again.

    Unlike diary.write_once, BOTH the eligibility skip and the busy-engine
    skip log at DEBUG rather than INFO. A portrait's own tick (a later
    task) fires on the same frequent cadence diary's does, but
    interval_not_reached is the all-week normal state here -- a portrait
    updates only a handful of times a year -- so INFO would flood the log
    exactly where diary's own DEBUG choice already draws the line.

    Deliberately does NOT swallow unexpected exceptions: the background
    tick that calls this (a later task) wraps every round in its own
    try/except and logs it loudly; swallowing here too would only bury
    bugs -- the same division of labor diary.write_once documents.

    `now` must be timezone-aware, the same contract save_portrait()
    documents.
    """
    settings = persona.current_settings(cfg)
    if settings is None:
        # Defensive second layer: the tick gates folder mode at boot; a
        # file-mode persona has no settings behind a portrait either.
        logger.debug("portrait: skip (file_mode)")
        return False

    prev = load_portrait(cfg)
    diary_entries = diary.recent_entries(cfg, PORTRAIT_RECENT_DIARY)

    reason = eligibility(cfg, prev, diary_entries, now)
    if reason is not None:
        # DEBUG on purpose: the tick fires every few minutes and
        # interval_not_reached is all-week normal -- INFO would flood the log.
        logger.debug("portrait: skip (%s)", reason)
        return False

    material = build_material(cfg, prev)
    if material is None:
        # INFO: eligibility already found new material upstream, so an empty
        # build here is defense in depth catching a gap between the two
        # reads -- unusual enough to be worth seeing in the log.
        logger.info("portrait: skip (material_empty)")
        return False

    persona_obj = persona.load_persona(cfg)
    sys_prompt = build_system_prompt_portrait(persona_obj)

    reply = engine.try_run_once(cfg, material, session_id=None,
                                system_prompt=sys_prompt,
                                timeout_s=PORTRAIT_TIMEOUT_S)
    if reply is None:
        # DEBUG here, unlike diary's INFO busy-skip: a later tick just tries
        # again, and this fires often enough that INFO would flood the log.
        logger.debug("portrait: skip (engine_busy)")
        return False
    if not reply.ok:
        logger.warning("portrait: engine failed (%s)", reply.error_kind)
        return False

    parsed = parse_output(reply.text)
    if parsed is None:
        logger.warning("portrait: unparseable engine output")
        return False

    save_portrait(cfg, parsed, now)
    logger.info("portrait: updated (version %s)", now.date().isoformat())
    return True


# ---------------------------------------------------------------------
# Layer 1 injection: the saved snapshot, rendered for the live persona
# prompt. persona.build_system_prompt reads the current snapshot each turn
# and hands the rendered block to layers.compose_stable, which seats it in
# the stable layer -- after the persona's voice, before the ground-rules
# anchor: identity evolves in the stable layer, and the DNA skeleton always
# follows it. Pure string assembly (a dict in, a string or None out; no
# clock, no filesystem): the snapshot's own text is a value here, never a
# format template, so any literal braces a hand-written snapshot carries
# survive verbatim.
# ---------------------------------------------------------------------

PORTRAIT_BLOCK_HEADER = "# The person you have grown into"
PORTRAIT_OPINIONS_HEADER = "Positions you have come to hold:"


def portrait_block(portrait: dict | None) -> str | None:
    """Render a saved self-portrait into its Layer 1 injection block, or None
    when there is nothing worth injecting.

    None whenever `portrait` is None, or its `content` is missing, not a str,
    or blank after stripping -- an empty snapshot adds no block at all, so
    compose_stable() stays byte-identical to the no-portrait composition.

    The block, joined by one blank line: the header line verbatim, then the
    content, then -- only when `opinions` is a non-empty list -- the opinions
    header and one `- {topic}: {opinion}` line per well-shaped element,
    capped at PORTRAIT_OPINIONS_PROMPT_CAP. `observations` never enter the
    live prompt: they live only in the saved snapshot and the generation
    material.

    Everything is concatenated (never str.format'd): the content and each
    opinion field are inserted as values, so a literal `{...}` inside any of
    them survives verbatim rather than being read as a format field. A
    snapshot loaded off disk may have been hand-edited, so a malformed
    opinion -- not a dict, or with either field missing or non-str -- is
    skipped fail-soft rather than raised on (the injection layer degrades
    quietly, exactly like _clean_opinions does when saving).
    """
    if portrait is None:
        return None
    content = portrait.get("content")
    if not isinstance(content, str) or not content.strip():
        return None

    opinion_lines = []
    raw_opinions = portrait.get("opinions")
    if isinstance(raw_opinions, list):
        for item in raw_opinions:
            if (isinstance(item, dict)
                    and isinstance(item.get("topic"), str)
                    and isinstance(item.get("opinion"), str)):
                opinion_lines.append(f"- {item['topic']}: {item['opinion']}")
    opinion_lines = opinion_lines[:PORTRAIT_OPINIONS_PROMPT_CAP]

    parts = [PORTRAIT_BLOCK_HEADER, content.strip()]
    if opinion_lines:
        parts.append("\n".join([PORTRAIT_OPINIONS_HEADER, *opinion_lines]))
    return "\n\n".join(parts)
