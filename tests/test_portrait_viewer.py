"""portrait_viewer.py: rendering data/portrait_history/*.json snapshots into
one self-contained, offline, double-clickable timeline HTML.

The viewer is a deliberate island -- it imports neither config nor bot -- so
these tests drive it purely through main(["--data-dir", ...]) against seeded
snapshot files in a tempdir, and read the emitted HTML back off disk. No
browser, no screenshots: visual acceptance belongs to the project owner; the
tests only pin structure, escaping, ordering, fail-soft skipping, the empty
state, and the self-contained (zero-external-reference) contract.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from everthine import portrait_viewer


def _snapshot(updated, content, opinions=None, observations=None):
    return {
        "updated": updated,
        "content": content,
        "opinions": opinions or [],
        "observations": observations or [],
    }


class PortraitViewerTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data_dir = Path(self._td.name)
        self.history_dir = self.data_dir / "portrait_history"
        self.out = self.data_dir / "portrait_timeline.html"

    # -- helpers ---------------------------------------------------------
    def _seed(self, name, data):
        self.history_dir.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        (self.history_dir / name).write_text(text, encoding="utf-8")

    def _run(self):
        # main() prints the output path; keep it out of the test runner's
        # stdout so a full-suite run stays print-clean (dots/summary only).
        with contextlib.redirect_stdout(io.StringIO()):
            return portrait_viewer.main(["--data-dir", str(self.data_dir)])

    def _html(self):
        return self.out.read_text(encoding="utf-8")

    # -- tests -----------------------------------------------------------
    def test_produces_file_and_exit_zero(self):
        self._seed("2026-07-01.json", _snapshot("2026-07-01", "the first week"))
        self._seed("2026-07-08.json", _snapshot("2026-07-08", "the second week"))
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())
        html = self._html()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn('<meta charset="utf-8">', html)

    def test_versions_dates_and_chronological_order(self):
        self._seed("2026-07-01.json", _snapshot("2026-07-01", "alpha"))
        self._seed("2026-07-08.json", _snapshot("2026-07-08", "beta"))
        self._seed("2026-07-15.json", _snapshot("2026-07-15", "gamma"))
        self._run()
        html = self._html()
        self.assertIn("Version 1 · 2026-07-01", html)
        self.assertIn("Version 2 · 2026-07-08", html)
        self.assertIn("Version 3 · 2026-07-15", html)
        # old -> new, top to bottom
        self.assertLess(html.index("Version 1"), html.index("Version 2"))
        self.assertLess(html.index("Version 2"), html.index("Version 3"))
        self.assertLess(html.index("2026-07-01"), html.index("2026-07-15"))

    def test_escapes_content(self):
        self._seed("2026-07-01.json",
                   _snapshot("2026-07-01", "<script>alert(1)</script>"))
        self._run()
        html = self._html()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_escapes_opinions_observations_and_date(self):
        self._seed("2026-07-01.json",
                   _snapshot("<b>D</b>", "body text",
                             opinions=[{"topic": "<i>t</i>", "opinion": "<u>o</u>"}],
                             observations=["<s>obs</s>"]))
        self._run()
        html = self._html()
        for raw in ("<b>D</b>", "<i>t</i>", "<u>o</u>", "<s>obs</s>"):
            self.assertNotIn(raw, html)
        self.assertIn("Positions", html)
        self.assertIn("Notes to self", html)

    def test_sections_absent_when_empty(self):
        self._seed("2026-07-01.json", _snapshot("2026-07-01", "just prose"))
        self._run()
        html = self._html()
        self.assertNotIn("Positions", html)
        self.assertNotIn("Notes to self", html)

    def test_bad_json_is_skipped(self):
        self._seed("2026-07-01.json", _snapshot("2026-07-01", "good one"))
        self._seed("2026-07-08.json", "{not valid json")
        self._seed("2026-07-15.json", _snapshot("2026-07-15", "good two"))
        rc = self._run()
        self.assertEqual(rc, 0)
        html = self._html()
        self.assertIn("Version 1 · 2026-07-01", html)
        self.assertIn("Version 2 · 2026-07-15", html)
        self.assertNotIn("Version 3", html)
        self.assertIn("good one", html)
        self.assertIn("good two", html)

    def test_invalid_utf8_bytes_are_skipped(self):
        # UnicodeDecodeError is a ValueError, not an OSError or a
        # JSONDecodeError -- a corrupt-encoding snapshot must be skipped
        # exactly like invalid JSON, never crash the render.
        self._seed("2026-07-01.json", _snapshot("2026-07-01", "good one"))
        (self.history_dir / "2026-07-08.json").write_bytes(b"\xff\xfe{ not utf8")
        self._seed("2026-07-15.json", _snapshot("2026-07-15", "good two"))
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())
        html = self._html()
        self.assertIn("Version 1 · 2026-07-01", html)
        self.assertIn("Version 2 · 2026-07-15", html)
        self.assertNotIn("Version 3", html)

    def test_non_object_json_is_skipped(self):
        self._seed("2026-07-01.json", json.dumps(["not", "a", "dict"]))
        self._seed("2026-07-08.json", _snapshot("2026-07-08", "kept"))
        rc = self._run()
        self.assertEqual(rc, 0)
        html = self._html()
        self.assertIn("Version 1 · 2026-07-08", html)
        self.assertNotIn("Version 2", html)

    def test_content_missing_is_skipped(self):
        self._seed("2026-07-01.json",
                   {"updated": "2026-07-01", "opinions": [], "observations": []})
        self._seed("2026-07-08.json", _snapshot("2026-07-08", "kept"))
        rc = self._run()
        self.assertEqual(rc, 0)
        html = self._html()
        self.assertIn("Version 1 · 2026-07-08", html)
        self.assertNotIn("Version 2", html)

    def test_updated_missing_falls_back_to_filename_stem(self):
        self._seed("2026-07-01.json",
                   {"content": "no updated field here",
                    "opinions": [], "observations": []})
        self._run()
        html = self._html()
        self.assertIn("Version 1 · 2026-07-01", html)

    def test_opinions_observations_missing_default_empty(self):
        self._seed("2026-07-01.json", {"updated": "2026-07-01", "content": "only content"})
        rc = self._run()
        self.assertEqual(rc, 0)
        html = self._html()
        self.assertIn("only content", html)
        self.assertNotIn("Positions", html)
        self.assertNotIn("Notes to self", html)

    def test_paragraphs_and_linebreaks_preserved(self):
        self._seed("2026-07-01.json",
                   _snapshot("2026-07-01", "para one line a\npara one line b\n\npara two"))
        self._run()
        html = self._html()
        self.assertIn("para one line a<br>para one line b", html)
        self.assertIn("<p>para two</p>", html)

    def test_empty_history_missing_dir(self):
        # history_dir is never created
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())
        html = self._html()
        self.assertNotIn("Version 1", html)
        self.assertIn("first one", html)  # empty-state copy present

    def test_empty_history_present_but_empty(self):
        self.history_dir.mkdir(parents=True, exist_ok=True)
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())
        self.assertNotIn("Version 1", self._html())

    def test_empty_history_when_all_files_skipped(self):
        self._seed("2026-07-01.json", "{not valid json")
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertNotIn("Version 1", self._html())

    def test_no_external_references(self):
        self._seed("2026-07-01.json",
                   _snapshot("2026-07-01", "hello there",
                             opinions=[{"topic": "tea", "opinion": "warm"}],
                             observations=["reads at night"]))
        self._run()
        html = self._html()
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        # by design the page carries no images, scripts, or links: pin the
        # absence of every external-resource vector, not just bare URLs.
        self.assertNotIn("url(", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("src=", html)

    def test_no_external_references_on_empty_page(self):
        self._run()
        html = self._html()
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("url(", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("src=", html)

    def test_positions_render_topic_only_and_opinion_only(self):
        # _render_positions renders a lenient one-part <li> for a hand-edited
        # element that carries only a topic string, or only an opinion string.
        self._seed("2026-07-01.json",
                   _snapshot("2026-07-01", "body text",
                             opinions=[{"topic": "TOPIC_ONLY_ITEM"},
                                       {"opinion": "OPINION_ONLY_ITEM"}]))
        self._run()
        html = self._html()
        self.assertIn("Positions", html)
        self.assertIn('<span class="topic">TOPIC_ONLY_ITEM</span>', html)
        self.assertIn("OPINION_ONLY_ITEM", html)

    def test_prints_output_path(self):
        self._seed("2026-07-01.json", _snapshot("2026-07-01", "x"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            portrait_viewer.main(["--data-dir", str(self.data_dir)])
        self.assertIn("portrait_timeline.html", buf.getvalue())

    def test_cjk_content_round_trips(self):
        self._seed("2026-07-01.json",
                   _snapshot("2026-07-01", "今天很安静。"))
        self._run()
        html = self._html()
        self.assertIn("今天很安静", html)

    def test_default_data_dir_argument(self):
        # --data-dir is optional; parser default must be "data"
        parser = portrait_viewer._build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.data_dir, "data")


if __name__ == "__main__":
    unittest.main()
