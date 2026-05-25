"""
tools/plugins.py — Dataverse Plugin Management
===============================================
Lists and manages plugin assemblies, steps, and trace logs.
"""

from __future__ import annotations
import requests
from tools.auth import get_dataverse_headers, get_active_env_url


def _dv_url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h():
    return get_dataverse_headers()


# ── READ ──────────────────────────────────────────────────────────────────────

def list_plugin_assemblies() -> list[dict]:
    """List all registered plugin assemblies."""
    url = _dv_url("pluginassemblies?$select=name,version,publickeytoken,isolationmode,createdon&$orderby=name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_plugin_types(assembly_id: str | None = None) -> list[dict]:
    """List plugin types, optionally filtered by assembly."""
    flt = f"?$filter=_pluginassemblyid_value eq '{assembly_id}'&" if assembly_id else "?"
    url = _dv_url(f"plugintypes{flt}$select=name,typename,friendlyname,workflowactivitygroupname&$orderby=name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_plugin_steps(plugin_type_id: str | None = None) -> list[dict]:
    """List SDK message processing steps (plugin registrations)."""
    flt = f"?$filter=_plugintypeid_value eq '{plugin_type_id}'&" if plugin_type_id else "?"
    url = _dv_url(
        f"sdkmessageprocessingsteps{flt}"
        "$select=name,mode,stage,rank,statecode,asyncautodelete,filteringattributes"
        "&$expand=sdkmessageid($select=name),plugintypeid($select=name)"
        "&$top=200&$orderby=stage"
    )
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_plugin_step_details(step_id: str) -> dict:
    """Get full details of a plugin step."""
    url = _dv_url(f"sdkmessageprocessingsteps({step_id})")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_plugin_trace_logs(top: int = 20) -> list[dict]:
    """List recent plugin execution trace logs."""
    url = _dv_url(f"plugintracelogs?$select=typename,messagename,exceptiondetails,performanceexecutionduration,createdon&$top={top}&$orderby=createdon desc")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_custom_apis() -> list[dict]:
    """List Custom API definitions."""
    url = _dv_url("customapis?$select=uniquename,displayname,description,bindingtype,isfunction,executeprivilegename&$orderby=uniquename")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_service_endpoints() -> list[dict]:
    """List Azure Service Bus / Event Hub service endpoints."""
    url = _dv_url("serviceendpoints?$select=name,connectionmode,contract,url,statecode&$orderby=name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── WRITE ─────────────────────────────────────────────────────────────────────

def enable_plugin_step(step_id: str) -> dict:
    """Enable a plugin step."""
    resp = requests.patch(_dv_url(f"sdkmessageprocessingsteps({step_id})"),
                          headers=_h(), json={"statecode": 0, "statuscode": 1}, timeout=30)
    return {"success": resp.status_code in (200, 204)}


def disable_plugin_step(step_id: str) -> dict:
    """Disable a plugin step."""
    resp = requests.patch(_dv_url(f"sdkmessageprocessingsteps({step_id})"),
                          headers=_h(), json={"statecode": 1, "statuscode": 2}, timeout=30)
    return {"success": resp.status_code in (200, 204)}


def delete_plugin_trace_logs(older_than_days: int = 7) -> dict:
    """Bulk delete plugin trace logs older than N days."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = _dv_url(f"plugintracelogs?$filter=createdon lt {cutoff}")
    resp = requests.get(url, headers=_h(), timeout=30)
    logs = resp.json().get("value", [])
    deleted = 0
    for log in logs:
        d = requests.delete(_dv_url(f"plugintracelogs({log['plugintracelogid']})"), headers=_h(), timeout=15)
        if d.status_code == 204:
            deleted += 1
    return {"deleted": deleted, "cutoff": cutoff}
