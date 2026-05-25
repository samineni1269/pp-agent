"""
tools/canvas.py — Canvas App Management
========================================
Uses Power Apps API / Dataverse entity for canvas app operations.
"""

from __future__ import annotations
import requests
import os
from tools.auth import get_dataverse_headers, get_management_headers, get_active_env_url

ENV_ID = lambda: os.getenv("PP_ENV_ID", "")


def _pp_headers():
    return get_management_headers()


def _dv_url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _dv_headers():
    return get_dataverse_headers()


# ── READ ──────────────────────────────────────────────────────────────────────

def list_canvas_apps() -> list[dict]:
    """List canvas apps in the environment."""
    eid = ENV_ID()
    if not eid:
        return [{"note": "PP_ENV_ID not set — add to .env to list canvas apps via API"}]
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apps?api-version=2020-06-01&$filter=environment/name eq '{eid}'"
    resp = requests.get(url, headers=_pp_headers(), timeout=30)
    resp.raise_for_status()
    apps = resp.json().get("value", [])
    return [{"id": a["name"], "displayName": a["properties"]["displayName"],
             "owner": a["properties"].get("owner", {}).get("displayName"),
             "lastModified": a["properties"].get("lastModifiedTime"),
             "sharedWithAll": a["properties"].get("sharedWithOrganization", False)}
            for a in apps]


def get_canvas_app_details(app_id: str) -> dict:
    """Get details of a specific canvas app."""
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apps/{app_id}?api-version=2020-06-01"
    resp = requests.get(url, headers=_pp_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_app_permissions(app_id: str) -> list[dict]:
    """List who has access to a canvas app."""
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apps/{app_id}/permissions?api-version=2020-06-01"
    resp = requests.get(url, headers=_pp_headers(), timeout=30)
    resp.raise_for_status()
    perms = resp.json().get("value", [])
    return [{"principal": p.get("principal", {}).get("displayName"),
             "role": p.get("roleName")} for p in perms]


def list_app_connections(app_id: str) -> list[dict]:
    """List data connections used by a canvas app."""
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apps/{app_id}/connections?api-version=2020-06-01"
    resp = requests.get(url, headers=_pp_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── WRITE ─────────────────────────────────────────────────────────────────────

def export_canvas_app(app_id: str) -> dict:
    """Export a canvas app package."""
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apps/{app_id}/exportPackage?api-version=2020-06-01"
    payload = {"baseResourceIds": [f"/providers/Microsoft.PowerApps/apps/{app_id}"]}
    resp = requests.post(url, headers=_pp_headers(), json=payload, timeout=60)
    resp.raise_for_status()
    return {"success": True, "export_link": resp.json().get("packageLink", {}).get("value", "")}


def publish_canvas_app(app_id: str) -> dict:
    """Publish a canvas app."""
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apps/{app_id}/publish?api-version=2020-06-01"
    resp = requests.post(url, headers=_pp_headers(), timeout=30)
    return {"success": resp.status_code in (200, 202, 204)}


def delete_canvas_app(app_id: str) -> dict:
    """Delete a canvas app."""
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apps/{app_id}?api-version=2020-06-01"
    resp = requests.delete(url, headers=_pp_headers(), timeout=30)
    return {"success": resp.status_code in (200, 204)}
