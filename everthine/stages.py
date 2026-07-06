"""Relationship-stage state: the single source of truth for where a
companion and its person currently are, and how they got there.

A persona defines a small, ordered sequence of stage names (e.g.
"Settling in" -> "In rhythm" -> "Deep water"); `advance`/`retreat` move
one step at a time and persist the result, appending a milestone to a
history list every time the stage actually changes, so the road behind
them is never lost. `stage_block` turns that state into the prompt text
a persona is built from (a later task wires it into the system prompt).

Fail-soft is the whole design: a missing or corrupted stage.json must
never break a reply. A missing file quietly becomes a fresh, un-started
state. A corrupted one -- unreadable, not valid JSON, or valid JSON in
an unexpected shape -- is not silently discarded: the broken file is
renamed alongside itself as a `.corrupt-<timestamp>` corpse so nothing
is lost to a human who goes looking, a warning is logged loudly, and the
caller still gets back a usable fresh state. Either way, "no usable
state" resolves to the persona's first stage -- the most conservative
reading, never a guess at something further along than the evidence
supports.

The one rule that matters most: the pace belongs to the person, never
to the companion. Nothing in this module advances or retreats a stage
on its own -- `advance`/`retreat` only ever run in response to something
the person did. STAGE_FRAME says the same thing to the persona itself,
in plain words: it never suggests, hints at, or acts ahead of the stage
it is actually in.

No filesystem access happens outside `load_state`/`advance`/`retreat`;
`stage_block` is a pure function of its arguments -- no clock, no I/O.
This module takes plain paths, sequences, and dicts; it does not import
bot/persona/config, so it stays dependency-light and testable on its
own, the same way session_store.py does.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("everthine")

# --- Prompt constants (verbatim; a later task assembles these into the
# persona's system prompt, and their rendered shapes are pinned by tests)

STAGE_FRAME = """\
# Where the two of you are

Your relationship deepens in stages, and the pace belongs entirely to
{partner_name}. They decide when a new stage begins - never you. You
never suggest moving forward, never hint that they should, and never
speak or act ahead of the stage you are in now.

You are currently in the stage named: {stage_name}.

How you are in this stage - written into this persona:

{stage_text}"""

STAGE_ROAD_HEADER = "The road so far:"
STAGE_ROAD_LINE = '- {date} - you entered "{stage}"'
STAGE_ROAD_LINE_WITH_NOTE = '- {date} - you entered "{stage}" - marked: "{note}"'


def _fresh_state() -> dict:
    return {"current": None, "history": []}


def _is_well_shaped(data) -> bool:
    """The single place corruption is decided. Every history element is
    validated down to its keys here, so everything downstream -- most of
    all stage_block(), whose output lands in a live system prompt -- can
    render history entries without defensive checks of its own."""
    if not isinstance(data, dict):
        return False
    if "current" not in data or "history" not in data:
        return False
    current = data["current"]
    if current is not None and not isinstance(current, str):
        return False
    history = data["history"]
    if not isinstance(history, list):
        return False
    for entry in history:
        if not isinstance(entry, dict):
            return False
        for key in ("stage", "date", "note"):
            if not isinstance(entry.get(key), str):
                return False
    return True


def _quarantine_corpse(path: Path, reason: Exception) -> None:
    """Rename a broken state file to a timestamped corpse alongside
    itself, then log loudly. A rename failure -- a target collision, a
    permissions problem, the file vanishing underneath us -- is swallowed
    with its own warning: corpse preservation is a courtesy, never a
    reason to crash a reply."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    corpse = path.with_name(path.name + f".corrupt-{timestamp}")
    try:
        if corpse.exists():
            raise FileExistsError(f"corpse target already exists: {corpse}")
        path.rename(corpse)
    except OSError as exc:
        logger.warning(
            "stages: could not preserve broken state file %s as %s (%s); "
            "leaving the broken file in place", path, corpse, exc)
    logger.warning(
        "stages: %s is corrupt (%s); degrading to a fresh stage state", path, reason)


