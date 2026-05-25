"""
tools/pages.py — Power Pages (Portal) Management
==================================================
Uses Dataverse Web API to inspect portal configuration.
"""

from __future__ import annotations
import requests
from tools.auth import get_dataverse_headers, get_active_env_url


def _dv_url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h():
    return get_dataverse_headers()


# ── READ ──────────────────────────────────────────────────────────────────────

def list_portal_sites() -> list[dict]:
    """List Power Pages portal sites."""
    url = _dv_url("mspp_websites?$select=mspp_name,mspp_websiteid,mspp_primarydomainname,statecode&$orderby=mspp_name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_portal_details(website_id: str) -> dict:
    """Get details of a portal site."""
    url = _dv_url(f"mspp_websites({website_id})")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_web_templates(website_id: str | None = None) -> list[dict]:
    """List web templates."""
    flt = f"?$filter=_mspp_websiteid_value eq '{website_id}'" if website_id else ""
    url = _dv_url(f"mspp_webtemplates{flt}&$select=mspp_name,mspp_webtplateid,modifiedon&$top=100")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_web_roles(website_id: str | None = None) -> list[dict]:
    """List web roles for a portal."""
    flt = f"?$filter=_mspp_websiteid_value eq '{website_id}'" if website_id else ""
    url = _dv_url(f"mspp_webroles{flt}&$select=mspp_name,mspp_webroleid,mspp_authenticatedusersrole,mspp_anonymoususersrole")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_web_pages(website_id: str | None = None, top: int = 50) -> list[dict]:
    """List web pages in the portal."""
    flt = f"$filter=_mspp_websiteid_value eq '{website_id}'&" if website_id else ""
    url = _dv_url(f"mspp_webpages?{flt}$select=mspp_name,mspp_partialurl,statecode&$top={top}&$orderby=mspp_name")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_table_permissions(website_id: str | None = None) -> list[dict]:
    """List table permissions configured for the portal."""
    flt = f"?$filter=_mspp_websiteid_value eq '{website_id}'" if website_id else ""
    url = _dv_url(f"mspp_entitypermissions{flt}&$select=mspp_entityname,mspp_scope,mspp_read,mspp_write,mspp_create,mspp_delete")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_site_settings(website_id: str | None = None) -> list[dict]:
    """List portal site settings."""
    flt = f"$filter=_mspp_websiteid_value eq '{website_id}'&" if website_id else ""
    url = _dv_url(f"mspp_sitesettings?{flt}$select=mspp_name,mspp_value&$top=100")
    resp = requests.get(url, headers=_h(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── WRITE ─────────────────────────────────────────────────────────────────────

def create_web_page(website_id: str, name: str, partial_url: str) -> dict:
    """Create a new web page in the portal."""
    payload = {
        "mspp_name": name,
        "mspp_partialurl": partial_url,
        "_mspp_websiteid_value": website_id,
    }
    resp = requests.post(_dv_url("mspp_webpages"), headers=_h(), json=payload, timeout=30)
    resp.raise_for_status()
    return {"success": True, "id": resp.headers.get("OData-EntityId", "")}


def update_site_setting(setting_id: str, value: str) -> dict:
    """Update a portal site setting."""
    resp = requests.patch(_dv_url(f"mspp_sitesettings({setting_id})"),
                          headers=_h(), json={"mspp_value": value}, timeout=30)
    return {"success": resp.status_code in (200, 204)}
