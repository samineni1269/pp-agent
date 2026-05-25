"""
tools/solution.py — Power Platform Solution Operations
=======================================================
List, create, export, import, delete, health-check, and layer-inspect solutions.
Uses pac CLI for export/import; Dataverse Web API for everything else.

All HTTP calls go through tools/_base.py for automatic retry + error detail.
"""

from __future__ import annotations
import os
import subprocess
from typing import Any

from tools.auth import get_dataverse_headers, get_active_env_url
from tools import _base as http
from tools.pp_knowledge import SOLUTION_COMPONENT_TYPES


def _url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h() -> dict:
    return get_dataverse_headers()


# ── READ ──────────────────────────────────────────────────────────────────────

def list_solutions(include_managed: bool = False) -> list[dict]:
    """List solutions in the active environment.

    Args:
        include_managed: If True, includes managed (locked) solutions.
    """
    flt = "" if include_managed else "&$filter=ismanaged eq false"
    url = _url(
        "solutions"
        "?$select=uniquename,friendlyname,version,ismanaged,solutiontype"
        f"{flt}"
        "&$orderby=friendlyname"
    )
    return http.get(url, _h()).json().get("value", [])


def get_solution_details(solution_name: str) -> dict:
    """Get full details for a solution by unique name."""
    url = _url(f"solutions?$filter=uniquename eq '{solution_name}'&$expand=publisherid")
    items = http.get(url, _h()).json().get("value", [])
    return items[0] if items else {}


def list_solution_components(solution_name: str) -> list[dict]:
    """List all components inside a solution (table, flow, form, view, web resource, etc.)."""
    sol = get_solution_details(solution_name)
    if not sol:
        return [{"error": f"Solution '{solution_name}' not found"}]
    sol_id = sol.get("solutionid")
    url = _url(
        f"msdyn_solutioncomponentsummaries"
        f"?$filter=msdyn_solutionid eq '{sol_id}'"
        "&$select=msdyn_name,msdyn_componenttype,msdyn_displayname,msdyn_objectid"
    )
    return http.get(url, _h()).json().get("value", [])


def get_unmanaged_customizations() -> list[dict]:
    """List unmanaged customisation solutions (non-default, non-managed)."""
    url = _url(
        "solutions"
        "?$filter=ismanaged eq false and uniquename ne 'Default'"
        "&$select=uniquename,friendlyname,version"
    )
    return http.get(url, _h()).json().get("value", [])


def check_solution_health(solution_name: str) -> dict:
    """Run a health summary on a solution.

    Checks:
    - Whether the solution exists
    - Component count by type
    - Whether it has a publisher with a non-default prefix (ALM best practice)
    - Whether it is managed or unmanaged

    Args:
        solution_name: Unique name of the solution.

    Returns:
        A health report dict with findings and recommendations.
    """
    sol = get_solution_details(solution_name)
    if not sol:
        return {"healthy": False, "error": f"Solution '{solution_name}' not found"}

    components = list_solution_components(solution_name)
    by_type: dict[str, int] = {}
    for comp in components:
        ctype = comp.get("msdyn_componenttype", "Unknown")
        by_type[ctype] = by_type.get(ctype, 0) + 1

    publisher = sol.get("publisherid", {})
    prefix = publisher.get("customizationprefix", "new")
    bad_prefix = prefix in ("new", "cr", "")

    findings = []
    recommendations = []

    if bad_prefix:
        findings.append(f"Publisher prefix '{prefix}' is the default — risk of collision in ALM")
        recommendations.append("Create a proper publisher with a unique prefix before building further")

    if sol.get("ismanaged"):
        findings.append("Solution is managed — cannot customise components directly")
        recommendations.append("Work in an unmanaged dev solution on top of the managed layer")

    if not components:
        findings.append("Solution is empty — no components found")

    return {
        "healthy": len(findings) == 0,
        "solution_name":   solution_name,
        "friendly_name":   sol.get("friendlyname"),
        "version":         sol.get("version"),
        "is_managed":      sol.get("ismanaged"),
        "publisher_prefix": prefix,
        "component_count": len(components),
        "components_by_type": by_type,
        "findings":        findings,
        "recommendations": recommendations,
    }


def get_solution_layers(solution_name: str) -> list[dict]:
    """Get the customisation layers for a solution's components.

    Shows which managed solution or active unmanaged layer owns each
    component — useful for debugging 'where did this change come from'.

    Args:
        solution_name: Unique name of the solution to inspect.
    """
    sol = get_solution_details(solution_name)
    if not sol:
        return [{"error": f"Solution '{solution_name}' not found"}]
    sol_id = sol.get("solutionid")

    url = _url(
        f"msdyn_solutionlayers"
        f"?$filter=_msdyn_solutionid_value eq '{sol_id}'"
        "&$select=msdyn_name,msdyn_layerorder,msdyn_componentid,msdyn_componenttype,msdyn_solutionname"
        "&$orderby=msdyn_componenttype,msdyn_layerorder"
    )
    try:
        return http.get(url, _h()).json().get("value", [])
    except Exception:
        return [{"note": "msdyn_solutionlayers entity may not be available in all environments"}]


