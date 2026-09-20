---
name: jev-review
description: Triage a diff before reviewing it by hand — splits it per file and screens each one (needs_review, risk, category, security_concern, missing_tests) through a self-hosted jev-review server, returning flagged files with full diff up front and clean files reduced to a one-line verdict. Use before doing a manual/deep review of a non-trivial diff (a PR, a batch of staged changes, the output of a multi-file edit), especially a large one, to spend attention only on what's actually risky.
---

# jev-review

A triage filter for diffs, not a replacement for review. It never approves
or rejects anything — it just tells you which files in a diff are worth
your full attention and which are safe to skim past (formatting, renames,
version bumps, mechanical changes).

## When to use this

Reach for it before spending real effort reviewing a diff that touches
more than a file or two — your own multi-file edit before summarizing it,
a PR someone else opened, or a batch of staged changes before commit.
Skip it for a one-file or clearly trivial diff; the round trip isn't worth
it.

## How to call it

The CLI client is `jev-review` (installed on PATH). It reads a diff from,
in order: a file argument, `--staged`, piped stdin, or a plain `git diff`
in the current repo as a fallback — so the common cases need no flags:

```bash
# review everything currently unstaged/staged-and-unstaged in this repo
jev-review

# review only what's staged (e.g. right before a commit)
jev-review --staged

# review a specific patch file
jev-review some.patch

# pipe an arbitrary diff in (e.g. a PR's diff from `gh pr diff`)
gh pr diff 123 | jev-review
```

It prints the JSON report to stdout and exits non-zero with a message on
stderr if the request fails (server unreachable, bad response, etc.) —
treat a non-zero exit as "couldn't triage," not "diff is clean."

By default it talks to this homelab's jev-review server
(`https://gerry.gobeep.xyz:8790/review`). Override with `--url` or
`$JEV_REVIEW_URL` if pointed at a different instance. Auth is off on that
server by default; if it's been switched on, pass `--key` or set
`$JEV_REVIEW_ACCESS_KEY`.

## Reading the report

```json
{
  "summary": { "total_files": 5, "flagged_files": 2 },
  "files": [
    { "path": "a.py", "additions": 3, "deletions": 1, "jev": { "...": "..." }, "flagged": true }
  ],
  "flagged_diffs": { "a.py": "diff --git a/a.py ..." }
}
```

- `files` has a one-line verdict (`jev.needs_review`, `jev.risk.score`
  0-4, `jev.category`, `jev.security_concern`, `jev.missing_tests`) for
  every file — flagged and clean alike. Use this to report the clean
  files without re-reading their diffs.
- `flagged_diffs` has the full diff, but only for files in `files` with
  `"flagged": true`. Spend your actual review effort here — read these
  diffs closely, in descending risk order (the report is already sorted
  that way).
- A file is flagged if `needs_review` or `security_concern` probability
  is >= 0.5, or `risk.score` >= 2. These are heuristic triage signals from
  a small classifier, not a verdict — a flagged file still needs your
  judgment, and an unflagged one is a "probably fine," not a guarantee.

## Source

`jev_client.py` and `jev_review.py` in this repo
(github.com/MaxIvanyshen/jev-review). `jev_review.py` is the triage logic
itself (per-file split, the fixed rubric sent to Jev, the flagging rule);
`server.py` is the HTTP wrapper this CLI talks to.
