"""
tools/flows.py — Power Automate Cloud Flows
============================================
List, inspect, create, enable/disable, run, and delete cloud flows.
Includes failing-flow scanner and action-level error detail retrieval.

All HTTP calls go through tools/_base.py for automatic retry + error detail.
"""

from __future__ import annotations
import copy
import json
import os
from typing import Any

from tools.auth import get_flow_headers
from tools import _base as http
from tools.pp_knowledge import FLOW_TEMPLATES


ENV_ID  = lambda: os.getenv("PP_ENV_ID", "")
_API    = "2016-11-01"


def _base_url() -> str | None:
    eid = ENV_ID()
    if not eid:
        return None
    return (
        f"https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple"
        f"/environments/{eid}"
    )


def _h() -> dict:
    return get_flow_headers()


def _no_env() -> list | dict:
    return {"error": "PP_ENV_ID is not set in .env — add it to enable Power Automate tools"}


# ── READ ──────────────────────────────────────────────────────────────────────

def list_flows(top: int = 100) -> list[dict]:
    """List cloud flows in the active environment.

    Returns a condensed list: id, displayName, state, trigger type.
    """
    base = _base_url()
    if not base:
        return [_no_env()]
    url = f"{base}/flows?api-version={_API}&$top={top}"
    flows = http.get(url, _h()).json().get("value", [])
    result = []
    for f in flows:
        props = f.get("properties", {})
        defn  = props.get("definition", {})
        triggers = list(defn.get("triggers", {}).keys()) if defn else []
        result.append({
            "id":          f["name"],
            "displayName": props.get("displayName", ""),
            "state":       props.get("state", ""),
            "trigger":     triggers[0] if triggers else "unknown",
            "modified":    props.get("lastModifiedTime", ""),
        })
    return result


def get_flow_details(flow_id: str) -> dict:
    """Get full definition and metadata for a specific flow."""
    base = _base_url()
    if not base:
        return _no_env()
    url = f"{base}/flows/{flow_id}?api-version={_API}&$expand=definition"
    return http.get(url, _h()).json()


def get_flow_run_history(flow_id: str, top: int = 20) -> list[dict]:
    """Get recent run history for a flow."""
    base = _base_url()
    if not base:
        return [_no_env()]
    url = f"{base}/flows/{flow_id}/runs?api-version={_API}&$top={top}"
    runs = http.get(url, _h()).json().get("value", [])
    return [
        {
            "id":        r["name"],
            "status":    r["properties"]["status"],
            "startTime": r["properties"].get("startTime"),
            "endTime":   r["properties"].get("endTime"),
            "duration":  r["properties"].get("duration"),
        }
        for r in runs
    ]


def list_failed_runs(flow_id: str, top: int = 50) -> list[dict]:
    """Return only the Failed runs from a flow's history."""
    runs = get_flow_run_history(flow_id, top=top)
    return [r for r in runs if r.get("status") == "Failed"]


def get_flow_run_errors(flow_id: str, run_id: str) -> dict:
    """Get action-level error detail for a specific failed run.

    Returns each action's status and error code/message so you can
    pinpoint exactly which step failed and why.

    Args:
        flow_id: Flow GUID or name.
        run_id:  Run name (from get_flow_run_history → 'id' field).
    """
    base = _base_url()
    if not base:
        return _no_env()
    url = f"{base}/flows/{flow_id}/runs/{run_id}?api-version={_API}&$expand=properties/actions"
    data = http.get(url, _h()).json()
    actions = data.get("properties", {}).get("actions", {})

    failed_actions = []
    all_actions = []
    for name, detail in actions.items():
        status = detail.get("status", "")
        entry: dict[str, Any] = {
            "action":  name,
            "status":  status,
            "started": detail.get("startTime"),
            "ended":   detail.get("endTime"),
        }
        if "error" in detail:
            entry["error_code"]    = detail["error"].get("code")
            entry["error_message"] = detail["error"].get("message")
        if status == "Failed":
            failed_actions.append(entry)
        all_actions.append(entry)

    return {
        "run_id":         run_id,
        "flow_id":        flow_id,
        "failed_actions": failed_actions,
        "all_actions":    all_actions,
        "total_actions":  len(all_actions),
        "failed_count":   len(failed_actions),
    }