def add_component_to_solution(
    solution_name: str,
    component_id: str,
    component_type: str | int,
    do_not_include_subcomponents: bool = False,
) -> dict:
    """Add a component to an unmanaged solution.

    Args:
        solution_name:                Unique name of the target solution.
        component_id:                 GUID of the component (e.g. table metadata ID, form ID).
        component_type:               Type name from SOLUTION_COMPONENT_TYPES or the integer code.
                                      E.g. 'Entity', 'Form', 'Workflow', 61 (WebResource).
        do_not_include_subcomponents: If True, only adds the top-level component (e.g. the
                                      table header) without its attributes/views/forms.

    Returns:
        {'success': True}
    """
    sol = get_solution_details(solution_name)
    if not sol:
        return {"error": f"Solution '{solution_name}' not found"}

    if isinstance(component_type, str):
        type_code = SOLUTION_COMPONENT_TYPES.get(component_type)
        if type_code is None:
            return {
                "error": f"Unknown component_type '{component_type}'. "
                         f"Valid names: {', '.join(SOLUTION_COMPONENT_TYPES.keys())}"
            }
    else:
        type_code = int(component_type)

    url = _url("AddSolutionComponent")
    payload = {
        "ComponentId":                 component_id,
        "ComponentType":               type_code,
        "SolutionUniqueName":          solution_name,
        "AddRequiredComponents":       False,
        "DoNotIncludeSubcomponents":   do_not_include_subcomponents,
    }
    http.post(url, _h(), json=payload)
    return {"success": True, "solution": solution_name, "component_id": component_id, "type_code": type_code}


# ── WRITE ─────────────────────────────────────────────────────────────────────

def export_solution(solution_name: str, managed: bool = False, output_dir: str = ".") -> dict:
    """Export a solution zip using pac CLI.

    Args:
        solution_name: Unique name.
        managed:       Export as managed.
        output_dir:    Directory to write the zip.
    """
    suffix = "_managed" if managed else ""
    path   = os.path.join(output_dir, f"{solution_name}{suffix}.zip")
    flags  = "--managed" if managed else ""
    cmd    = f"pac solution export --name {solution_name} --path {path} {flags}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {
        "success": result.returncode == 0,
        "path":    path,
        "stdout":  result.stdout,
        "stderr":  result.stderr,
    }


def import_solution(zip_path: str, activate_plugins: bool = True) -> dict:
    """Import a solution zip using pac CLI.

    Args:
        zip_path:         Path to the .zip file.
        activate_plugins: If True, enable plugins after import.
    """
    ap  = "--activate-plugins" if activate_plugins else ""
    cmd = f"pac solution import --path {zip_path} {ap}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {
        "success": result.returncode == 0,
        "stdout":  result.stdout,
        "stderr":  result.stderr,
    }


def publish_all_customizations() -> dict:
    """Publish all pending customisations (required after schema changes)."""
    http.post(_url("PublishAllXml"), _h(), timeout=120)
    return {"success": True}


def create_solution(
    unique_name: str,
    friendly_name: str,
    publisher_prefix: str = "new",
    version: str = "1.0.0.0",
) -> dict:
    """Create a new unmanaged solution.

    Args:
        unique_name:       Schema/unique name (no spaces).
        friendly_name:     Human-readable name.
        publisher_prefix:  Customisation prefix of the publisher.
        version:           Version string (default '1.0.0.0').
    """
    pub_url = _url(f"publishers?$filter=customizationprefix eq '{publisher_prefix}'&$select=publisherid")
    pubs = http.get(pub_url, _h()).json().get("value", [])
    if not pubs:
        return {"error": f"Publisher with prefix '{publisher_prefix}' not found — create it first"}

    payload: dict[str, Any] = {
        "uniquename":    unique_name,
        "friendlyname":  friendly_name,
        "version":       version,
        "publisherid@odata.bind": f"/publishers({pubs[0]['publisherid']})",
    }
    resp = http.post(_url("solutions"), _h(), json=payload)
    return {"success": True, "id": resp.headers.get("OData-EntityId", "")}


def delete_solution(solution_name: str) -> dict:
    """Delete an unmanaged solution (components are retained).

    ⚠️  Irreversible — the agent always asks for confirmation first.
    """
    sol = get_solution_details(solution_name)
    if not sol:
        return {"error": f"Solution '{solution_name}' not found"}
    http.delete(_url(f"solutions({sol['solutionid']})"), _h())
    return {"success": True}


def clone_solution(solution_name: str) -> dict:
    """Clone a solution using pac CLI."""
    cmd    = f"pac solution clone --name {solution_name}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {"success": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}
