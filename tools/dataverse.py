"""
tools/dataverse.py — Dataverse Web API Operations
==================================================
CRUD, FetchXML, schema inspection, table/column creation, relationships,
bulk operations, global choices, record counts.

All HTTP calls go through tools/_base.py for automatic retry + error detail.
"""

from __future__ import annotations
import json
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from tools.auth import get_dataverse_headers, get_active_env_url
from tools import _base as http
from tools.pp_knowledge import COLUMN_TYPES, get_plural


# ── Helpers ───────────────────────────────────────────────────────────────────

def _url(path: str) -> str:
    return f"{get_active_env_url().rstrip('/')}/api/data/v9.2/{path}"


def _h() -> dict:
    return get_dataverse_headers()


def _label(text: str, lang: int = 1033) -> dict:
    """Build a Dataverse LocalizedLabel object."""
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.Label",
        "LocalizedLabels": [
            {
                "@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                "Label": text,
                "LanguageCode": lang,
            }
        ],
    }


# ── READ ──────────────────────────────────────────────────────────────────────

def list_tables(custom_only: bool = False) -> list[dict]:
    """List Dataverse tables (entities).

    Args:
        custom_only: If True, return only custom (non-OOB) tables.
    """
    flt = "$filter=iscustomentity eq true&" if custom_only else ""
    url = _url(
        f"EntityDefinitions?{flt}"
        "$select=LogicalName,DisplayName,Description,IsCustomEntity,TableType"
        "&$orderby=LogicalName"
    )
    return http.get(url, _h()).json().get("value", [])


def get_table_schema(table_name: str) -> dict:
    """Get full schema + attributes for a table."""
    url = _url(f"EntityDefinitions(LogicalName='{table_name}')?$expand=Attributes")
    return http.get(url, _h()).json()


def list_columns(table_name: str) -> list[dict]:
    """List all columns for a table with type and required-level info."""
    url = _url(
        f"EntityDefinitions(LogicalName='{table_name}')/Attributes"
        "?$select=LogicalName,DisplayName,AttributeType,RequiredLevel,IsCustomAttribute"
    )
    return http.get(url, _h()).json().get("value", [])


def query_records(
    table: str,
    select: str | None = None,
    filter_query: str | None = None,
    order_by: str | None = None,
    top: int = 50,
    expand: str | None = None,
) -> list[dict]:
    """Query records using OData.

    Args:
        table:        OData plural collection name (e.g. 'accounts', 'opportunities').
        select:       Comma-separated column list.
        filter_query: OData $filter expression.
        order_by:     OData $orderby expression.
        top:          Maximum records to return.
        expand:       OData $expand expression for related records.
    """
    params = [f"$top={top}"]
    if select:       params.append(f"$select={select}")
    if filter_query: params.append(f"$filter={filter_query}")
    if order_by:     params.append(f"$orderby={order_by}")
    if expand:       params.append(f"$expand={expand}")
    url = _url(f"{table}?{'&'.join(params)}")
    return http.get(url, _h()).json().get("value", [])


def get_record(table: str, record_id: str, select: str | None = None) -> dict:
    """Get a single record by GUID."""
    qs = f"?$select={select}" if select else ""
    return http.get(_url(f"{table}({record_id}){qs}"), _h()).json()


def run_fetchxml(fetchxml: str) -> list[dict]:
    """Execute a FetchXML query against the correct entity collection.

    Automatically derives the OData collection name using pp_knowledge.get_plural()
    so that e.g. 'opportunity' correctly maps to 'opportunities' (not 'opportunitys').
    """
    root = ET.fromstring(fetchxml)
    entity_el = root.find(".//entity")
    if entity_el is None:
        return [{"error": "Could not determine entity from FetchXML — missing <entity> element"}]
    logical_name = entity_el.get("name", "")
    plural = get_plural(logical_name)
    encoded = urllib.parse.quote(fetchxml)
    url = _url(f"{plural}?fetchXml={encoded}")
    return http.get(url, _h()).json().get("value", [])


