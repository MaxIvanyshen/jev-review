import unittest

from jev_evidence import build_report, split_candidates


class TestSplitCandidates(unittest.TestCase):
    def test_splits_on_grep_context_separator(self):
        text = "a.py:1:foo\na.py:2:bar\n--\nb.py:5:baz\n"
        self.assertEqual(split_candidates(text), ["a.py:1:foo\na.py:2:bar", "b.py:5:baz"])

    def test_no_separator_is_single_candidate(self):
        self.assertEqual(split_candidates("a.py:1:foo\n"), ["a.py:1:foo"])

    def test_blank_input_is_no_candidates(self):
        self.assertEqual(split_candidates("\n\n"), [])


class TestBuildReport(unittest.TestCase):
    def test_selects_only_relevant_sorted_by_probability(self):
        results = [
            {"index": 0, "excerpt": "low", "probability": 0.6, "relevant": True},
            {"index": 1, "excerpt": "no", "probability": 0.2, "relevant": False},
            {"index": 2, "excerpt": "high", "probability": 0.9, "relevant": True},
        ]
        report = build_report("q", results)
        self.assertEqual(report["total_candidates"], 3)
        self.assertEqual(report["selected"], 2)
        self.assertEqual(report["evidence"], ["high", "low"])


if __name__ == "__main__":
    unittest.main()