def get_failing_flows(top: int = 100) -> list[dict]:
    """Scan all flows in the environment and return those with recent failures.

    For each flow that has ≥1 failed run in the last 20 runs, includes:
    - flow id, name, state
    - count of recent failures
    - the most recent failed run's start time

    Useful for a health dashboard / 'show me what's broken' prompt.
    """
    all_flows = list_flows(top=top)
    failing = []

    for flow in all_flows:
        if isinstance(flow, dict) and "error" in flow:
            return [flow]   # propagate auth error
        flow_id = flow["id"]
        try:
            history = get_flow_run_history(flow_id, top=20)
            failed  = [r for r in history if r.get("status") == "Failed"]
            if failed:
                failing.append({
                    "id":              flow_id,
                    "displayName":     flow.get("displayName", ""),
                    "state":           flow.get("state", ""),
                    "failed_runs":     len(failed),
                    "last_failure":    failed[0].get("startTime"),
                    "last_run_id":     failed[0].get("id"),
                })
        except Exception as exc:
            failing.append({
                "id":          flow_id,
                "displayName": flow.get("displayName", ""),
                "error":       f"Could not fetch runs: {exc}",
            })

    return failing or [{"message": "No failing flows found — all flows appear healthy"}]


def list_flow_connections(flow_id: str) -> list[dict]:
    """List connection references used by a flow."""
    details = get_flow_details(flow_id)
    conns = details.get("properties", {}).get("connectionReferences", {})
    return [
        {
            "key":           k,
            "displayName":   v.get("displayName"),
            "connectorName": v.get("connector", {}).get("name"),
            "connectionId":  v.get("connection", {}).get("id"),
        }
        for k, v in conns.items()
    ]


# ── WRITE ─────────────────────────────────────────────────────────────────────

def create_flow(
    display_name: str,
    trigger_type: str,
    actions: dict | None = None,
    description: str = "",
    **trigger_kwargs: Any,
) -> dict:
    """Create a new cloud flow.

    Args:
        display_name:   Human-readable name for the flow.
        trigger_type:   One of: 'recurrence', 'http_trigger', 'dataverse_create',
                        'dataverse_update'. Maps to FLOW_TEMPLATES keys.
        actions:        Dict of action definitions to include in the flow body.
                        If None, creates a minimal flow with no actions (a skeleton).
        description:    Optional description.
        **trigger_kwargs: Override trigger parameters. E.g. for 'recurrence':
                          frequency='Week', interval=1
                          for 'dataverse_create': entityname='contact'

    Returns:
        {'success': True, 'flow_id': ..., 'displayName': ...}

    Example — daily recurrence flow:
        create_flow(
            display_name='Daily Cleanup',
            trigger_type='recurrence',
            frequency='Day',
            interval=1,
        )

    Example — Dataverse create trigger:
        create_flow(
            display_name='On Account Created',
            trigger_type='dataverse_create',
            entityname='account',
            actions={
                'Send_notification': {
                    'type': 'OpenApiConnection',
                    'inputs': {
                        'host': {'connectionName': 'shared_office365', 'operationId': 'SendEmailV2'},
                        'parameters': {
                            'emailMessage/To': 'admin@org.com',
                            'emailMessage/Subject': 'New Account Created',
                            'emailMessage/Body': '<p>A new account has been added.</p>',
                        },
                    },
                    'runAfter': {},
                }
            },
        )
    """
    base = _base_url()
    if not base:
        return _no_env()

    if trigger_type not in FLOW_TEMPLATES:
        return {
            "error": (
                f"Unknown trigger_type '{trigger_type}'. "
                f"Valid types: {', '.join(FLOW_TEMPLATES.keys())}"
            )
        }

    # Deep-copy the trigger template so we don't mutate the shared dict
    template_triggers = copy.deepcopy(FLOW_TEMPLATES[trigger_type]["trigger"])

    # Apply caller overrides (e.g. frequency, entityname)
    _apply_trigger_overrides(template_triggers, trigger_type, trigger_kwargs)

    flow_definition: dict[str, Any] = {
        "$schema":             "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion":      "1.0.0.0",
        "parameters":          {},
        "triggers":            template_triggers,
        "actions":             actions or {},
        "outputs":             {},
    }

    payload: dict[str, Any] = {
        "properties": {
            "displayName": display_name,
            "description": description,
            "definition":  flow_definition,
            "state":       "Enabled",
        }
    }

    url = f"{base}/flows?api-version={_API}"
    resp = http.post(url, _h(), json=payload)
    data = resp.json()
    flow_id = data.get("name", "")
    return {
        "success":     True,
        "flow_id":     flow_id,
        "displayName": display_name,
        "state":       data.get("properties", {}).get("state", ""),
    }