def get_record_count(table: str, filter_query: str | None = None) -> int:
    """Return the total count of records in a table (optionally filtered).

    Uses $count endpoint which returns an integer.
    """
    qs = f"?$filter={urllib.parse.quote(filter_query)}" if filter_query else ""
    url = _url(f"{table}/$count{qs}")
    headers = {**_h(), "OData-MaxVersion": "4.0", "OData-Version": "4.0", "Accept": "application/json"}
    resp = http.get(url, headers)
    try:
        return int(resp.text.strip())
    except ValueError:
        return resp.json().get("@odata.count", 0)


def list_choices(table_name: str) -> list[dict]:
    """List all choice/picklist columns for a table."""
    url = _url(
        f"EntityDefinitions(LogicalName='{table_name}')/Attributes"
        "/Microsoft.Dynamics.CRM.PicklistAttributeMetadata"
        "?$select=LogicalName,DisplayName"
    )
    return http.get(url, _h()).json().get("value", [])


def list_global_choices() -> list[dict]:
    """List all global (reusable) option sets."""
    url = _url("GlobalOptionSetDefinitions?$select=Name,DisplayName,IsCustomOptionSet")
    return http.get(url, _h()).json().get("value", [])


def search_records(entity: str, search_term: str, fields: str | None = None) -> list[dict]:
    """Full-text relevance search across a table.

    Args:
        entity:      Logical entity name (e.g. 'account').
        search_term: Free-text search string.
        fields:      Comma-separated column names to return.
    """
    url = _url("search/query")
    payload: dict[str, Any] = {
        "search": search_term,
        "entities": [
            {
                "name": entity,
                "selectcolumns": fields.split(",") if fields else [],
            }
        ],
        "count": True,
        "top": 25,
    }
    return http.post(url, _h(), json=payload).json().get("value", [])


# ── WRITE — Records ───────────────────────────────────────────────────────────

def create_record(table: str, data: dict) -> dict:
    """Create a new record.

    Args:
        table: OData collection name (e.g. 'accounts').
        data:  Dict of field values.

    Returns:
        {'success': True, 'id': '<guid>'}
    """
    resp = http.post(_url(table), _h(), json=data)
    entity_id_header = resp.headers.get("OData-EntityId", "")
    # Extract GUID from URL like .../accounts(xxxxxxxx-...)
    record_id = ""
    if "(" in entity_id_header:
        record_id = entity_id_header.split("(")[-1].rstrip(")")
    return {"success": True, "id": record_id, "location": entity_id_header}


def update_record(table: str, record_id: str, data: dict) -> dict:
    """PATCH (partial update) an existing record."""
    http.patch(_url(f"{table}({record_id})"), _h(), json=data)
    return {"success": True}


def delete_record(table: str, record_id: str) -> dict:
    """Delete a record by GUID."""
    http.delete(_url(f"{table}({record_id})"), _h())
    return {"success": True}


def upsert_record(
    table: str,
    alternate_key_name: str,
    alternate_key_value: str,
    data: dict,
) -> dict:
    """Upsert (create or update) by alternate key.

    Args:
        table:               OData collection name.
        alternate_key_name:  Logical name of the alternate key column.
        alternate_key_value: Value of the alternate key.
        data:                Fields to set.
    """
    url = _url(f"{table}({alternate_key_name}='{alternate_key_value}')")
    resp = http.patch(url, _h(), json=data)
    created = resp.status_code == 201
    return {"success": True, "created": created}


