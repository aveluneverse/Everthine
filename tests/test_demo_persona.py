"""Tests for the M2 demo personas: the two shipped persona folders
(personas/default English, personas/default-zh Traditional Chinese) that are
Everthine's public-facing example couple -- Theo and Wren. Conventions follow
tests/test_persona_loader.py (loader-level checks) and
tests/test_persona_assembly_wiring.py (the NOW/naive-`now` convention for the
pure assembler).

The zh demo doubles as the framework's COMPLETE Chinese line pack: its
settings.yaml `lines:` block must cover every overridable messages.py key --
everything in _MESSAGES except the two security/ops keys
(unauthorized_silence, cli_missing) and "thinking" (which travels as its own
list, not inside `lines`). Test class TestZhPackCompleteness pins that
property, so a future milestone that adds a new message key fails loud here
as a reminder to extend the Chinese pack, instead of silently shipping an
incomplete localization.

The EN demo is the opposite case on purpose: a partial override (six keys +
thinking) that demonstrates the "leave the rest on the built-in default"
pattern, so TestEnPackIsPartialOverride pins "strict subset", not equality.
"""
import shutil
import subprocess
import unittest
from datetime import datetime
from pathlib import Path

from everthine import messages, persona
from everthine.config import Config
from everthine.layers import DECLARATION_TEMPLATE

REPO_ROOT = Path(__file__).resolve().parent.parent
EN_DIR = REPO_ROOT / "personas" / "default"
ZH_DIR = REPO_ROOT / "personas" / "default-zh"

# Aware local, fixed date, mid-afternoon -- then collapsed to naive local, the
# shape assemble_folder_prompt()'s `now_naive` parameter requires. Mirrors
# tests/test_persona_assembly_wiring.py's NOW / _naive_local() convention.
_NOW_AWARE = datetime(2026, 7, 3, 14, 30, 0).astimezone()
NOW_NAIVE = _NOW_AWARE.astimezone().replace(tzinfo=None)

# Every key a folder persona may re-voice via `lines:`, per persona.py's own
# _LINE_KEY_WHITELIST derivation, minus "thinking" (handled separately from
# `lines` both in the loader and here).
OVERRIDABLE_KEYS = frozenset(messages._MESSAGES) - {"unauthorized_silence", "cli_missing", "thinking"}


def _cfg(persona_path: Path) -> Config:
    return Config(bot_token="x", authorized_user_id=1, persona_path=persona_path)


def _load(folder: Path) -> persona.Persona:
    return persona.load_persona(_cfg(folder))


# --- 1. load_persona succeeds on both demo folders -------------------------

class TestBothDemosLoad(unittest.TestCase):
    def test_en_demo_loads_theo_and_wren(self):
        p = _load(EN_DIR)
        self.assertEqual(p.mode, "folder")
        self.assertEqual(p.settings.companion_name, "Theo")
        self.assertEqual(p.settings.partner_name, "Wren")

    def test_zh_demo_loads_theo_and_wren(self):
        p = _load(ZH_DIR)
        self.assertEqual(p.mode, "folder")
        self.assertEqual(p.settings.companion_name, "Theo")
        self.assertEqual(p.settings.partner_name, "Wren")

    def test_zh_voice_declares_chinese_only_reply(self):
        p = _load(ZH_DIR)
        self.assertIn("他永遠用繁體中文回應。", p.voice_text)


# --- 2. Assembly integration: declaration + DNA heading (+ zh boundaries) --

class TestAssemblyIntegration(unittest.TestCase):
    def _assembled(self, folder: Path):
        p = _load(folder)
        return p, persona.assemble_folder_prompt(p, NOW_NAIVE, None, True)

    def test_en_demo_assembles_declaration_and_dna(self):
        p, result = self._assembled(EN_DIR)
        expected_declaration = DECLARATION_TEMPLATE.format(
            companion_name=p.settings.companion_name,
            partner_name=p.settings.partner_name,
        )
        self.assertIn(expected_declaration, result)
        self.assertIn("# The ground rules", result)

    def test_zh_demo_assembles_declaration_dna_and_boundaries(self):
        p, result = self._assembled(ZH_DIR)
        expected_declaration = DECLARATION_TEMPLATE.format(
            companion_name=p.settings.companion_name,
            partner_name=p.settings.partner_name,
        )
        self.assertIn(expected_declaration, result)
        self.assertIn("# The ground rules", result)
        self.assertIn("地雷清單", result)


