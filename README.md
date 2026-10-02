# jevkit

Self-hosted [Jev](https://docs.typesafe.ai) tools for coding agents (and
humans): a small HTTP server that holds the TypeSafe key, plus stdlib CLIs,
agent skills, and a Pi tool that call it. Agents ask Jev typed questions
about code instead of reading it into their own context.

```
jevkit/          server package (stdlib only)
  jev.py         TypeSafe client: ask_jev() + 429/529 retries
  ask.py         /ask: validation + batch fan-out
  review.py      /review: per-file diff triage rubric
  server.py      HTTP routing: /health, /ask, /review
cli/             standalone scripts, copied to ~/.local/bin by install.sh
  ask_jev.py     ask-jev
  jev_review.py  jev-review
integrations/
  pi/ask-jev.ts  Pi extension: native ask_jev tool
  skills/        agent skills: ask-jev, jev-review
tests/
```

The CLIs never import the `jevkit` package — they're installed as single
files, so each stays self-contained.

## Server

```bash
docker build -t jevkit . && docker run -e TYPESAFE_API_KEY=... -p 8787:8787 jevkit
# or without Docker
TYPESAFE_API_KEY=... python3 -m jevkit.server
```

`TYPESAFE_API_KEY` from console.typesafe.ai. Set `JEV_ACCESS_KEY` (legacy:
`JEV_REVIEW_ACCESS_KEY`) to require `Authorization: Bearer <key>` on POST
endpoints; unset means open to anyone who can reach it. `PORT` defaults to
8787. The homelab instance is at `https://gerry.gobeep.xyz:8790` behind
nginx TLS.

### POST /ask

Evaluate a state, or a batch of up to 100, against typed questions —
`noul` (yes/no probability), `choice` (option + distribution), `score`
(probability-weighted position on a rubric):

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

Batch mode swaps `state` for `items: [{"id": "a.py", "state": "..."}, ...]`.
`"private": true` answers with [Kev-0.8B](https://github.com/jaredpalmer/kev)
on the homelab instead of TypeSafe (same API, open weights): nothing leaves the
homelab, but it runs on CPU at ~2.3s per item and clips states to 32K chars
(`KEV_URL`, default `http://kev:8009/v1/systemone`).
States are clipped at 200KB, then halved until they fit Jev's 32k-token
limit; either way the item is flagged `_state_truncated`. Response:
`{"answers": {"<id>": {...}}, "usage": {"input_tokens": N, "output_tokens": N}}`.
A failed item gets `{"error": "..."}`; if every item fails, 502. Full schemas:
[docs.typesafe.ai/api](https://docs.typesafe.ai/api).

### POST /review

Body is a raw diff. Splits it per file, asks a fixed triage rubric
(needs_review, risk, category, security_concern, missing_tests), and
returns flagged files with full diff, clean files as one-line verdicts:

```json
{
  "summary": { "total_files": 5, "flagged_files": 2 },
  "files": [
    { "path": "a.py", "additions": 3, "deletions": 1, "jev": { "...": "..." }, "flagged": true }
  ],
  "flagged_diffs": { "a.py": "diff --git a/a.py ..." }
}
```

Flagged = needs_review or security_concern ≥ 0.5, or risk score ≥ 2. Also
runnable locally without the server:
`git diff | TYPESAFE_API_KEY=... python3 -m jevkit.review`.

## Clients

```bash
./install.sh
```

Installs `ask-jev` and `jev-review` to `~/.local/bin`, both skills into
`~/.claude/skills` and `~/.agents/skills` (whichever exist), and the Pi
extension into `~/.pi/agent/extensions`. Re-run after pulling. No client-side
TypeSafe key needed.

### ask-jev

```bash
# Question first, then files/globs (quote globs so the CLI expands them).
ask-jev 'Is this good?' ./src/code.c
ask-jev 'Does this file contain TODO or FIXME comments?' 'src/**/*.py'

# Inline text or stdin.
ask-jev 'Does this contain a hardcoded credential?' --text 'password = "demo"'
printf '%s\n' 'TODO: write tests' | ask-jev 'Does this contain a TODO?'

# Typed question map (choice/score), JSON for agents.
ask-jev --questions questions.json --json 'src/**/*.go'
```

The question can also be passed as `-q/--question`. `questions.json` is a
question map like the `/ask` example above (all positionals are then paths).
`--private` (or `JEV_ASK_PRIVATE=1` as the default) answers with Kev instead of
TypeSafe. By default it runs on this machine: `integrations/kev/install-kev.sh`
sets up Kev-0.8B as a launchd agent on 127.0.0.1:8009 (~0.3–1s/file on an M4,
~2GB RAM). If no local Kev is running it falls back to the homelab's CPU Kev
(still private, ~2–8s/file); `--private-backend homelab` or
`JEV_ASK_PRIVATE_BACKEND=homelab` always uses the homelab. `--context` adds a task goal; `--url` / `JEV_ASK_URL` and `--key` /
`JEV_ASK_ACCESS_KEY` point at another instance. A nonzero exit means
incomplete or failed classification — not that every file was clean.

### jev-review

```bash
jev-review              # git diff in the current repo
jev-review --staged     # staged changes
gh pr diff 123 | jev-review
```

`$JEV_REVIEW_URL` / `$JEV_REVIEW_ACCESS_KEY` override the endpoint and auth.

### Agents

- **Claude Code / any shell agent:** the `ask-jev` and `jev-review` skills
  teach classification-first use of the CLIs via Bash — no MCP needed.
- **Pi:** `integrations/pi/ask-jev.ts` registers a native `ask_jev` tool
  against the same `/ask` endpoint.

**Privacy:** file contents leave your machine and go through the server to
TypeSafe. Only send code/text you're permitted to. Jev answers are scouting
signals, not verified facts — read source for exact evidence.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```
