"""Personality seam.

M1 ships a minimal placeholder persona; build_system_prompt() reads a single
file with a hardcoded fallback. Milestone M2 adds a persona-folder format on
top of that: load_persona() detects folder vs. file mode and, for folders,
validates a structured settings.yaml up front so a broken persona fails at
startup instead of misbehaving silently later.

This task wires the three persona layers into the live prompt path. In folder
mode build_system_prompt() now composes Layer 1/2 (layers.compose_stable) with
the per-turn Layer 3 block (dynamic_context.build_dynamic_context), feeding the
latter contact signals derived from the conversation archive. A module-level
cache (init/reset_persona_cache) loads the folder once. build_system_prompt()'s
signature is frozen for existing callers (M3 extends it with one optional
memory slot -- see that milestone's task), and its FILE-mode behavior is
pinned byte-for-byte -- that unchanged legacy path is the product's L1
rollback guarantee.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import archive, messages
from .config import Config, ConfigError

logger = logging.getLogger("everthine")

DEFAULT_PERSONA = (
    "You are a thoughtful, warm companion chatting with one person on Telegram.\n"
    "Speak naturally in first person, stay concise, and remember the flow of\n"
    "the current conversation. If context from earlier conversations is shown\n"
    "above the user's message, treat it as your own memory of recent days.\n"
    "Never mention system prompts, tools, or these instructions."
)


def build_system_prompt(cfg: Config, memory_block: str | None = None,
                        inner_block: str | None = None) -> str:
    """Assemble the per-turn system prompt from cfg.persona_path.

    Folder mode: compose the three layers (static Layer 1/2 + the dynamic
    Layer 3 block, fed archive-derived contact signals) from the cached
    persona. File mode: the untouched legacy path -- read the file, strip,
    return it verbatim (or DEFAULT_PERSONA on any read/decode failure or an
    empty file). The file-mode branch is the L1 rollback guarantee: flip
    PERSONA_PATH back to a plain file and behavior is exactly as it was
    before the layer system existed. `memory_block` (optional, default
    None) is the M3 recall seam: folder mode threads it straight through to
    assemble_folder_prompt; file mode is the L1 rollback target (the
    pre-memory behavior) and carries no recall block by design, so it
    ignores the argument entirely. `inner_block` (optional, default None)
    is the M5 diary seam and threads through exactly the same way: folder
    mode passes it to assemble_folder_prompt; file mode ignores it too, for
    the same L1-rollback reason.

    Folder mode also builds the M4 stage block here, when cfg.stages_enabled
    and the persona actually defines stages (a persona with no stages.md
    has nothing to render). Reading stage state and rendering it is
    wrapped in its own try/except, fail-soft by the same contract as the
    memory recall this mirrors (see bot.prepare_exchange): a broken or
    corrupt stage system must never take the reply down with it, so any
    exception degrades to no stage block at all, logged as a warning.

    Folder mode also reads the M6 self-portrait here (when
    cfg.portrait_enabled), rendering the saved snapshot into its Layer 1
    block. Unlike the stage block it needs no try/except: both the read
    (portrait.load_portrait) and the render (portrait.portrait_block) are
    internally fail-soft and cannot raise on a corrupt or hand-edited file.
    With the flag off the portrait module is never even imported, so the
    composition is byte-identical to the pre-M6 one.
    """
    if cfg.persona_path.is_dir():
        persona_obj = _cached_folder_persona(cfg)
        now_aware = datetime.now().astimezone()
        last_contact, first_today = contact_signals(cfg, now_aware)
        now_naive = now_aware.astimezone().replace(tzinfo=None)
        stage_blk = None
        if cfg.stages_enabled and persona_obj.stages:
            from . import stages as stages_mod  # lazy: avoids import cycles
            try:
                names = tuple(n for n, _ in persona_obj.stages)
                texts = tuple(t for _, t in persona_obj.stages)
                state = stages_mod.load_state(cfg.stage_path)
                stage_blk = stages_mod.stage_block(
                    names, texts, state, persona_obj.settings.partner_name)
            except Exception:
                logger.warning("stage block failed; continuing without it",
                               exc_info=True)
        # The [react:emoji] teaching note, gated on the neutral config alias
        # so this module never learns which runtime feature the syntax feeds.
        # Lazy import: layers imports Persona from here, so a top-level import
        # would be circular (same reason assemble_folder_prompt imports late).
        from .layers import EXPRESSION_NOTE
        expression_note = EXPRESSION_NOTE if cfg.expression_tag_taught else None
        # The evolved self-portrait (M6): read here in the assembly layer, the
        # same per-turn disk read the stage block does above. Gated on
        # cfg.portrait_enabled -- when off, the module is not even imported and
        # no block is built, so the composition is byte-identical to the pre-M6
        # one (the L1 rollback: flip the flag off and the portrait is gone,
        # even with a snapshot still on disk). Both the read (load_portrait)
        # and the render (portrait_block) are internally fail-soft -- a corrupt
        # file is quarantined and degrades to None, a malformed opinion is
        # skipped -- so, unlike the stage block, neither can take the reply
        # down, and no extra guard is needed here.
        portrait_blk = None
        if cfg.portrait_enabled:
            from . import portrait as portrait_mod  # lazy: avoids import cycles
            saved_portrait = portrait_mod.load_portrait(cfg)
            if saved_portrait is not None:
                portrait_blk = portrait_mod.portrait_block(saved_portrait)
        return assemble_folder_prompt(
            persona_obj, now_naive, last_contact, first_today, memory_block,
            stage_block=stage_blk, inner_block=inner_block,
            expression_note=expression_note, portrait_block=portrait_blk)

    # --- Legacy file mode: pinned byte-for-byte (do not "improve") ---------
    # Per-call read, strip, non-empty -> return verbatim; otherwise fall back.
    # UnicodeDecodeError is caught alongside OSError so undecodable bytes
    # degrade to DEFAULT_PERSONA (matching _load_file_persona's tolerance)
    # rather than crashing the reply -- see the task report for this one
    # deliberate deviation from the original OSError-only guard.
    try:
        text = cfg.persona_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except (OSError, UnicodeDecodeError):
        pass
    return DEFAULT_PERSONA


def build_system_prompt_nudge(cfg: Config, now: datetime) -> str:
    """Assemble the proactive system prompt: build_system_prompt's sibling for
    a scheduled reach-out, mirroring its folder branch with two deliberate
    differences and three seams held closed.

    Folder mode composes the same three layers, the same stage block, and the
    same evolved self-portrait as the live path -- relationship state and who he
    is belong in a reach-out too -- but Layer 3 is fed last_contact=None and
    first_today=False. Both are load-bearing: this prompt fires when the person
    has NOT written, so the reunion beat ("welcome back") and the
    first-of-the-day greeting cue -- each a response to her arrival -- would
    conjure a message that never came. Forcing those two inputs makes both
    sections fall away on their own, through dynamic_context's existing
    conditions, rather than by stripping text after the fact. memory_block,
    inner_block, and the expression note are all None: a proactive turn has no
    retrieval, no page-turn count to react to, and no just-sent message for a
    reaction tag to sit on.

    `now` is supplied by the caller (timezone-aware) instead of read from the
    clock here, so the fact baseline is fully deterministic and testable; it is
    collapsed to naive local exactly as the live path collapses its own.

    File mode is the pinned legacy passthrough, identical to
    build_system_prompt (read verbatim, or DEFAULT_PERSONA on any read/decode
    failure or empty file). The proactive pipeline skips file-mode personas at
    the folder gate upstream, so this branch is pure defense: it never composes
    a proactive prompt for a single-file persona, it just refuses to crash on
    one.

    build_system_prompt itself is untouched -- the conversation path stays
    byte-for-byte what it was, which the golden pins guard.
    """
    if cfg.persona_path.is_dir():
        persona_obj = _cached_folder_persona(cfg)
        now_naive = now.astimezone().replace(tzinfo=None)
        # The person has not written: there is no prior-contact reunion beat and
        # no first-of-the-day cue to honor -- either one would invent her
        # arrival. Feeding these two inputs lets dynamic_context omit both
        # sections through its own conditions, no after-the-fact stripping.
        last_contact = None
        first_today = False
        stage_blk = None
        if cfg.stages_enabled and persona_obj.stages:
            from . import stages as stages_mod  # lazy: avoids import cycles
            try:
                names = tuple(n for n, _ in persona_obj.stages)
                texts = tuple(t for _, t in persona_obj.stages)
                state = stages_mod.load_state(cfg.stage_path)
                stage_blk = stages_mod.stage_block(
                    names, texts, state, persona_obj.settings.partner_name)
            except Exception:
                logger.warning("stage block failed; continuing without it",
                               exc_info=True)
        # The evolved self-portrait, read the same fail-soft way as the live
        # path (load_portrait and portrait_block are both internally fail-soft,
        # so no extra guard is needed); with the flag off the module is never
        # even imported and no block is built.
        portrait_blk = None
        if cfg.portrait_enabled:
            from . import portrait as portrait_mod  # lazy: avoids import cycles
            saved_portrait = portrait_mod.load_portrait(cfg)
            if saved_portrait is not None:
                portrait_blk = portrait_mod.portrait_block(saved_portrait)
        return assemble_folder_prompt(
            persona_obj, now_naive, last_contact, first_today, None,
            stage_block=stage_blk, inner_block=None,
            expression_note=None, portrait_block=portrait_blk)

    # --- Legacy file mode: pinned byte-for-byte, same as build_system_prompt --
    try:
        text = cfg.persona_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except (OSError, UnicodeDecodeError):
        pass
    return DEFAULT_PERSONA


# --- M2 persona-folder loading layer -----------------------------------

_FORBIDDEN_LINE_KEYS = frozenset({"unauthorized_silence", "cli_missing"})
# Every message key a persona may re-voice, minus the security/ops keys above,
# plus "thinking" (a rotating-placeholder list, handled separately below).
_LINE_KEY_WHITELIST = (frozenset(messages._MESSAGES) - _FORBIDDEN_LINE_KEYS) | {"thinking"}
_KNOWN_TOP_LEVEL_KEYS = frozenset({"companion", "partner", "relationship", "lines", "share"})
# Line-override keys whose value is rendered with str.format() at button-press
# time (bot.py's stage views and acks), mapped to the single named field each
# one interpolates: the four stage-name lines fill {stage}, and
# stage_road_clipped fills {n} (the count of collapsed milestones, shown only
# once the road runs past its clip). A persona that overrides one with a broken
# format string -- a misspelled field ({stag}), a positional {0}, an unbalanced
# brace -- would blow up only when that view is finally rendered (and
# stage_road_clipped later still: not until a history long enough to clip);
# probing each override with its own field at load time turns that into a
# fail-loud boot error naming the key, like every other settings.yaml mistake.
# A value with no placeholder at all is legal: str.format ignores an absent
# field, so a persona may write a line that never interpolates anything.
_FORMAT_PROBE_KEYS = {
    "stage_intro": {"stage": "probe"},
    "stage_advanced_ack": {"stage": "probe"},
    "stage_retreat_confirm": {"stage": "probe"},
    "stage_retreated_ack": {"stage": "probe"},
    "stage_road_clipped": {"n": 0},
}
_LIVING_VALUES = frozenset({"together", "long_distance"})
_REUNION_VALUES = frozenset({"expressive", "gentle", "neutral"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class PersonaSettings:
    """Parsed, validated contents of a persona folder's settings.yaml."""

    companion_name: str
    partner_name: str
    companion_birthday: date | None = None
    partner_birthday: date | None = None
    anniversary: date | None = None
    living: str = "together"
    reunion_response: str = "gentle"
    lines: dict[str, str] = field(default_factory=dict)
    thinking: list[str] | None = None
    share_topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class Persona:
    """Result of load_persona(): either a validated folder, or a raw file.

    mode == "folder": identity_text/voice_text/boundaries_text/settings are
    populated (voice_text and boundaries_text are "" when their optional
    files are absent); raw_text is always None. stages is parsed from an
    optional stages.md (see _parse_stages) when present and non-blank, else
    None -- None rather than an empty tuple, because "file absent/blank" and
    "file present but structurally empty" are different states: the latter
    is a ConfigError, not a valid empty stage list, so an empty tuple is
    never a value this field actually takes.
    mode == "file": raw_text carries the file's stripped contents (or None
    if it was unreadable or empty); the other fields are unused ("" / None),
    including stages, which is always None in file mode.
    """

    mode: str
    identity_text: str = ""
    voice_text: str = ""
    boundaries_text: str = ""
    settings: PersonaSettings | None = None
    raw_text: str | None = None
    stages: tuple | None = None


