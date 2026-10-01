#!/usr/bin/env python3
"""jevkit HTTP server: Jev tools for coding agents on the LAN/tailnet, so
the TypeSafe key lives here instead of on every client.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jevkit.ask import AllItemsFailed, prepare_ask, run_ask
from jevkit.review import build_report, review_file, split_diff

API_KEY = os.environ.get("TYPESAFE_API_KEY")
ACCESS_KEY = os.environ.get("JEV_ACCESS_KEY") or os.environ.get("JEV_REVIEW_ACCESS_KEY")
PORT = int(os.environ.get("PORT", "8787"))
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10MB, generous for a diff
MAX_ASK_BODY_BYTES = 32 * 1024 * 1024  # batch of states, larger than a diff


class Handler(BaseHTTPRequestHandler):
    server_version = "jevkit/1.0"

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
        routes = {"/review": self._handle_review, "/ask": self._handle_ask}
        handler = routes.get(self.path)
        if handler is None:
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        handler()

    def _handle_review(self):
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

        try:
            result = run_ask(states, questions, API_KEY)
        except AllItemsFailed as e:
            self._send_json(502, {"error": f"jev gateway error: {e}"})
            return

        self._send_json(200, result)


def main():
    if not API_KEY:
        sys.exit("TYPESAFE_API_KEY not set")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
