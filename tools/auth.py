"""
tools/auth.py — MSAL Authentication for Power Platform
========================================================
Supports two flows:
  1. Device Code Flow — interactive, asks user to sign in via browser (default)
  2. Client Credentials — headless/service principal, set PP_CLIENT_SECRET in .env

Token is cached to token_cache.json and refreshed automatically.
All credentials are read from .env — change anytime without touching code.
"""

import os
import json
import time
import threading
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Read credentials from .env ─────────────────────────────────────────────────
CLIENT_ID    = os.getenv("PP_CLIENT_ID",    "")
TENANT_ID    = os.getenv("PP_TENANT_ID",    "common")
CLIENT_SECRET = os.getenv("PP_CLIENT_SECRET", "")   # optional — for client creds flow
ENV_URL      = os.getenv("PP_ENV_URL",      "")     # e.g. https://yourorg.crm11.dynamics.com

# Cache file sits next to this script
CACHE_FILE   = Path(__file__).parent.parent / "token_cache.json"

# Scopes needed for all PP APIs
DATAVERSE_SCOPE     = lambda url: [f"{url.rstrip('/')}/.default"]
POWER_PLATFORM_SCOPE = ["https://management.azure.com/.default"]
FLOW_SCOPE          = ["https://service.flow.microsoft.com/.default"]


# ─────────────────────────────────────────────────────────────────────────────
#  TOKEN CACHE
# ─────────────────────────────────────────────────────────────────────────────

def _load_cache():
    import msal
    cache = msal.SerializableTokenCache()
    if CACHE_FILE.exists():
        cache.deserialize(CACHE_FILE.read_text())
    return cache


def _save_cache(cache):
    if cache.has_state_changed:
        CACHE_FILE.write_text(cache.serialize())


# ─────────────────────────────────────────────────────────────────────────────
#  BUILD MSAL APP
# ─────────────────────────────────────────────────────────────────────────────

def _build_app(cache=None):
    """Build ConfidentialClientApplication when secret is set (for client-creds silent flow)."""
    import msal
    authority = f"https://login.microsoftonline.com/{TENANT_ID}"

    if not CLIENT_ID:
        raise RuntimeError(
            "❌ PP_CLIENT_ID not set in .env\n"
            "   Register a free Azure app at https://portal.azure.com → App registrations\n"
            "   See README_SETUP.md for full instructions."
        )

    if CLIENT_SECRET:
        return msal.ConfidentialClientApplication(
            CLIENT_ID,
            authority=authority,
            client_credential=CLIENT_SECRET,
            token_cache=cache,
        )
    else:
        return msal.PublicClientApplication(
            CLIENT_ID,
            authority=authority,
            token_cache=cache,
        )


def _build_public_app(cache=None):
    """Always returns a PublicClientApplication — required for device code flow."""
    import msal
    if not CLIENT_ID:
        raise RuntimeError("❌ PP_CLIENT_ID not set in .env")
    return msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GET TOKEN — main public function
# ─────────────────────────────────────────────────────────────────────────────

def get_token(scope: list[str]) -> str:
    """
    Get a valid access token for the given scope.
    Uses cache first. Falls back to device code login if cache misses.
    Returns the token string (bearer token).
    """
    cache = _load_cache()
    app   = _build_app(cache)

    # Try silent (cached) first
    accounts = app.get_accounts()
    result   = None

    if accounts:
        result = app.acquire_token_silent(scope, account=accounts[0])

    if not result and CLIENT_SECRET:
        # Client credentials flow (no user interaction)
        result = app.acquire_token_for_client(scopes=scope)

    if not result:
        # Device code flow — always needs PublicClientApplication
        pub_app = _build_public_app(cache)
        flow    = pub_app.initiate_device_flow(scopes=scope)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error_description', 'unknown error')}")

        print("\n" + "═" * 55)
        print("  🔐  Sign in to Power Platform")
        print("═" * 55)
        print(f"\n  1. Open:  {flow['verification_uri']}")
        print(f"  2. Enter: {flow['user_code']}")
        print("\n  Waiting for you to sign in…\n")

        result = pub_app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if "access_token" not in result:
        err = result.get("error_description") or result.get("error") or str(result)
        raise RuntimeError(f"❌ Authentication failed: {err}")

    return result["access_token"]


