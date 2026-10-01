# jev-review

Splits a diff into per-file chunks, runs a fixed triage rubric through
[Jev](https://docs.typesafe.ai) per file, and prints a JSON report:
flagged files (with full diff) up front, clean files reduced to a
one-line verdict. Feed the report to a review agent instead of the raw
diff so it only spends tokens on what's actually risky.

## Setup

```bash
export TYPESAFE_API_KEY=your_key_here   # from console.typesafe.ai
```

## Run (CLI)

```bash
git diff | python3 jev_review.py > report.json
# or
python3 jev_review.py some.patch > report.json
```

Point your review agent at `report.json` instead of the raw diff.

## Run on your homelab

**Bare minimum** — just run the CLI over SSH:

```bash
git diff | ssh homelab 'TYPESAFE_API_KEY=your_key_here python3 /path/to/jev-review/jev_review.py' > report.json
```

**As a service**, if you want to call it over the network from wherever
your coding agent runs, wrap it in stdlib `http.server` — no framework
needed for one endpoint:

```python
# server.py
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from jev_review import split_diff, review_file, build_report

API_KEY = os.environ["TYPESAFE_API_KEY"]

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        diff_text = self.rfile.read(int(self.headers["Content-Length"])).decode()
        chunks = split_diff(diff_text)
        diffs_by_path = dict(chunks)
        results = [review_file(p, d, API_KEY) for p, d in chunks]
        body = json.dumps(build_report(results, diffs_by_path)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("0.0.0.0", 8787), Handler).serve_forever()
```

Run with `python3 server.py`, then call it from your agent:

```bash
curl -X POST --data-binary @diff.patch http://homelab:8787 > report.json
```

Keep it on your LAN only — the handler has no auth. If it needs to be
reachable from outside, put it behind whatever reverse proxy you run
(Caddy/Traefik) with a shared secret header.

**Survive reboots** with a systemd unit:

```ini
# /etc/systemd/system/jev-review.service
[Unit]
Description=jev-review triage server

[Service]
Environment=TYPESAFE_API_KEY=your_key_here
WorkingDirectory=/path/to/jev-review
ExecStart=/usr/bin/python3 server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now jev-review
```

## CLI client

`jev_client.py` talks to a running jev-review server (see above) — pipe it
a diff, or let it fall back to `git diff` in the current repo:

```bash
jev_client.py --staged        # review what's staged
gh pr diff 123 | jev_client.py # review an arbitrary diff
```

Points at `$JEV_REVIEW_URL` (default: this homelab's public endpoint) and
sends `$JEV_REVIEW_ACCESS_KEY` as a Bearer token if the server has auth
enabled. `jev_client.py --help` for all flags.

## Ask Jev (generic endpoint)

`POST /ask` with a state (or batch of states) and a map of typed
questions; the server fans a batch out to Jev in parallel and returns
answers keyed by id:

```bash
curl -X POST https://gerry.gobeep.xyz:8790/ask \
  -H 'Content-Type: application/json' \
  -d '{
    "state": "def f(a, b): return a + b  # TODO: overflow",
    "questions": {
      "has_todo":   {"type": "noul",   "instructions": "Does this code contain a TODO or FIXME?"},
      "language":   {"type": "choice", "instructions": "What language is this?",
                       "criteria": {"python": "python", "js": "javascript", "other": "neither"}},
      "complexity": {"type": "score",  "instructions": "Rate the complexity.",
                       "criteria": ["Trivial", "Simple", "Moderate", "Complex"]}
    }
  }'
```

Batch mode swaps `state` for `items` (same questions for every item, up
to 100, states truncated at 200KB):

```json
{
  "items": [{"id": "a.py", "state": "..."}, {"id": "b.py", "state": "..."}],
  "questions": {"...": "..."}
}
```

Response: `{"answers": {"<id>": {"has_todo": {"type": "noul", "noul": 1.0}, "...": "..."}},
"usage": {"input_tokens": 400, "output_tokens": 72}}`. An item that
fails at the gateway gets `{"error": "..."}` instead of answers; if
every item fails the whole request returns 502. See
[docs.typesafe.ai/api](https://docs.typesafe.ai/api) for the full question
and answer schemas (noul returns a 0-1 probability; choice returns the
picked option plus a full probability distribution; score returns a
probability-weighted value that can land between levels).

## Install (CLI)

```bash
./install.sh
```

Copies `jev_client.py` to `~/.local/bin/jev-review` (executable). Re-run
it after pulling changes — it's a plain copy, not a symlink.

## Agent skill

`skills/jev-review/SKILL.md` teaches a coding agent when and how to call
this (triage before a deep manual review, not a substitute for one) and
how to read the report. Every coding agent keeps skills/rules in a
different place, so there's no one path to copy it to — instead, paste
this prompt to whichever agent you're using and let it install itself:

> Clone github.com/MaxIvanyshen/jev-review (or use the local copy at
> `<path>`), run its `install.sh` to install the `jev-review` CLI, then
> read `skills/jev-review/SKILL.md` and install it as a skill/rule using
> your own convention (e.g. `~/.claude/skills/`, `~/.agents/skills/`,
> `.cursor/rules/`) so you can use it in future sessions.

## Report shape

```json
{
  "summary": { "total_files": 5, "flagged_files": 2 },
  "files": [
    { "path": "a.py", "additions": 3, "deletions": 1, "jev": { "...": "..." }, "flagged": true }
  ],
  "flagged_diffs": { "a.py": "diff --git a/a.py ..." }
}
```

## Tests

```bash
python3 -m unittest test_jev_review.py -v
```
