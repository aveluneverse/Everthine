"""Integrity tests for the setup-wizard documents (M8).

These documents are instructions for the *user's* Claude Code. A wrong
path or a misspelled env key means a stranger's setup breaks on the very
first day, so every reference they contain is pinned against the real
repo here. Prose quality is reviewed by humans; this file only guards
the mechanical layer.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CLAUDE_MD = REPO / "CLAUDE.md"
SETUP_SKILL = REPO / ".claude" / "skills" / "everthine-setup" / "SKILL.md"
INTERVIEW = REPO / ".claude" / "skills" / "everthine-setup" / "references" / "interview.md"
DEPLOY = REPO / ".claude" / "skills" / "everthine-setup" / "references" / "deploy.md"
TROUBLESHOOT = REPO / ".claude" / "skills" / "everthine-troubleshoot" / "SKILL.md"
README = REPO / "README.md"
PERSONA_GUIDE = REPO / "docs" / "persona-guide.zh-TW.md"
PERSONA_GUIDE_EN = REPO / "docs" / "persona-guide.md"
FAQ = REPO / "docs" / "faq.zh-TW.md"
FAQ_EN = REPO / "docs" / "faq.md"
README_EN = REPO / "README.en.md"

# The engine-isolation guarantee CLAUDE.md must keep stating verbatim.
ISOLATION_ANCHOR = ("The companion's engine runs in a neutral working "
                    "directory outside this repo")

# Paths quoted in wizard prose that intentionally do not exist yet
# (created for the user during setup, or created at first boot).
EXPECTED_ABSENT = {"personas/mine", "personas/mine/", "data", "data/",
                   "data/portrait_timeline.html", "data/observatory.html"}

_PATH_RE = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*)`")
_ENV_ASSIGN_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=")
_ENV_TICK_RE = re.compile(r"`([A-Z][A-Z0-9_]{2,})`")


def _extract_repo_paths(text: str) -> set[str]:
    """Backtick-quoted tokens that look like repo-relative paths."""
    out = set()
    for tok in _PATH_RE.findall(text):
        if "/" in tok and not tok.startswith(("http", "-")):
            out.add(tok)
    return out


def _extract_env_keys(text: str) -> set[str]:
    """Env keys named as `KEY=value` or as a bare backticked `KEY`."""
    return set(_ENV_ASSIGN_RE.findall(text)) | set(_ENV_TICK_RE.findall(text))


def _load_frontmatter(path: Path) -> dict:
    import yaml
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"{path.name}: missing YAML frontmatter")
    block = text.split("---\n", 2)[1]
    data = yaml.safe_load(block)
    if not isinstance(data, dict):
        raise AssertionError(f"{path.name}: frontmatter is not a mapping")
    return data


def _known_env_keys() -> set[str]:
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    return set(_ENV_ASSIGN_RE.findall(example))


def _observatory_days_default() -> int:
    """The CLI default for --days, read straight from the observatory module,
    so the number the FAQ prints can be pinned against the code and can never
    silently drift from it."""
    from everthine import observatory
    return observatory._build_parser().parse_args([]).days


# Chinese spellings of the small integers the --days default might realistically
# take; extend this map when the CLI default changes to a value not listed.
_CN_NUMERALS = {14: "十四"}


class WizardDocMixin:
    """Shared checks; subclasses set DOC."""
    DOC: Path

    def test_exists(self):
        self.assertTrue(self.DOC.is_file(), f"{self.DOC} missing")

    def test_quoted_paths_exist(self):
        text = self.DOC.read_text(encoding="utf-8")
        for rel in sorted(_extract_repo_paths(text)):
            if rel in EXPECTED_ABSENT:
                continue
            with self.subTest(path=rel):
                self.assertTrue(
                    (REPO / rel).exists() or (self.DOC.parent / rel).exists(),
                    f"{self.DOC.name} references {rel!r} which does not "
                    f"exist in the repo (checked repo root and the "
                    f"document's own folder)")

    def test_env_keys_are_real(self):
        text = self.DOC.read_text(encoding="utf-8")
        known = _known_env_keys()
        for key in sorted(_extract_env_keys(text)):
            with self.subTest(key=key):
                self.assertIn(key, known,
                              f"{self.DOC.name} mentions unknown env key "
                              f"{key!r} -- a typo would break a user's setup")


class TestInterview(WizardDocMixin, unittest.TestCase):
    DOC = INTERVIEW

    def test_mandatory_first_questions_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        # Naming first (2026-07-07 ruling), then both genders (R1).
        self.assertIn("name their companion", text)
        self.assertIn("user's own gender", text)
        self.assertIn("companion's gender", text)


class TestDeploy(WizardDocMixin, unittest.TestCase):
    DOC = DEPLOY

    def test_covers_first_boot_download_warning(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("hundred MB", text)

    def test_viewer_command_verbatim(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("python -m everthine.portrait_viewer --data-dir data",
                      text)


class TestGitignoreProtectsUserPersona(unittest.TestCase):
    def test_personas_mine_ignored(self):
        lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("personas/mine/", lines)


class TestSetupSkill(WizardDocMixin, unittest.TestCase):
    DOC = SETUP_SKILL

    def test_frontmatter_shape(self):
        fm = _load_frontmatter(self.DOC)
        self.assertEqual(fm.get("name"), "everthine-setup")
        self.assertIn("description", fm)
        self.assertLess(len(fm["description"]), 500)

    def test_references_both_guides(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("references/interview.md", text)
        self.assertIn("references/deploy.md", text)


class TestTroubleshootSkill(WizardDocMixin, unittest.TestCase):
    DOC = TROUBLESHOOT

    def test_frontmatter_shape(self):
        fm = _load_frontmatter(self.DOC)
        self.assertEqual(fm.get("name"), "everthine-troubleshoot")
        self.assertIn("description", fm)
        self.assertLess(len(fm["description"]), 500)

    def test_covers_known_failures(self):
        text = self.DOC.read_text(encoding="utf-8")
        for marker in ("BOT_TOKEN is required", "LOG_LEVEL",
                       "AUTHORIZED_USER_ID", "silence is by design"):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)


class TestClaudeMd(WizardDocMixin, unittest.TestCase):
    DOC = CLAUDE_MD

    def test_isolation_anchor_present(self):
        self.assertIn(ISOLATION_ANCHOR,
                      self.DOC.read_text(encoding="utf-8"))

    def test_routes_to_both_skills(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("everthine-setup", text)
        self.assertIn("everthine-troubleshoot", text)


class TestReadmeEn(WizardDocMixin, unittest.TestCase):
    DOC = README_EN

    def test_one_line_start_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("Set me up", text)
        self.assertIn("幫我開始", text)

    def test_bootstrap_section_for_claude_code(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("If you are Claude Code", text)
        self.assertIn("git clone", text)

    def test_language_switch_line(self):
        # M10 P2: the English translation points back to the Traditional
        # Chinese facade right under the title.
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("[繁體中文](README.md)", text)

    def test_observatory_section_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        # Command + output filename, both pinned literally (their truth as a
        # real module is checked in TestObservatoryDocsMatchModule).
        self.assertIn("python -m everthine.observatory", text)
        self.assertIn("observatory.html", text)
        # Anchor: the promise that he cannot see this window -- the whole
        # reason the page can be honest, unlikely to be casually reworded.
        self.assertIn("he does not know this window exists", text)

    def test_relative_markdown_links_resolve(self):
        text = self.DOC.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(((?!https?://)[^)#]+)\)", text):
            with self.subTest(link=target):
                self.assertTrue((REPO / target).exists(),
                                f"{self.DOC.name} links to {target!r} "
                                f"which does not exist")


class TestPersonaGuide(WizardDocMixin, unittest.TestCase):
    DOC = PERSONA_GUIDE

    def test_five_house_rules_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("## 五條家規", text)

    def test_placeholder_warning_present(self):
        # The warning is generic on purpose ({curly braces}, not a named
        # placeholder): stages are retired from every shipped document
        # (owner ruling, 2026-07-10), so no doc may name {stage} again.
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("{大括號}", text)
        self.assertNotIn("{stage}", text)


class TestPersonaGuideEn(WizardDocMixin, unittest.TestCase):
    DOC = PERSONA_GUIDE_EN

    def test_five_house_rules_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("## Five house rules", text)

    def test_placeholder_warning_present(self):
        # Generic on purpose -- see TestPersonaGuide's twin for why no
        # shipped document may name {stage} again.
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("{curly braces}", text)
        self.assertNotIn("{stage}", text)


class TestFaq(WizardDocMixin, unittest.TestCase):
    DOC = FAQ

    def test_honest_disclosures_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        for marker in ("他不會主動對你揭露", "token 已經自動遮罩",
                       "LOG_LEVEL", "電腦關著，還能找到他嗎"):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_facts_memory_entry_present(self):
        # D1: the structured-facts notebook and its off-switch promise.
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("FACTS_ENABLED=false", text)
        # Distinctive anchor -- the "he'd rather ask than fabricate"
        # promise, unlikely to be casually reworded.
        self.assertIn("他寧可問你，不硬編", text)

    def test_observatory_entry_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("python -m everthine.observatory", text)
        self.assertIn("observatory.html", text)
        # The look-back knob, pinned in both FAQs (see the module cross-check).
        self.assertIn("--days", text)
        # Anchor: 「他不知道它存在」-- the reason not to quote his diary back.
        self.assertIn("他不知道它存在", text)


class TestFaqEn(WizardDocMixin, unittest.TestCase):
    DOC = FAQ_EN

    def test_honest_disclosures_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        for marker in ("won't volunteer", "bot<TOKEN>",
                       "LOG_LEVEL", "computer is off"):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_facts_memory_entry_present(self):
        # D1: the structured-facts notebook and its off-switch promise.
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("FACTS_ENABLED=false", text)
        # Distinctive anchor -- the don't-fabricate promise.
        self.assertIn("rather ask you than make it up", text)

    def test_observatory_entry_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("python -m everthine.observatory", text)
        self.assertIn("observatory.html", text)
        # The look-back knob, pinned in both FAQs (see the module cross-check).
        self.assertIn("--days", text)
        # Anchor: the reassurance he cannot see the page he is written into.
        self.assertIn("he does not know it exists", text)


class TestReadmeZh(WizardDocMixin, unittest.TestCase):
    DOC = README

    def test_decree_copy_anchors(self):
        text = self.DOC.read_text(encoding="utf-8")
        for anchor in ("自訂是出廠設定；養成是歲月",
                       "這個迴圈就是養成引擎",
                       "快不了，也假不了"):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, text)

    def test_cost_section_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("## 費用，誠實說", text)

    def test_observatory_section_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("python -m everthine.observatory", text)
        self.assertIn("observatory.html", text)
        # Anchor: 「他不知道這扇窗存在」-- the reassurance the whole section turns on.
        self.assertIn("他不知道這扇窗存在", text)

    def test_language_switch_line(self):
        # M10 P2: the facade offers the English translation right under
        # the title.
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("[English](README.en.md)", text)

    def test_relative_markdown_links_resolve(self):
        text = self.DOC.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(((?!https?://)[^)#]+)\)", text):
            with self.subTest(link=target):
                self.assertTrue((REPO / target).exists(),
                                f"{self.DOC.name} links to {target!r} "
                                f"which does not exist")


class TestObservatoryDocsMatchModule(unittest.TestCase):
    """The Observatory section names a real module and a real default; both
    are pinned against the code so the four docs cannot drift from it."""

    def test_module_is_real(self):
        # `python -m everthine.observatory`, quoted in all four docs, must
        # resolve to an importable module with a CLI entry point.
        import importlib
        mod = importlib.import_module("everthine.observatory")
        self.assertTrue(hasattr(mod, "main"))

    def test_faq_recent_days_matches_cli_default(self):
        default = _observatory_days_default()
        faq_en = FAQ_EN.read_text(encoding="utf-8")
        faq_zh = FAQ.read_text(encoding="utf-8")
        self.assertIn("--days", faq_en)
        self.assertIn("--days", faq_zh)
        # Each FAQ states the look-back in its own script; the number must be
        # the code's real default, so a CLI change surfaces here rather than
        # as stale prose.
        self.assertIn(str(default), faq_en)
        spelled = _CN_NUMERALS.get(default)
        self.assertIsNotNone(
            spelled, f"add the Chinese numeral for {default} to _CN_NUMERALS")
        self.assertIn(spelled, faq_zh)


if __name__ == "__main__":
    unittest.main()
