"""TypeSafe client: evaluate a state against typed questions with Jev."""
import json
import time
import urllib.error
import urllib.request

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
RETRYABLE_STATUS = {429, 529}


def ask_jev(state, questions, api_key, retries=2):
    """Evaluate one state against a question map. Retries 429/529 with
    exponential backoff, per the TypeSafe API docs."""
    body = json.dumps({"model": JEV_MODEL, "state": state, "questions": questions}).encode()
    last_error = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            JEV_URL,
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code in RETRYABLE_STATUS and attempt < retries:
                time.sleep(2**attempt)
                continue
            raise
    raise last_error
