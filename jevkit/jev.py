"""TypeSafe client: evaluate a state against typed questions with Jev."""
import json
import time
import urllib.error
import urllib.request

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
RETRYABLE_STATUS = {429, 529}
# Jev caps state + longest question at 32k tokens, and bytes-per-token
# varies too much to clip up front, so oversized states are halved until
# they fit. Rejected calls are fast and unbilled.
MAX_HALVINGS = 6
MIN_STATE_CHARS = 1000


class JevError(Exception):
    pass


def _post(state, questions, api_key):
    body = json.dumps({"model": JEV_MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        JEV_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def ask_jev(state, questions, api_key, retries=2):
    """Evaluate one state against a question map. Retries 429/529 with
    exponential backoff, per the TypeSafe API docs. If the state is over
    Jev's token limit it is halved until it fits, and the response gets
    "state_truncated": True."""
    truncated = False
    attempt = 0
    halvings = 0
    while True:
        try:
            resp = _post(state, questions, api_key)
            if truncated:
                resp["state_truncated"] = True
            return resp
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in RETRYABLE_STATUS and attempt < retries:
                time.sleep(2**attempt)
                attempt += 1
                continue
            if (
                e.code == 400
                and "max_tokens_exceeded" in detail
                and halvings < MAX_HALVINGS
                and len(state) > MIN_STATE_CHARS
            ):
                state = state[: len(state) // 2]
                truncated = True
                halvings += 1
                continue
            raise JevError(f"HTTP {e.code}: {detail}") from e
