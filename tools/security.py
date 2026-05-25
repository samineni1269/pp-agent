"""
tools/security.py — Security Roles, DLP Policies, Users, Field Security
========================================================================
Manage Dataverse security roles, user assignments, field-level security,
and DLP policy inspection.

All HTTP calls go through tools/_base.py for automatic retry + error detail.
"""

from __future__ import annotations
from typing import Any

from tools.auth import get_dataverse_headers, get_active_env_url, get_management_headers
from tools import _base as http
from tools.pp_knowledge import PRIVILEGE_LEVELS, PRIVILEGE_ACTIONS

BAP_BASE = "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform"


def _url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h() -> dict:
    return get_dataverse_headers()


def _mh() -> dict:
    return get_management_headers()


# ── SECURITY ROLES — READ ─────────────────────────────────────────────────────

def list_security_roles(business_unit_id: str | None = None) -> list[dict]:
    """List security roles, optionally scoped to a business unit."""
    flt = f"&$filter=_businessunitid_value eq '{business_unit_id}'" if business_unit_id else ""
    url = _url(f"roles?$select=name,roleid,iscustomizable,createdon{flt}&$orderby=name&$top=200")
    return http.get(url, _h()).json().get("value", [])


def get_role_privileges(role_id: str) -> list[dict]:
    """Get all privileges assigned to a security role.

    Returns a list of privileges with their names and access rights.
    """
    url = _url(f"roles({role_id})/roleprivileges_association?$select=name,accessright")
    return http.get(url, _h()).json().get("value", [])


def list_role_members(role_id: str) -> list[dict]:
    """List users currently assigned to a security role."""
    url = _url(f"roles({role_id})/systemusers_association?$select=fullname,domainname,isdisabled")
    return http.get(url, _h()).json().get("value", [])


def list_users(top: int = 100) -> list[dict]:
    """List active system users in the environment."""
    url = _url(
        f"systemusers"
        "?$select=fullname,domainname,businessunitid,isdisabled,accessmode"
        "&$filter=isdisabled eq false"
        f"&$top={top}"
        "&$orderby=fullname"
    )
    return http.get(url, _h()).json().get("value", [])


def get_user_roles(user_id: str) -> list[dict]:
    """Get security roles currently assigned to a user."""
    url = _url(f"systemusers({user_id})/systemuserroles_association?$select=name,roleid")
    return http.get(url, _h()).json().get("value", [])


def list_business_units() -> list[dict]:
    """List all business units in the environment."""
    url = _url("businessunits?$select=name,businessunitid,parentbusinessunitid&$orderby=name")
    return http.get(url, _h()).json().get("value", [])


# ── SECURITY ROLES — WRITE ────────────────────────────────────────────────────

def create_security_role(name: str, business_unit_id: str) -> dict:
    """Create a new security role in the specified business unit.

    Args:
        name:             Display name for the new role.
        business_unit_id: GUID of the business unit this role belongs to.
    """
    payload: dict[str, Any] = {
        "name": name,
        "businessunitid@odata.bind": f"/businessunits({business_unit_id})",
    }
    resp = http.post(_url("roles"), _h(), json=payload)
    return {"success": True, "id": resp.headers.get("OData-EntityId", "")}