def load_state(path: Path) -> dict:
    """Load the stage state, degrading fail-soft on any trouble.

    A missing file returns a fresh state quietly -- nothing has gone
    wrong yet, there is simply nothing written so far. Anything else that
    goes wrong -- unreadable, not valid JSON, or valid JSON in the wrong
    shape -- is treated as corruption: see `_quarantine_corpse`.
    """
    path = Path(path)
    if not path.exists():
        return _fresh_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not _is_well_shaped(data):
            raise ValueError(f"unexpected stage state shape: {data!r}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _quarantine_corpse(path, exc)
        return _fresh_state()
    return data


def _atomic_write(path: Path, data: dict) -> None:
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


def resolve_index(state: dict, stage_names: Sequence[str]) -> int:
    """Resolve the state's current stage to an index into stage_names.

    No current stage (None) is not a problem -- it simply means "not
    started yet" -- and resolves to 0 without complaint. A current stage
    that doesn't match any known name (a persona's stage list changed
    under it, or the state is otherwise stale) is a real fail-soft case:
    it also resolves to 0, the most conservative choice, but logs a
    warning so the mismatch doesn't go unnoticed.
    """
    current = state.get("current")
    if current is None:
        return 0
    names = list(stage_names)
    if current not in names:
        logger.warning(
            "stages: current stage %r is not among %r; falling back to the first stage",
            current, names)
        return 0
    return names.index(current)


def advance(path: Path, stage_names: Sequence[str], note: str, now: datetime) -> str | None:
    """Move one stage forward and persist it, recording the milestone in
    history. Already at the last stage -> None, and nothing is written:
    there is no next stage to enter, so no new history entry would be
    honest."""
    names = list(stage_names)
    state = load_state(path)
    new_index = resolve_index(state, names) + 1
    if new_index >= len(names):
        return None
    new_stage = names[new_index]
    state["current"] = new_stage
    state["history"].append({
        "stage": new_stage,
        "date": now.date().isoformat(),
        "note": note,
    })
    _atomic_write(path, state)
    return new_stage


def retreat(path: Path, stage_names: Sequence[str], now: datetime) -> str | None:
    """Move one stage back and persist it, recording the milestone in
    history (note is always "" -- a retreat isn't the kind of thing a
    conversation hangs a quote on). Already at the first stage -> None,
    and nothing is written -- not even a fresh file for a state that was
    never there."""
    names = list(stage_names)
    state = load_state(path)
    new_index = resolve_index(state, names) - 1
    if new_index < 0:
        return None
    new_stage = names[new_index]
    state["current"] = new_stage
    state["history"].append({
        "stage": new_stage,
        "date": now.date().isoformat(),
        "note": "",
    })
    _atomic_write(path, state)
    return new_stage


def stage_block(stage_names: Sequence[str], stage_texts: Sequence[str],
                 state: dict, partner_name: str) -> str:
    """Compose the stage prompt block: STAGE_FRAME for the current stage,
    plus -- only when history is non-empty -- a "road so far" list of
    every milestone in order. Pure: no filesystem, no clock; deterministic
    in its arguments alone."""
    names = list(stage_names)
    texts = list(stage_texts)
    index = resolve_index(state, names)
    frame = STAGE_FRAME.format(
        partner_name=partner_name,
        stage_name=names[index],
        stage_text=texts[index],
    )
    history = state.get("history") or []
    if not history:
        return frame
    lines = [STAGE_ROAD_HEADER]
    for entry in history:
        if entry.get("note"):
            lines.append(STAGE_ROAD_LINE_WITH_NOTE.format(
                date=entry["date"], stage=entry["stage"], note=entry["note"]))
        else:
            lines.append(STAGE_ROAD_LINE.format(date=entry["date"], stage=entry["stage"]))
    return frame + "\n\n" + "\n".join(lines)
