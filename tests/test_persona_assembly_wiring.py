"""Tests for the M2 assembler wiring: contact_signals (archive-derived Layer 3
inputs), the module-level persona cache + init/reset, assemble_folder_prompt,
and build_system_prompt's folder-vs-file split. Conventions follow
tests/test_persona_assembly.py and tests/test_warmth.py.

The single most important pin here is the legacy file-mode path: when
persona_path is a FILE, build_system_prompt must keep returning that file's
stripped text verbatim (or DEFAULT_PERSONA on any read/decode failure or empty
file) with NO layers, NO dynamic block, NO cache -- that unchanged path is the
product's L1 rollback guarantee.

Time handling is the second trap: the archive stores timezone-AWARE local
timestamps, while dynamic_context consumes NAIVE local datetimes; comparing the
two raises TypeError. Every test that touches contact_signals passes an explicit
aware `now` and explicit entry timestamps, so nothing here depends on the wall
clock (the one real-clock test, the folder-mode integration, is written
structurally so it cannot flake at an hour boundary). NOW is anchored to
mid-afternoon so `now - a few seconds` never crosses midnight and disturbs the
first_today date math.
"""
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from everthine import archive, persona
from everthine.config import Config, ConfigError
from everthine.dynamic_context import FINAL_CHECK_TEMPLATE, build_dynamic_context
from everthine.layers import compose_stable
from everthine.persona import Persona, PersonaSettings

# Aware local, fixed date, mid-afternoon (far from midnight): subtracting a few
# seconds for the exclusion-window tests never rolls the local date over.
NOW = datetime(2026, 7, 3, 14, 30, 0).astimezone()

SETTINGS_YAML = """\
companion:
  name: {companion}
partner:
  name: {partner}
relationship:
  living: together
  reunion_response: gentle
"""

IDENTITY_TEXT = "I am {companion}: warm, steady, always half a page ahead in the book.\n"


def _naive_local(ts: datetime) -> datetime:
    """Mirror the code's normalization so expected values line up exactly."""
    return ts.astimezone().replace(tzinfo=None)


def _write_folder(root: Path, *, companion="Alex", partner="Sam") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "identity.md").write_text(
        IDENTITY_TEXT.format(companion=companion), encoding="utf-8")
    (root / "settings.yaml").write_text(
        SETTINGS_YAML.format(companion=companion, partner=partner), encoding="utf-8")
    return root


def _folder_persona(*, companion="Alex", partner="Sam", voice_text="",
                    boundaries_text="") -> Persona:
    # Mirror the loader, which strips every block: a faithful direct fixture
    # must too, or a trailing newline would fake a stray blank block.
    settings = PersonaSettings(companion_name=companion, partner_name=partner)
    return Persona(
        mode="folder",
        identity_text=IDENTITY_TEXT.format(companion=companion).strip(),
        voice_text=voice_text.strip(),
        boundaries_text=boundaries_text.strip(),
        settings=settings,
    )


class _CacheResetTest(unittest.TestCase):
    """Base for anything that touches the module-level persona cache: the cache
    is process-global, so it must be cleared before and after each test or one
    test's persona leaks into the next.
    """

    def setUp(self):
        persona.reset_persona_cache()

    def tearDown(self):
        persona.reset_persona_cache()


# --- 1. Legacy file-mode pin (the L1 rollback guarantee) -----------------

