"""
tools/connectors.py — Custom Connector Management
==================================================
Uses Power Platform connector API to inspect and manage custom connectors.
"""

from __future__ import annotations
import os
import requests
from tools.auth import get_management_headers

ENV_ID = lambda: os.getenv("PP_ENV_ID", "")
PP_BASE = "https://api.powerapps.com"


def _h():
    return get_management_headers()


def _pp_headers():
    from tools.auth import get_token
    token = get_token(["https://service.powerapps.com/.default"])
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }


# ── READ ──────────────────────────────────────────────────────────────────────

def list_custom_connectors() -> list[dict]:
    """List custom connectors in the environment."""
    eid = ENV_ID()
    if not eid:
        return [{"note": "PP_ENV_ID not set in .env"}]
    url = f"{PP_BASE}/providers/Microsoft.PowerApps/apis?api-version=2016-11-01&$filter=environment/name eq '{eid}' and isCustomApi eq true"
    resp = requests.get(url, headers=_pp_headers(), timeout=30)
    resp.raise_for_status()
    apis = resp.json().get("value", [])
    return [{"id": a["name"], "displayName": a["properties"]["displayName"],
             "iconUri": a["properties"].get("iconUri"),
             "created": a["properties"].get("createdTime")} for a in apis]


def get_connector_definition(connector_id: str) -> dict:
    """Get full definition (OpenAPI spec) for a custom connector."""
    url = f"{PP_BASE}/providers/Microsoft.PowerApps/apis/{connector_id}?api-version=2016-11-01&$expand=connectionParameters,swagger"
    resp = requests.get(url, headers=_pp_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_connector_connections(connector_id: str) -> list[dict]:
    """List connections to a custom connector."""
    eid = ENV_ID()
    if not eid:
        return [{"note": "PP_ENV_ID not set"}]
    url = f"{PP_BASE}/providers/Microsoft.PowerApps/apis/{connector_id}/connections?api-version=2016-11-01&environment={eid}"
    resp = requests.get(url, headers=_pp_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_connector_operations(connector_id: str) -> list[dict]:
    """List all operations (actions/triggers) of a connector."""
    defn = get_connector_definition(connector_id)
    swagger = defn.get("properties", {}).get("swagger", {})
    paths   = swagger.get("paths", {})
    ops = []
    for path, methods in paths.items():
        for method, op in methods.items():
            ops.append({
                "path":      path,
                "method":    method.upper(),
                "operationId": op.get("operationId"),
                "summary":     op.get("summary"),
            })
    return ops


# ── WRITE ─────────────────────────────────────────────────────────────────────

def create_connector_from_swagger(swagger: dict, display_name: str) -> dict:
    """Create a new custom connector from an OpenAPI/Swagger definition."""
    eid = ENV_ID()
    if not eid:
        return {"error": "PP_ENV_ID not set"}
    url = f"{PP_BASE}/providers/Microsoft.PowerApps/apis?api-version=2016-11-01"
    payload = {
        "properties": {
            "displayName": display_name,
            "swagger": swagger,
            "environment": {"name": eid},
        }
    }
    resp = requests.post(url, headers=_pp_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def update_connector_swagger(connector_id: str, swagger: dict) -> dict:
    """Update the OpenAPI definition of an existing connector."""
    url = f"{PP_BASE}/providers/Microsoft.PowerApps/apis/{connector_id}?api-version=2016-11-01"
    payload = {"properties": {"swagger": swagger}}
    resp = requests.patch(url, headers=_pp_headers(), json=payload, timeout=30)
    return {"success": resp.status_code in (200, 204)}


def delete_connector(connector_id: str) -> dict:
    """Delete a custom connector."""
    url = f"{PP_BASE}/providers/Microsoft.PowerApps/apis/{connector_id}?api-version=2016-11-01"
    resp = requests.delete(url, headers=_pp_headers(), timeout=30)
    return {"success": resp.status_code in (200, 204)}
