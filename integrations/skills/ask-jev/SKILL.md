---
name: ask-jev
description: Classify code or text without reading its contents into context. Use first for yes/no content judgments, categorizing files, or rating complexity/risk across file globs. Calls the portable ask-jev CLI through Bash, or Pi's native ask_jev tool when available.
---

# Ask Jev

Always use Ask Jev first when only a classification or judgment is needed,
not the source itself. Jev is a classifier, not a free-text summarizer.
Files are read locally and sent through the homelab to TypeSafe. Only use
this for code/text you are permitted to send to that service.

## Calling from any coding agent or shell

```bash
ask-jev --question 'Does this file contain TODO or FIXME comments?' --json 'src/**/*.go'
ask-jev --question 'Does this text contain a TODO?' --text 'TODO: write tests'
printf '%s\n' 'sample text' | ask-jev --question 'Does this convey urgency?'
ask-jev --questions /tmp/jev-questions.json --json 'src/**/*.py'
```

Quote globs so the CLI expands them. Relative paths resolve against the
command's working directory. Batch the same generic questions across files
instead of making one command per file. For goal-relative judgments add
`--context 'task goal'`.

`--questions` accepts a JSON file containing a named question map:

```json
{
  "is_test": {"type": "noul", "instructions": "Is this an automated test file?"},
  "role": {
    "type": "choice",
    "instructions": "What is this file's primary role?",
    "criteria": {"server": "Receives HTTP requests", "client": "Sends HTTP requests", "other": "Neither"}
  },
  "complexity": {
    "type": "score",
    "instructions": "Rate complexity.",
    "criteria": ["Trivial", "Simple", "Moderate", "Complex"]
  }
}
```

- `noul`: yes/no probability, 0 to 1. `--question` is shorthand for one noul question.
- `choice`: picks a criteria key with confidence; uncertain classifications need inspection.
- `score`: weighted position along the ordered rubric, not necessarily an integer.
- Prefer `--json` for agent consumption. Read returned answers, warnings/errors and usage, not file contents.
- A nonzero exit means failure or incomplete coverage. Preserve successful results, report what failed/skipped/truncated, and never infer that unchecked files are clean.
- Read source afterward only for exact evidence, implementation details or editing. Jev's judgments are not verified facts or security guarantees.

If Pi's native `ask_jev` tool is available, use it directly with the same
question types and file/glob inputs. If neither the tool nor CLI works,
fall back explicitly to search/read; never claim a Jev call that didn't run.

## Installation and config

Run `install.sh` from github.com/MaxIvanyshen/jevkit; it installs
`~/.local/bin/ask-jev`. Use that absolute path if it isn't on PATH.
Default endpoint: `https://gerry.gobeep.xyz:8790/ask`.
Override with `--url` or `JEV_ASK_URL`. Optional homelab auth:
`JEV_ASK_ACCESS_KEY` (prefer env over `--key` to avoid shell history).
No client-side TypeSafe API key is required.
