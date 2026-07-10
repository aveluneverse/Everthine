"""Single source of user-visible strings.

Neutral English defaults. Users localize or re-voice their companion either
by editing this file directly, or -- with a folder-mode persona -- by
setting `lines.<key>` (and `lines.thinking`) in settings.yaml; persona.py's
loader validates those and messages.load_overrides() activates them at bot
startup. Everything below _MESSAGES is that override layer: dumb storage
plus one belt-and-braces security check (the loader is the real gate; see
load_overrides' docstring).
"""
import logging

logger = logging.getLogger("everthine")

_MESSAGES = {
    "busy": "One moment - I'm still finishing my previous thought.",
    "generic_glitch": "Something glitched on my side. Please say that again.",
    "timeout": "That took me too long and I stopped myself. Please try again.",
    "cli_missing": "I can't find the Claude Code CLI on this machine.",
    "auth": "My connection to Claude needs a re-login. Try again in a minute.",
    "nonzero": "I lost my train of thought there. Please send that once more.",
    "notebook_full": "Our current notebook is getting heavy. Use /start to open a fresh one - I'll keep the warmth.",
    "cmd_start_desc": "New chat - or pick up where we left off",
    "btn_resume": "Continue where we left off",
    "btn_warm": "Fresh page, keep the warmth",
    "btn_clean": "Start completely clean",
    "btn_cancel": "Stop",
    "cancel_ack": "Alright - never mind.",
    "thinking": "...",
    "resume_ack": "Right where we left off.",
    "warm_ack": "New page - I still remember what we just shared.",
    "clean_ack": "A clean page. Nice to meet you again.",
    "start_fresh": "Hello. Send me a message to begin.",
    "start_has_session": "Welcome back. How should we pick things up?",
    "unauthorized_silence": "",
    "cmd_stage_desc": "Where the two of you are",
    "cmd_album_desc": "Moments you both kept",
    "stage_intro": "You are here: {stage}",
    "btn_stage_advance": "Take a step forward",
    "btn_stage_retreat": "Step back one stage",
    "btn_stage_close": "Close",
    "stage_note_prompt": ("A word to mark this step? The next message you "
                          "send is kept beside it, word for word -- it goes "
                          "to the record, not into the chat. Or skip below."),
    "btn_note_skip": "No note -- step forward",
    "btn_note_cancel": "Cancel this step",
    "stage_advanced_ack": "You are now at: {stage}",
    "note_saved_ack": "Kept beside this step, word for word.",
    "stage_retreat_confirm": 'Step back to "{stage}"?',
    "btn_retreat_yes": "Yes, step back",
    "btn_retreat_no": "Stay here",
    "stage_retreated_ack": "Back at: {stage}",
    "stage_road_clipped": "...and {n} earlier steps, all kept.",
    "album_expired": "That message is too old for me to reach now - it was not kept.",
    "album_empty": "Nothing kept yet. React with a heart to keep a moment.",
}


# --- M2 override layer: persona-voiced lines + rotating thinking placeholder

# Security/ops keys a persona must never re-voice. This mirrors
# persona._FORBIDDEN_LINE_KEYS, duplicated rather than imported: persona.py
# already imports this module (`from . import archive, messages`), so an
# import the other way would be circular. The loader is the primary gate
# (ConfigError on any settings.yaml that names either key); this is a second,
# independent gate in case a future caller ever feeds load_overrides() a
# dict that bypassed the loader.
_FORBIDDEN_OVERRIDE_KEYS = frozenset({"unauthorized_silence", "cli_missing"})

_line_overrides: dict[str, str] = {}
_thinking_overrides: list[str] | None = None
_thinking_counter: int = 0


def msg(key: str) -> str:
    if key in _line_overrides:
        return _line_overrides[key]
    return _MESSAGES.get(key, _MESSAGES["generic_glitch"])  # unknown -> generic_glitch, unchanged


def load_overrides(lines: dict, thinking: list[str] | None = None) -> None:
    """Replace the active persona overrides (bot-startup wiring / test hook).

    Always calls reset_overrides() first, so a second load *replaces* the
    previous one rather than merging with it -- no stale key from an earlier
    persona (or an earlier test) survives a fresh load. Stores independent
    copies of `lines` and `thinking`, so later mutation of the caller's own
    dict/list can never reach back into this module's state.

    `unauthorized_silence` and `cli_missing` are dropped on sight (with a
    logged warning) even though persona.py's loader already guarantees a
    settings.yaml naming either raises ConfigError before this function ever
    sees them -- see _FORBIDDEN_OVERRIDE_KEYS above.

    `thinking` travels only as this separate list, consumed solely by
    thinking_line()'s rotation below -- `lines` never carries a "thinking"
    entry in practice (the loader splits it out before building
    PersonaSettings.lines), so msg("thinking") is untouched by this
    mechanism and keeps returning the built-in "..." unless the rotation
    path is active.
    """
    reset_overrides()
    global _line_overrides, _thinking_overrides
    clean = dict(lines)
    for key in _FORBIDDEN_OVERRIDE_KEYS:
        if key in clean:
            logger.warning(
                "messages.load_overrides: dropping forbidden override key %r "
                "(security/ops line, a persona must never re-voice it)", key)
            del clean[key]
    _line_overrides = clean
    _thinking_overrides = list(thinking) if thinking else None


def reset_overrides() -> None:
    """Clear the active overrides and rewind the thinking rotation back to
    its first item. Test hook; also called internally at the top of
    load_overrides so repeated loads replace rather than merge."""
    global _line_overrides, _thinking_overrides, _thinking_counter
    _line_overrides = {}
    _thinking_overrides = None
    _thinking_counter = 0


def thinking_line() -> str:
    """The "deep in thought" placeholder shown while a reply streams in.

    No persona thinking-list loaded -> msg("thinking") (the built-in "...").
    List loaded -> the next line in a deterministic cycle through it (item 0,
    then 1, ..., wrapping back to 0), so consecutive replies show different
    phrasing instead of repeating the same line. The counter is process-
    global and rewinds only on reset_overrides()/load_overrides(), matching
    persona.py's own module-cache pattern.
    """
    global _thinking_counter
    if not _thinking_overrides:
        return msg("thinking")
    line = _thinking_overrides[_thinking_counter % len(_thinking_overrides)]
    _thinking_counter += 1
    return line