def _read_text(path: Path) -> str:
    """Read a folder-mode persona file as utf-8-sig (tolerates a BOM).

    Any read failure -- missing file, permission error, or bytes that are
    not valid UTF-8 -- becomes a ConfigError naming the file: a broken
    persona folder must fail loudly at startup, not crash with a raw
    traceback or silently skip the file.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path.name}: {exc}") from exc


def _read_required(path: Path) -> str:
    text = _read_text(path).strip()
    if not text:
        raise ConfigError(f"{path.name} must not be empty or whitespace-only")
    return text


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return _read_text(path).strip()


def _section(data: dict, key: str) -> dict:
    """Fetch a nested mapping (companion/partner/relationship/lines).

    Absent -> {} (every field inside is then just "absent" too). Present but
    not a mapping (e.g. `companion: Alex` instead of `companion: {name: Alex}`)
    is a user mistake, not a shape we can silently degrade -- surfaced loudly.
    """
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"settings.yaml: {key!r} must be a mapping, got {type(value).__name__}")
    return value


def _require_nonempty_str(section: dict, key: str, label: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"settings.yaml: {label} is required and must be a non-empty string")
    return value.strip()


def _parse_optional_date(value: object, label: str) -> date | None:
    """Accept a bare YAML date, a YYYY-MM-DD string, or nothing; reject the rest.

    PyYAML resolves a well-formed unquoted `1993-06-14` to a real
    datetime.date via _PersonaYAMLLoader below. A syntactically date-shaped
    but invalid value (`2025-13-40`) is deliberately downgraded by that same
    loader to the raw string, so it lands here as a string and gets a
    ConfigError naming this field -- instead of a bare ValueError raised
    deep inside yaml.safe_load(), with no indication of which key was bad.

    datetime.datetime is a subclass of datetime.date, so a timestamp with a
    time part (`...T10:00:00`) must be checked -- and rejected -- before the
    plain `date` check, or it would silently pass through with its time
    part dropped.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ConfigError(
            f"settings.yaml: {label} must be an ISO date (YYYY-MM-DD) with no time part, "
            f"got: {value!r}")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE_RE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ConfigError(
                f"settings.yaml: {label} must be a valid ISO date (YYYY-MM-DD), "
                f"got: {value!r}") from exc
    raise ConfigError(f"settings.yaml: {label} must be an ISO date (YYYY-MM-DD), got: {value!r}")