def bulk_create_records(table: str, records: list[dict]) -> dict:
    """Create multiple records using the $batch endpoint.

    Sends records in chunks of up to 100 per batch request.
    Returns a summary of successes and failures.

    Args:
        table:   OData collection name (e.g. 'accounts').
        records: List of dicts, each representing one record's field values.
    """
    base = get_active_env_url().rstrip("/")
    collection_url = f"{base}/api/data/v9.2/{table}"
    batch_url = f"{base}/api/data/v9.2/$batch"

    CHUNK = 100
    successes, failures = 0, []

    headers_base = _h()

    for chunk_start in range(0, len(records), CHUNK):
        chunk = records[chunk_start : chunk_start + CHUNK]
        boundary = f"batch_{chunk_start}"
        body_parts: list[str] = []

        for i, rec in enumerate(chunk):
            part = (
                f"--{boundary}\r\n"
                "Content-Type: application/http\r\n"
                "Content-Transfer-Encoding: binary\r\n"
                "\r\n"
                f"POST {collection_url} HTTP/1.1\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(json.dumps(rec))}\r\n"
                "\r\n"
                f"{json.dumps(rec)}\r\n"
            )
            body_parts.append(part)

        body = "\r\n".join(body_parts) + f"\r\n--{boundary}--"
        batch_headers = {
            **headers_base,
            "Content-Type": f"multipart/mixed;boundary={boundary}",
        }

        resp = http.post(batch_url, batch_headers, data=body.encode())
        resp_text = resp.text

        # Count HTTP 204/201 responses in the multipart response body
        chunk_success = resp_text.count("HTTP/1.1 201") + resp_text.count("HTTP/1.1 204")
        chunk_fail = len(chunk) - chunk_success
        successes += chunk_success
        if chunk_fail > 0:
            failures.append(f"Chunk starting at {chunk_start}: {chunk_fail} failures")

    return {
        "success": len(failures) == 0,
        "total": len(records),
        "created": successes,
        "failed": len(records) - successes,
        "failure_details": failures,
    }


# ── WRITE — Schema ────────────────────────────────────────────────────────────

def create_table(
    logical_name: str,
    display_name: str,
    plural_display_name: str,
    description: str = "",
    ownership: str = "UserOwned",
) -> dict:
    """Create a new custom Dataverse table.

    Args:
        logical_name:       Logical name (must include publisher prefix, e.g. 'cr123_project').
        display_name:       Human-readable singular name (e.g. 'Project').
        plural_display_name: Human-readable plural name (e.g. 'Projects').
        description:        Optional description.
        ownership:          'UserOwned' (default) or 'OrganizationOwned'.

    Returns:
        {'success': True, 'logical_name': ..., 'metadata_id': ...}
    """
    payload = {
        "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
        "SchemaName": logical_name,
        "LogicalName": logical_name.lower(),
        "DisplayName": _label(display_name),
        "DisplayCollectionName": _label(plural_display_name),
        "Description": _label(description),
        "OwnershipType": ownership,
        "IsActivity": False,
        "HasActivities": False,
        "HasNotes": False,
        "PrimaryNameAttribute": f"{logical_name.lower()}_name",
        "Attributes": [
            {
                "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
                "SchemaName": f"{logical_name}_name",
                "LogicalName": f"{logical_name.lower()}_name",
                "RequiredLevel": {"Value": "ApplicationRequired"},
                "MaxLength": 100,
                "FormatName": {"Value": "Text"},
                "DisplayName": _label("Name"),
                "Description": _label("Primary name column"),
            }
        ],
    }

    url = _url("EntityDefinitions")
    resp = http.post(url, _h(), json=payload)
    metadata_id = resp.headers.get("OData-EntityId", "").split("EntityDefinitions(")[-1].rstrip(")")
    return {
        "success": True,
        "logical_name": logical_name.lower(),
        "metadata_id": metadata_id,
    }


