"""
tools/_base.py — Shared HTTP Utilities
=======================================
Retry logic, error normalisation, and convenience wrappers used by every
tool file. Import from here instead of calling `requests` directly.

Features:
  - Auto-retry on 429 (throttle) / 5xx transient errors with back-off
  - Raises HTTPError with the actual API error message (not just status code)
  - Thin get/post/patch/delete wrappers so tool code stays clean
"""

from __future__ import annotations
import time
import requests
from typing import Any

# ── Retry config ──────────────────────────────────────────────────────────────
_MAX_RETRIES   = 3
_BACKOFF_SECS  = [1, 3, 6]          # wait between retries
_RETRY_ON      = {429, 500, 502, 503, 504}


def api_call(
    method: str,
    url: str,
    headers: dict,
    timeout: int = 30,
    **kwargs: Any,
) -> requests.Response:
    """
    Execute an HTTP request with automatic retry on transient failures.
    Returns the Response object. Does NOT raise on HTTP errors — call
    raise_for_status_detail() to surface them with a useful message.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            if resp.status_code in _RETRY_ON and attempt < _MAX_RETRIES - 1:
                wait = int(resp.headers.get("Retry-After", _BACKOFF_SECS[attempt]))
                time.sleep(wait)
                continue
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_SECS[attempt])
    raise last_exc or requests.ConnectionError("Max retries exceeded")


def raise_for_status_detail(resp: requests.Response) -> None:
    """
    Raise requests.HTTPError with the actual API error body, not just
    '400 Client Error'. Dataverse errors are nested under resp.json()['error'].
    """
    if resp.ok:
        return
    msg = f"HTTP {resp.status_code}"
    try:
        body = resp.json()
        # Dataverse / OData error shape
        if "error" in body:
            err = body["error"]
            msg = f"HTTP {resp.status_code}: {err.get('message', err)}"
        elif "Message" in body:
            msg = f"HTTP {resp.status_code}: {body['Message']}"
        else:
            msg = f"HTTP {resp.status_code}: {str(body)[:400]}"
    except Exception:
        msg = f"HTTP {resp.status_code}: {resp.text[:400]}"
    raise requests.HTTPError(msg, response=resp)


# ── Convenience wrappers ──────────────────────────────────────────────────────

def get(url: str, headers: dict, timeout: int = 30, **kw) -> requests.Response:
    r = api_call("GET", url, headers, timeout, **kw)
    raise_for_status_detail(r)
    return r


def post(url: str, headers: dict, timeout: int = 30, **kw) -> requests.Response:
    r = api_call("POST", url, headers, timeout, **kw)
    raise_for_status_detail(r)
    return r


def patch(url: str, headers: dict, timeout: int = 30, **kw) -> requests.Response:
    r = api_call("PATCH", url, headers, timeout, **kw)
    raise_for_status_detail(r)
    return r


def delete(url: str, headers: dict, timeout: int = 30, **kw) -> requests.Response:
    r = api_call("DELETE", url, headers, timeout, **kw)
    raise_for_status_detail(r)
    return r


def safe_json(resp: requests.Response, default: Any = None) -> Any:
    """Return parsed JSON or default if body is empty / not JSON."""
    try:
        return resp.json()
    except Exception:
        return default
