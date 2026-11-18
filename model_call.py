"""model_call.py: the one place the pipeline talks to Ollama.

Every reader used to build its own urllib request to /api/chat (or /api/generate, /api/ps) and parse the reply.
They now call these helpers. The helpers only do the HTTP part: they send exactly the body the caller builds and
hand back Ollama's reply, so prompts, schemas, timeouts and each reader's own answer parsing stay where they were.

Errors are not swallowed or retried here: urllib.error.HTTPError / URLError / socket timeouts reach the caller as
before (worker.py, for example, retries an HTTP 400 at lower resolution and turns a dead Ollama into OllamaDown).

    chat(url, body, timeout)          -> Ollama's whole /api/chat reply (dict)
    chat_content(url, body, timeout)  -> just reply["message"]["content"] (KeyError if missing, as before)
    generate(url, body, timeout)      -> Ollama's whole /api/generate reply (dict)
    loaded(url, timeout)              -> the "models" list of /api/ps (raises if Ollama is down)
    is_up(url, timeout)               -> True when /api/tags answers
    version(url, timeout)             -> /api/version's "version"
    unload(url, model, timeout)       -> asks Ollama to drop a model from the GPU (keep_alive 0)
    image_b64(path)                   -> a page image as the base64 text Ollama wants in "images"
    check_length(resp, num_predict)   -> raises when the answer stopped at the token cap (684)
"""
import base64
import json
import urllib.request

_HEADERS = {"Content-Type": "application/json"}


def _base(url):
    return url.rstrip("/")


def _post(url, path, body, timeout):
    req = urllib.request.Request(_base(url) + path, data=json.dumps(body).encode(), headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url, path, timeout):
    with urllib.request.urlopen(_base(url) + path, timeout=timeout) as r:
        return json.loads(r.read())


def chat(url, body, timeout):
    """POST body to /api/chat and return the parsed reply."""
    return _post(url, "/api/chat", body, timeout)


def chat_content(url, body, timeout):
    """POST body to /api/chat and return the model's answer text."""
    return chat(url, body, timeout)["message"]["content"]


def generate(url, body, timeout):
    """POST body to /api/generate and return the parsed reply."""
    return _post(url, "/api/generate", body, timeout)


def loaded(url, timeout=5):
    """The models Ollama holds in memory right now: the raw /api/ps "models" list. Raises when Ollama is down."""
    return _get(url, "/api/ps", timeout).get("models", [])


def is_up(url, timeout=5):
    try:
        with urllib.request.urlopen(_base(url) + "/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


def version(url, timeout=3):
    return _get(url, "/api/version", timeout).get("version")


def unload(url, model, timeout=60):
    """Ask Ollama to unload `model` now (an empty generate with keep_alive 0)."""
    req = urllib.request.Request(_base(url) + "/api/generate", data=json.dumps({"model": model, "keep_alive": 0}).encode(),
                                 headers=_HEADERS)
    urllib.request.urlopen(req, timeout=timeout).read()


def image_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def check_length(resp, num_predict):
    """684: the model looped until the num_predict cap. Fail now with the same message every reader used."""
    if resp.get("done_reason") == "length":
        raise RuntimeError(f"answer stopped at the {num_predict}-token cap "
                           f"({round((resp.get('eval_duration') or 0) / 1e9)} s): the model was repeating itself on this page")