def add_column(
    table_name: str,
    logical_name: str,
    display_name: str,
    column_type: str,
    required: bool = False,
    description: str = "",
    **type_kwargs: Any,
) -> dict:
    """Add a column to an existing table.

    Args:
        table_name:   Logical name of the parent table.
        logical_name: Logical name for the new column (include publisher prefix).
        display_name: Human-readable label.
        column_type:  One of the keys in pp_knowledge.COLUMN_TYPES:
                      text, multiline_text, whole_number, decimal, currency,
                      boolean, date_only, date_time, choice, choices, lookup,
                      file, image, auto_number, float, duration, url, email, phone.
        required:     Whether the column is required.
        description:  Optional description.
        **type_kwargs: Override extra metadata for the attribute type.
                      e.g. MaxLength=200, MinValue=0, MaxValue=100.

    Returns:
        {'success': True, 'logical_name': ..., 'metadata_id': ...}
    """
    if column_type not in COLUMN_TYPES:
        return {
            "success": False,
            "error": f"Unknown column_type '{column_type}'. "
                     f"Valid types: {', '.join(COLUMN_TYPES.keys())}",
        }

    type_info = COLUMN_TYPES[column_type]
    extra: dict = {**type_info.get("extra", {}), **type_kwargs}

    payload: dict[str, Any] = {
        "@odata.type": type_info["odata_type"],
        "SchemaName": logical_name,
        "LogicalName": logical_name.lower(),
        "DisplayName": _label(display_name),
        "Description": _label(description),
        "RequiredLevel": {"Value": "ApplicationRequired" if required else "None"},
        **extra,
    }

    # Choice columns require an OptionSet definition if not using a global one
    if column_type in ("choice", "choices") and "OptionSet" not in extra:
        payload["OptionSet"] = {
            "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
            "IsGlobal": False,
            "OptionSetType": "Picklist",
            "Options": type_kwargs.get("options", [
                {
                    "Value": 100000000,
                    "Label": _label("Option 1"),
                },
                {
                    "Value": 100000001,
                    "Label": _label("Option 2"),
                },
            ]),
        }

    url = _url(f"EntityDefinitions(LogicalName='{table_name}')/Attributes")
    resp = http.post(url, _h(), json=payload)
    metadata_id = resp.headers.get("OData-EntityId", "")
    return {
        "success": True,
        "logical_name": logical_name.lower(),
        "metadata_id": metadata_id,
    }


def create_lookup_relationship(
    primary_table: str,
    related_table: str,
    lookup_column_name: str,
    lookup_display_name: str,
    schema_name: str | None = None,
) -> dict:
    """Create a many-to-one lookup relationship (adds a lookup column to related_table).

    E.g. 'each Contact belongs to one Account' →
         primary_table='account', related_table='contact',
         lookup_column_name='cr123_accountid'

    Args:
        primary_table:       The 'one' side (e.g. 'account').
        related_table:       The table that gets the lookup column (the 'many' side).
        lookup_column_name:  Logical name of the new lookup column.
        lookup_display_name: Display label for the lookup column.
        schema_name:         Relationship schema name (auto-generated if omitted).

    Returns:
        {'success': True, 'schema_name': ..., 'metadata_id': ...}
    """
    rel_schema = schema_name or f"{primary_table}_{related_table}_{lookup_column_name}"
    payload = {
        "@odata.type": "Microsoft.Dynamics.CRM.OneToManyRelationshipMetadata",
        "SchemaName": rel_schema,
        "ReferencedEntity": primary_table,
        "ReferencingEntity": related_table,
        "ReferencedAttribute": f"{primary_table}id",
        "ReferencingAttribute": lookup_column_name.lower(),
        "Lookup": {
            "@odata.type": "Microsoft.Dynamics.CRM.LookupAttributeMetadata",
            "SchemaName": lookup_column_name,
            "LogicalName": lookup_column_name.lower(),
            "DisplayName": _label(lookup_display_name),
            "RequiredLevel": {"Value": "None"},
        },
        "AssociatedMenuConfiguration": {
            "Behavior": "UseCollectionName",
            "Group": "Details",
            "Label": _label(primary_table.capitalize()),
            "Order": 10000,
        },
        "CascadeConfiguration": {
            "Assign": "NoCascade",
            "Delete": "RemoveLink",
            "Merge": "NoCascade",
            "Reparent": "NoCascade",
            "Share": "NoCascade",
            "Unshare": "NoCascade",
        },
    }

    url = _url("RelationshipDefinitions")
    resp = http.post(url, _h(), json=payload)
    metadata_id = resp.headers.get("OData-EntityId", "")
    return {"success": True, "schema_name": rel_schema, "metadata_id": metadata_id}


