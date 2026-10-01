import unittest

from jev_review import build_report, is_flagged, parse_stat, split_diff
from server import MAX_ASK_ITEMS, MAX_ASK_STATE_BYTES, prepare_ask

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
            "needs_review": {"type": "noul", "noul": needs_review},
            "security_concern": {"type": "noul", "noul": security},
            "risk": {"type": "score", "score": risk},
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


QUESTIONS = {
    "has_todo": {"type": "noul", "instructions": "TODO or FIXME?"},
}


class TestPrepareAsk(unittest.TestCase):
    def _prepare(self, payload):
        return prepare_ask(payload)

    def test_single_state_wrapped_as_one_item(self):
        states, questions = self._prepare({"state": "hello", "questions": QUESTIONS})
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["id"], "state")
        self.assertEqual(states[0]["state"], "hello")
        self.assertFalse(states[0]["truncated"])
        self.assertIs(questions, QUESTIONS)

    def test_items_kept_with_string_ids(self):
        payload = {
            "items": [{"id": 7, "state": "a"}, {"state": "b"}],
            "questions": QUESTIONS,
        }
        states, _ = self._prepare(payload)
        self.assertEqual([s["id"] for s in states], ["7", "1"])

    def test_non_object_body_rejected(self):
        for bad in ([], "x", 3, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._prepare(bad)

    def test_missing_questions_rejected(self):
        with self.assertRaises(ValueError):
            self._prepare({"state": "hello"})

    def test_bad_question_type_rejected(self):
        payload = {
            "state": "hello",
            "questions": {"q": {"type": "boolean", "instructions": "?"}},
        }
        with self.assertRaises(ValueError):
            self._prepare(payload)

    def test_missing_state_and_items_rejected(self):
        with self.assertRaises(ValueError):
            self._prepare({"questions": QUESTIONS})

    def test_duplicate_ids_rejected(self):
        payload = {
            "items": [{"id": "a", "state": "x"}, {"id": "a", "state": "y"}],
            "questions": QUESTIONS,
        }
        with self.assertRaises(ValueError):
            self._prepare(payload)

    def test_too_many_items_rejected(self):
        payload = {
            "items": [{"id": i, "state": "x"} for i in range(MAX_ASK_ITEMS + 1)],
            "questions": QUESTIONS,
        }
        with self.assertRaises(ValueError):
            self._prepare(payload)

    def test_oversize_state_truncated_in_bytes(self):
        # multibyte text: 150_000 chars of 2-byte cyrillic = 300_000 UTF-8 bytes
        big = "\u0444" * 150_000
        states, _ = self._prepare({"state": big, "questions": QUESTIONS})
        self.assertTrue(states[0]["truncated"])
        self.assertLessEqual(len(states[0]["state"].encode("utf-8")), MAX_ASK_STATE_BYTES)

    def test_small_state_not_truncated(self):
        states, _ = self._prepare({"state": "tiny", "questions": QUESTIONS})
        self.assertFalse(states[0]["truncated"])


if __name__ == "__main__":
    unittest.main()
