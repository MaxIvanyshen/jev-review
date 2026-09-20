#!/usr/bin/env python3
"""Given a question and grep-style candidate excerpts, ask Jev which ones
are actually relevant, and return only those. Same triage pattern as
jev_review.py (per-chunk boolean through Jev) applied to search results
instead of diff hunks — the useful intervention point is before a large
grep/log dump enters an agent's context.

Usage:
    grep -n -C2 -r "pattern" . | python3 jev_evidence.py "question text"
"""
import json
import os
import sys
import urllib.request

JEV_URL = "https://ai-gateway.vercel.sh/v1/evaluate"
MAX_CANDIDATES = 20
MAX_CANDIDATE_CHARS = 2000

QUESTION_TEMPLATE = {
    "relevant": {
        "type": "boolean",
        "instructions": "Given the question: {question!r}, does this excerpt contain evidence that helps answer it?",
    },
}


def ask_jev(state, questions, api_key):
    req = urllib.request.Request(
        JEV_URL,
        data=json.dumps({"model": "typesafe-ai/jev", "state": state, "questions": questions}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def split_candidates(text):
    """`grep -C` separates match groups with a `--` line; fall back to the
    whole text as one candidate if there's no such separator."""
    blocks = [b.strip("\n") for b in text.split("\n--\n") if b.strip()]
    return blocks[:MAX_CANDIDATES] if blocks else []


def select_relevant(question, candidates, api_key):
    questions = {
        "relevant": {
            "type": "boolean",
            "instructions": QUESTION_TEMPLATE["relevant"]["instructions"].format(question=question),
        }
    }
    results = []
    for i, excerpt in enumerate(candidates):
        answer = ask_jev(excerpt[:MAX_CANDIDATE_CHARS], questions, api_key)["answers"]["relevant"]
        results.append(
            {
                "index": i,
                "excerpt": excerpt,
                "probability": answer["probability"],
                "relevant": answer["probability"] >= 0.5,
            }
        )
    return results


def build_report(question, results):
    selected = sorted(
        (r for r in results if r["relevant"]), key=lambda r: r["probability"], reverse=True
    )
    return {
        "question": question,
        "total_candidates": len(results),
        "selected": len(selected),
        "evidence": [r["excerpt"] for r in selected],
    }


def main():
    api_key = os.environ.get("AI_GATEWAY_API_KEY")
    if not api_key:
        sys.exit("AI_GATEWAY_API_KEY not set")
    if len(sys.argv) < 2:
        sys.exit("usage: jev_evidence.py QUESTION < candidates")

    question = sys.argv[1]
    candidates = split_candidates(sys.stdin.read())
    if not candidates:
        sys.exit("no candidates on stdin")

    results = select_relevant(question, candidates, api_key)
    print(json.dumps(build_report(question, results), indent=2))


if __name__ == "__main__":
    main()
