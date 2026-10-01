"""Generic Ask Jev: evaluate one or more states against a typed question
map (noul/choice/score), fanned out over a process-wide worker pool."""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from jevkit.jev import ask_jev, ask_kev

MAX_ASK_ITEMS = 100
MAX_ASK_STATE_BYTES = 200 * 1024  # per state, in UTF-8 bytes
UPSTREAM_WORKERS = 8  # shared across every request, so load stays bounded
UPSTREAM_TIMEOUT = 120  # per item, seconds

QUESTION_TYPES = {"noul", "choice", "score"}

# One pool for the whole process: a burst of /ask requests queues instead of
# multiplying concurrent upstream calls (each of which can retry on 429/529).
EXECUTOR = ThreadPoolExecutor(max_workers=UPSTREAM_WORKERS)
# ponytail: CPU Kev runs one request at a time (more in flight measured no
# faster), so 2 workers keep it busy without private batches taking Jev's pool.
KEV_EXECUTOR = ThreadPoolExecutor(max_workers=2)


class AllItemsFailed(Exception):
    pass


def prepare_ask(payload):
    """Validate an /ask body and return (states, questions).

    States is a list of {"id", "state", "truncated"} — the state clipped to
    MAX_ASK_STATE_BYTES UTF-8 bytes. Raises ValueError with a client-facing
    message on any invalid shape.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    # a typo'd "private" must not silently send content to TypeSafe
    if not isinstance(payload.get("private", False), bool):
        raise ValueError("private must be true or false")

    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty object")
    for name, q in questions.items():
        if not isinstance(q, dict) or not isinstance(q.get("type"), str) or q["type"] not in QUESTION_TYPES:
            raise ValueError(f"question {name!r} must have type noul, choice, or score")

    items = payload.get("items")
    if items is None:
        state = payload.get("state")
        if not isinstance(state, str) or not state.strip():
            raise ValueError("provide a state string or an items array")
        items = [{"id": "state", "state": state}]
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty array")
    if len(items) > MAX_ASK_ITEMS:
        raise ValueError(f"too many items (max {MAX_ASK_ITEMS})")

    states = []
    seen_ids = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("state"), str):
            raise ValueError(f"items[{i}] must have a state string")
        sid = str(item.get("id", i))
        if sid in seen_ids:
            raise ValueError(f"duplicate item id {sid!r}")
        seen_ids.add(sid)
        raw = item["state"]
        encoded = raw.encode("utf-8")
        if len(encoded) > MAX_ASK_STATE_BYTES:
            state = encoded[:MAX_ASK_STATE_BYTES].decode("utf-8", errors="ignore")
            states.append({"id": sid, "state": state, "truncated": True})
        else:
            states.append({"id": sid, "state": raw, "truncated": False})
    return states, questions


def run_ask(states, questions, api_key, private=False):
    """Evaluate every state and return {"answers", "usage"}. Per-item
    failures become {"error": ...}; raises AllItemsFailed if none succeed."""

    def evaluate(s):
        try:
            resp = ask_kev(s["state"], questions) if private else ask_jev(s["state"], questions, api_key)
            answers = resp.get("answers") if isinstance(resp, dict) else None
            if not isinstance(answers, dict):
                raise ValueError("jev returned no answers")
            usage = resp.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            return {"answers": answers, "usage": usage, "truncated": bool(resp.get("state_truncated"))}
        except Exception as e:
            return {"error": str(e)}

    pool = KEV_EXECUTOR if private else EXECUTOR
    futures = [(s, pool.submit(evaluate, s)) for s in states]
    results = {}
    for s, fut in futures:
        try:
            results[s["id"]] = fut.result(timeout=UPSTREAM_TIMEOUT)
        except FutureTimeoutError:
            fut.cancel()
            results[s["id"]] = {"error": f"jev upstream timeout ({UPSTREAM_TIMEOUT}s)"}
        except Exception as e:
            results[s["id"]] = {"error": str(e)}

    if all("error" in r for r in results.values()):
        raise AllItemsFailed(next(iter(results.values()))["error"])

    answers = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    truncated_ids = {s["id"] for s in states if s["truncated"]}
    for sid, r in results.items():
        if "error" in r:
            answers[sid] = {"error": r["error"]}
            continue
        answers[sid] = r["answers"]
        for k in usage:
            try:
                usage[k] += int(r["usage"].get(k, 0))
            except (TypeError, ValueError):
                pass
        if sid in truncated_ids or r["truncated"]:
            answers[sid]["_state_truncated"] = True

    return {"answers": answers, "usage": usage}
