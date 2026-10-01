"""Evaluate a state against typed questions: Jev via TypeSafe, or Kev
(open-weight, same System One API) on the homelab for private requests."""
import json
import os
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

KEV_URL = os.environ.get("KEV_URL", "http://kev:8009/v1/systemone")
# ponytail: Kev-0.8B is validated to ~8k tokens and only refuses past 65k, so
# clip up front instead of letting accuracy degrade silently on long files.
KEV_MAX_STATE_CHARS = 32_000


class JevError(Exception):
    pass


def _post(state, questions, api_key, url=JEV_URL, model=JEV_MODEL, timeout=30):
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def ask_kev(state, questions):
    """Evaluate on the homelab Kev server: nothing leaves the homelab.
    Long states are clipped and the response gets "state_truncated": True."""
    try:
        resp = _post(state[:KEV_MAX_STATE_CHARS], questions, None, KEV_URL, "kev-latest", timeout=120)
    except urllib.error.HTTPError as e:
        raise JevError(f"kev HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    if len(state) > KEV_MAX_STATE_CHARS:
        resp["state_truncated"] = True
    return resp


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