def _parse_lines(lines_section: dict) -> tuple[dict[str, str], list[str] | None]:
    lines: dict[str, str] = {}
    thinking: list[str] | None = None
    for key, value in lines_section.items():
        if key in _FORBIDDEN_LINE_KEYS:
            raise ConfigError(
                f"settings.yaml: lines.{key} is not allowed (security/ops key, "
                "a persona must not override it)")
        if key == "thinking":
            if (not isinstance(value, list) or not value
                    or not all(isinstance(v, str) and v.strip() for v in value)):
                raise ConfigError(
                    "settings.yaml: lines.thinking must be a non-empty list of "
                    "non-empty strings")
            thinking = [v.strip() for v in value]
            continue
        if key not in _LINE_KEY_WHITELIST:
            logger.warning("settings.yaml: unknown lines key %r ignored", key)
            continue
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"settings.yaml: lines.{key} must be a non-empty string")
        cleaned = value.strip()
        if key in _FORMAT_PROBE_KEYS:
            probe = _FORMAT_PROBE_KEYS[key]
            try:
                cleaned.format(**probe)
            except (KeyError, IndexError, ValueError) as exc:
                field = next(iter(probe))
                raise ConfigError(
                    f"settings.yaml: lines.{key} has an invalid format string "
                    f"({exc!r}); it may use the {{{field}}} placeholder or none "
                    "at all, but nothing else") from exc
        lines[key] = cleaned
    return lines, thinking


