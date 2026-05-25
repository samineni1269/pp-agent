"""
tools/powerbi.py — Power BI REST API Operations
================================================
Uses Power BI REST API v1.0 with Azure AD token.
"""

from __future__ import annotations
import os
import requests
from tools.auth import get_token

PBI_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
PBI_BASE  = "https://api.powerbi.com/v1.0/myorg"


def _h():
    token = get_token(PBI_SCOPE)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }


# ── READ ──────────────────────────────────────────────────────────────────────

def list_workspaces() -> list[dict]:
    """List Power BI workspaces (groups)."""
    resp = requests.get(f"{PBI_BASE}/groups?$top=100", headers=_h(), timeout=30)
    resp.raise_for_status()
    return [{"id": g["id"], "name": g["name"], "type": g.get("type"),
             "isOnDedicatedCapacity": g.get("isOnDedicatedCapacity")}
            for g in resp.json().get("value", [])]


def list_reports(workspace_id: str | None = None) -> list[dict]:
    """List reports in a workspace (or My Workspace)."""
    base = f"{PBI_BASE}/groups/{workspace_id}/reports" if workspace_id else f"{PBI_BASE}/reports"
    resp = requests.get(base, headers=_h(), timeout=30)
    resp.raise_for_status()
    return [{"id": r["id"], "name": r["name"], "datasetId": r.get("datasetId"),
             "webUrl": r.get("webUrl")} for r in resp.json().get("value", [])]


def list_datasets(workspace_id: str | None = None) -> list[dict]:
    """List datasets in a workspace."""
    base = f"{PBI_BASE}/groups/{workspace_id}/datasets" if workspace_id else f"{PBI_BASE}/datasets"
    resp = requests.get(base, headers=_h(), timeout=30)
    resp.raise_for_status()
    return [{"id": d["id"], "name": d["name"], "configuredBy": d.get("configuredBy"),
             "isRefreshable": d.get("isRefreshable"), "isOnPremGatewayRequired": d.get("isOnPremGatewayRequired")}
            for d in resp.json().get("value", [])]


def list_dashboards(workspace_id: str | None = None) -> list[dict]:
    """List dashboards in a workspace."""
    base = f"{PBI_BASE}/groups/{workspace_id}/dashboards" if workspace_id else f"{PBI_BASE}/dashboards"
    resp = requests.get(base, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_dataset_refresh_history(workspace_id: str, dataset_id: str) -> list[dict]:
    """Get refresh history for a dataset."""
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/refreshes?$top=10"
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── WRITE ─────────────────────────────────────────────────────────────────────

def refresh_dataset(workspace_id: str, dataset_id: str) -> dict:
    """Trigger a dataset refresh."""
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/refreshes"
    resp = requests.post(url, headers=_h(), json={}, timeout=30)
    return {"success": resp.status_code == 202, "status_code": resp.status_code}


def create_workspace(name: str) -> dict:
    """Create a new Power BI workspace."""
    resp = requests.post(f"{PBI_BASE}/groups", headers=_h(), json={"name": name}, timeout=30)
    resp.raise_for_status()
    return resp.json()
