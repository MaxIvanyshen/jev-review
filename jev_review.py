#!/usr/bin/env python3
"""Split a diff into per-file chunks, ask Jev a fixed triage rubric per
file, and print a JSON report: flagged files (with full diff) up front,
clean files reduced to a one-line verdict. Feed the report to a review
agent instead of the raw diff.

Usage:
    git diff | AI_GATEWAY_API_KEY=... python3 jev_review.py
    python3 jev_review.py some.patch
"""
import json
import os
import re
import sys
import urllib.request

JEV_URL = "https://ai-gateway.vercel.sh/v1/evaluate"

QUESTIONS = {
    "needs_review": {
        "type": "boolean",
        "instructions": (
            "Does this diff contain changes that need careful review "
            "(logic changes, security-sensitive code, non-trivial refactors)? "
            "Answer false only for trivial/mechanical changes (formatting, "
            "comments, renames, version bumps)."
        ),
    },
    "risk": {
        "type": "score",
        "instructions": "Rate the risk of this diff introducing a bug or regression.",
        "criteria": [
            "Trivial - no logic change",
            "Low - isolated, simple change",
            "Moderate - touches shared logic",
            "High - complex or security-sensitive",
            "Critical - core/auth/payment/data-integrity path",
        ],
    },
    "category": {
        "type": "choice",
        "instructions": "Pick the category that best describes this diff.",
        "criteria": {
            "bugfix": "fixes a defect",
            "feature": "adds new functionality",
            "refactor": "restructures without behavior change",
            "config": "config, build, or dependency changes",
            "test": "test-only changes",
            "docs": "documentation only",
            "other": "none of the above",
        },
    },
    "security_concern": {
        "type": "boolean",
        "instructions": (
            "Does this diff touch authentication, authorization, secrets, "
            "input validation, or otherwise have a plausible security implication?"
        ),
    },
    "missing_tests": {
        "type": "boolean",
        "instructions": (
            "Does this diff change logic/behavior without any corresponding "
            "test changes in the same diff?"
        ),
    },
}

FILE_HEADER_RE = re.compile(r"^diff --git a/.* b/(.*)$", re.MULTILINE)


def ask_jev(state, questions, api_key):
    req = urllib.request.Request(
        JEV_URL,
        data=json.dumps({"model": "typesafe-ai/jev", "state": state, "questions": questions}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def split_diff(text):
    """Return [(path, diff_text)]. Falls back to a single whole-diff chunk
    if there are no `diff --git` headers (e.g. plain `diff -u` output)."""
    matches = list(FILE_HEADER_RE.finditer(text))
    if not matches:
        return [("diff", text)]
    chunks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunks.append((m.group(1), text[start:end]))
    return chunks


def parse_stat(diff_text):
    additions = sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))
    deletions = sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---"))
    return additions, deletions


def is_flagged(answers):
    return (
        answers["needs_review"]["probability"] >= 0.5
        or answers["security_concern"]["probability"] >= 0.5
        or answers["risk"]["score"] >= 2
    )


def review_file(path, diff_text, api_key):
    answers = ask_jev(diff_text, QUESTIONS, api_key)["answers"]
    additions, deletions = parse_stat(diff_text)
    return {
        "path": path,
        "additions": additions,
        "deletions": deletions,
        "jev": answers,
        "flagged": is_flagged(answers),
    }


def build_report(results, diffs_by_path):
    flagged = sorted(
        (r for r in results if r["flagged"]),
        key=lambda r: r["jev"]["risk"]["score"],
        reverse=True,
    )
    return {
        "summary": {"total_files": len(results), "flagged_files": len(flagged)},
        "files": results,
        "flagged_diffs": {r["path"]: diffs_by_path[r["path"]] for r in flagged},
    }


def main():
    api_key = os.environ.get("AI_GATEWAY_API_KEY")
    if not api_key:
        sys.exit("AI_GATEWAY_API_KEY not set")

    text = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    if not text.strip():
        sys.exit("empty diff")

    chunks = split_diff(text)
    diffs_by_path = dict(chunks)
    results = [review_file(path, diff_text, api_key) for path, diff_text in chunks]
    print(json.dumps(build_report(results, diffs_by_path), indent=2))


if __name__ == "__main__":
    main()