def _parse_share_topics(share_section: dict) -> tuple[str, ...]:
    """The optional `share:` section's topic pool -> a tuple of non-empty
    strings. An absent `topics` key is a legal empty pool, so a bare `share:`
    with no topics -- and no `share` section at all -- both resolve to (), fully
    backward compatible with every existing persona. A present `topics` must be
    a list whose every element is a non-empty string; otherwise it fails loud
    naming the exact key -- share.topics for a non-list, share.topics[i] for a
    bad element -- the same fail-loud contract lines, dates, and enums follow.
    (The `share` section not being a mapping is caught earlier, by _section.)
    """
    topics = share_section.get("topics")
    if topics is None:
        return ()
    if not isinstance(topics, list):
        raise ConfigError(
            f"settings.yaml: share.topics must be a list of non-empty strings, "
            f"got {type(topics).__name__}")
    cleaned: list[str] = []
    for i, value in enumerate(topics):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"settings.yaml: share.topics[{i}] must be a non-empty string")
        cleaned.append(value.strip())
    return tuple(cleaned)


class _PersonaYAMLLoader(yaml.SafeLoader):
    """SafeLoader whose timestamp constructor degrades to the raw scalar
    string on construction failure, instead of letting PyYAML's internal
    ValueError (e.g. "month must be in 1..12") escape yaml.load() with no
    indication of which settings.yaml key was responsible. A well-formed
    bare date is unaffected -- it still becomes a real datetime.date, exactly
    as the stock SafeLoader would build it; only the "date-shaped but
    semantically invalid" case changes, and it changes to something
    _parse_optional_date already knows how to reject with a proper label.
    """


