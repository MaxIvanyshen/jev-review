import unittest

from jev_review import build_report, is_flagged, parse_stat, split_diff

SAMPLE = """diff --git a/a.py b/a.py
index 111..222 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 def f():
-    return 1
+    return 2
+    # comment
diff --git a/b.py b/b.py
index 333..444 100644
--- a/b.py
+++ b/b.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
"""


class TestSplitDiff(unittest.TestCase):
    def test_splits_per_file(self):
        chunks = split_diff(SAMPLE)
        self.assertEqual([path for path, _ in chunks], ["a.py", "b.py"])
        self.assertIn("return 2", dict(chunks)["a.py"])
        self.assertIn("x = 2", dict(chunks)["b.py"])
        self.assertNotIn("x = 2", dict(chunks)["a.py"])

    def test_no_git_header_falls_back_to_single_chunk(self):
        chunks = split_diff("--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n")
        self.assertEqual([path for path, _ in chunks], ["diff"])


class TestParseStat(unittest.TestCase):
    def test_counts_additions_and_deletions(self):
        a_chunk = dict(split_diff(SAMPLE))["a.py"]
        self.assertEqual(parse_stat(a_chunk), (2, 1))


class TestFlagging(unittest.TestCase):
    def _answers(self, needs_review=0.0, security=0.0, risk=0.0):
        return {
            "needs_review": {"probability": needs_review},
            "security_concern": {"probability": security},
            "risk": {"score": risk},
        }

    def test_flags_on_needs_review(self):
        self.assertTrue(is_flagged(self._answers(needs_review=0.9)))

    def test_flags_on_security(self):
        self.assertTrue(is_flagged(self._answers(security=0.7)))

    def test_flags_on_risk_score(self):
        self.assertTrue(is_flagged(self._answers(risk=3)))

    def test_clean_change_not_flagged(self):
        self.assertFalse(is_flagged(self._answers()))


class TestBuildReport(unittest.TestCase):
    def test_flagged_diffs_only_include_flagged_files(self):
        results = [
            {"path": "a.py", "jev": {"risk": {"score": 3}}, "flagged": True},
            {"path": "b.py", "jev": {"risk": {"score": 0}}, "flagged": False},
        ]
        diffs_by_path = {"a.py": "diff a", "b.py": "diff b"}
        report = build_report(results, diffs_by_path)
        self.assertEqual(report["summary"], {"total_files": 2, "flagged_files": 1})
        self.assertEqual(report["flagged_diffs"], {"a.py": "diff a"})


if __name__ == "__main__":
    unittest.main()
