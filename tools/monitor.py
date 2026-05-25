"""
tools/monitor.py — Environment Health, Plugins, API Usage, Audit Logs
======================================================================
Monitoring and observability tools for Power Platform.
Covers: connectivity checks, plugin assembly health, flow error summaries,
system job monitoring, audit logs, and storage consumption.

All HTTP calls go through tools/_base.py for automatic retry + error detail.
"""

from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone

from tools.auth import get_dataverse_headers, get_active_env_url, get_management_headers, get_flow_headers
from tools import _base as http

BAP_BASE = "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform"


def _url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h() -> dict:
    return get_dataverse_headers()


def _mh() -> dict:
    return get_management_headers()


# ── CONNECTIVITY & API HEALTH ─────────────────────────────────────────────────

def check_environment_health() -> dict:
    """Check basic connectivity and API health of the active environment.

    Runs a WhoAmI call to confirm auth + Dataverse API reachability.
    Returns the authenticated user ID and environment name.
    """
    try:
        url  = _url("WhoAmI")
        data = http.get(url, _h(), timeout=10).json()
        return {
            "ok":            True,
            "user_id":       data.get("UserId"),
            "org_id":        data.get("OrganizationId"),
            "env_url":       get_active_env_url(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "env_url": get_active_env_url()}


def get_api_usage(env_id: str | None = None, days: int = 7) -> dict:
    """Get API call usage metrics for an environment (admin required).

    Args:
        env_id: Power Platform environment ID (falls back to PP_ENV_ID in .env).
        days:   Look-back window in days.
    """
    eid = env_id or os.getenv("PP_ENV_ID", "")
    if not eid:
        return {"note": "PP_ENV_ID not set — add it to .env to enable API usage metrics"}
    url = f"{BAP_BASE}/scopes/admin/environments/{eid}/analytics?api-version=2021-04-01"
    resp = http.get(url, _mh())
    return resp.json()


def get_storage_consumption() -> dict:
    """Get rough record-count figures for common tables as a storage proxy.

    Uses RetrieveTotalRecordCount — fast but approximate.
    For authoritative capacity figures, use the Power Platform Admin Center.
    """
    url = _url(
        "RetrieveTotalRecordCount(EntityNames=@p1)"
        "?@p1=['account','contact','incident','opportunity','email','task','annotation']"
    )
    try:
        data = http.get(url, _h()).json()
        counts = {item["EntityName"]: item["Count"] for item in data.get("EntityRecordCountCollection", {}).get("Values", [])}
        return {"success": True, "record_counts": counts, "note": "Approximate — use PPAC for accurate storage capacity"}
    except Exception as exc:
        return {"note": f"Could not retrieve counts: {exc}. Use PPAC for storage details."}


# ── PLUGIN HEALTH ─────────────────────────────────────────────────────────────

def check_plugin_health() -> dict:
    """Check the health of plugin assemblies and their processing steps.

    Inspects:
    - All registered plugin assemblies (isolationMode, isCustomizable)
    - All SDK message processing steps — their state (enabled/disabled)
    - Steps with execution mode Async (asyncoperations issues)
    - Any steps in broken state (stage != expected)

    Returns a health summary with counts and any warnings.
    """
    # Assemblies
    try:
        asm_url = _url(
            "pluginassemblies"
            "?$select=name,isolationmode,ismanaged,createdon,solutionid"
            "&$filter=ishidden/Value eq false"
            "&$top=100"
        )
        assemblies = http.get(asm_url, _h()).json().get("value", [])
    except Exception as exc:
        return {"error": f"Failed to fetch plugin assemblies: {exc}"}

    # Steps
    try:
        step_url = _url(
            "sdkmessageprocessingsteps"
            "?$select=name,statecode,statuscode,stage,mode,rank,asyncautodelete"
            "&$filter=ishidden/Value eq false"
            "&$top=500"
        )
        steps = http.get(step_url, _h()).json().get("value", [])
    except Exception as exc:
        return {"assemblies": len(assemblies), "error": f"Failed to fetch plugin steps: {exc}"}

    enabled_steps   = [s for s in steps if s.get("statecode") == 0]
    disabled_steps  = [s for s in steps if s.get("statecode") == 1]
    async_steps     = [s for s in steps if s.get("mode") == 1]   # 1 = Async
    sync_steps      = [s for s in steps if s.get("mode") == 0]   # 0 = Synchronous

    # Isolation mode: 1=None (sandbox off), 2=Sandbox
    non_sandboxed = [a for a in assemblies if a.get("isolationmode") == 1]

    warnings = []
    if non_sandboxed:
        warnings.append(
            f"{len(non_sandboxed)} assemblies run outside sandbox isolation — "
            "they can access the network and disk freely (security risk in production)."
        )
    if disabled_steps:
        names = ", ".join(s.get("name", "?") for s in disabled_steps[:5])
        warnings.append(f"{len(disabled_steps)} plugin steps are disabled: {names}...")

    return {
        "healthy":          len(warnings) == 0,
        "assemblies":       len(assemblies),
        "non_sandboxed":    len(non_sandboxed),
        "steps_total":      len(steps),
        "steps_enabled":    len(enabled_steps),
        "steps_disabled":   len(disabled_steps),
        "steps_async":      len(async_steps),
        "steps_sync":       len(sync_steps),
        "warnings":         warnings,
        "assembly_names":   [a.get("name") for a in assemblies[:20]],
    }


def list_plugin_steps(assembly_name: str | None = None) -> list[dict]:
    """List plugin processing steps, optionally filtered to one assembly.

    Args:
        assembly_name: Filter by assembly name (partial match). If None, returns all steps.
    """
    flt = ""
    if assembly_name:
        flt = f"&$filter=contains(name,'{assembly_name}')"
    url = _url(
        "sdkmessageprocessingsteps"
        "?$select=name,statecode,stage,mode,rank,asyncautodelete,sdkmessageid"
        f"{flt}"
        "&$orderby=name"
        "&$top=200"
    )
    return http.get(url, _h()).json().get("value", [])


# ── FLOW MONITORING ──────────────────────────────────────────────────────────

def get_flow_error_summary() -> dict:
    """High-level summary of flow states across the environment.

    Returns counts of Enabled / Disabled / Suspended flows.
    Suspended flows have encountered repeated errors and been auto-disabled by the platform.

    Requires PP_ENV_ID in .env.
    """
    eid = os.getenv("PP_ENV_ID", "")
    if not eid:
        return {"note": "PP_ENV_ID not set — add it to .env to enable flow monitoring"}

    url  = (
        f"https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple"
        f"/environments/{eid}/flows?api-version=2016-11-01&$top=100"
    )
    try:
        flows     = http.get(url, get_flow_headers()).json().get("value", [])
        enabled   = [f for f in flows if f.get("properties", {}).get("state") == "Started"]
        disabled  = [f for f in flows if f.get("properties", {}).get("state") == "Stopped"]
        suspended = [f for f in flows if f.get("properties", {}).get("state") == "Suspended"]
        return {
            "total_flows":      len(flows),
            "enabled":          len(enabled),
            "disabled":         len(disabled),
            "suspended":        len(suspended),
            "suspended_names":  [f["properties"]["displayName"] for f in suspended[:10]],
            "disabled_names":   [f["properties"]["displayName"] for f in disabled[:10]],
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_failing_flows_detail() -> list[dict]:
    """Detailed failing flow report — delegates to flows.get_failing_flows().

    Provides per-flow failure counts and most-recent failed run IDs so the
    agent can follow up with flows.get_flow_run_errors() for root-cause detail.
    """
    from tools.flows import get_failing_flows
    return get_failing_flows()


# ── AUDIT LOGS ────────────────────────────────────────────────────────────────

def list_audit_logs(days_back: int = 1, top: int = 50) -> list[dict]:
    """List recent Dataverse audit log entries.

    Args:
        days_back: How many days back to retrieve entries.
        top:       Maximum number of entries to return.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = _url(
        f"audits"
        f"?$filter=createdon ge {cutoff}"
        "&$select=createdon,operation,action,objecttypecode,userid"
        f"&$top={top}"
        "&$orderby=createdon desc"
    )
    return http.get(url, _h()).json().get("value", [])


def get_entity_change_history(entity_name: str, record_id: str) -> list[dict]:
    """Get change history for a specific record from audit logs.

    Args:
        entity_name: Logical entity name (e.g. 'account').
        record_id:   GUID of the record.
    """
    url = _url(
        f"audits"
        f"?$filter=objectid eq '{record_id}' and objecttypecode eq '{entity_name}'"
        "&$top=50"
        "&$orderby=createdon desc"
    )
    return http.get(url, _h()).json().get("value", [])


# ── ASYNC OPERATIONS ─────────────────────────────────────────────────────────

def list_system_jobs(top: int = 20, failed_only: bool = False) -> list[dict]:
    """List async system jobs (background operations like imports, exports, bulk deletes).

    Args:
        top:         Maximum entries to return.
        failed_only: If True, return only failed/cancelled jobs.
    """
    flt = "&$filter=statuscode eq 31 or statuscode eq 32" if failed_only else ""  # 31=Failed, 32=Cancelled
    url = _url(
        f"asyncoperations"
        "?$select=name,operationtype,statuscode,starttime,createdon,message"
        f"{flt}"
        f"&$top={top}"
        "&$orderby=createdon desc"
    )
    return http.get(url, _h()).json().get("value", [])