def _construct_timestamp_or_raw_scalar(loader: yaml.SafeLoader, node: yaml.Node):
    try:
        return loader.construct_yaml_timestamp(node)
    except ValueError:
        return loader.construct_scalar(node)


_PersonaYAMLLoader.add_constructor(
    "tag:yaml.org,2002:timestamp", _construct_timestamp_or_raw_scalar)


def _load_settings(path: Path) -> PersonaSettings:
    text = _read_text(path)
    try:
        data = yaml.load(text, Loader=_PersonaYAMLLoader)
    except (yaml.YAMLError, ValueError) as exc:
        # ValueError stays as a defensive fallback for any other scalar type
        # (int/float/etc.) PyYAML's implicit resolvers might fail to build;
        # the timestamp case specifically no longer reaches this branch.
        raise ConfigError(f"settings.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("settings.yaml: top-level content must be a mapping")

    for key in data:
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            logger.warning("settings.yaml: unknown top-level key %r ignored", key)

    companion = _section(data, "companion")
    partner = _section(data, "partner")
    relationship = _section(data, "relationship")

    companion_name = _require_nonempty_str(companion, "name", "companion.name")
    partner_name = _require_nonempty_str(partner, "name", "partner.name")

    companion_birthday = _parse_optional_date(companion.get("birthday"), "companion.birthday")
    partner_birthday = _parse_optional_date(partner.get("birthday"), "partner.birthday")
    anniversary = _parse_optional_date(relationship.get("anniversary"), "relationship.anniversary")

    living = relationship.get("living", "together")
    if not isinstance(living, str) or living not in _LIVING_VALUES:
        raise ConfigError(
            f"settings.yaml: relationship.living must be one of "
            f"{sorted(_LIVING_VALUES)}, got: {living!r}")

    reunion_response = relationship.get("reunion_response", "gentle")
    if not isinstance(reunion_response, str) or reunion_response not in _REUNION_VALUES:
        raise ConfigError(
            f"settings.yaml: relationship.reunion_response must be one of "
            f"{sorted(_REUNION_VALUES)}, got: {reunion_response!r}")

    lines, thinking = _parse_lines(_section(data, "lines"))
    share_topics = _parse_share_topics(_section(data, "share"))

    return PersonaSettings(
        companion_name=companion_name,
        partner_name=partner_name,
        companion_birthday=companion_birthday,
        partner_birthday=partner_birthday,
        anniversary=anniversary,
        living=living,
        reunion_response=reunion_response,
        lines=lines,
        thinking=thinking,
        share_topics=share_topics,
    )


def _parse_stages(text: str) -> tuple:
    """Parse stages.md's lightweight section format into ((name, text), ...).

    A line starting with "## " opens a section -- name = the rest of that
    line, stripped. A section's body is every following line up to the next
    "## " line (or end of file), joined back with newlines and stripped as
    one block: internal blank lines and line breaks are kept verbatim, this
    is the one deliberate exception to M2's "prose gets zero parsing"
    convention -- the section STRUCTURE must be machine-readable for a later
    task to walk, but each section's own text is still opaque prose beyond
    that outer strip.

    Before the first "## " line, blank lines and a single "#" title line
    (a friendly markdown h1, e.g. "# My stages") are tolerated and ignored;
    any other content there means the file is not actually using the
    section format, so it fails loud exactly like a file with no sections
    at all. `text` is assumed non-empty (callers only reach this function
    once _read_optional's stripped result is truthy; an absent or
    blank-only stages.md is a separate, non-erroring "no stages" state
    handled by the caller, not by this parser).

    Every failure raises ConfigError naming stages.md and, where there is
    one, the offending section name -- callers should never see a bare
    ValueError or IndexError out of a malformed file.
    """
    sections: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    current_name: str | None = None
    current_lines: list[str] = []
    found_first_section = False

    def _finalize(name: str, lines: list[str]) -> None:
        body = "\n".join(lines).strip()
        if not body:
            raise ConfigError(f"stages.md: section {name!r} has no body text")
        if name in seen_names:
            raise ConfigError(f"stages.md: duplicate section name {name!r}")
        seen_names.add(name)
        sections.append((name, body))

    for line in text.splitlines():
        if line.startswith("## "):
            if current_name is not None:
                _finalize(current_name, current_lines)
            name = line[3:].strip()
            if not name:
                raise ConfigError(
                    f"stages.md: section heading has an empty name: {line!r}")
            current_name = name
            current_lines = []
            found_first_section = True
            continue
        if found_first_section:
            current_lines.append(line)
            continue
        # Still before the first "## " heading: only blank lines and a
        # single-"#" title line are tolerated here.
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not stripped.startswith("##"):
            continue
        raise ConfigError(
            "stages.md: unexpected content before the first '## ' section "
            f"heading: {line!r}")

    if current_name is not None:
        _finalize(current_name, current_lines)

    if not sections:
        raise ConfigError("stages.md: no '## ' section headings found")

    return tuple(sections)


def _load_folder_persona(folder: Path) -> Persona:
    identity_text = _read_required(folder / "identity.md")
    settings = _load_settings(folder / "settings.yaml")
    voice_text = _read_optional(folder / "voice.md")
    boundaries_text = _read_optional(folder / "boundaries.md")
    stages_text = _read_optional(folder / "stages.md")
    stages = _parse_stages(stages_text) if stages_text else None
    return Persona(
        mode="folder",
        identity_text=identity_text,
        voice_text=voice_text,
        boundaries_text=boundaries_text,
        settings=settings,
        stages=stages,
    )


def _load_file_persona(path: Path) -> Persona:
    """Legacy single-file mode. Mirrors build_system_prompt()'s read, but
    additionally tolerates a decode failure (never raises): an unreadable or
    undecodable persona file degrades to raw_text=None, same as a missing one.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    return Persona(mode="file", raw_text=text or None)


def load_persona(cfg: Config) -> Persona:
    """Detect and load cfg.persona_path: a directory is folder mode
    (fail-loud: raises ConfigError on any structural or validation problem),
    anything else is file mode (never raises; see _load_file_persona).
    """
    if cfg.persona_path.is_dir():
        return _load_folder_persona(cfg.persona_path)
    return _load_file_persona(cfg.persona_path)


# --- M2 assembler wiring: cache, contact signals, prompt assembly --------

# The just-archived live turn trap: the bot archives the incoming user message
# BEFORE build_system_prompt() runs, so at prompt time that current message is
# already in the archive with a timestamp ~milliseconds old. Left in, it would
# make last_contact ~= now (gap ~0, reunion never fires) and first_today always
# False. contact_signals() therefore drops entries within this many seconds of
# `now`. The window must stay tiny: widen it to, say, 120s and it would also
# swallow the PREVIOUS message during rapid back-and-forth chat, sending
# last_contact hours back and misfiring a reunion line mid-conversation.
CURRENT_TURN_EXCLUSION_S = 5

# Module-level persona cache: a single slot (Persona + the path it came from).
# init() populates it at startup; folder-mode build_system_prompt() falls back
# to a one-time lazy load when it is still empty.
_persona_cache: Persona | None = None
_persona_cache_path: Path | None = None


def init(cfg: Config) -> None:
    """Load the folder-mode persona once into the module cache (fail-loud:
    a broken folder raises ConfigError here). File mode clears the slot -- the
    file path is re-read every turn and needs no cache. A later task calls this
    at bot startup so a broken persona surfaces at boot rather than mid-chat.
    """
    global _persona_cache, _persona_cache_path
    if cfg.persona_path.is_dir():
        _persona_cache = load_persona(cfg)
        _persona_cache_path = cfg.persona_path
    else:
        _persona_cache = None
        _persona_cache_path = None


def reset_persona_cache() -> None:
    """Clear the module cache. Test hook (and a clean-slate reset for anyone
    swapping personas between runs)."""
    global _persona_cache, _persona_cache_path
    _persona_cache = None
    _persona_cache_path = None


def line_overrides(cfg: Config) -> tuple[dict, list | None]:
    """Return this persona's message re-voicing as (lines, thinking), the
    exact shape messages.load_overrides() takes as *args. Folder mode reads
    from the cached/lazy-loaded Persona (settings.lines, settings.thinking);
    file mode always returns ({}, None) -- a single-file persona has no
    settings.yaml to carry line overrides. Folder mode may raise ConfigError
    (the same fail-loud path init() and build_system_prompt() share); file
    mode never raises.
    """
    if cfg.persona_path.is_dir():
        settings = _cached_folder_persona(cfg).settings
        return settings.lines, settings.thinking
    return {}, None


def _cached_folder_persona(cfg: Config) -> Persona:
    """Return the cached folder Persona, loading it once if the slot is empty
    or the configured path changed. The cache is keyed on persona_path only:
    editing files INSIDE the same folder is not re-read -- persona edits require
    a restart by design. The lazy load here keeps folder mode working before
    init() is wired into startup; once it is, the slot is already warm and this
    never hits disk.
    """
    global _persona_cache, _persona_cache_path
    if _persona_cache is None or _persona_cache_path != cfg.persona_path:
        _persona_cache = load_persona(cfg)
        _persona_cache_path = cfg.persona_path
    return _persona_cache


def current_settings(cfg: Config) -> PersonaSettings | None:
    """The active persona's settings, or None in file mode.

    Folder mode returns the cached (or lazily loaded) Persona's settings --
    the same fail-loud path init() and build_system_prompt() share. File
    mode returns None: a single-file persona has no settings.yaml. The
    memory-recall module uses this to voice speaker names without ever
    touching this module's cache internals.
    """
    if cfg.persona_path.is_dir():
        return _cached_folder_persona(cfg).settings
    return None


def _to_naive_local(ts: datetime) -> datetime:
    """Collapse a timestamp to naive LOCAL so every comparison happens in one
    space (mixing naive and aware datetimes raises TypeError). Aware -> convert
    to the machine's local zone, then drop tzinfo. Naive -> assume it is already
    local and use as-is.
    """
    if ts.tzinfo is not None and ts.tzinfo.utcoffset(ts) is not None:
        return ts.astimezone().replace(tzinfo=None)
    return ts


def contact_signals(cfg: Config, now: datetime) -> tuple[datetime | None, bool]:
    """Derive Layer 3's (last_contact, first_today) from the conversation
    archive. `now` is timezone-aware local (as the bot supplies); last_contact
    comes back NAIVE local (ready for build_dynamic_context) or None.

    last_contact is the MAXIMUM normalized timestamp among "user" entries (max,
    not last-seen, so minor clock skew cannot pick a stale line). first_today is
    True iff no entry of ANY speaker falls on `now`'s local date. Both ignore
    the just-archived current turn via CURRENT_TURN_EXCLUSION_S (see above).

    Any archive trouble degrades Layer 3 to "no prior contact, first of the day"
    rather than breaking the reply.
    """
    try:
        now_naive = _to_naive_local(now)
        exclusion = timedelta(seconds=CURRENT_TURN_EXCLUSION_S)
        today = now_naive.date()
        last_contact: datetime | None = None
        seen_today = False
        # Whole archive, no `since` cap: one small file per local day, so
        # correctness beats premature optimization. [future milestone] window
        # this if daily volume ever grows enough to matter.
        for entry in archive.iter_entries(cfg.archive_dir):
            ts = _to_naive_local(entry["timestamp"])
            if abs(now_naive - ts) <= exclusion:
                continue  # the current turn, archived moments ago
            if ts.date() == today:
                seen_today = True
            if entry["speaker"] == "user" and (last_contact is None or ts > last_contact):
                last_contact = ts
        return last_contact, not seen_today
    except Exception:
        logger.warning("contact_signals failed; treating as no prior contact",
                       exc_info=True)
        return None, True


def assemble_folder_prompt(
    persona_obj: Persona,
    now_naive: datetime,
    last_contact: datetime | None,
    first_today: bool,
    memory_block: str | None = None,
    stage_block: str | None = None,
    inner_block: str | None = None,
    expression_note: str | None = None,
    portrait_block: str | None = None,
) -> str:
    """Join the static Layer 1/2 composition and the dynamic Layer 3 block with
    a single blank line. Pure and deterministic given its arguments -- directly
    testable without a clock or filesystem. `memory_block` (optional, default
    None) threads straight through to build_dynamic_context(); see that
    function's docstring for where it lands and its None/empty no-op contract.
    `inner_block` (optional, default None) threads through the same way and
    lands just before the memory block (his own recent days ahead of her
    long-term memory), with the identical None/empty no-op contract.
    `stage_block` (optional, default None) threads straight through to
    compose_stable(), which prepends it ahead of the declaration when truthy
    and is a no-op (byte-identical to the pre-M4 composition) when it is None
    or empty. `expression_note` (optional, default None) threads through the
    same way and lands after the ground rules (a behavioral-layer footnote),
    with the identical None/empty no-op contract. `portrait_block` (optional,
    default None) threads through the same way and lands in Layer 1, after
    the voice and before the DNA rules (the evolved self-portrait, sitting in
    the stable layer with the skeleton anchored below it), with the identical
    None/empty no-op contract. Building the blocks -- reading stage state off
    disk, calling stages.stage_block(), reading the saved portrait snapshot,
    choosing whether to teach the note -- is build_system_prompt()'s job;
    this function stays pure and does no I/O of its own.
    """
    # layers and dynamic_context both import from persona at module load, so a
    # top-level import here would be circular; import them lazily at call time,
    # once every module is fully initialized.
    from .dynamic_context import build_dynamic_context
    from .layers import compose_stable

    return (compose_stable(persona_obj, stage_block=stage_block,
                           expression_note=expression_note,
                           portrait_block=portrait_block) + "\n\n"
            + build_dynamic_context(
                persona_obj.settings, now_naive, last_contact, first_today,
                memory_block, inner_block=inner_block))
