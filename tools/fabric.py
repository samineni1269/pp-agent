"""
tools/fabric.py — Microsoft Fabric Operations
==============================================
Uses Microsoft Fabric REST API (fabric.microsoft.com).
"""

from __future__ import annotations
import requests
from tools.auth import get_token

FABRIC_SCOPE = ["https://api.fabric.microsoft.com/.default"]
FABRIC_BASE  = "https://api.fabric.microsoft.com/v1"


def _h():
    token = get_token(FABRIC_SCOPE)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }


# ── READ ──────────────────────────────────────────────────────────────────────

def list_fabric_workspaces() -> list[dict]:
    """List Microsoft Fabric workspaces."""
    resp = requests.get(f"{FABRIC_BASE}/workspaces", headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_workspace_items(workspace_id: str, item_type: str | None = None) -> list[dict]:
    """List items in a Fabric workspace."""
    flt = f"?type={item_type}" if item_type else ""
    resp = requests.get(f"{FABRIC_BASE}/workspaces/{workspace_id}/items{flt}", headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_lakehouses(workspace_id: str) -> list[dict]:
    """List lakehouses in a workspace."""
    return list_workspace_items(workspace_id, "Lakehouse")


def list_notebooks(workspace_id: str) -> list[dict]:
    """List notebooks in a workspace."""
    return list_workspace_items(workspace_id, "Notebook")


def list_pipelines(workspace_id: str) -> list[dict]:
    """List data pipelines in a workspace."""
    return list_workspace_items(workspace_id, "DataPipeline")


def list_capacities() -> list[dict]:
    """List Fabric capacities."""
    resp = requests.get(f"{FABRIC_BASE}/capacities", headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_lakehouse_details(workspace_id: str, lakehouse_id: str) -> dict:
    """Get details of a lakehouse."""
    resp = requests.get(f"{FABRIC_BASE}/workspaces/{workspace_id}/lakehouses/{lakehouse_id}", headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── WRITE ─────────────────────────────────────────────────────────────────────

def create_lakehouse(workspace_id: str, display_name: str) -> dict:
    """Create a new lakehouse in a workspace."""
    payload = {"displayName": display_name}
    resp = requests.post(f"{FABRIC_BASE}/workspaces/{workspace_id}/lakehouses",
                         headers=_h(), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def run_notebook(workspace_id: str, notebook_id: str) -> dict:
    """Run a notebook (trigger execution)."""
    resp = requests.post(f"{FABRIC_BASE}/workspaces/{workspace_id}/notebooks/{notebook_id}/jobs/instances",
                         headers=_h(), json={"executionData": {}}, timeout=30)
    return {"success": resp.status_code in (200, 202), "location": resp.headers.get("Location", "")}
