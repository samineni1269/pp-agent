"""
tools/environments.py — Power Platform Environment Lifecycle
=============================================================
Uses BAP API (api.bap.microsoft.com) for environment management.
"""

from __future__ import annotations
import requests
from tools.auth import get_management_headers

BAP_BASE = "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform"


def _h():
    return get_management_headers()


# ── READ ──────────────────────────────────────────────────────────────────────

def list_environments() -> list[dict]:
    """List all Power Platform environments in the tenant."""
    url = f"{BAP_BASE}/scopes/admin/environments?api-version=2021-04-01"
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    envs = resp.json().get("value", [])
    return [{
        "id":          e["name"],
        "displayName": e["properties"].get("displayName"),
        "type":        e["properties"].get("environmentSku"),
        "location":    e["properties"].get("location"),
        "state":       e["properties"].get("states", {}).get("management", {}).get("id"),
        "dataverse":   e["properties"].get("linkedEnvironmentMetadata", {}).get("instanceUrl"),
    } for e in envs]


def get_environment_details(env_id: str) -> dict:
    """Get detailed info about an environment."""
    url = f"{BAP_BASE}/scopes/admin/environments/{env_id}?api-version=2021-04-01&$expand=permissions"
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_environment_admins(env_id: str) -> list[dict]:
    """List admins of an environment."""
    url = f"https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/environments/{env_id}/roleAssignments?api-version=2021-04-01"
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_environment_capacity(env_id: str) -> dict:
    """Get storage capacity usage for an environment."""
    url = f"{BAP_BASE}/scopes/admin/environments/{env_id}/capacity?api-version=2021-04-01"
    resp = requests.get(url, headers=_h(), timeout=30)
    if not resp.ok:
        return {"error": resp.text}
    return resp.json()


# ── WRITE ─────────────────────────────────────────────────────────────────────

def create_environment(display_name: str, location: str = "unitedstates",
                       sku: str = "Sandbox", currency: str = "USD", language: int = 1033) -> dict:
    """Create a new Power Platform environment."""
    url = f"{BAP_BASE}/environments?api-version=2021-04-01&retainOnProvisionFailure=false"
    payload = {
        "location": location,
        "properties": {
            "displayName": display_name,
            "environmentSku": sku,
            "databaseType": "CommonDataService",
            "linkedEnvironmentMetadata": {
                "baseLanguage": language,
                "currency": {"code": currency},
            },
        }
    }
    resp = requests.post(url, headers=_h(), json=payload, timeout=60)
    resp.raise_for_status()
    return {"success": True, "location": resp.headers.get("Location", ""), "response": resp.json() if resp.content else {}}


def delete_environment(env_id: str) -> dict:
    """Delete a Power Platform environment (DESTRUCTIVE)."""
    url = f"{BAP_BASE}/scopes/admin/environments/{env_id}?api-version=2021-04-01"
    resp = requests.delete(url, headers=_h(), timeout=60)
    return {"success": resp.status_code in (200, 202, 204)}


def copy_environment(source_env_id: str, target_display_name: str) -> dict:
    """Copy an environment to a new sandbox."""
    url = f"{BAP_BASE}/scopes/admin/environments/{source_env_id}/copy?api-version=2021-04-01"
    payload = {"CopyType": "FullCopy", "TargetEnvironmentName": target_display_name}
    resp = requests.post(url, headers=_h(), json=payload, timeout=60)
    return {"success": resp.status_code in (200, 202), "location": resp.headers.get("Location", "")}


def reset_environment(env_id: str, language: int = 1033, currency: str = "USD") -> dict:
    """Reset (wipe) a sandbox environment."""
    url = f"{BAP_BASE}/scopes/admin/environments/{env_id}/reset?api-version=2021-04-01"
    payload = {"ResetType": "Reset", "BaseLanguage": language, "Currency": {"code": currency}}
    resp = requests.post(url, headers=_h(), json=payload, timeout=60)
    return {"success": resp.status_code in (200, 202)}