def create_nn_relationship(
    table_a: str,
    table_b: str,
    schema_name: str | None = None,
    intersect_entity: str | None = None,
) -> dict:
    """Create a many-to-many relationship between two tables.

    Args:
        table_a:         First entity logical name.
        table_b:         Second entity logical name.
        schema_name:     Relationship schema name (auto-generated if omitted).
        intersect_entity: Logical name for the auto-created intersect table.

    Returns:
        {'success': True, 'schema_name': ..., 'metadata_id': ...}
    """
    rel_schema = schema_name or f"{table_a}_{table_b}_nn"
    intersect = intersect_entity or f"{table_a}_{table_b}"

    payload = {
        "@odata.type": "Microsoft.Dynamics.CRM.ManyToManyRelationshipMetadata",
        "SchemaName": rel_schema,
        "Entity1LogicalName": table_a,
        "Entity2LogicalName": table_b,
        "IntersectEntityName": intersect,
        "Entity1AssociatedMenuConfiguration": {
            "Behavior": "UseCollectionName",
            "Group": "Details",
            "Label": _label(table_b.capitalize()),
            "Order": 10000,
        },
        "Entity2AssociatedMenuConfiguration": {
            "Behavior": "UseCollectionName",
            "Group": "Details",
            "Label": _label(table_a.capitalize()),
            "Order": 10000,
        },
    }

    url = _url("RelationshipDefinitions")
    resp = http.post(url, _h(), json=payload)
    metadata_id = resp.headers.get("OData-EntityId", "")
    return {"success": True, "schema_name": rel_schema, "metadata_id": metadata_id}


def create_global_choice(
    name: str,
    display_name: str,
    options: list[dict],
    description: str = "",
) -> dict:
    """Create a reusable global option set (choice) that can be shared across tables.

    Args:
        name:         Schema name (include publisher prefix, e.g. 'cr123_projectstatus').
        display_name: Human-readable label.
        options:      List of dicts: [{'value': 100000000, 'label': 'Active'}, ...]
        description:  Optional description.

    Returns:
        {'success': True, 'name': ..., 'metadata_id': ...}
    """
    option_items = [
        {
            "Value": opt["value"],
            "Label": _label(opt["label"]),
            "Description": _label(opt.get("description", "")),
        }
        for opt in options
    ]

    payload = {
        "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
        "Name": name,
        "DisplayName": _label(display_name),
        "Description": _label(description),
        "IsGlobal": True,
        "OptionSetType": "Picklist",
        "Options": option_items,
    }

    url = _url("GlobalOptionSetDefinitions")
    resp = http.post(url, _h(), json=payload)
    metadata_id = resp.headers.get("OData-EntityId", "")
    return {"success": True, "name": name, "metadata_id": metadata_id}


def list_relationships(table_name: str) -> dict:
    """List all one-to-many and many-to-many relationships for a table."""
    url = _url(
        f"EntityDefinitions(LogicalName='{table_name}')"
        "?$expand=OneToManyRelationships,ManyToOneRelationships,ManyToManyRelationships"
        "&$select=LogicalName"
    )
    data = http.get(url, _h()).json()
    return {
        "one_to_many": data.get("OneToManyRelationships", []),
        "many_to_one": data.get("ManyToOneRelationships", []),
        "many_to_many": data.get("ManyToManyRelationships", []),
    }
