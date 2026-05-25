"""
tools/ado.py — Azure DevOps Integration
=========================================
Work items, pipelines, repos via Azure DevOps REST API.
"""

from __future__ import annotations
import os
import base64
import requests

ADO_ORG  = lambda: os.getenv("ADO_ORG",  "")   # e.g. "mycompany"
ADO_PAT  = lambda: os.getenv("ADO_PAT",  "")   # Personal Access Token
ADO_PROJ = lambda: os.getenv("ADO_PROJECT", "") # e.g. "PowerPlatformDev"

ADO_BASE = lambda: f"https://dev.azure.com/{ADO_ORG()}"


def _h():
    pat = ADO_PAT()
    if not pat:
        raise RuntimeError("ADO_PAT not set in .env — create a PAT at https://dev.azure.com")
    creds = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def _check():
    if not ADO_ORG() or not ADO_PAT():
        return {"note": "ADO_ORG and ADO_PAT must be set in .env"}
    return None


# ── READ ──────────────────────────────────────────────────────────────────────

def list_work_items(project: str | None = None, assigned_to_me: bool = True, top: int = 20) -> list[dict]:
    """List active work items from Azure DevOps."""
    err = _check()
    if err: return [err]
    proj = project or ADO_PROJ()
    who  = "and [System.AssignedTo] = @me" if assigned_to_me else ""
    wiql = {"query": f"SELECT [System.Id],[System.Title],[System.State],[System.WorkItemType] FROM WorkItems WHERE [System.TeamProject] = @project AND [System.State] <> 'Closed' {who} ORDER BY [System.ChangedDate] DESC"}
    url  = f"{ADO_BASE()}/{proj}/_apis/wit/wiql?api-version=7.1&$top={top}"
    resp = requests.post(url, headers=_h(), json=wiql, timeout=30)
    resp.raise_for_status()
    ids  = [str(item["id"]) for item in resp.json().get("workItems", [])]
    if not ids:
        return []
    # Batch fetch details
    url2 = f"{ADO_BASE()}/{proj}/_apis/wit/workitemsbatch?api-version=7.1"
    payload = {"ids": [int(i) for i in ids[:50]], "fields": ["System.Id","System.Title","System.State","System.WorkItemType","System.AssignedTo"]}
    r2 = requests.post(url2, headers=_h(), json=payload, timeout=30)
    r2.raise_for_status()
    return [{"id": i["id"], "title": i["fields"]["System.Title"], "state": i["fields"]["System.State"],
             "type": i["fields"]["System.WorkItemType"]} for i in r2.json().get("value", [])]


def list_pipelines(project: str | None = None) -> list[dict]:
    """List build/release pipelines."""
    err = _check()
    if err: return [err]
    proj = project or ADO_PROJ()
    url  = f"{ADO_BASE()}/{proj}/_apis/pipelines?api-version=7.1"
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return [{"id": p["id"], "name": p["name"], "folder": p.get("folder")} for p in resp.json().get("value", [])]


def get_pipeline_runs(pipeline_id: int, project: str | None = None, top: int = 10) -> list[dict]:
    """Get recent runs for a pipeline."""
    err = _check()
    if err: return [err]
    proj = project or ADO_PROJ()
    url  = f"{ADO_BASE()}/{proj}/_apis/pipelines/{pipeline_id}/runs?api-version=7.1&$top={top}"
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return [{"id": r["id"], "state": r["state"], "result": r.get("result"),
             "createdDate": r.get("createdDate"), "finishedDate": r.get("finishedDate")}
            for r in resp.json().get("value", [])]


def list_repos(project: str | None = None) -> list[dict]:
    """List Git repositories."""
    err = _check()
    if err: return [err]
    proj = project or ADO_PROJ()
    url  = f"{ADO_BASE()}/{proj}/_apis/git/repositories?api-version=7.1"
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return [{"id": r["id"], "name": r["name"], "defaultBranch": r.get("defaultBranch"),
             "remoteUrl": r.get("remoteUrl")} for r in resp.json().get("value", [])]


def list_open_prs(project: str | None = None, top: int = 20) -> list[dict]:
    """List open pull requests."""
    err = _check()
    if err: return [err]
    proj = project or ADO_PROJ()
    url  = f"{ADO_BASE()}/{proj}/_apis/git/pullrequests?searchCriteria.status=active&$top={top}&api-version=7.1"
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return [{"id": p["pullRequestId"], "title": p["title"], "createdBy": p.get("createdBy", {}).get("displayName"),
             "targetBranch": p.get("targetRefName", "")} for p in resp.json().get("value", [])]


# ── WRITE ─────────────────────────────────────────────────────────────────────

def create_work_item(title: str, work_item_type: str = "Task",
                     project: str | None = None, description: str | None = None) -> dict:
    """Create a new work item."""
    err = _check()
    if err: return err
    proj = project or ADO_PROJ()
    url  = f"{ADO_BASE()}/{proj}/_apis/wit/workitems/${work_item_type}?api-version=7.1"
    ops  = [{"op": "add", "path": "/fields/System.Title", "value": title}]
    if description:
        ops.append({"op": "add", "path": "/fields/System.Description", "value": description})
    h = dict(_h())
    h["Content-Type"] = "application/json-patch+json"
    resp = requests.post(url, headers=h, json=ops, timeout=30)
    resp.raise_for_status()
    return {"success": True, "id": resp.json().get("id"), "url": resp.json().get("url")}


def run_pipeline(pipeline_id: int, project: str | None = None, branch: str = "main") -> dict:
    """Trigger a pipeline run."""
    err = _check()
    if err: return err
    proj = project or ADO_PROJ()
    url  = f"{ADO_BASE()}/{proj}/_apis/pipelines/{pipeline_id}/runs?api-version=7.1"
    payload = {"resources": {"repositories": {"self": {"refName": f"refs/heads/{branch}"}}}}
    resp = requests.post(url, headers=_h(), json=payload, timeout=30)
    resp.raise_for_status()
    return {"success": True, "run_id": resp.json().get("id"), "state": resp.json().get("state")}
