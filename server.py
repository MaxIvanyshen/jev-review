#!/usr/bin/env python3
"""HTTP wrapper around jev_review.py so coding agents on the LAN/tailnet can
POST a diff and get back the triage report as JSON, instead of shelling out
to the CLI over SSH.

Endpoints:
    GET  /health         -> 200 "ok", no auth (used by Docker healthcheck)
    POST /review         -> body is a raw diff, response is the JSON report
    POST /ask            -> body is {"state"|"items", "questions"}: ask Jev
                            typed questions (noul/choice/score) about one
                            state or a batch of states, answers keyed by id

If JEV_ACCESS_KEY (or legacy JEV_REVIEW_ACCESS_KEY) is set, POST endpoints
require `Authorization: Bearer <key>`.
"""
import json
import os
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jev_review import ask_jev, build_report, review_file, split_diff

API_KEY = os.environ.get("TYPESAFE_API_KEY")
ACCESS_KEY = os.environ.get("JEV_ACCESS_KEY") or os.environ.get("JEV_REVIEW_ACCESS_KEY")
PORT = int(os.environ.get("PORT", "8787"))
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10MB, generous for a diff
MAX_ASK_BODY_BYTES = 32 * 1024 * 1024  # batch of states, larger than a diff
MAX_ASK_ITEMS = 100
MAX_ASK_STATE_BYTES = 200 * 1024  # per state, in UTF-8 bytes
UPSTREAM_WORKERS = 8  # shared across every request, so load stays bounded
UPSTREAM_TIMEOUT = 120  # per item, seconds

QUESTION_TYPES = {"noul", "choice", "score"}

# One pool for the whole process: a burst of /ask requests queues instead of
# multiplying concurrent upstream calls (each of which can retry on 429/529).
EXECUTOR = ThreadPoolExecutor(max_workers=UPSTREAM_WORKERS)


def prepare_ask(payload):
    """Validate an /ask body and return (states, questions).

    States is a list of {"id", "state", "truncated"} — the state clipped to
    MAX_ASK_STATE_BYTES UTF-8 bytes. Raises ValueError with a client-facing
    message on any invalid shape.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")

    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty object")
    for name, q in questions.items():
        if not isinstance(q, dict) or not isinstance(q.get("type"), str) or q["type"] not in QUESTION_TYPES:
            raise ValueError(f"question {name!r} must have type noul, choice, or score")

    items = payload.get("items")
    if items is None:
        state = payload.get("state")
        if not isinstance(state, str) or not state.strip():
            raise ValueError("provide a state string or an items array")
        items = [{"id": "state", "state": state}]
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty array")
    if len(items) > MAX_ASK_ITEMS:
        raise ValueError(f"too many items (max {MAX_ASK_ITEMS})")

    states = []
    seen_ids = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("state"), str):
            raise ValueError(f"items[{i}] must have a state string")
        sid = str(item.get("id", i))
        if sid in seen_ids:
            raise ValueError(f"duplicate item id {sid!r}")
        seen_ids.add(sid)
        raw = item["state"]
        encoded = raw.encode("utf-8")
        if len(encoded) > MAX_ASK_STATE_BYTES:
            state = encoded[:MAX_ASK_STATE_BYTES].decode("utf-8", errors="ignore")
            states.append({"id": sid, "state": state, "truncated": True})
        else:
            states.append({"id": sid, "state": raw, "truncated": False})
    return states, questions


class Handler(BaseHTTPRequestHandler):
    server_version = "jev-review/1.2"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not ACCESS_KEY:
            return True
        return self.headers.get("Authorization") == f"Bearer {ACCESS_KEY}"

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not found"})

    def _read_body(self, limit):
        """Read and return the request body, or None after sending an error."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return None
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return None
        if length > limit:
            self._send_json(413, {"error": "body too large"})
            return None
        return self.rfile.read(length)

    def do_POST(self):
        if self.path == "/review":
            self._handle_review()
            return
        if self.path == "/ask":
            self._handle_ask()
            return
        self._send_json(404, {"error": "not found"})

    def _handle_review(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        body = self._read_body(MAX_BODY_BYTES)
        if body is None:
            return

        diff_text = body.decode(errors="replace")
        if not diff_text.strip():
            self._send_json(400, {"error": "empty diff"})
            return

        try:
            chunks = split_diff(diff_text)
            diffs_by_path = dict(chunks)
            results = [review_file(p, d, API_KEY) for p, d in chunks]
            report = build_report(results, diffs_by_path)
        except urllib.error.URLError as e:
            self._send_json(502, {"error": f"jev gateway error: {e}"})
            return
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return

        self._send_json(200, report)

    def _handle_ask(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        body = self._read_body(MAX_ASK_BODY_BYTES)
        if body is None:
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"invalid JSON: {e}"})
            return

        try:
            states, questions = prepare_ask(payload)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        def evaluate(s):
            try:
                resp = ask_jev(s["state"], questions, API_KEY)
                answers = resp.get("answers") if isinstance(resp, dict) else None
                if not isinstance(answers, dict):
                    raise ValueError("jev returned no answers")
                usage = resp.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
                return {"answers": answers, "usage": usage}
            except Exception as e:
                return {"error": str(e)}

        futures = [(s, EXECUTOR.submit(evaluate, s)) for s in states]
        results = {}
        for s, fut in futures:
            try:
                results[s["id"]] = fut.result(timeout=UPSTREAM_TIMEOUT)
            except FutureTimeoutError:
                fut.cancel()
                results[s["id"]] = {"error": f"jev upstream timeout ({UPSTREAM_TIMEOUT}s)"}
            except Exception as e:
                results[s["id"]] = {"error": str(e)}

        if all("error" in r for r in results.values()):
            first = next(iter(results.values()))["error"]
            self._send_json(502, {"error": f"jev gateway error: {first}"})
            return

        answers = {}
        usage = {"input_tokens": 0, "output_tokens": 0}
        truncated_ids = {s["id"] for s in states if s["truncated"]}
        for sid, r in results.items():
            if "error" in r:
                answers[sid] = {"error": r["error"]}
                continue
            answers[sid] = r["answers"]
            for k in usage:
                try:
                    usage[k] += int(r["usage"].get(k, 0))
                except (TypeError, ValueError):
                    pass
            if sid in truncated_ids:
                answers[sid]["_state_truncated"] = True

        self._send_json(200, {"answers": answers, "usage": usage})


def main():
    if not API_KEY:
        sys.exit("TYPESAFE_API_KEY not set")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
