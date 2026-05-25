"""
tools/crm.py — Dynamics 365 CRM Operations
============================================
Sales, Customer Service, common CRM entities via Dataverse Web API.
"""

from __future__ import annotations
import requests
from tools.auth import get_dataverse_headers, get_active_env_url
from datetime import datetime, timedelta


def _dv_url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h():
    return get_dataverse_headers()


# ── SALES ─────────────────────────────────────────────────────────────────────

def list_opportunities(stage: str | None = None, top: int = 50) -> list[dict]:
    """List opportunities, optionally filtered by stage name."""
    flt = f"&$filter=contains(stepname,'{stage}')" if stage else ""
    url = _dv_url(f"opportunities?$select=name,estimatedvalue,closedate,statecode,stepname,_ownerid_value{flt}&$orderby=closedate&$top={top}")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_leads(status: str = "open", top: int = 50) -> list[dict]:
    """List leads by status."""
    flt = "$filter=statecode eq 0&" if status == "open" else ""
    url = _dv_url(f"leads?{flt}$select=fullname,companyname,emailaddress1,leadsourcecode,statecode&$top={top}&$orderby=createdon desc")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_accounts(top: int = 50) -> list[dict]:
    """List accounts."""
    url = _dv_url(f"accounts?$select=name,accountnumber,telephone1,emailaddress1,revenue&$top={top}&$orderby=name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_contacts(account_id: str | None = None, top: int = 50) -> list[dict]:
    """List contacts, optionally for an account."""
    flt = f"?$filter=_parentcustomerid_value eq '{account_id}'&" if account_id else "?"
    url = _dv_url(f"contacts{flt}$select=fullname,emailaddress1,jobtitle,telephone1&$top={top}")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_pipeline_summary() -> dict:
    """Get a summary of the sales pipeline."""
    opps = list_opportunities(top=500)
    total_value = sum(float(o.get("estimatedvalue") or 0) for o in opps)
    stages: dict[str, int] = {}
    for o in opps:
        s = o.get("stepname") or "Unknown"
        stages[s] = stages.get(s, 0) + 1
    return {
        "total_opportunities": len(opps),
        "total_estimated_value": total_value,
        "by_stage": stages,
    }


# ── CUSTOMER SERVICE ──────────────────────────────────────────────────────────

def list_cases(status: str = "active", top: int = 50) -> list[dict]:
    """List service cases."""
    flt = "$filter=statecode eq 0&" if status == "active" else ""
    url = _dv_url(f"incidents?{flt}$select=title,ticketnumber,statecode,prioritycode,createdon,_ownerid_value&$top={top}&$orderby=createdon desc")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_queues() -> list[dict]:
    """List queues."""
    url = _dv_url("queues?$select=name,queueid,queuetypecode&$orderby=name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── WRITE ─────────────────────────────────────────────────────────────────────

def create_contact(firstname: str, lastname: str, email: str | None = None,
                    job_title: str | None = None, phone: str | None = None) -> dict:
    """Create a new contact."""
    payload: dict = {"firstname": firstname, "lastname": lastname}
    if email:     payload["emailaddress1"] = email
    if job_title: payload["jobtitle"]      = job_title
    if phone:     payload["telephone1"]    = phone
    resp = requests.post(_dv_url("contacts"), headers=_h(), json=payload, timeout=30)
    resp.raise_for_status()
    return {"success": True, "id": resp.headers.get("OData-EntityId", "")}


def create_lead(topic: str, firstname: str, lastname: str,
                company: str | None = None, email: str | None = None) -> dict:
    """Create a new lead."""
    payload: dict = {"subject": topic, "firstname": firstname, "lastname": lastname}
    if company: payload["companyname"]  = company
    if email:   payload["emailaddress1"] = email
    resp = requests.post(_dv_url("leads"), headers=_h(), json=payload, timeout=30)
    resp.raise_for_status()
    return {"success": True, "id": resp.headers.get("OData-EntityId", "")}


def create_opportunity(name: str, estimated_value: float,
                        close_date: str | None = None, account_id: str | None = None) -> dict:
    """Create a new opportunity."""
    close = close_date or (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    payload: dict = {"name": name, "estimatedvalue": estimated_value, "estimatedclosedate": close}
    if account_id:
        payload["customerid_account@odata.bind"] = f"/accounts({account_id})"
    resp = requests.post(_dv_url("opportunities"), headers=_h(), json=payload, timeout=30)
    resp.raise_for_status()
    return {"success": True, "id": resp.headers.get("OData-EntityId", "")}


def create_case(title: str, customer_id: str | None = None,
                priority: int = 2, description: str | None = None) -> dict:
    """Create a new service case (incident). Priority: 1=High, 2=Normal, 3=Low."""
    payload: dict = {"title": title, "prioritycode": priority}
    if description:  payload["description"] = description
    if customer_id:
        payload["customerid_account@odata.bind"] = f"/accounts({customer_id})"
    resp = requests.post(_dv_url("incidents"), headers=_h(), json=payload, timeout=30)
    resp.raise_for_status()
    return {"success": True, "id": resp.headers.get("OData-EntityId", "")}
