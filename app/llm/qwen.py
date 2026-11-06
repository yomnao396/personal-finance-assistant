# app/llm/qwen.py
# Talks to a local Qwen model served by Ollama (http://localhost:11434).
# No internet and no paid API: the model runs on your own machine.
# These settings can be changed without editing code:
#   OLLAMA_HOST=http://localhost:11434   OLLAMA_MODEL=qwen2.5:3b
#   OLLAMA_TIMEOUT=600   seconds to wait for one reply (raise it on a slow PC)

import os

import requests

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_URL = f"{OLLAMA_HOST}/api/chat"
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")   # small model that runs on a normal laptop
# Without OLLAMA_MODEL, any installed Qwen 2.5 (e.g. qwen2.5:latest) is used rather
# than reporting the default tag as missing.
MODEL_IS_SET = "OLLAMA_MODEL" in os.environ
# A CPU-only laptop needs minutes for the first question: the prompt with all the
# tool descriptions is a few thousand tokens.
TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "600"))


class LLMUnavailable(RuntimeError):
    """Ollama is not running, or the model has not been pulled."""


_chosen = None


def pick_model(installed):
    """The configured model if installed, else (when none was configured) any
    installed qwen2.5 tag, else None."""
    for name in installed:
        if name == MODEL or name.split(":")[0] == MODEL or name == f"{MODEL}:latest":
            return name
    if not MODEL_IS_SET:
        family = MODEL.split(":")[0]
        return next((n for n in installed if n.split(":")[0] == family), None)
    return None


def _installed():
    resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
    resp.raise_for_status()
    return [m.get("name", "") for m in resp.json().get("models", [])]


def model_name():
    """The model chat() sends requests to."""
    global _chosen
    if _chosen is None:
        try:
            _chosen = pick_model(_installed())
        except requests.RequestException:
            return MODEL
    return _chosen or MODEL


def status():
    """{'available': bool, 'model': ..., 'reason': ...} — never raises."""
    global _chosen
    try:
        installed = _installed()
    except requests.RequestException:
        return {
            "available": False,
            "model": MODEL,
            "reason": f"Ollama is not running at {OLLAMA_HOST}. Start it with: ollama serve",
        }

    _chosen = pick_model(installed)
    if _chosen is None:
        return {
            "available": False,
            "model": MODEL,
            "reason": f"Model {MODEL} is not installed. Run: ollama pull {MODEL}",
        }

    return {"available": True, "model": _chosen, "reason": "ready"}


def chat(messages, tools=None):
    # messages: list of {"role": "...", "content": "..."} dicts.
    model = model_name()
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # Deterministic answers: the same question about the same money gets the
        # same answer, and evaluation runs are repeatable.
        "options": {"temperature": 0, "seed": 42},
        # Keep the model in memory between questions; reloading it is slow.
        "keep_alive": "30m",
    }

    if tools:
        payload["tools"] = tools

    try:
        resp = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.ConnectionError as exc:
        raise LLMUnavailable(
            f"Ollama is not running at {OLLAMA_HOST}. Start it with: ollama serve"
        ) from exc
    except requests.Timeout as exc:
        raise LLMUnavailable(
            f"The model took longer than {TIMEOUT:.0f} seconds to answer. This computer may be "
            f"too slow for {model}; set OLLAMA_TIMEOUT higher or use a smaller model "
            "(OLLAMA_MODEL=qwen2.5:1.5b)."
        ) from exc

    if resp.status_code == 404:
        raise LLMUnavailable(f"Model {model} is not installed. Run: ollama pull {model}")

    if not resp.ok:
        # Ollama explains its own failures (e.g. not enough memory) in "error".
        try:
            reason = resp.json().get("error", resp.text)
        except ValueError:
            reason = resp.text
        raise LLMUnavailable(f"Ollama could not answer ({resp.status_code}): {reason}")

    return resp.json()["message"]
