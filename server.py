#!/usr/bin/env python3
"""HTTP wrapper around jev_review.py so coding agents on the LAN/tailnet can
POST a diff and get back the triage report as JSON, instead of shelling out
to the CLI over SSH.

Endpoints:
    GET  /health         -> 200 "ok", no auth (used by Docker healthcheck)
    POST /review         -> body is a raw diff, response is the JSON report

If JEV_REVIEW_ACCESS_KEY is set, POST /review requires
`Authorization: Bearer <key>`.
"""
import json
import os
import sys
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jev_review import build_report, review_file, split_diff

API_KEY = os.environ.get("AI_GATEWAY_API_KEY")
ACCESS_KEY = os.environ.get("JEV_REVIEW_ACCESS_KEY")
PORT = int(os.environ.get("PORT", "8787"))
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10MB, generous for a diff


class Handler(BaseHTTPRequestHandler):
    server_version = "jev-review/1.0"

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

    def do_POST(self):
        if self.path != "/review":
            self._send_json(404, {"error": "not found"})
            return

        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "diff too large"})
            return

        diff_text = self.rfile.read(length).decode(errors="replace")
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


def main():
    if not API_KEY:
        sys.exit("AI_GATEWAY_API_KEY not set")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
