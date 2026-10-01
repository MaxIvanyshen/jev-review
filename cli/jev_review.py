#!/usr/bin/env python3
"""CLI client for a jev-review server: send a diff, print back the JSON
triage report (flagged files with full diff up front, clean files reduced
to a one-line verdict).

Usage:
    git diff | jev_client.py
    jev_client.py --staged
    jev_client.py some.patch
    jev_client.py --url https://host:port/review --key SECRET < diff.patch

Diff source, in order of preference: a FILE argument, --staged (`git diff
--staged`), piped stdin, or plain `git diff` in the current repo as a
fallback.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("JEV_REVIEW_URL", "https://gerry.gobeep.xyz:8790/review")


def run_git_diff(*extra_args):
    try:
        result = subprocess.run(
            ["git", "diff", *extra_args], capture_output=True, text=True
        )
    except FileNotFoundError:
        sys.exit("git not found on PATH")
    if result.returncode != 0:
        sys.exit(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def get_diff(args):
    if args.file:
        try:
            return open(args.file).read()
        except OSError as e:
            sys.exit(f"could not read {args.file}: {e}")
    if args.staged:
        return run_git_diff("--staged")
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data
    return run_git_diff()


def main():
    parser = argparse.ArgumentParser(
        description="Send a diff to a jev-review server and print the JSON triage report."
    )
    parser.add_argument(
        "file", nargs="?", help="read diff from this file instead of stdin/git diff"
    )
    parser.add_argument(
        "--staged", action="store_true", help="use `git diff --staged` as the source"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"jev-review server URL (default: {DEFAULT_URL}, or $JEV_REVIEW_URL)",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("JEV_REVIEW_ACCESS_KEY"),
        help="bearer token, only needed if the server has auth enabled ($JEV_REVIEW_ACCESS_KEY)",
    )
    args = parser.parse_args()

    diff_text = get_diff(args)
    if not diff_text.strip():
        sys.exit("no diff to review (nothing staged/changed?)")

    headers = {"Content-Type": "text/plain"}
    if args.key:
        headers["Authorization"] = f"Bearer {args.key}"

    req = urllib.request.Request(
        args.url, data=diff_text.encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"jev-review server error {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"could not reach jev-review server at {args.url}: {e}")

    # Round-trip through json so a malformed response fails loudly here
    # rather than being handed downstream as if it were a valid report.
    print(json.dumps(json.loads(body), indent=2))


if __name__ == "__main__":
    main()
