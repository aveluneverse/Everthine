import unittest

from everthine.chunking import split_message


class TestChunking(unittest.TestCase):
    def test_short_passthrough(self):
        self.assertEqual(split_message("hello"), ["hello"])

    def test_empty(self):
        self.assertEqual(split_message(""), [])

    def test_splits_on_line_boundaries(self):
        text = "\n".join(f"line {i} " + "x" * 90 for i in range(60))
        parts = split_message(text, max_len=1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("\n".join(parts), text)

    def test_code_fence_reopened(self):
        text = "```python\n" + "\n".join("code line " + "y" * 80 for i in range(30)) + "\n```"
        parts = split_message(text, max_len=800)
        self.assertGreater(len(parts), 1)
        for p in parts[1:]:
            self.assertTrue(p.startswith("```"), f"continuation lost fence: {p[:20]!r}")
        for p in parts:
            self.assertEqual(p.count("```") % 2, 0, "unbalanced fence in a part")

    def test_monster_single_line(self):
        text = "z" * 9000
        parts = split_message(text, max_len=4096)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertEqual("".join(parts), text)

    def test_fenced_monster_line_stays_within_limit(self):
        text = "```python\n" + "z" * 9000 + "\n```"
        parts = split_message(text, max_len=4096)
        self.assertTrue(all(len(p) <= 4096 for p in parts), [len(p) for p in parts])
        for p in parts:
            self.assertEqual(p.count("```") % 2, 0, "unbalanced fence in a part")
        for p in parts[1:]:
            self.assertTrue(p.startswith("```"), f"continuation lost fence: {p[:20]!r}")
        self.assertEqual(sum(p.count("z") for p in parts), 9000)

    def test_fenced_near_budget_lines_stay_within_limit(self):
        text = "```python\n" + "a" * 96 + "\n" + "b" * 96 + "\n```"
        parts = split_message(text, max_len=100)
        self.assertTrue(all(len(p) <= 100 for p in parts), [len(p) for p in parts])
        for p in parts:
            self.assertEqual(p.count("```") % 2, 0)
        self.assertEqual(sum(p.count("a") for p in parts), 96)
        self.assertEqual(sum(p.count("b") for p in parts), 96)

    def test_tiny_max_len_rejected(self):
        with self.assertRaises(ValueError):
            split_message("anything", max_len=4)


if __name__ == "__main__":
    unittest.main()