# ─────────────────────────────────────────────────────────────────────────────
#  HEADERS — per-environment helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_dataverse_headers(env_url: str | None = None) -> dict:
    """Return auth headers for Dataverse Web API calls."""
    url   = (env_url or ENV_URL).rstrip("/")
    token = get_token(DATAVERSE_SCOPE(url))
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version":    "4.0",
    }


def get_flow_headers() -> dict:
    """Return auth headers for Power Automate API calls."""
    token = get_token(FLOW_SCOPE)
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


def get_management_headers() -> dict:
    """Return auth headers for Azure Management / BAP API calls."""
    token = get_token(POWER_PLATFORM_SCOPE)
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIRONMENT MANAGER — switch environments at runtime
# ─────────────────────────────────────────────────────────────────────────────

_active_env = {
    "url":  ENV_URL,
    "name": os.getenv("PP_ENV_NAME", "Default"),
}


def set_active_environment(url: str, name: str = ""):
    """Switch the active environment. Persists for the session."""
    _active_env["url"]  = url.rstrip("/")
    _active_env["name"] = name or url


def get_active_environment() -> dict:
    """Return the current active environment."""
    return dict(_active_env)


def get_active_env_url() -> str:
    return _active_env["url"]


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH STATUS — for the UI right panel
# ─────────────────────────────────────────────────────────────────────────────

def get_auth_status() -> dict:
    """
    Check if a valid cached token exists without triggering a new login.
    Returns dict with connected bool and account info.
    """
    try:
        cache = _load_cache()
        app   = _build_app(cache)
        accounts = app.get_accounts()
        if not accounts:
            return {"connected": False, "account": None, "env": _active_env}

        env_url = get_active_env_url()
        if not env_url:
            return {"connected": False, "account": accounts[0].get("username"), "env": _active_env, "error": "PP_ENV_URL not set"}

        result = app.acquire_token_silent(DATAVERSE_SCOPE(env_url), account=accounts[0])
        if result and "access_token" in result:
            return {
                "connected": True,
                "account":   accounts[0].get("username"),
                "env":       _active_env,
            }
    except Exception as e:
        return {"connected": False, "error": str(e), "env": _active_env}

    return {"connected": False, "account": None, "env": _active_env}


def clear_token_cache():
    """Force re-login by deleting cached tokens."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
    return "Token cache cleared. Next API call will prompt login."


# ─────────────────────────────────────────────────────────────────────────────
#  WEB DEVICE CODE FLOW — non-blocking, for Flask UI
# ─────────────────────────────────────────────────────────────────────────────

_device_flow_state: dict = {"status": "idle", "error": None}  # idle | pending | success | failed
_device_flow_lock  = threading.Lock()


def get_device_flow_state() -> dict:
    """Return current state of the background device code auth."""
    with _device_flow_lock:
        return dict(_device_flow_state)


def initiate_device_flow_web(scope: list[str] | None = None) -> dict:
    """
    Start the MSAL device code flow without blocking.
    Returns {user_code, verification_uri, expires_in, message} immediately.
    Token acquisition runs in a background thread.
    Check get_device_flow_state() to poll for completion.
    """
    env_url   = get_active_env_url() or os.getenv("PP_ENV_URL", "").strip()
    use_scope = scope or (DATAVERSE_SCOPE(env_url) if env_url else ["https://service.powerapps.com/.default"])

    cache   = _load_cache()
    pub_app = _build_public_app(cache)  # device code always needs PublicClientApplication

    flow = pub_app.initiate_device_flow(scopes=use_scope)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow init failed: {flow.get('error_description', 'unknown')}")

    with _device_flow_lock:
        _device_flow_state["status"] = "pending"
        _device_flow_state["error"]  = None

    def _background_acquire():
        try:
            result = pub_app.acquire_token_by_device_flow(flow)
            _save_cache(cache)
            if "access_token" in result:
                with _device_flow_lock:
                    _device_flow_state["status"] = "success"
            else:
                err = result.get("error_description") or result.get("error") or "unknown"
                with _device_flow_lock:
                    _device_flow_state["status"] = "failed"
                    _device_flow_state["error"]  = err
        except Exception as e:
            with _device_flow_lock:
                _device_flow_state["status"] = "failed"
                _device_flow_state["error"]  = str(e)

    threading.Thread(target=_background_acquire, daemon=True).start()

    return {
        "user_code":        flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "expires_in":       flow.get("expires_in", 900),
        "message":          flow.get("message", ""),
    }
