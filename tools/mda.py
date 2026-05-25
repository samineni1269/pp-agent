"""
tools/mda.py — Model-Driven App Management
===========================================
Uses Dataverse Web API to inspect and manage model-driven apps.
"""

from __future__ import annotations
import requests
from tools.auth import get_dataverse_headers, get_active_env_url


def _dv_url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h():
    return get_dataverse_headers()


# ── READ ──────────────────────────────────────────────────────────────────────

def list_model_driven_apps() -> list[dict]:
    """List all model-driven apps."""
    url = _dv_url("appmodules?$select=name,uniquename,description,appmoduleid&$orderby=name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_app_details(app_unique_name: str) -> dict:
    """Get details of a model-driven app."""
    url = _dv_url(f"appmodules?$filter=uniquename eq '{app_unique_name}'")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    items = resp.json().get("value", [])
    return items[0] if items else {}


def list_app_sitemap_areas(app_unique_name: str) -> list[dict]:
    """List site map areas for a model-driven app."""
    app = get_app_details(app_unique_name)
    if not app:
        return [{"error": "App not found"}]
    app_id = app.get("appmoduleid")
    url = _dv_url(f"sitemaps?$filter=_appmoduleid_value eq '{app_id}'")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_app_security_roles(app_unique_name: str) -> list[dict]:
    """List security roles associated with a model-driven app."""
    app = get_app_details(app_unique_name)
    if not app:
        return [{"error": "App not found"}]
    app_id = app.get("appmoduleid")
    url = _dv_url(f"appmoduleroles?$filter=_appmoduleid_value eq '{app_id}'&$expand=roleid($select=name)")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_app_components(app_unique_name: str) -> list[dict]:
    """List all components registered in the app."""
    app = get_app_details(app_unique_name)
    if not app:
        return [{"error": "App not found"}]
    app_id = app.get("appmoduleid")
    url = _dv_url(f"appmodulecomponents?$filter=_appmoduleid_value eq '{app_id}'&$select=componenttype,objectid")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── WRITE ─────────────────────────────────────────────────────────────────────

def publish_model_driven_app(app_unique_name: str) -> dict:
    """Publish a model-driven app."""
    app = get_app_details(app_unique_name)
    if not app:
        return {"error": "App not found"}
    app_id = app.get("appmoduleid")
    url = _dv_url(f"appmodules({app_id})/Microsoft.Dynamics.CRM.ValidateApp")
    resp = requests.get(url, headers=_h(), timeout=30)
    return {"validation": resp.json() if resp.ok else resp.text}