def clone_security_role(source_role_id: str, new_name: str, business_unit_id: str) -> dict:
    """Clone an existing security role with all its privileges.

    Creates a new role, copies all privileges from the source, and assigns
    them to the new role. This is the closest equivalent to 'Copy Role'
    via the Dataverse Web API.

    Args:
        source_role_id:   GUID of the role to clone.
        new_name:         Name for the cloned role.
        business_unit_id: GUID of the target business unit.

    Returns:
        {'success': True, 'new_role_id': ..., 'privileges_copied': N}
    """
    # Step 1: Create blank role
    new_role_result = create_security_role(new_name, business_unit_id)
    new_role_url = new_role_result.get("id", "")
    if not new_role_url:
        return {"error": "Failed to create the cloned role", "detail": new_role_result}

    # Extract GUID from OData-EntityId URL
    new_role_id = new_role_url.split("roles(")[-1].rstrip(")")

    # Step 2: Get source role's privileges
    source_privs = get_role_privileges(source_role_id)

    # Step 3: Assign same privileges to new role
    if source_privs:
        result = set_table_privileges(
            role_id=new_role_id,
            # Pass raw privilege list — each priv has 'privilegeid' from the API
            # We'll use the AddPrivilegesRole action
            privileges=[
                {
                    "Depth": _access_right_to_depth(p.get("accessright", 0)),
                    "PrivilegeId": p.get("privilegeid"),
                }
                for p in source_privs
                if p.get("privilegeid")
            ],
        )
    else:
        result = {"note": "Source role had no privileges to copy"}

    return {
        "success":          True,
        "new_role_id":      new_role_id,
        "new_role_name":    new_name,
        "privileges_copied": len(source_privs),
        "assign_result":    result,
    }


def _access_right_to_depth(access_right: int) -> str:
    """Convert an accessright integer to a depth string for AddPrivilegesRole."""
    mapping = {0: "None", 1: "Basic", 2: "Local", 4: "Deep", 8: "Global"}
    return mapping.get(access_right, "None")


def set_table_privileges(
    role_id: str,
    table_name: str | None = None,
    privileges: list[dict] | None = None,
    create_access: int = 0,
    read_access: int = 0,
    write_access: int = 0,
    delete_access: int = 0,
    append_access: int = 0,
    append_to_access: int = 0,
) -> dict:
    """Set CRUD privileges for a table on a security role.

    Two modes:
    1. Pass `table_name` + individual access level params (0-8) — recommended for simple use.
    2. Pass `privileges` directly — a list of {'PrivilegeId': ..., 'Depth': ...} dicts.

    Access levels: 0=None, 1=User, 2=BusinessUnit, 4=ParentChildBU, 8=Organization

    Args:
        role_id:          GUID of the security role to update.
        table_name:       Logical name of the entity (e.g. 'account').
                          Required for mode 1.
        privileges:       Pre-built privilege list for mode 2 (bypasses other params).
        create_access:    Create privilege level (0-8).
        read_access:      Read privilege level (0-8).
        write_access:     Write privilege level (0-8).
        delete_access:    Delete privilege level (0-8).
        append_access:    Append privilege level (0-8).
        append_to_access: AppendTo privilege level (0-8).

    Returns:
        {'success': True} or {'error': ...}
    """
    # Mode 2: caller passed explicit privilege list
    if privileges is not None:
        url = _url(f"roles({role_id})/Microsoft.Dynamics.CRM.AddPrivilegesRole")
        payload = {"Privileges": privileges}
        http.post(url, _h(), json=payload)
        return {"success": True, "privileges_set": len(privileges)}

    # Mode 1: look up privilege GUIDs by entity + action name
    if not table_name:
        return {"error": "Either table_name or privileges must be provided"}

    access_map = {
        "prvCreate":   create_access,
        "prvRead":     read_access,
        "prvWrite":    write_access,
        "prvDelete":   delete_access,
        "prvAppend":   append_access,
        "prvAppendTo": append_to_access,
    }

    depth_map = {0: "None", 1: "Basic", 2: "Local", 4: "Deep", 8: "Global"}

    # Fetch privilege IDs for this entity
    priv_url = _url(
        f"privileges?$filter=accessright ne null"
        f" and contains(name,'{table_name.capitalize()}')"
        "&$select=privilegeid,name,accessright"
        "&$top=50"
    )
    privs = http.get(priv_url, _h()).json().get("value", [])
    priv_map = {p["name"]: p["privilegeid"] for p in privs}

    role_privs = []
    for action_name, level in access_map.items():
        if level == 0:
            continue
        # Privilege name pattern: prvCreateAccount, prvReadAccount, etc.
        cap = table_name.capitalize()
        full_name = f"{action_name}{cap}"
        pid = priv_map.get(full_name)
        if pid:
            role_privs.append({
                "Depth": depth_map.get(level, "None"),
                "PrivilegeId": pid,
            })

    if not role_privs:
        return {
            "warning": f"No matching privileges found for table '{table_name}'. "
                       "Verify the entity logical name is correct.",
            "searched_names": list(access_map.keys()),
        }

    url = _url(f"roles({role_id})/Microsoft.Dynamics.CRM.AddPrivilegesRole")
    http.post(url, _h(), json={"Privileges": role_privs})
    return {"success": True, "table": table_name, "privileges_set": len(role_privs)}


