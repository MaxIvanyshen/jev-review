#!/usr/bin/env python3
"""CLI for the jevkit `/ask` endpoint: ask Jev typed questions (noul,
choice, score) about files, inline text, or stdin, and print the answers.
Never echoes file contents — only the resulting judgments.

Usage:
    ask-jev 'Does this file contain a TODO?' 'src/**/*.py'
    ask-jev -q 'Does this file contain a TODO?' 'src/**/*.py'
    ask-jev --questions questions.json src/foo.py src/bar.py
    git diff | ask-jev 'Is this diff risky?'
    ask-jev --text 'some code' 'Does this use eval()?'

Environment:
    JEV_ASK_URL          default server URL (else homelab /ask)
    JEV_ASK_ACCESS_KEY   bearer token, only needed if the server requires auth
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = os.environ.get("JEV_ASK_URL", "https://gerry.gobeep.xyz:8790/ask")


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)

QUESTION_TYPES = {"noul", "choice", "score"}
EXCLUDED_DIRS = {"node_modules", ".git", ".venv"}
MAX_STATE_BYTES = 200 * 1024  # matches server's per-item clip, so we clip first
READ_CAP = MAX_STATE_BYTES + 8192  # bounds memory; header adds a little overhead
MAX_ITEMS_PER_BATCH = 100
MAX_TOTAL_FILES = 300
MAX_VISITED_ENTRIES = 50_000


class LimitExceeded(Exception):
    pass


# --- file discovery -------------------------------------------------------


def iter_files_under(base, visited):
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        visited[0] += 1
        if visited[0] > MAX_VISITED_ENTRIES:
            raise LimitExceeded(
                f"too many filesystem entries under {base!r} "
                f"(>{MAX_VISITED_ENTRIES}); narrow your patterns"
            )
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name in EXCLUDED_DIRS:
                continue
            yield from iter_files_under(entry.path, visited)
        elif entry.is_file():
            yield entry.path


def split_base(pattern):
    """Directory to start walking from: the prefix before the first
    wildcard segment."""
    base_parts = []
    for part in pattern.split("/"):
        if re.search(r"[*?]", part):
            break
        base_parts.append(part)
    return "/".join(base_parts) or ("/" if pattern.startswith("/") else ".")


def glob_to_regex(pattern):
    """Translate a glob pattern (*, ?, recursive **) to an anchored regex.
    ** matches across path separators, * does not (braces/classes aren't
    supported — passed through literally)."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def discover_files(patterns):
    """Resolve positional args (explicit files, directories, globs) into a
    deduped list of relative paths, plus a list of (pattern, reason) skips
    for anything that didn't resolve to a file."""
    visited = [0]
    matched = []
    skipped = []
    seen = set()

    def add(path):
        rel = os.path.relpath(path, ".")
        if rel not in seen:
            seen.add(rel)
            matched.append(rel)

    for pattern in patterns:
        if "{" in pattern or "}" in pattern:
            skipped.append((pattern, "brace expansion is not supported"))
            continue
        has_wildcard = bool(re.search(r"[*?]", pattern))
        if not has_wildcard:
            if os.path.isdir(pattern):
                found = list(iter_files_under(pattern, visited))
                if not found:
                    skipped.append((pattern, "directory contains no files"))
                for f in found:
                    add(f)
            elif os.path.isfile(pattern):
                add(pattern)
            else:
                skipped.append((pattern, "path not found"))
            continue

        # match absolute paths so absolute, relative and ./ patterns all work
        abs_pattern = os.path.abspath(pattern)
        base = split_base(abs_pattern)
        if not os.path.isdir(base):
            skipped.append((pattern, f"base directory not found: {base}"))
            continue
        regex = glob_to_regex(abs_pattern)
        found_any = False
        for f in iter_files_under(base, visited):
            if regex.fullmatch(os.path.abspath(f)):
                found_any = True
                add(f)
        if not found_any:
            skipped.append((pattern, "no files matched"))

    return matched, skipped


# --- building request items -------------------------------------------------


