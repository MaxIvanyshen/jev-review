import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from cli import ask_jev as cli


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def echo_handler(req):
    """Default fake server: answers 'answer' noul=0.9 for every item, and
    reports usage proportional to item count."""
    payload = json.loads(req.data)
    items = payload["items"]
    questions = payload["questions"]
    answers = {}
    for it in items:
        answers[it["id"]] = {
            name: {"type": q["type"], "noul": 0.9} if q["type"] == "noul"
            else {"type": q["type"], "choice": "x", "confidence": 0.8, "probabilities": {}}
            if q["type"] == "choice"
            else {"type": q["type"], "score": 1.8, "confidence": 0.8,
                  "legend": {"0": "Low", "1": "Mid", "2": "High"}, "probabilities": {}}
            for name, q in questions.items()
        }
    body = {"answers": answers, "usage": {"input_tokens": 10 * len(items), "output_tokens": 5 * len(items)}}
    return json.dumps(body).encode()


def fake_urlopen(handler):
    calls = []

    def _urlopen(req, timeout=None):
        calls.append((req, timeout))
        result = handler(req)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    _urlopen.calls = calls
    return _urlopen


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class TestQuestionArgument(unittest.TestCase):
    def ask(self, argv):
        opener = fake_urlopen(echo_handler)
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            open("code.c", "w").write("int main(){}")
            with patch("urllib.request.urlopen", opener):
                code, out, err = run_main(argv)
        sent = [json.loads(req.data) for req, _ in opener.calls]
        return code, out, err, sent

    def test_positional_question_then_paths(self):
        code, out, err, sent = self.ask(["Is this good?", "./code.c", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sent[0]["questions"]["answer"]["instructions"], "Is this good?")
        self.assertEqual([f["path"] for f in json.loads(out)["files"]], ["code.c"])

    def test_short_q_flag(self):
        code, out, err, sent = self.ask(["-q", "Is this good?", "code.c"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sent[0]["questions"]["answer"]["instructions"], "Is this good?")

    def test_positional_question_with_text(self):
        code, out, err, sent = self.ask(["--text", "x = eval(y)", "Uses eval?"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sent[0]["questions"]["answer"]["instructions"], "Uses eval?")

    def test_missing_question_is_an_error(self):
        code, out, err, sent = self.ask([])
        self.assertNotEqual(code, 0)
        self.assertIn("a question is required", err)
        self.assertEqual(sent, [])

    def test_path_in_question_position_is_rejected(self):
        code, out, err, sent = self.ask(["code.c"])
        self.assertNotEqual(code, 0)
        self.assertIn("put the question first", err)
        self.assertEqual(sent, [])


class TestPrivateFlag(unittest.TestCase):
    def sent_private(self, argv, env=None):
        opener = fake_urlopen(echo_handler)
        with patch.dict(os.environ, env or {}, clear=False), patch("urllib.request.urlopen", opener):
            if not env:
                os.environ.pop("JEV_ASK_PRIVATE", None)
            code, out, err = run_main(argv)
        self.assertEqual(code, 0, err)
        return json.loads(opener.calls[0][0].data)["private"], out

    def test_default_is_not_private(self):
        private, out = self.sent_private(["q?", "--text", "x"])
        self.assertIs(private, False)
        self.assertIn("(jev)", out)

    def test_private_flag(self):
        private, out = self.sent_private(["q?", "--text", "x", "--private", "--private-backend", "homelab"])
        self.assertIs(private, True)
        self.assertIn("(private: homelab Kev)", out)

    def test_env_makes_private_the_default(self):
        private, _ = self.sent_private(["q?", "--text", "x"], {"JEV_ASK_PRIVATE": "1", "JEV_ASK_PRIVATE_BACKEND": "homelab"})
        self.assertIs(private, True)


class TestLocalKev(unittest.TestCase):
    def handler(self, req):
        if req.full_url.startswith("http://127.0.0.1:8009"):
            body = json.loads(req.data)
            self.local_states.append(body["state"])
            return json.dumps({"answers": {"answer": {"type": "noul", "noul": 0.8}}, "usage": {"input_tokens": 3, "output_tokens": 1}}).encode()
        return echo_handler(req)

    def run_private(self, local_up):
        self.local_states = []
        def handler(req):
            if not local_up and req.full_url.startswith("http://127.0.0.1"):
                return urllib.error.URLError(ConnectionRefusedError(61, "refused"))
            return self.handler(req)
        opener = fake_urlopen(handler)
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp), patch("urllib.request.urlopen", opener), \
                patch.dict(os.environ, {}, clear=False):
            for k in ("JEV_ASK_PRIVATE", "JEV_ASK_PRIVATE_BACKEND", "JEV_ASK_KEV_URL"):
                os.environ.pop(k, None)
            open("a.py", "w").write("x" * (cli.KEV_MAX_STATE_CHARS + 50))
            open("b.py", "w").write("y")
            code, out, err = run_main(["q?", "a.py", "b.py", "--private", "--json"])
        self.assertEqual(code, 0, err)
        self.calls = [(req.full_url, req.data) for req, _ in opener.calls]
        return json.loads(out), [u for u, _ in self.calls]

    def test_local_kev_answers_without_touching_homelab(self):
        report, urls = self.run_private(local_up=True)
        self.assertEqual(report["backend"], "private: local Kev")
        self.assertTrue(all(u.startswith("http://127.0.0.1:8009") for u in urls), urls)
        self.assertEqual(len(self.local_states), 2)
        self.assertLessEqual(max(map(len, self.local_states)), cli.KEV_MAX_STATE_CHARS)
        by_path = {f["path"]: f for f in report["files"]}
        self.assertTrue(by_path["a.py"]["truncated"])
        self.assertFalse(by_path["b.py"]["truncated"])
        self.assertEqual(report["usage"], {"input_tokens": 6, "output_tokens": 2})

    def test_falls_back_to_homelab_when_no_local_kev(self):
        report, urls = self.run_private(local_up=False)
        self.assertEqual(report["backend"], "private: homelab Kev (no local Kev running)")
        homelab = [json.loads(d) for u, d in self.calls if not u.startswith("http://127.0.0.1")]
        self.assertEqual(len(homelab), 1)
        self.assertIs(homelab[0]["private"], True)


class TestBasicFlow(unittest.TestCase):
    def test_single_text_question_json_report(self):
        with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
            code, out, err = run_main(["--text", "hello world", "--question", "Is this risky?", "--json"])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(len(report["files"]), 1)
        self.assertEqual(report["files"][0]["path"], "text")
        self.assertIsNone(report["files"][0]["error"])
        self.assertEqual(report["usage"], {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(report["skipped"], [])

    def test_stdin_input(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)), patch(
                "sys.stdin", io.StringIO("some piped text")
            ), patch("sys.stdin.isatty", return_value=False, create=True):
                code, out, err = run_main(["--question", "q?", "--json"])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["files"][0]["path"], "stdin")

    def test_human_output_format(self):
        with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
            code, out, err = run_main(["--text", "hi", "--question", "q?"])
        self.assertEqual(code, 0, err)
        self.assertIn("text", out)
        self.assertIn("answer: True (p=0.90)", out)
        self.assertIn("usage: input_tokens=10 output_tokens=5", out)


class TestRegressions(unittest.TestCase):
    def test_human_score_shows_legend_label(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            json.dump({"s": {"type": "score", "instructions": "rate", "criteria": ["Low", "Mid", "High"]}},
                      open("q.json", "w"))
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--text", "hi", "--questions", "q.json"])
        self.assertEqual(code, 0, err)
        self.assertIn("s: High (score=1.80", out)

    def test_human_skipped_shows_path_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            open("a.py", "w").write("a")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "a.py", "missing.py"])
        self.assertEqual(code, 1)
        self.assertIn("skipped: missing.py (path not found)", out)

    def test_absolute_and_dot_slash_globs_match(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            os.makedirs("src/sub")
            open("src/sub/a.py", "w").write("a")
            for pattern in (os.path.join(os.getcwd(), "src/**/*.py"), "./src/**/*.py"):
                with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                    code, out, err = run_main(["--question", "q?", "--json", pattern])
                self.assertEqual(code, 0, f"{pattern}: {err}")
                self.assertEqual([f["path"] for f in json.loads(out)["files"]], ["src/sub/a.py"])

    def test_double_star_requires_separator(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            os.makedirs("src/x")
            open("src/b.go", "w").write("a")
            open("src/x/b.go", "w").write("a")
            open("src/xb.go", "w").write("a")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "--json", "src/**/b.go"])
        self.assertEqual(code, 0, err)
        paths = sorted(f["path"] for f in json.loads(out)["files"])
        self.assertEqual(paths, ["src/b.go", "src/x/b.go"])

    def test_unreadable_directory_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            os.makedirs("src/locked")
            open("src/a.py", "w").write("a")
            os.chmod("src/locked", 0)
            try:
                with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                    code, out, err = run_main(["--question", "q?", "--json", "src/**/*.py"])
            finally:
                os.chmod("src/locked", 0o755)
        self.assertEqual(code, 0, err)
        self.assertEqual([f["path"] for f in json.loads(out)["files"]], ["src/a.py"])


class TestFileDiscovery(unittest.TestCase):
    def test_relative_recursive_glob_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            os.makedirs("src/pkg/sub")
            open("src/a.py", "w").write("a")
            open("src/pkg/b.py", "w").write("b")
            open("src/pkg/sub/c.py", "w").write("c")
            open("src/skip.txt", "w").write("x")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "--json", "src/**/*.py", "src/a.py"])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        paths = sorted(f["path"] for f in report["files"])
        self.assertEqual(paths, ["src/a.py", "src/pkg/b.py", "src/pkg/sub/c.py"])

    def test_bare_directory_argument(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            os.makedirs("proj/node_modules")
            open("proj/keep.py", "w").write("a")
            open("proj/node_modules/ignored.py", "w").write("b")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "--json", "proj"])
        report = json.loads(out)
        paths = sorted(f["path"] for f in report["files"])
        self.assertEqual(paths, ["proj/keep.py"])

    def test_symlinked_directory_not_traversed(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            os.makedirs("real_outside")
            open("real_outside/secret.py", "w").write("x")
            os.makedirs("proj")
            open("proj/visible.py", "w").write("y")
            os.symlink(os.path.abspath("real_outside"), "proj/linked")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "--json", "proj/**/*.py"])
        report = json.loads(out)
        paths = sorted(f["path"] for f in report["files"])
        self.assertEqual(paths, ["proj/visible.py"])

    def test_brace_pattern_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            open("a.py", "w").write("x")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "--json", "{a,b}.py"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["skipped"][0]["reason"], "brace expansion is not supported")

    def test_unmatched_pattern_disclosed(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            os.makedirs("src")
            open("src/a.py", "w").write("x")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "--json", "src/*.nomatch"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["skipped"][0]["reason"], "no files matched")

    def test_binary_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            with open("bin.dat", "wb") as f:
                f.write(b"\x00\x01\x02binary")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "--json", "bin.dat"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["skipped"][0], {"path": "bin.dat", "reason": "binary file"})

    def test_too_many_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            os.makedirs("many")
            for i in range(cli.MAX_TOTAL_FILES + 1):
                open(f"many/f{i}.py", "w").write("x")
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "many"])
        self.assertEqual(code, 1)
        self.assertIn("too many files matched", err)

    def test_visited_entries_cap(self):
        old = cli.MAX_VISITED_ENTRIES
        cli.MAX_VISITED_ENTRIES = 3
        try:
            with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
                os.makedirs("many")
                for i in range(10):
                    open(f"many/f{i}.py", "w").write("x")
                with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                    code, out, err = run_main(["--question", "q?", "many"])
            self.assertEqual(code, 1)
            self.assertIn("too many filesystem entries", err)
        finally:
            cli.MAX_VISITED_ENTRIES = old