class TestLegacyFileModePin(_CacheResetTest):
    def _cfg(self, persona_path: Path) -> Config:
        return Config(bot_token="x", authorized_user_id=1, persona_path=persona_path)

    def test_existing_file_returns_stripped_content_byte_equal(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            body = "You are Testbot.\nSecond line stays intact."
            p.write_text("\n\n  " + body + "  \n\n", encoding="utf-8")
            result = persona.build_system_prompt(self._cfg(p))
            self.assertEqual(result, body)  # exact, stripped, verbatim

    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "does_not_exist.md"
            self.assertEqual(persona.build_system_prompt(self._cfg(p)),
                             persona.DEFAULT_PERSONA)

    def test_empty_file_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.md"
            p.write_text("", encoding="utf-8")
            self.assertEqual(persona.build_system_prompt(self._cfg(p)),
                             persona.DEFAULT_PERSONA)

    def test_whitespace_only_file_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "blank.md"
            p.write_text("   \n\t\n  ", encoding="utf-8")
            self.assertEqual(persona.build_system_prompt(self._cfg(p)),
                             persona.DEFAULT_PERSONA)

    def test_undecodable_bytes_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.md"
            p.write_bytes(b"\xff\xfe\x00bad utf8 \x80\x81")
            self.assertEqual(persona.build_system_prompt(self._cfg(p)),
                             persona.DEFAULT_PERSONA)

    def test_file_mode_has_no_dynamic_content(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            p.write_text("You are Testbot.", encoding="utf-8")
            result = persona.build_system_prompt(self._cfg(p))
            self.assertNotIn("# Right now", result)
            self.assertNotIn("# The ground rules", result)

    def test_file_mode_never_populates_cache(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            p.write_text("You are Testbot.", encoding="utf-8")
            persona.build_system_prompt(self._cfg(p))
            # File mode must not seed the folder-mode cache.
            self.assertIsNone(persona._persona_cache)


# --- 2. contact_signals on a real archive --------------------------------

class TestContactSignalsArchive(unittest.TestCase):
    def _cfg(self, data_dir: Path) -> Config:
        return Config(bot_token="x", authorized_user_id=1, data_dir=data_dir)

    def test_last_contact_is_newest_user_naive_local(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            d = cfg.archive_dir
            archive.write_entry(d, "user", "older", ts=NOW - timedelta(hours=3))
            archive.write_entry(d, "companion", "reply", ts=NOW - timedelta(hours=2))
            newest_user = NOW - timedelta(hours=1)
            archive.write_entry(d, "user", "newest", ts=newest_user)
            last_contact, _ = persona.contact_signals(cfg, NOW)
            self.assertEqual(last_contact, _naive_local(newest_user))
            self.assertIsNone(last_contact.tzinfo)  # naive, ready for Layer 3

    def test_last_contact_is_max_not_last_seen(self):
        # A later-written but chronologically OLDER user line (minor clock skew)
        # must not shadow the true newest.
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            d = cfg.archive_dir
            newest_user = NOW - timedelta(hours=1)
            archive.write_entry(d, "user", "newest", ts=newest_user)
            archive.write_entry(d, "user", "skewed older", ts=NOW - timedelta(hours=5))
            last_contact, _ = persona.contact_signals(cfg, NOW)
            self.assertEqual(last_contact, _naive_local(newest_user))

    def test_companion_only_returns_none_last_contact(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            d = cfg.archive_dir
            archive.write_entry(d, "companion", "hi", ts=NOW - timedelta(hours=2))
            last_contact, _ = persona.contact_signals(cfg, NOW)
            self.assertIsNone(last_contact)

    def test_first_today_false_when_entry_today(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            d = cfg.archive_dir
            archive.write_entry(d, "user", "earlier today", ts=NOW - timedelta(hours=3))
            _, first_today = persona.contact_signals(cfg, NOW)
            self.assertFalse(first_today)

    def test_first_today_true_when_only_older_days(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            d = cfg.archive_dir
            archive.write_entry(d, "user", "two days ago", ts=NOW - timedelta(days=2))
            archive.write_entry(d, "companion", "two days ago", ts=NOW - timedelta(days=2))
            _, first_today = persona.contact_signals(cfg, NOW)
            self.assertTrue(first_today)

    def test_empty_dir_returns_none_true(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            cfg.archive_dir.mkdir(parents=True, exist_ok=True)
            self.assertEqual(persona.contact_signals(cfg, NOW), (None, True))

    def test_missing_dir_returns_none_true(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td) / "nonexistent")
            self.assertEqual(persona.contact_signals(cfg, NOW), (None, True))


# --- 3. Aware/naive robustness (the TypeError trap) ----------------------

class TestAwareNaiveRobustness(unittest.TestCase):
    def test_mixed_aware_and_naive_entries_no_typeerror(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(bot_token="x", authorized_user_id=1, data_dir=Path(td))
            d = cfg.archive_dir
            # Aware entry (the normal path) ...
            aware_user = NOW - timedelta(hours=2)
            archive.write_entry(d, "user", "aware", ts=aware_user)
            # ... and a NAIVE entry (write_entry stores it without an offset).
            naive_user = (NOW - timedelta(hours=1)).replace(tzinfo=None)
            archive.write_entry(d, "user", "naive", ts=naive_user)
            # Must not raise; both normalize into the same naive-local space.
            last_contact, first_today = persona.contact_signals(cfg, NOW)
            self.assertEqual(last_contact, naive_user)  # already naive local
            self.assertFalse(first_today)


# --- 4. Exclusion window (the just-archived live turn) --------------------

class TestExclusionWindow(unittest.TestCase):
    def _cfg(self, data_dir: Path) -> Config:
        return Config(bot_token="x", authorized_user_id=1, data_dir=data_dir)

    def test_live_turn_excluded_keeps_older_user(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            d = cfg.archive_dir
            archive.write_entry(d, "user", "live turn", ts=NOW - timedelta(seconds=2))
            older = NOW - timedelta(hours=26)
            archive.write_entry(d, "user", "yesterday", ts=older)
            last_contact, _ = persona.contact_signals(cfg, NOW)
            self.assertEqual(last_contact, _naive_local(older))

    def test_only_live_turn_returns_none_and_first_today_true(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            d = cfg.archive_dir
            archive.write_entry(d, "user", "live turn", ts=NOW - timedelta(seconds=2))
            last_contact, first_today = persona.contact_signals(cfg, NOW)
            self.assertIsNone(last_contact)   # only entry excluded -> no user
            self.assertTrue(first_today)      # excluded entry doesn't count today

    def test_rapid_fire_keeps_previous_message(self):
        # The misfire trap: live turn at now-2s (excluded) plus the PREVIOUS
        # message of a hot conversation at now-50s (kept). last_contact must be
        # the 50s one -> gap ~0 -> caller emits no reunion; it must NOT skip to
        # something hours old and misfire a reunion line mid-chat.
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            d = cfg.archive_dir
            archive.write_entry(d, "user", "live turn", ts=NOW - timedelta(seconds=2))
            prev = NOW - timedelta(seconds=50)
            archive.write_entry(d, "user", "previous", ts=prev)
            archive.write_entry(d, "user", "hours ago", ts=NOW - timedelta(hours=6))
            last_contact, first_today = persona.contact_signals(cfg, NOW)
            self.assertEqual(last_contact, _naive_local(prev))
            self.assertFalse(first_today)


# --- 5. Exception degradation --------------------------------------------

class TestExceptionDegradation(unittest.TestCase):
    def test_iter_entries_raises_degrades_to_none_true_with_warning(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(bot_token="x", authorized_user_id=1, data_dir=Path(td))
            original = archive.iter_entries

            def boom(*args, **kwargs):
                raise RuntimeError("archive on fire")

            archive.iter_entries = boom
            try:
                with self.assertLogs("everthine", level="WARNING"):
                    result = persona.contact_signals(cfg, NOW)
            finally:
                archive.iter_entries = original
            self.assertEqual(result, (None, True))


# --- 6. assemble_folder_prompt (pure, deterministic) ---------------------

class TestAssembleFolderPrompt(unittest.TestCase):
    def test_stable_first_dynamic_after_joined_blank_line(self):
        p = _folder_persona()
        now_naive = _naive_local(NOW)
        last_contact = now_naive - timedelta(hours=30)
        result = persona.assemble_folder_prompt(p, now_naive, last_contact, False)
        expected = (compose_stable(p) + "\n\n"
                    + build_dynamic_context(p.settings, now_naive, last_contact, False))
        self.assertEqual(result, expected)

    def test_byte_identical_across_two_calls(self):
        p = _folder_persona()
        now_naive = _naive_local(NOW)
        a = persona.assemble_folder_prompt(p, now_naive, None, True)
        b = persona.assemble_folder_prompt(p, now_naive, None, True)
        self.assertEqual(a, b)

    def test_contains_dna_heading_and_right_now(self):
        p = _folder_persona()
        result = persona.assemble_folder_prompt(p, _naive_local(NOW), None, True)
        self.assertIn("# The ground rules", result)
        self.assertIn("# Right now", result)

    def test_final_check_block_is_last(self):
        p = _folder_persona()
        result = persona.assemble_folder_prompt(p, _naive_local(NOW), None, True)
        self.assertTrue(result.endswith(FINAL_CHECK_TEMPLATE))
        self.assertNotIn("\n\n\n", result)  # no stray blank block


# --- 7. build_system_prompt folder mode, end-to-end (real clock) ---------

class TestBuildSystemPromptFolderIntegration(_CacheResetTest):
    def _setup(self, td) -> Config:
        folder = _write_folder(Path(td) / "persona", companion="Alex", partner="Sam")
        return Config(bot_token="x", authorized_user_id=1,
                      persona_path=folder, data_dir=Path(td) / "state")

    def test_folder_mode_structure_present(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._setup(td)
            result = persona.build_system_prompt(cfg)
            self.assertIn("You are Alex.", result)          # declaration
            self.assertIn("# The ground rules", result)
            self.assertIn("# Right now", result)            # dynamic block wired
            self.assertTrue(result.endswith(FINAL_CHECK_TEMPLATE))
            self.assertEqual(len(__import__("re").findall(
                r"^## \d+\. ", result, flags=__import__("re").M)), 7)

    def test_two_consecutive_calls_stable_prefix_identical(self):
        # Robust against an hour-boundary straddle: the stable prefix (Layer 1/2)
        # never depends on the clock, so it is byte-equal across calls even if
        # the dynamic block's rounded hour were to tick over between them.
        with tempfile.TemporaryDirectory() as td:
            cfg = self._setup(td)
            a = persona.build_system_prompt(cfg)
            b = persona.build_system_prompt(cfg)
            self.assertEqual(a.split("# Right now")[0], b.split("# Right now")[0])
            self.assertIn("# Right now", a)
            self.assertIn("# Right now", b)


# --- 8/9. Persona cache: init, disk break, reset, path change ------------

class TestPersonaCache(_CacheResetTest):
    def test_init_caches_and_survives_disk_break(self):
        with tempfile.TemporaryDirectory() as td:
            folder = _write_folder(Path(td) / "persona", companion="Alex")
            cfg = Config(bot_token="x", authorized_user_id=1,
                         persona_path=folder, data_dir=Path(td) / "state")
            persona.init(cfg)
            # Break the persona on disk but keep the directory (still folder mode).
            (folder / "identity.md").unlink()
            result = persona.build_system_prompt(cfg)  # served from cache
            self.assertIn("You are Alex.", result)

    def test_reset_then_reload_reflects_broken_disk_fail_loud(self):
        with tempfile.TemporaryDirectory() as td:
            folder = _write_folder(Path(td) / "persona", companion="Alex")
            cfg = Config(bot_token="x", authorized_user_id=1,
                         persona_path=folder, data_dir=Path(td) / "state")
            persona.init(cfg)
            (folder / "identity.md").unlink()
            persona.reset_persona_cache()
            # Lazy reload now sees the broken folder and fails loud.
            with self.assertRaises(ConfigError):
                persona.build_system_prompt(cfg)

    def test_lazy_load_without_init_works(self):
        with tempfile.TemporaryDirectory() as td:
            folder = _write_folder(Path(td) / "persona", companion="Alex")
            cfg = Config(bot_token="x", authorized_user_id=1,
                         persona_path=folder, data_dir=Path(td) / "state")
            # No init() call: folder mode must still work via lazy-load.
            result = persona.build_system_prompt(cfg)
            self.assertIn("You are Alex.", result)
            self.assertIsNotNone(persona._persona_cache)

    def test_init_file_mode_clears_slot(self):
        with tempfile.TemporaryDirectory() as td:
            folder = _write_folder(Path(td) / "persona", companion="Alex")
            cfg_folder = Config(bot_token="x", authorized_user_id=1,
                                persona_path=folder, data_dir=Path(td) / "state")
            persona.init(cfg_folder)
            self.assertIsNotNone(persona._persona_cache)
            f = Path(td) / "p.md"
            f.write_text("You are Testbot.", encoding="utf-8")
            cfg_file = Config(bot_token="x", authorized_user_id=1, persona_path=f)
            persona.init(cfg_file)
            self.assertIsNone(persona._persona_cache)

    def test_path_change_serves_new_folder(self):
        with tempfile.TemporaryDirectory() as td:
            folder_a = _write_folder(Path(td) / "a", companion="Alex")
            folder_b = _write_folder(Path(td) / "b", companion="Robin")
            cfg_a = Config(bot_token="x", authorized_user_id=1,
                           persona_path=folder_a, data_dir=Path(td) / "state")
            cfg_b = Config(bot_token="x", authorized_user_id=1,
                           persona_path=folder_b, data_dir=Path(td) / "state")
            persona.init(cfg_a)
            result = persona.build_system_prompt(cfg_b)  # path differs -> reload
            self.assertIn("You are Robin.", result)
            self.assertNotIn("You are Alex.", result)


# --- 10. M3 seam: memory_block threading + current_settings --------------

class TestMemoryBlockWiring(_CacheResetTest):
    """Folder mode: build_system_prompt threads memory_block straight through
    to assemble_folder_prompt (positioned before the final check, per
    dynamic_context's own ordering pin), and omitting it stays byte-identical
    to passing memory_block=None explicitly -- the same seam-silence contract
    dynamic_context pins directly, now proven at the outer wiring layer.
    """

    def _setup(self, td) -> Config:
        folder = _write_folder(Path(td) / "persona", companion="Alex", partner="Sam")
        return Config(bot_token="x", authorized_user_id=1,
                      persona_path=folder, data_dir=Path(td) / "state")

    def test_folder_mode_memory_block_before_final_check(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._setup(td)
            result = persona.build_system_prompt(cfg, memory_block="MEMBLOCK-SENTINEL")
            self.assertIn("MEMBLOCK-SENTINEL", result)
            self.assertLess(result.index("MEMBLOCK-SENTINEL"), result.index(FINAL_CHECK_TEMPLATE))

    def test_folder_mode_memory_block_none_byte_identical_to_no_arg(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._setup(td)
            a = persona.build_system_prompt(cfg, memory_block=None)
            b = persona.build_system_prompt(cfg)
            self.assertEqual(a, b)

    def test_current_settings_folder_mode_returns_settings(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._setup(td)
            settings = persona.current_settings(cfg)
            self.assertIsNotNone(settings)
            self.assertEqual(settings.companion_name, "Alex")


class TestFileModeMemoryBlockIgnored(_CacheResetTest):
    """File mode is the L1 rollback target: memory_block must be a no-op,
    byte-identical to the no-arg call, same as every other legacy pin in
    section 1 above.
    """

    def _cfg(self, persona_path: Path) -> Config:
        return Config(bot_token="x", authorized_user_id=1, persona_path=persona_path)

    def test_file_mode_memory_block_ignored_and_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            p.write_text("You are Testbot.", encoding="utf-8")
            cfg = self._cfg(p)
            with_block = persona.build_system_prompt(cfg, memory_block="MEMBLOCK-SENTINEL")
            without_block = persona.build_system_prompt(cfg)
            self.assertNotIn("MEMBLOCK-SENTINEL", with_block)
            self.assertEqual(with_block, without_block)

    def test_current_settings_file_mode_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            p.write_text("You are Testbot.", encoding="utf-8")
            cfg = self._cfg(p)
            self.assertIsNone(persona.current_settings(cfg))


# --- 11. M5 T7 seam: inner_block threading (his own recent diary days) ----

class TestInnerBlockWiring(_CacheResetTest):
    """Folder mode: build_system_prompt threads inner_block straight through
    to assemble_folder_prompt -> build_dynamic_context (positioned before
    the memory block and the final check, per dynamic_context's ordering
    pin), and omitting it stays byte-identical to passing inner_block=None
    explicitly -- the same seam-silence contract memory_block already honors,
    now proven at the outer wiring layer for the new seam too.
    """

    def _setup(self, td) -> Config:
        folder = _write_folder(Path(td) / "persona", companion="Alex", partner="Sam")
        return Config(bot_token="x", authorized_user_id=1,
                      persona_path=folder, data_dir=Path(td) / "state")

    def test_folder_mode_inner_block_before_final_check(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._setup(td)
            result = persona.build_system_prompt(cfg, inner_block="INNERBLOCK-SENTINEL")
            self.assertIn("INNERBLOCK-SENTINEL", result)
            self.assertLess(result.index("INNERBLOCK-SENTINEL"),
                            result.index(FINAL_CHECK_TEMPLATE))

    def test_folder_mode_inner_before_memory_when_both_present(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._setup(td)
            result = persona.build_system_prompt(
                cfg, memory_block="MEMBLOCK-SENTINEL", inner_block="INNERBLOCK-SENTINEL")
            self.assertLess(result.index("INNERBLOCK-SENTINEL"),
                            result.index("MEMBLOCK-SENTINEL"))

    def test_folder_mode_inner_block_none_byte_identical_to_no_arg(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._setup(td)
            a = persona.build_system_prompt(cfg, inner_block=None)
            b = persona.build_system_prompt(cfg)
            self.assertEqual(a, b)


class TestFileModeInnerBlockIgnored(_CacheResetTest):
    """File mode is the L1 rollback target: inner_block must be a no-op,
    byte-identical to the no-arg call, exactly like memory_block."""

    def _cfg(self, persona_path: Path) -> Config:
        return Config(bot_token="x", authorized_user_id=1, persona_path=persona_path)

    def test_file_mode_inner_block_ignored_and_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.md"
            p.write_text("You are Testbot.", encoding="utf-8")
            cfg = self._cfg(p)
            with_block = persona.build_system_prompt(cfg, inner_block="INNERBLOCK-SENTINEL")
            without_block = persona.build_system_prompt(cfg)
            self.assertNotIn("INNERBLOCK-SENTINEL", with_block)
            self.assertEqual(with_block, without_block)


if __name__ == "__main__":
    unittest.main()