def read_item(path):
    try:
        with open(path, "rb") as f:
            data = f.read(READ_CAP + 1)
    except OSError as e:
        return None, f"unreadable: {e}"
    if b"\x00" in data[:8192]:
        return None, "binary file"
    return data.decode("utf-8", errors="replace"), None


def build_state(context, label, content):
    parts = []
    if context:
        parts.append(context.strip())
    parts.append(f"=== {label} ===")
    parts.append(content)
    state = "\n\n".join(parts)
    encoded = state.encode("utf-8")
    truncated = False
    if len(encoded) > MAX_STATE_BYTES:
        state = encoded[:MAX_STATE_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    return state, truncated


# --- questions --------------------------------------------------------------


def validate_questions(data):
    if not isinstance(data, dict) or not data:
        die("questions must be a non-empty JSON object")
    for name, q in data.items():
        if not isinstance(q, dict) or q.get("type") not in QUESTION_TYPES:
            die(f"question {name!r} must have type noul, choice, or score")
        if "instructions" not in q:
            die(f"question {name!r} is missing instructions")
        if q["type"] in ("choice", "score") and not q.get("criteria"):
            die(f"question {name!r} ({q['type']}) requires criteria")
    return data


def load_questions_file(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError as e:
        die(f"cannot read questions file: {e}")
    except json.JSONDecodeError as e:
        die(f"invalid JSON in questions file: {e}")
    return validate_questions(data)


# --- server call --------------------------------------------------------------


def post_batch(url, key, timeout, items, questions):
    body = json.dumps(
        {
            "items": [{"id": it["id"], "state": it["state"]} for it in items],
            "questions": questions,
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"server error {e.code}: {e.read().decode(errors='replace')[:300]}"
    except urllib.error.URLError as e:
        return None, f"connection error: {e.reason}"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON response: {e}"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
        return None, "invalid response shape: missing answers object"
    return parsed, None


def run_batches(url, key, timeout, items, questions):
    files = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    question_names = set(questions)

    for start in range(0, len(items), MAX_ITEMS_PER_BATCH):
        batch = items[start : start + MAX_ITEMS_PER_BATCH]
        parsed, err = post_batch(url, key, timeout, batch, questions)
        if err:
            for it in batch:
                files.append(
                    {"path": it["id"], "answers": None, "error": err, "truncated": it["client_truncated"]}
                )
            continue

        answers = parsed["answers"]
        for k in usage:
            try:
                usage[k] += int(parsed.get("usage", {}).get(k, 0))
            except (TypeError, ValueError):
                pass

        for it in batch:
            ans = answers.get(it["id"])
            truncated = it["client_truncated"]
            if ans is None:
                files.append({"path": it["id"], "answers": None, "error": "no answer returned for this id", "truncated": truncated})
                continue
            if set(ans) == {"error"}:
                files.append({"path": it["id"], "answers": None, "error": ans["error"], "truncated": truncated})
                continue
            missing = question_names - set(ans)
            if missing:
                files.append(
                    {"path": it["id"], "answers": None, "error": f"incomplete response: missing {sorted(missing)}", "truncated": truncated}
                )
                continue
            truncated = truncated or bool(ans.get("_state_truncated"))
            files.append({"path": it["id"], "answers": ans, "error": None, "truncated": truncated})

    return files, usage


# --- output --------------------------------------------------------------


def format_answer(name, ans):
    t = ans.get("type")
    if t == "noul":
        return f"  {name}: {ans['noul'] >= 0.5} (p={ans['noul']:.2f})"
    if t == "choice":
        return f"  {name}: {ans['choice']} (confidence={ans['confidence']:.2f})"
    if t == "score":
        label = (ans.get("legend") or {}).get(str(round(ans["score"])), "")
        return f"  {name}: {label} (score={ans['score']:.2f}, confidence={ans['confidence']:.2f})"
    return f"  {name}: {ans}"


def print_human(report):
    for f in report["files"]:
        print(f["path"])
        if f["error"]:
            print(f"  error: {f['error']}")
        else:
            for name, ans in f["answers"].items():
                if name == "_state_truncated":
                    continue
                print(format_answer(name, ans))
        if f["truncated"]:
            print("  (truncated: only the start of the input was evaluated)")
    for s in report["skipped"]:
        print(f"skipped: {s['path']} ({s['reason']})")
    u = report["usage"]
    print(f"usage: input_tokens={u['input_tokens']} output_tokens={u['output_tokens']}")


# --- main --------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Ask Jev typed questions about files, inline text, or stdin.",
        usage="ask-jev [options] QUESTION [PATH ...]\n       ask-jev [options] (-q QUESTION | --questions FILE) [PATH ...]",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="the yes/no question (unless -q/--questions is given), then file paths, directories, or globs (*, ?, **)",
    )
    parser.add_argument("--text", help="ask about this inline text instead of files")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--question", help="a single yes/no question (shorthand for one noul question)")
    group.add_argument("--questions", help="path to a JSON file of typed questions")
    parser.add_argument("--context", help="goal/context prepended to every state")
    parser.add_argument("--json", action="store_true", help="print a compact JSON report")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"ask endpoint URL (default: {DEFAULT_URL}, or $JEV_ASK_URL)")
    parser.add_argument("--key", default=os.environ.get("JEV_ASK_ACCESS_KEY"), help="bearer token ($JEV_ASK_ACCESS_KEY)")
    parser.add_argument("--timeout", type=float, default=180, help="request timeout in seconds (default: 180)")
    args = parser.parse_args(argv)

    if not args.question and not args.questions:
        if not args.paths:
            parser.error("a question is required: ask-jev 'Is this ...?' [PATH ...]")
        args.question = args.paths.pop(0)
        if os.path.exists(args.question):
            parser.error(f"{args.question!r} is a path, not a question — put the question first")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.text is not None and args.paths:
        parser.error("cannot combine --text with file paths")
    return args


def check_auth_safety(url, key):
    if not key:
        return
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        die("refusing to send --key over plain HTTP to a non-loopback host")


def collect_items(args):
    """Returns (items, skipped). Exits on unrecoverable input errors."""
    if args.paths:
        try:
            matched, skipped = discover_files(args.paths)
        except LimitExceeded as e:
            die(str(e))
        if len(matched) > MAX_TOTAL_FILES:
            die(f"too many files matched ({len(matched)} > {MAX_TOTAL_FILES}); narrow your patterns")
        items = []
        for rel in matched:
            content, err = read_item(rel)
            if err:
                skipped.append((rel, err))
                continue
            state, truncated = build_state(args.context, rel, content)
            items.append({"id": rel, "state": state, "client_truncated": truncated})
        return items, skipped

    if args.text is not None:
        state, truncated = build_state(args.context, "inline text", args.text)
        return [{"id": "text", "state": state, "client_truncated": truncated}], []

    if not sys.stdin.isatty():
        data = sys.stdin.read(READ_CAP + 1)
        if not data.strip():
            die("no input: stdin was empty")
        state, truncated = build_state(args.context, "stdin", data)
        return [{"id": "stdin", "state": state, "client_truncated": truncated}], []

    die("no input: provide file paths/globs, --text, or pipe stdin")


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    check_auth_safety(args.url, args.key)

    if args.questions:
        questions = load_questions_file(args.questions)
    else:
        questions = {"answer": {"type": "noul", "instructions": args.question}}

    items, skipped = collect_items(args)

    if not items:
        report = {"files": [], "skipped": [{"path": p, "reason": r} for p, r in skipped], "usage": {"input_tokens": 0, "output_tokens": 0}}
        if args.json:
            print(json.dumps(report, separators=(",", ":")))
        else:
            print_human(report)
        sys.exit(1)

    files, usage = run_batches(args.url, args.key, args.timeout, items, questions)
    report = {
        "files": files,
        "skipped": [{"path": p, "reason": r} for p, r in skipped],
        "usage": usage,
    }

    if args.json:
        print(json.dumps(report, separators=(",", ":")))
    else:
        print_human(report)

    ok = not skipped and all(f["error"] is None for f in files)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