class TestTruncation(unittest.TestCase):
    def test_build_state_truncates_multibyte_safely(self):
        big = "\u0444" * 150_000  # 2 bytes each in utf-8 = 300_000 bytes
        state, truncated = cli.build_state(None, "big", big)
        self.assertTrue(truncated)
        self.assertLessEqual(len(state.encode("utf-8")), cli.MAX_STATE_BYTES)

    def test_small_state_not_truncated(self):
        state, truncated = cli.build_state(None, "small", "hi")
        self.assertFalse(truncated)

    def test_truncated_file_reported_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            with open("big.py", "w", encoding="utf-8") as f:
                f.write("\u0444" * 150_000)
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--question", "q?", "--json", "big.py"])
        report = json.loads(out)
        self.assertTrue(report["files"][0]["truncated"])


class TestServerFailures(unittest.TestCase):
    def test_partial_batch_failure(self):
        old_batch = cli.MAX_ITEMS_PER_BATCH
        cli.MAX_ITEMS_PER_BATCH = 1

        def flaky(req):
            payload = json.loads(req.data)
            if payload["items"][0]["id"] == "b.py":
                return urllib.error.URLError("boom")
            return echo_handler(req)

        try:
            with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
                open("a.py", "w").write("x")
                open("b.py", "w").write("y")
                with patch("urllib.request.urlopen", fake_urlopen(flaky)):
                    code, out, err = run_main(["--question", "q?", "--json", "a.py", "b.py"])
            self.assertEqual(code, 1)
            report = json.loads(out)
            by_path = {f["path"]: f for f in report["files"]}
            self.assertIsNone(by_path["a.py"]["error"])
            self.assertIn("connection error", by_path["b.py"]["error"])
            self.assertEqual(report["usage"], {"input_tokens": 10, "output_tokens": 5})
        finally:
            cli.MAX_ITEMS_PER_BATCH = old_batch

    def test_total_failure_all_items_errored(self):
        def always_fail(req):
            return urllib.error.URLError("down")

        with patch("urllib.request.urlopen", fake_urlopen(always_fail)):
            code, out, err = run_main(["--text", "hi", "--question", "q?", "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertIn("connection error", report["files"][0]["error"])

    def test_invalid_json_response(self):
        def bad_json(req):
            return b"not json"

        with patch("urllib.request.urlopen", fake_urlopen(bad_json)):
            code, out, err = run_main(["--text", "hi", "--question", "q?", "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertIn("invalid JSON response", report["files"][0]["error"])

    def test_invalid_response_shape(self):
        def missing_answers(req):
            return json.dumps({"nope": True}).encode()

        with patch("urllib.request.urlopen", fake_urlopen(missing_answers)):
            code, out, err = run_main(["--text", "hi", "--question", "q?", "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertIn("invalid response shape", report["files"][0]["error"])

    def test_missing_answer_for_id(self):
        def empty_answers(req):
            return json.dumps({"answers": {}, "usage": {}}).encode()

        with patch("urllib.request.urlopen", fake_urlopen(empty_answers)):
            code, out, err = run_main(["--text", "hi", "--question", "q?", "--json"])
        report = json.loads(out)
        self.assertEqual(report["files"][0]["error"], "no answer returned for this id")

    def test_incomplete_answer_missing_question_key(self):
        def incomplete(req):
            payload = json.loads(req.data)
            return json.dumps({"answers": {it["id"]: {} for it in payload["items"]}, "usage": {}}).encode()

        with patch("urllib.request.urlopen", fake_urlopen(incomplete)):
            code, out, err = run_main(["--text", "hi", "--question", "q?", "--json"])
        report = json.loads(out)
        self.assertIn("incomplete response", report["files"][0]["error"])

    def test_server_side_truncation_flag_propagated(self):
        def truncated_answer(req):
            payload = json.loads(req.data)
            answers = {it["id"]: {"answer": {"type": "noul", "noul": 0.1}, "_state_truncated": True} for it in payload["items"]}
            return json.dumps({"answers": answers, "usage": {}}).encode()

        with patch("urllib.request.urlopen", fake_urlopen(truncated_answer)):
            code, out, err = run_main(["--text", "hi", "--question", "q?", "--json"])
        report = json.loads(out)
        self.assertTrue(report["files"][0]["truncated"])

    def test_timeout_reported_as_error_not_crash(self):
        def timeout_err(req):
            return urllib.error.URLError(TimeoutError("timed out"))

        with patch("urllib.request.urlopen", fake_urlopen(timeout_err)) as _:
            with patch("urllib.request.urlopen", fake_urlopen(timeout_err)):
                code, out, err = run_main(["--text", "hi", "--question", "q?", "--json", "--timeout", "1"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertIn("connection error", report["files"][0]["error"])


class TestQuestionsFile(unittest.TestCase):
    def test_questions_file_loaded_and_sent(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            with open("qs.json", "w") as f:
                json.dump(
                    {
                        "risk": {"type": "score", "instructions": "rate it", "criteria": ["low", "high"]},
                        "category": {"type": "choice", "instructions": "pick", "criteria": {"a": "a", "b": "b"}},
                    },
                    f,
                )
            with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
                code, out, err = run_main(["--text", "hi", "--questions", "qs.json", "--json"])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        answers = report["files"][0]["answers"]
        self.assertIn("risk", answers)
        self.assertIn("category", answers)

    def test_questions_file_bad_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            with open("qs.json", "w") as f:
                json.dump({"q": {"type": "boolean", "instructions": "x"}}, f)
            code, out, err = run_main(["--text", "hi", "--questions", "qs.json"])
        self.assertEqual(code, 1)
        self.assertIn("must have type noul, choice, or score", err)

    def test_question_and_questions_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            cli.parse_args(["--question", "q?", "--questions", "qs.json", "--text", "hi"])


class TestAuthSafety(unittest.TestCase):
    def test_refuses_key_over_plain_http_non_loopback(self):
        code, out, err = run_main(
            ["--text", "hi", "--question", "q?", "--url", "http://example.com/ask", "--key", "SECRET"]
        )
        self.assertEqual(code, 1)
        self.assertIn("refusing to send --key over plain HTTP", err)
        self.assertNotIn("SECRET", err)

    def test_allows_key_over_http_loopback(self):
        with patch("urllib.request.urlopen", fake_urlopen(echo_handler)):
            code, out, err = run_main(
                ["--text", "hi", "--question", "q?", "--url", "http://127.0.0.1:8787/ask", "--key", "SECRET", "--json"]
            )
        self.assertEqual(code, 0, err)

    def test_timeout_must_be_positive(self):
        with self.assertRaises(SystemExit):
            cli.parse_args(["--text", "hi", "--question", "q?", "--timeout", "0"])


if __name__ == "__main__":
    unittest.main()