def _apply_trigger_overrides(triggers: dict, trigger_type: str, overrides: dict) -> None:
    """Mutate a trigger dict to apply caller-supplied overrides."""
    if trigger_type == "recurrence":
        trig = triggers.get("Recurrence", {})
        rec  = trig.setdefault("recurrence", {})
        if "frequency" in overrides: rec["frequency"] = overrides["frequency"]
        if "interval"  in overrides: rec["interval"]  = overrides["interval"]
        if "startTime" in overrides: rec["startTime"] = overrides["startTime"]
        if "timeZone"  in overrides: rec["timeZone"]  = overrides["timeZone"]

    elif trigger_type in ("dataverse_create", "dataverse_update"):
        trig_key = list(triggers.keys())[0]
        params = triggers[trig_key].setdefault("inputs", {}).setdefault("parameters", {})
        if "entityname" in overrides:
            params["subscriptionRequest/entityname"] = overrides["entityname"]
        if "scope" in overrides:
            params["subscriptionRequest/scope"] = overrides["scope"]


def enable_flow(flow_id: str) -> dict:
    """Enable (turn on) a cloud flow."""
    base = _base_url()
    if not base:
        return _no_env()
    url = f"{base}/flows/{flow_id}/start?api-version={_API}"
    http.post(url, _h())
    return {"success": True, "flow_id": flow_id, "state": "Enabled"}


def disable_flow(flow_id: str) -> dict:
    """Disable (turn off) a cloud flow."""
    base = _base_url()
    if not base:
        return _no_env()
    url = f"{base}/flows/{flow_id}/stop?api-version={_API}"
    http.post(url, _h())
    return {"success": True, "flow_id": flow_id, "state": "Disabled"}


def delete_flow(flow_id: str) -> dict:
    """Permanently delete a cloud flow.

    ⚠️  This is irreversible. The agent will always ask for confirmation before calling this.

    Args:
        flow_id: Flow GUID or name.
    """
    base = _base_url()
    if not base:
        return _no_env()
    url = f"{base}/flows/{flow_id}?api-version={_API}"
    http.delete(url, _h())
    return {"success": True, "flow_id": flow_id, "deleted": True}


def run_flow(flow_id: str, trigger_body: dict | None = None) -> dict:
    """Trigger a manual/HTTP-triggered flow run.

    Args:
        flow_id:      Flow GUID or name.
        trigger_body: Optional JSON body to pass to the trigger.
    """
    base = _base_url()
    if not base:
        return _no_env()
    url = f"{base}/flows/{flow_id}/triggers/manual/run?api-version={_API}"
    resp = http.post(url, _h(), json=trigger_body or {})
    return {"success": True, "status_code": resp.status_code}
