# ui/api.py
# Thin HTTP client for the FastAPI backend. Every call returns the JSON body,
# or None after showing a translated error, so pages never crash on a bad call.

import os

import requests
import streamlit as st

from ui.i18n import t

API = os.getenv("PFA_API_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_USER_ID = int(os.getenv("PFA_USER_ID", "1"))
# Longer than the API waits for the model (OLLAMA_TIMEOUT), so its message arrives.
CHAT_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "600")) + 60


def user_id():
    """The profile chosen in the sidebar (falls back to PFA_USER_ID)."""
    return st.session_state.get("user_id", DEFAULT_USER_ID)


class ApiDown(RuntimeError):
    pass


def _detail(response):
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text


def _call(method, path, params=None, data=None, timeout=30, quiet=False):
    try:
        response = requests.request(
            method, f"{API}{path}", params=params, json=data, timeout=timeout
        )
    except requests.ConnectionError as exc:
        raise ApiDown(API) from exc

    if response.ok:
        return response.json()

    if not quiet:
        st.error(t("error.request", detail=_detail(response)))
    return None


def get(path, **params):
    return _call("GET", path, params={"user_id": user_id(), **params})


def post(path, data=None, timeout=30, quiet=False):
    return _call("POST", path, data=data, timeout=timeout, quiet=quiet)


def put(path, data=None):
    return _call("PUT", path, data=data)


def delete(path, **params):
    return _call("DELETE", path, params=params or None)


def chat(message, history, language):
    """Returns (reply dict with answer / tools_used / sources, error_text)."""
    try:
        response = requests.post(
            f"{API}/chat",
            json={"user_id": user_id(), "message": message,
                  "history": history, "language": language},
            timeout=CHAT_TIMEOUT,
        )
    except requests.ConnectionError as exc:
        raise ApiDown(API) from exc
    except requests.Timeout:
        return None, t("chat.too_slow")

    if response.ok:
        return response.json(), None
    return None, _detail(response)
