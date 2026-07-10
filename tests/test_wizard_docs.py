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
FAQ = REPO / "docs" / "faq.zh-TW.md"
README_ZH = REPO / "README.zh-TW.md"

# The engine-isolation guarantee CLAUDE.md must keep stating verbatim.
ISOLATION_ANCHOR = ("The companion's engine runs in a neutral working "
                    "directory outside this repo")

# Paths quoted in wizard prose that intentionally do not exist yet
# (created for the user during setup, or created at first boot).
EXPECTED_ABSENT = {"personas/mine", "personas/mine/", "data", "data/",
                   "data/portrait_timeline.html"}

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


class TestReadme(WizardDocMixin, unittest.TestCase):
    DOC = README

    def test_one_line_start_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("Set me up", text)
        self.assertIn("幫我開始", text)

    def test_bootstrap_section_for_claude_code(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("If you are Claude Code", text)
        self.assertIn("git clone", text)

    def test_links_chinese_readme(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("README.zh-TW.md", text)


class TestPersonaGuide(WizardDocMixin, unittest.TestCase):
    DOC = PERSONA_GUIDE

    def test_five_house_rules_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("## 五條家規", text)

    def test_placeholder_warning_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("{stage}", text)


class TestFaq(WizardDocMixin, unittest.TestCase):
    DOC = FAQ

    def test_honest_disclosures_present(self):
        text = self.DOC.read_text(encoding="utf-8")
        for marker in ("他不會唸日記給你聽", "token 已經自動遮罩",
                       "LOG_LEVEL"):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)


class TestReadmeZh(WizardDocMixin, unittest.TestCase):
    DOC = README_ZH

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

    def test_relative_markdown_links_resolve(self):
        text = self.DOC.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(((?!https?://)[^)#]+)\)", text):
            with self.subTest(link=target):
                self.assertTrue((REPO / target).exists(),
                                f"README.zh-TW.md links to {target!r} "
                                f"which does not exist")


if __name__ == "__main__":
    unittest.main()
