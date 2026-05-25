"""
tools/copilot.py — Copilot Studio (Power Virtual Agents) Management
=====================================================================
Uses Dataverse + Power Virtual Agents API.
"""

from __future__ import annotations
import requests
from tools.auth import get_dataverse_headers, get_active_env_url


def _dv_url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h():
    return get_dataverse_headers()


# ── READ ──────────────────────────────────────────────────────────────────────

def list_copilot_agents() -> list[dict]:
    """List all Copilot Studio agents (bots)."""
    url = _dv_url("bots?$select=name,botid,publishedon,statecode,statuscode,modifiedon&$orderby=name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_agent_details(bot_id: str) -> dict:
    """Get full details of an agent."""
    url = _dv_url(f"bots({bot_id})")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_agent_topics(bot_id: str) -> list[dict]:
    """List topics for a Copilot Studio agent."""
    url = _dv_url(f"conversationtranscripts?$filter=_bot_value eq '{bot_id}'&$select=name,statecode")
    # Use botcomponents for topics
    url = _dv_url(f"botcomponents?$filter=_parentbotid_value eq '{bot_id}'&$select=name,componenttype,statecode&$top=100")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_agent_publishing_status(bot_id: str) -> dict:
    """Check publishing status of an agent."""
    url = _dv_url(f"bots({bot_id})?$select=name,publishedon,statecode,statuscode")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "name":       data.get("name"),
        "published":  data.get("publishedon"),
        "state":      "Active" if data.get("statecode") == 0 else "Inactive",
        "status":     data.get("statuscode"),
    }


def list_agent_sessions(bot_id: str, top: int = 20) -> list[dict]:
    """List recent conversation sessions."""
    url = _dv_url(f"conversationtranscripts?$filter=_bot_value eq '{bot_id}'&$select=name,createdon,statecode&$top={top}&$orderby=createdon desc")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── WRITE ─────────────────────────────────────────────────────────────────────

def publish_agent(bot_id: str) -> dict:
    """Publish a Copilot Studio agent."""
    url = _dv_url(f"bots({bot_id})/Microsoft.Dynamics.CRM.pvaPublish")
    resp = requests.post(url, headers=_h(), json={}, timeout=60)
    return {"success": resp.status_code in (200, 202, 204)}


def enable_agent(bot_id: str) -> dict:
    """Enable an agent (set state to Active)."""
    resp = requests.patch(_dv_url(f"bots({bot_id})"), headers=_h(),
                          json={"statecode": 0, "statuscode": 1}, timeout=30)
    return {"success": resp.status_code in (200, 204)}


def disable_agent(bot_id: str) -> dict:
    """Disable an agent."""
    resp = requests.patch(_dv_url(f"bots({bot_id})"), headers=_h(),
                          json={"statecode": 1, "statuscode": 2}, timeout=30)
    return {"success": resp.status_code in (200, 204)}
