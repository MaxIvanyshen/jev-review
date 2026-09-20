#!/usr/bin/env python3
"""CLI client for a jev-review server's /evidence endpoint: given a
question and grep-style candidate excerpts on stdin, print back only the
excerpts Jev flagged as relevant.

Usage:
    grep -n -C2 -r "pattern" . | jev_evidence_client.py "question text"
    jev_evidence_client.py "question" --url https://host:port/evidence
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get(
    "JEV_REVIEW_URL", "https://gerry.gobeep.xyz:8790/review"
).rsplit("/review", 1)[0] + "/evidence"


def main():
    parser = argparse.ArgumentParser(
        description="Send grep-style candidates to a jev-review server and print only relevant excerpts."
    )
    parser.add_argument("question", help="the question the excerpts should help answer")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"evidence endpoint (default: {DEFAULT_URL})")
    parser.add_argument(
        "--key", default=os.environ.get("JEV_REVIEW_ACCESS_KEY"), help="bearer token, if server auth is enabled"
    )
    args = parser.parse_args()

    text = sys.stdin.read()
    if not text.strip():
        sys.exit("no candidates on stdin")

    headers = {"Content-Type": "application/json"}
    if args.key:
        headers["Authorization"] = f"Bearer {args.key}"

    req = urllib.request.Request(
        args.url,
        data=json.dumps({"question": args.question, "text": text}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        # ponytail: /evidence calls Jev once per candidate sequentially,
        # so worst case (20 candidates) can take minutes; batch/parallel
        # calls if this needs to be fast.
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"jev-review server error {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"could not reach jev-review server at {args.url}: {e}")

    print(json.dumps(json.loads(body), indent=2))


if __name__ == "__main__":
    main()