# --- 3. zh pack completeness pin --------------------------------------------

class TestZhPackCompleteness(unittest.TestCase):
    def test_zh_lines_cover_every_overridable_key(self):
        p = _load(ZH_DIR)
        keys = set(p.settings.lines.keys())
        self.assertEqual(keys, set(OVERRIDABLE_KEYS))

    def test_zh_thinking_is_a_nonempty_list(self):
        p = _load(ZH_DIR)
        self.assertIsInstance(p.settings.thinking, list)
        self.assertGreater(len(p.settings.thinking), 0)


# --- 4. EN demo overrides are a strict subset (partial-override pattern) ---

class TestEnPackIsPartialOverride(unittest.TestCase):
    def test_en_lines_are_strict_subset_of_overridable_keys(self):
        p = _load(EN_DIR)
        keys = set(p.settings.lines.keys())
        self.assertTrue(keys.issubset(OVERRIDABLE_KEYS),
                         f"unexpected/invalid keys: {keys - OVERRIDABLE_KEYS}")
        self.assertLess(len(keys), len(OVERRIDABLE_KEYS))  # strict, not all of them


# --- 5. .gitignore behavior --------------------------------------------------

class TestGitignoreBehavior(unittest.TestCase):
    def _is_ignored(self, relpath: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "-q", relpath],
            cwd=REPO_ROOT, capture_output=True,
        )
        return result.returncode == 0

    def test_stray_persona_folder_is_ignored(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self.assertTrue(self._is_ignored("personas/somebody/identity.md"))

    def test_shipped_demos_are_not_ignored(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self.assertFalse(self._is_ignored("personas/default/identity.md"))
        self.assertFalse(self._is_ignored("personas/default-zh/settings.yaml"))


# --- 6. M4 stage catalog: both demos walk the same three-stage arc ---------

class TestDemoStages(unittest.TestCase):
    def test_both_demo_personas_have_three_stages(self):
        for folder in (EN_DIR, ZH_DIR):
            p = _load(folder)
            self.assertIsNotNone(p.stages)
            self.assertEqual(len(p.stages), 3)

    def test_demo_stage_names_match_catalog(self):
        en = _load(EN_DIR).stages
        self.assertEqual(tuple(n for n, _ in en),
                         ("Settling in", "In rhythm", "Deep water"))
        zh = _load(ZH_DIR).stages
        self.assertEqual(tuple(n for n, _ in zh), ("安頓", "合拍", "深水區"))


# --- 7. M7 share-topic pools: both demos ship a five-topic pool -------------

class TestDemoShareTopics(unittest.TestCase):
    """Both shipped demos carry a `share:` pool of exactly five topics, all in
    the bookish/homebound register the two personas already anchor to -- book,
    tea, handwriting, a sound at home -- with zero third parties and zero
    physical outings, mirroring the framework's no-fabrication contract."""

    _EN_TOPICS = (
        "the book you are rereading and what it does to you this time",
        "how the light is moving across the bookshelves right now",
        "the tea you just made and the smell of it",
        "a line you copied out by hand today because it deserved ink",
        "a small sound at home you have grown fond of",
    )
    _ZH_TOPICS = (
        "重讀到一半的那本書，這次讀出了什麼新東西",
        "此刻光線正怎麼爬過書牆",
        "剛泡好的茶，還有它的香氣",
        "今天抄下來的一句話，覺得它值得墨水",
        "家裡某個你越來越喜歡的小聲音",
    )

    def test_en_demo_share_topics_exact(self):
        p = _load(EN_DIR)
        self.assertEqual(len(p.settings.share_topics), 5)
        self.assertEqual(p.settings.share_topics, self._EN_TOPICS)

    def test_zh_demo_share_topics_exact(self):
        p = _load(ZH_DIR)
        self.assertEqual(len(p.settings.share_topics), 5)
        self.assertEqual(p.settings.share_topics, self._ZH_TOPICS)


if __name__ == "__main__":
    unittest.main()