def assign_role_to_user(user_id: str, role_id: str) -> dict:
    """Assign a security role to a user."""
    url     = _url(f"systemusers({user_id})/systemuserroles_association/$ref")
    payload = {"@odata.id": _url(f"roles({role_id})")}
    http.post(url, _h(), json=payload)
    return {"success": True, "user_id": user_id, "role_id": role_id}


def remove_role_from_user(user_id: str, role_id: str) -> dict:
    """Remove a security role from a user.

    Args:
        user_id: GUID of the system user.
        role_id: GUID of the role to remove.
    """
    url = _url(
        f"systemusers({user_id})/systemuserroles_association({role_id})/$ref"
    )
    http.delete(url, _h())
    return {"success": True, "user_id": user_id, "role_id": role_id, "removed": True}


# ── FIELD SECURITY ────────────────────────────────────────────────────────────

def list_field_security_profiles() -> list[dict]:
    """List all field security profiles (column-level security) in the environment.

    Field security profiles control which users can read/write specific columns
    that are marked as secured (field security enabled).

    Returns a list of profiles with their IDs and names.
    """
    url = _url("fieldsecurityprofiles?$select=name,fieldsecurityprofileid,description&$orderby=name")
    return http.get(url, _h()).json().get("value", [])


def get_field_security_profile_permissions(profile_id: str) -> list[dict]:
    """Get the column permissions granted by a field security profile.

    Args:
        profile_id: GUID of the field security profile.
    """
    url = _url(
        f"fieldpermissions"
        f"?$filter=_fieldsecurityprofileid_value eq '{profile_id}'"
        "&$select=attributelogicalname,entityname,canread,canupdate,cancreate"
    )
    return http.get(url, _h()).json().get("value", [])


def assign_field_security_profile_to_user(profile_id: str, user_id: str) -> dict:
    """Assign a field security profile to a user.

    Args:
        profile_id: GUID of the field security profile.
        user_id:    GUID of the system user.
    """
    url     = _url(f"fieldsecurityprofiles({profile_id})/systemusers_association/$ref")
    payload = {"@odata.id": _url(f"systemusers({user_id})")}
    http.post(url, _h(), json=payload)
    return {"success": True, "profile_id": profile_id, "user_id": user_id}


# ── DLP POLICIES ──────────────────────────────────────────────────────────────

def list_dlp_policies() -> list[dict]:
    """List DLP policies in the tenant."""
    url      = f"{BAP_BASE}/apiPolicies?api-version=2016-11-01"
    policies = http.get(url, _mh()).json().get("value", [])
    return [
        {
            "id":          p["name"],
            "displayName": p["properties"].get("displayName"),
            "created":     p["properties"].get("createdTime"),
            "type":        p["properties"].get("environmentType"),
        }
        for p in policies
    ]


def get_dlp_policy_details(policy_id: str) -> dict:
    """Get full details of a DLP policy including connector classifications."""
    url = f"{BAP_BASE}/apiPolicies/{policy_id}?api-version=2016-11-01"
    return http.get(url, _mh()).json()
