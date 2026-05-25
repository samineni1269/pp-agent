"""
Power Platform Agent — agent.py
================================
Multi-LLM autonomous agent for all Power Platform operations.

Default LLM: MiniMax M2.7 (Token Plan)
Also supports: Claude, GPT-4o, Gemini, OpenRouter
Switch model at runtime via the UI model picker or LLM_PROVIDER in .env

Run:
    python agent.py           — interactive chat
    python agent.py briefing  — one-shot org health briefing
"""

import os
import sys
import json
import time
import datetime
import threading
import concurrent.futures
from dotenv import load_dotenv

load_dotenv()

from tools.llm_provider import call_llm, detect_provider, get_active_model, MODEL_OPTIONS
from tools.auth import get_auth_status, get_active_environment
from tools.pp_knowledge import REFERENCE as PP_KNOWLEDGE_REFERENCE

# ══════════════════════════════════════════════════════════════════════════════
#  ENV CONTEXT CACHE  — fetched once per session start, injected into prompt
# ══════════════════════════════════════════════════════════════════════════════

_ENV_CONTEXT_CACHE: dict[str, str] = {}   # key: env_url → summary string
_ENV_CONTEXT_LOCK  = threading.Lock()


def _safe_tool_call(module_name: str, fn_name: str, args: dict | None = None):
    """Import a tool module and call a function, returning None on failure."""
    try:
        import importlib
        m = importlib.import_module(f"tools.{module_name}")
        return getattr(m, fn_name)(**(args or {}))
    except Exception:
        return None


def _fetch_env_context(env_url: str) -> str:
    """
    Fetch a quick live snapshot of the environment for the system prompt.
    Runs three parallel reads (health + solutions + custom tables).
    Safe to call on every session start — result is cached per env_url.
    """
    with _ENV_CONTEXT_LOCK:
        if env_url in _ENV_CONTEXT_CACHE:
            return _ENV_CONTEXT_CACHE[env_url]

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            f_health = pool.submit(_safe_tool_call, "monitor",   "check_environment_health", {})
            f_sols   = pool.submit(_safe_tool_call, "solution",  "list_solutions", {"include_managed": False})
            f_tables = pool.submit(_safe_tool_call, "dataverse", "list_tables",    {"custom_only": True})
            health = f_health.result(timeout=10)
            sols   = f_sols.result(timeout=10)
            tables = f_tables.result(timeout=10)

        lines = []
        if isinstance(health, dict) and health.get("ok"):
            lines.append("✅ API reachable and authenticated")
        elif isinstance(health, dict) and "error" in health:
            lines.append(f"⚠️ API issue: {health['error']}")

        if isinstance(sols, list) and sols:
            names = [s.get("friendlyname") or s.get("uniquename", "?") for s in sols[:10]]
            lines.append(f"Unmanaged solutions ({len(sols)}): {', '.join(names)}")

        if isinstance(tables, list) and tables:
            names = [t.get("logicalname", "?") for t in tables[:20]]
            lines.append(f"Custom tables ({len(tables)}): {', '.join(names)}")

        ctx = "\n".join(lines)
    except Exception:
        ctx = ""

    with _ENV_CONTEXT_LOCK:
        _ENV_CONTEXT_CACHE[env_url] = ctx
    return ctx


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL RESULT COMPRESSOR  — strips noise before the LLM sees the output
# ══════════════════════════════════════════════════════════════════════════════

_COMPRESS_KEEP: dict[str, set[str]] = {
    "list_solutions":         {"uniquename", "friendlyname", "version", "ismanaged", "solutiontype"},
    "list_tables":            {"logicalname", "displayname", "ismanaged", "iscustomizable", "iscustom"},
    "list_flows":             {"name", "id", "properties"},
    "list_security_roles":    {"name", "roleid", "iscustomizable"},
    "list_audit_logs":        {"createdon", "operation", "action", "objecttypecode"},
    "list_environments":      {"displayName", "name", "id", "properties"},
    "list_canvas_apps":       {"name", "id", "displayName", "lastModifiedTime"},
    "list_custom_connectors": {"name", "id", "displayName"},
    "list_plugins":           {"name", "pluginassemblyid", "isolationmode"},
    "list_business_units":    {"name", "businessunitid"},
    "list_users":             {"fullname", "domainname", "isdisabled"},
    "get_table_columns":      {"logicalname", "displayname", "attributetype", "requiredlevel", "ismanaged"},
}
_MAX_LIST_ITEMS = 25


def _compress_result(tool_name: str, raw: str) -> str:
    """
    Reduce large tool outputs before the LLM receives them.
    - Strips irrelevant OData fields
    - Caps list results at _MAX_LIST_ITEMS items
    - Hard-truncates non-JSON blobs at 5 000 chars
    """
    if not raw:
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        # Not JSON — hard truncate
        return raw[:5000] + (" ...[truncated]" if len(raw) > 5000 else "")

    keep = _COMPRESS_KEEP.get(tool_name)

    # OData-style {"value": [...]} response
    if isinstance(data, dict) and isinstance(data.get("value"), list):
        total  = len(data["value"])
        items  = data["value"][:_MAX_LIST_ITEMS]
        if keep:
            items = [{k: v for k, v in item.items() if k in keep} for item in items]
        out = {"value": items}
        if total > _MAX_LIST_ITEMS:
            out["_note"] = f"Showing {_MAX_LIST_ITEMS} of {total} items"
        return json.dumps(out)

    # Plain list
    if isinstance(data, list):
        total = len(data)
        items = data[:_MAX_LIST_ITEMS]
        if keep:
            items = [
                {k: v for k, v in item.items() if k in keep} if isinstance(item, dict) else item
                for item in items
            ]
        if total > _MAX_LIST_ITEMS:
            items.append({"_note": f"… {total - _MAX_LIST_ITEMS} more items omitted"})
        return json.dumps(items)

    # Dict — ensure not too long
    out = json.dumps(data, default=str)
    if len(out) > 8000:
        return out[:8000] + " ...[truncated]"
    return out


try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.markdown import Markdown
    from rich.prompt import Prompt, Confirm
    console = Console()
    RICH = True
except ImportError:
    RICH = False
    class _FallbackConsole:
        def print(self, *a, **kw): print(*a)
    console = _FallbackConsole()


# ══════════════════════════════════════════════════════════════════════════════
#  READ vs WRITE TOOL SETS
#  READ  → execute immediately, no confirmation
#  WRITE → always show preview + require "yes" before executing
# ══════════════════════════════════════════════════════════════════════════════

READ_TOOLS = {
    # Solutions
    "list_solutions", "get_solution_components", "get_solution_history",
    "check_solution_health", "get_solution_layers", "list_solution_components",
    # Dataverse
    "list_tables", "get_table_columns", "get_table_relationships",
    "list_table_forms", "list_table_views", "query_records", "run_fetchxml",
    "get_record", "list_choices", "get_record_count", "list_global_choices",
    "search_records", "list_relationships",
    # Canvas Apps
    "list_canvas_apps", "get_canvas_app_details", "get_canvas_app_users",
    # Model-Driven Apps
    "list_model_driven_apps", "get_mda_sitemap", "get_mda_components",
    # Power Automate
    "list_flows", "get_flow_details", "get_flow_runs", "get_flow_errors",
    "get_flow_connections", "list_flow_connections", "get_flow_run_history",
    "list_failed_runs", "get_failing_flows", "get_flow_run_errors",
    # Copilot Studio
    "list_agents", "get_agent_topics", "get_agent_analytics",
    # Power Pages
    "list_portals", "get_portal_pages", "get_portal_table_permissions",
    "get_portal_web_roles", "list_web_templates",
    # Power BI
    "list_powerbi_workspaces", "list_powerbi_reports", "list_powerbi_datasets",
    "get_dataset_refresh_history", "get_report_details",
    # Fabric
    "list_fabric_workspaces", "list_capacities", "list_lakehouses",
    "list_notebooks", "list_pipelines",
    # Dataverse (data layer)
    "list_plugins", "get_plugin_steps", "get_plugin_details",
    # D365 CRM
    "list_leads", "list_opportunities", "list_accounts", "list_contacts",
    "get_crm_record", "search_crm", "get_crm_pipeline",
    # Custom Connectors
    "list_custom_connectors", "get_connector_details", "get_connector_actions",
    # Environments
    "list_environments", "get_environment_details", "get_environment_capacity",
    "list_environment_makers",
    # Security
    "list_security_roles", "get_role_privileges", "get_user_roles", "list_role_members",
    "list_system_admins", "list_business_units", "list_users",
    "list_field_security_profiles", "get_field_security_profile_permissions",
    # DLP
    "list_dlp_policies", "get_dlp_policy_details", "check_connector_dlp_status",
    # Monitor
    "get_failing_flows", "get_audit_log", "get_api_call_stats",
    "get_solution_checker_results", "get_dataverse_capacity",
    "get_connector_health", "check_environment_health", "check_plugin_health",
    "get_failing_flows_detail", "list_plugin_steps", "list_system_jobs",
    "list_audit_logs", "get_entity_change_history",
    # ALM
    "list_alm_pipelines", "get_pipeline_runs",
    # Azure DevOps
    "list_ado_repos", "list_ado_pipelines", "get_ado_pipeline_runs",
    "get_ado_pull_requests",
    # Knowledge
    "search_knowledge_base", "get_memory_summary",
}

WRITE_TOOLS = {
    # Solutions
    "export_solution", "import_solution", "publish_solution",
    "create_solution", "delete_solution", "clone_solution",
    "add_component_to_solution", "remove_component_from_solution",
    "run_alm_pipeline", "upgrade_solution",
    # Dataverse schema
    "create_table", "update_table", "delete_table",
    "create_column", "add_column", "update_column", "delete_column",
    "create_relationship", "create_lookup_relationship", "create_nn_relationship",
    "create_global_choice",
    # Dataverse records
    "create_record", "update_record", "delete_record", "upsert_record",
    "bulk_create_records",
    # Canvas Apps
    "export_canvas_app", "share_canvas_app", "publish_canvas_app",
    # Flows
    "enable_flow", "disable_flow", "trigger_flow", "run_flow",
    "create_flow", "delete_flow", "repair_flow_connections",
    # Copilot Studio
    "create_agent_topic", "publish_agent", "delete_agent_topic",
    "add_knowledge_source",
    # Power Pages
    "publish_portal", "update_table_permission", "create_web_role",
    # Power BI
    "trigger_dataset_refresh", "create_powerbi_workspace",
    # Environments
    "create_environment", "delete_environment", "copy_environment",
    "assign_environment_admin",
    # Security
    "assign_role_to_user", "remove_role_from_user",
    "create_security_role", "clone_security_role", "update_security_role",
    "set_table_privileges", "assign_field_security_profile_to_user",
    # Solutions (write)
    "add_component_to_solution",
    # DLP
    "create_dlp_policy", "update_dlp_policy", "delete_dlp_policy",
    # Plugins
    "register_plugin", "update_plugin_step", "disable_plugin_step",
    # D365 CRM
    "create_crm_record", "update_crm_record", "assign_crm_record",
    # Azure DevOps
    "trigger_ado_pipeline", "create_ado_pr",
    # Custom Connectors
    "create_custom_connector", "update_custom_connector",
    # Memory
    "update_memory_entry",
}


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL DEFINITIONS — sent to the LLM
# ══════════════════════════════════════════════════════════════════════════════

PP_TOOLS = [
    # ── Solutions ──────────────────────────────────────────────────────────────
    {"name": "list_solutions", "description": "List all solutions in the active environment. Shows name, version, managed/unmanaged, publisher, component count.", "parameters": {"type": "object", "properties": {"managed_only": {"type": "boolean", "description": "If true, only return managed solutions"}}, "required": []}},
    {"name": "get_solution_components", "description": "Get all components inside a specific solution (tables, flows, apps, plugins, etc).", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string"}}, "required": ["solution_name"]}},
    {"name": "export_solution", "description": "Export a solution as a .zip file. Managed or unmanaged.", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string"}, "managed": {"type": "boolean", "default": True}}, "required": ["solution_name"]}},
    {"name": "import_solution", "description": "Import a solution .zip into the active or a target environment.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}, "target_env_url": {"type": "string", "description": "Optional: override target environment URL"}}, "required": ["file_path"]}},
    {"name": "publish_solution", "description": "Publish all customizations in a solution.", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string"}}, "required": ["solution_name"]}},
    {"name": "create_solution", "description": "Create a new solution in the environment.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "unique_name": {"type": "string"}, "publisher_prefix": {"type": "string", "default": "new"}, "version": {"type": "string", "default": "1.0.0.0"}}, "required": ["name", "unique_name"]}},
    {"name": "delete_solution", "description": "Delete a solution from the environment.", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string"}}, "required": ["solution_name"]}},
    {"name": "run_alm_pipeline", "description": "Trigger an ALM deployment pipeline to promote a solution through environments.", "parameters": {"type": "object", "properties": {"pipeline_name": {"type": "string"}, "solution_name": {"type": "string"}}, "required": ["pipeline_name", "solution_name"]}},
    # ── Dataverse ─────────────────────────────────────────────────────────────
    {"name": "list_tables", "description": "List all Dataverse tables in the environment or a specific solution.", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string", "description": "Filter by solution (optional)"}, "custom_only": {"type": "boolean", "default": False}}, "required": []}},
    {"name": "get_table_columns", "description": "Get all columns/fields for a Dataverse table with types and metadata.", "parameters": {"type": "object", "properties": {"table_name": {"type": "string", "description": "Logical name e.g. account, contact, new_customtable"}}, "required": ["table_name"]}},
    {"name": "query_records", "description": "Query records from a Dataverse table using OData filter.", "parameters": {"type": "object", "properties": {"table": {"type": "string"}, "filter": {"type": "string", "description": "OData $filter expression"}, "select": {"type": "string", "description": "Comma-separated columns to return"}, "top": {"type": "integer", "default": 10}}, "required": ["table"]}},
    {"name": "run_fetchxml", "description": "Execute a FetchXML query against Dataverse.", "parameters": {"type": "object", "properties": {"fetchxml": {"type": "string"}, "table": {"type": "string"}}, "required": ["fetchxml"]}},
    {"name": "create_record", "description": "Create a new record in a Dataverse table.", "parameters": {"type": "object", "properties": {"table": {"type": "string"}, "data": {"type": "object"}}, "required": ["table", "data"]}},
    {"name": "update_record", "description": "Update an existing record in Dataverse.", "parameters": {"type": "object", "properties": {"table": {"type": "string"}, "record_id": {"type": "string"}, "data": {"type": "object"}}, "required": ["table", "record_id", "data"]}},
    {"name": "delete_record", "description": "Delete a record from Dataverse.", "parameters": {"type": "object", "properties": {"table": {"type": "string"}, "record_id": {"type": "string"}}, "required": ["table", "record_id"]}},
    {"name": "create_column", "description": "Add a new column to an existing Dataverse table.", "parameters": {"type": "object", "properties": {"table_name": {"type": "string"}, "display_name": {"type": "string"}, "schema_name": {"type": "string"}, "type": {"type": "string", "enum": ["Text", "Number", "DateTime", "Boolean", "Lookup", "Choice", "MultiSelectPicklist", "Currency", "Decimal", "Email", "Phone", "URL"]}, "required_level": {"type": "string", "enum": ["None", "Recommended", "Required"], "default": "None"}}, "required": ["table_name", "display_name", "schema_name", "type"]}},
    # ── Canvas Apps ───────────────────────────────────────────────────────────
    {"name": "list_canvas_apps", "description": "List all canvas apps in the active environment with owner, last modified, and usage stats.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "export_canvas_app", "description": "Export a canvas app as a .msapp or .zip package.", "parameters": {"type": "object", "properties": {"app_id": {"type": "string"}, "app_name": {"type": "string", "description": "Use if app_id unknown"}}, "required": []}},
    # ── Model-Driven Apps ─────────────────────────────────────────────────────
    {"name": "list_model_driven_apps", "description": "List all model-driven apps in the environment.", "parameters": {"type": "object", "properties": {}, "required": []}},
    # ── Power Automate ────────────────────────────────────────────────────────
    {"name": "list_flows", "description": "List all Power Automate cloud flows in the environment with status, owner, and trigger type.", "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["all", "on", "off", "error"], "default": "all"}}, "required": []}},
    {"name": "get_flow_runs", "description": "Get recent run history for a specific flow, including failed runs with error details.", "parameters": {"type": "object", "properties": {"flow_id": {"type": "string"}, "flow_name": {"type": "string"}, "days": {"type": "integer", "default": 7}}, "required": []}},
    {"name": "enable_flow", "description": "Enable (turn on) a Power Automate flow.", "parameters": {"type": "object", "properties": {"flow_id": {"type": "string"}, "flow_name": {"type": "string"}}, "required": []}},
    {"name": "disable_flow", "description": "Disable (turn off) a Power Automate flow.", "parameters": {"type": "object", "properties": {"flow_id": {"type": "string"}, "flow_name": {"type": "string"}}, "required": []}},
    {"name": "trigger_flow", "description": "Manually trigger a Power Automate flow (must have HTTP/manual trigger).", "parameters": {"type": "object", "properties": {"flow_id": {"type": "string"}, "body": {"type": "object", "description": "Request body for the trigger"}}, "required": ["flow_id"]}},
    {"name": "get_failing_flows", "description": "Get all flows that have failed in the last N hours.", "parameters": {"type": "object", "properties": {"hours": {"type": "integer", "default": 24}}, "required": []}},
    # ── Copilot Studio ────────────────────────────────────────────────────────
    {"name": "list_agents", "description": "List all Copilot Studio agents in the environment.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_agent_topics", "description": "Get all topics for a specific Copilot Studio agent.", "parameters": {"type": "object", "properties": {"agent_id": {"type": "string"}, "agent_name": {"type": "string"}}, "required": []}},
    {"name": "publish_agent", "description": "Publish a Copilot Studio agent to make changes live.", "parameters": {"type": "object", "properties": {"agent_id": {"type": "string"}, "agent_name": {"type": "string"}}, "required": []}},
    # ── Power Pages ───────────────────────────────────────────────────────────
    {"name": "list_portals", "description": "List all Power Pages portals in the environment.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_portal_table_permissions", "description": "List all table permissions for a Power Pages portal.", "parameters": {"type": "object", "properties": {"portal_id": {"type": "string"}, "portal_name": {"type": "string"}}, "required": []}},
    # ── Power BI ──────────────────────────────────────────────────────────────
    {"name": "list_powerbi_workspaces", "description": "List all Power BI workspaces the service principal has access to.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_powerbi_reports", "description": "List all reports in a Power BI workspace.", "parameters": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "workspace_name": {"type": "string"}}, "required": []}},
    {"name": "get_dataset_refresh_history", "description": "Get the refresh history for a Power BI dataset.", "parameters": {"type": "object", "properties": {"dataset_id": {"type": "string"}, "dataset_name": {"type": "string"}}, "required": []}},
    {"name": "trigger_dataset_refresh", "description": "Trigger an on-demand refresh of a Power BI dataset.", "parameters": {"type": "object", "properties": {"dataset_id": {"type": "string"}, "dataset_name": {"type": "string"}}, "required": []}},
    # ── Microsoft Fabric ──────────────────────────────────────────────────────
    {"name": "list_fabric_workspaces", "description": "List all Microsoft Fabric workspaces the service principal can access.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_capacities", "description": "List all Microsoft Fabric capacities in the tenant, including SKU and state.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_lakehouses", "description": "List all lakehouses inside a Fabric workspace.", "parameters": {"type": "object", "properties": {"workspace_id": {"type": "string", "description": "Fabric workspace GUID"}}, "required": ["workspace_id"]}},
    {"name": "list_notebooks", "description": "List all notebooks inside a Fabric workspace.", "parameters": {"type": "object", "properties": {"workspace_id": {"type": "string"}}, "required": ["workspace_id"]}},
    {"name": "list_pipelines", "description": "List all data pipelines inside a Fabric workspace.", "parameters": {"type": "object", "properties": {"workspace_id": {"type": "string"}}, "required": ["workspace_id"]}},
    {"name": "run_notebook", "description": "Trigger execution of a Fabric notebook.", "parameters": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "notebook_id": {"type": "string"}}, "required": ["workspace_id", "notebook_id"]}},
    # ── D365 CRM ──────────────────────────────────────────────────────────────
    {"name": "list_leads", "description": "List CRM leads with status, owner, and score.", "parameters": {"type": "object", "properties": {"top": {"type": "integer", "default": 20}, "status": {"type": "string", "default": "Open"}}, "required": []}},
    {"name": "list_opportunities", "description": "List CRM opportunities with stage, value, and close date.", "parameters": {"type": "object", "properties": {"top": {"type": "integer", "default": 20}, "stage": {"type": "string"}}, "required": []}},
    {"name": "list_accounts", "description": "List CRM accounts.", "parameters": {"type": "object", "properties": {"top": {"type": "integer", "default": 20}, "filter": {"type": "string"}}, "required": []}},
    {"name": "create_crm_record", "description": "Create a record in D365 CRM (Lead, Opportunity, Account, Contact, Case, etc).", "parameters": {"type": "object", "properties": {"entity": {"type": "string", "description": "e.g. lead, opportunity, account"}, "data": {"type": "object"}}, "required": ["entity", "data"]}},
    # ── Plugins ───────────────────────────────────────────────────────────────
    {"name": "list_plugins", "description": "List all registered plugins and their assemblies.", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string"}}, "required": []}},
    {"name": "get_plugin_steps", "description": "Get all steps/event handlers for a plugin.", "parameters": {"type": "object", "properties": {"plugin_name": {"type": "string"}}, "required": ["plugin_name"]}},
    # ── Custom Connectors ─────────────────────────────────────────────────────
    {"name": "list_custom_connectors", "description": "List all custom connectors in the environment.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "create_custom_connector", "description": "Create a custom connector from an OpenAPI spec file or URL.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "openapi_url": {"type": "string"}, "openapi_file_path": {"type": "string"}}, "required": ["name"]}},
    # ── Environments ──────────────────────────────────────────────────────────
    {"name": "list_environments", "description": "List all Power Platform environments in the tenant.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_environment_capacity", "description": "Get storage capacity usage for an environment.", "parameters": {"type": "object", "properties": {"env_id": {"type": "string"}}, "required": []}},
    {"name": "create_environment", "description": "Create a new Power Platform environment.", "parameters": {"type": "object", "properties": {"display_name": {"type": "string"}, "type": {"type": "string", "enum": ["Sandbox", "Production", "Developer", "Trial"]}, "region": {"type": "string", "default": "unitedkingdom"}, "provision_database": {"type": "boolean", "default": True}}, "required": ["display_name", "type"]}},
    # ── Security ──────────────────────────────────────────────────────────────
    {"name": "list_security_roles", "description": "List all security roles in the active environment.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_user_roles", "description": "Get all security roles assigned to a specific user.", "parameters": {"type": "object", "properties": {"user_email": {"type": "string"}}, "required": ["user_email"]}},
    {"name": "assign_role_to_user", "description": "Assign a security role to a user.", "parameters": {"type": "object", "properties": {"user_email": {"type": "string"}, "role_name": {"type": "string"}}, "required": ["user_email", "role_name"]}},
    {"name": "list_dlp_policies", "description": "List all DLP policies in the tenant.", "parameters": {"type": "object", "properties": {}, "required": []}},
    # ── ALM / Azure DevOps ────────────────────────────────────────────────────
    {"name": "list_alm_pipelines", "description": "List all ALM deployment pipelines configured in the environment.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_ado_repos", "description": "List Azure DevOps repositories containing Power Platform solutions.", "parameters": {"type": "object", "properties": {"project": {"type": "string"}}, "required": []}},
    {"name": "trigger_ado_pipeline", "description": "Trigger an Azure DevOps pipeline build.", "parameters": {"type": "object", "properties": {"pipeline_id": {"type": "integer"}, "pipeline_name": {"type": "string"}, "branch": {"type": "string", "default": "main"}}, "required": []}},
    # ── Monitor ───────────────────────────────────────────────────────────────
    {"name": "get_audit_log", "description": "Read the Dataverse audit log for a user or action type.", "parameters": {"type": "object", "properties": {"user_email": {"type": "string"}, "action": {"type": "string"}, "days": {"type": "integer", "default": 7}}, "required": []}},
    {"name": "get_dataverse_capacity", "description": "Get Dataverse storage capacity breakdown (database, file, log).", "parameters": {"type": "object", "properties": {}, "required": []}},
    # ── Knowledge / Memory ────────────────────────────────────────────────────
    {"name": "search_knowledge_base", "description": "Search the local knowledge base for PP documentation and org-specific policies.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "update_memory_entry", "description": "Save an important fact to persistent memory (org defaults, user preferences, etc).", "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}},
    # ── Phase 3: Dataverse schema ─────────────────────────────────────────────
    {"name": "create_table", "description": "Create a new custom Dataverse table. logical_name must include publisher prefix (e.g. cr123_project). Also creates a primary name column automatically.", "parameters": {"type": "object", "properties": {"logical_name": {"type": "string"}, "display_name": {"type": "string"}, "plural_display_name": {"type": "string"}, "description": {"type": "string"}, "ownership": {"type": "string", "enum": ["UserOwned", "OrganizationOwned"], "default": "UserOwned"}}, "required": ["logical_name", "display_name", "plural_display_name"]}},
    {"name": "add_column", "description": "Add a column to a Dataverse table. column_type must be one of: text, multiline_text, whole_number, decimal, currency, boolean, date_only, date_time, choice, choices, lookup, file, image, auto_number, float, url, email, phone.", "parameters": {"type": "object", "properties": {"table_name": {"type": "string"}, "logical_name": {"type": "string", "description": "Logical name with publisher prefix"}, "display_name": {"type": "string"}, "column_type": {"type": "string"}, "required": {"type": "boolean", "default": False}, "description": {"type": "string"}}, "required": ["table_name", "logical_name", "display_name", "column_type"]}},
    {"name": "create_lookup_relationship", "description": "Create a many-to-one lookup — adds a lookup column on related_table pointing to primary_table.", "parameters": {"type": "object", "properties": {"primary_table": {"type": "string"}, "related_table": {"type": "string"}, "lookup_column_name": {"type": "string"}, "lookup_display_name": {"type": "string"}}, "required": ["primary_table", "related_table", "lookup_column_name", "lookup_display_name"]}},
    {"name": "create_nn_relationship", "description": "Create a many-to-many relationship between two Dataverse tables.", "parameters": {"type": "object", "properties": {"table_a": {"type": "string"}, "table_b": {"type": "string"}, "schema_name": {"type": "string"}}, "required": ["table_a", "table_b"]}},
    {"name": "bulk_create_records", "description": "Create multiple records at once via $batch API. Much faster than one-by-one creation.", "parameters": {"type": "object", "properties": {"table": {"type": "string"}, "records": {"type": "array", "items": {"type": "object"}}}, "required": ["table", "records"]}},
    {"name": "get_record_count", "description": "Get total count of records in a table, optionally filtered.", "parameters": {"type": "object", "properties": {"table": {"type": "string"}, "filter_query": {"type": "string"}}, "required": ["table"]}},
    {"name": "create_global_choice", "description": "Create a reusable global option set (choice) shared across tables.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "display_name": {"type": "string"}, "options": {"type": "array", "items": {"type": "object"}}, "description": {"type": "string"}}, "required": ["name", "display_name", "options"]}},
    # ── Phase 3: Flows ────────────────────────────────────────────────────────
    {"name": "create_flow", "description": "Create a new Power Automate cloud flow. trigger_type: recurrence, http_trigger, dataverse_create, dataverse_update.", "parameters": {"type": "object", "properties": {"display_name": {"type": "string"}, "trigger_type": {"type": "string", "enum": ["recurrence","http_trigger","dataverse_create","dataverse_update"]}, "description": {"type": "string"}}, "required": ["display_name", "trigger_type"]}},
    {"name": "delete_flow", "description": "Permanently delete a cloud flow. IRREVERSIBLE.", "parameters": {"type": "object", "properties": {"flow_id": {"type": "string"}}, "required": ["flow_id"]}},
    {"name": "get_flow_run_errors", "description": "Get action-level error details for a specific failed flow run.", "parameters": {"type": "object", "properties": {"flow_id": {"type": "string"}, "run_id": {"type": "string"}}, "required": ["flow_id", "run_id"]}},
    # ── Phase 3: Solutions ────────────────────────────────────────────────────
    {"name": "check_solution_health", "description": "Health check a solution — publisher prefix, managed status, component counts, ALM compliance.", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string"}}, "required": ["solution_name"]}},
    {"name": "get_solution_layers", "description": "Show customisation layers for a solution's components.", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string"}}, "required": ["solution_name"]}},
    {"name": "add_component_to_solution", "description": "Add a component (table, flow, form, etc.) to an unmanaged solution by component GUID and type.", "parameters": {"type": "object", "properties": {"solution_name": {"type": "string"}, "component_id": {"type": "string"}, "component_type": {"type": "string"}, "do_not_include_subcomponents": {"type": "boolean", "default": False}}, "required": ["solution_name", "component_id", "component_type"]}},
    # ── Phase 3: Security ─────────────────────────────────────────────────────
    {"name": "clone_security_role", "description": "Clone an existing security role and all its privileges into a new role.", "parameters": {"type": "object", "properties": {"source_role_id": {"type": "string"}, "new_name": {"type": "string"}, "business_unit_id": {"type": "string"}}, "required": ["source_role_id", "new_name", "business_unit_id"]}},
    {"name": "set_table_privileges", "description": "Set CRUD access levels for a table on a security role. Levels: 0=None 1=User 2=BU 4=ParentChild 8=Org.", "parameters": {"type": "object", "properties": {"role_id": {"type": "string"}, "table_name": {"type": "string"}, "create_access": {"type": "integer", "default": 0}, "read_access": {"type": "integer", "default": 0}, "write_access": {"type": "integer", "default": 0}, "delete_access": {"type": "integer", "default": 0}}, "required": ["role_id", "table_name"]}},
    {"name": "list_field_security_profiles", "description": "List all field security profiles (column-level security) in the environment.", "parameters": {"type": "object", "properties": {}, "required": []}},
    # ── Phase 3: Monitor ──────────────────────────────────────────────────────
    {"name": "check_environment_health", "description": "Quick connectivity + auth check — confirms Dataverse API is reachable and auth is valid.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "check_plugin_health", "description": "Audit plugin assemblies and steps — isolation mode, enabled vs disabled, async vs sync. Returns warnings.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_failing_flows_detail", "description": "Scan all flows and return those with recent failures — per-flow failure counts and last run IDs.", "parameters": {"type": "object", "properties": {}, "required": []}},
]


# ══════════════════════════════════════════════════════════════════════════════
#  RETRY WRAPPER
# ══════════════════════════════════════════════════════════════════════════════

def _with_retry(fn, tool_name: str, max_attempts: int = 3) -> str:
    NO_RETRY = ("401", "403", "400", "invalid", "not found", "unauthorized")
    delay, last_err = 1.0, None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if any(s in str(e).lower() for s in NO_RETRY):
                break
            if attempt < max_attempts - 1:
                time.sleep(delay)
                delay *= 2
    return json.dumps({"error": f"⚠ {tool_name.replace('_',' ')} unavailable.", "detail": str(last_err)})


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════

def dispatch_tool(name: str, args: dict) -> str:
    """Route a tool call to the appropriate tool module."""
    try:
        # Lazy-import tool modules
        if name in {"list_solutions","get_solution_components","export_solution","import_solution",
                    "publish_solution","create_solution","delete_solution","clone_solution",
                    "run_alm_pipeline","add_component_to_solution","upgrade_solution",
                    "list_alm_pipelines","get_pipeline_runs","get_solution_history",
                    "check_solution_health","get_solution_layers","list_solution_components",
                    "publish_all_customizations"}:
            from tools import solution as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_tables","get_table_columns","get_table_relationships","query_records",
                      "run_fetchxml","get_record","create_record","update_record","delete_record",
                      "upsert_record","bulk_create_records","create_table","update_table","delete_table",
                      "create_column","add_column","update_column","delete_column",
                      "create_relationship","create_lookup_relationship","create_nn_relationship",
                      "list_table_forms","list_table_views","list_choices",
                      "get_record_count","list_global_choices","create_global_choice",
                      "search_records","list_relationships"}:
            from tools import dataverse as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_canvas_apps","get_canvas_app_details","get_canvas_app_users",
                      "export_canvas_app","share_canvas_app","publish_canvas_app"}:
            from tools import canvas as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_model_driven_apps","get_mda_sitemap","get_mda_components"}:
            from tools import mda as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_flows","get_flow_details","get_flow_runs","get_flow_errors",
                      "get_flow_connections","enable_flow","disable_flow","trigger_flow","run_flow",
                      "create_flow","delete_flow","repair_flow_connections",
                      "list_flow_connections","get_failing_flows",
                      "get_flow_run_history","list_failed_runs","get_flow_run_errors"}:
            from tools import flows as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_agents","get_agent_topics","get_agent_analytics","create_agent_topic",
                      "publish_agent","delete_agent_topic","add_knowledge_source"}:
            from tools import copilot as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_portals","get_portal_pages","get_portal_table_permissions",
                      "get_portal_web_roles","list_web_templates","publish_portal",
                      "update_table_permission","create_web_role"}:
            from tools import pages as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_powerbi_workspaces","list_powerbi_reports","list_powerbi_datasets",
                      "get_dataset_refresh_history","get_report_details","trigger_dataset_refresh",
                      "create_powerbi_workspace"}:
            from tools import powerbi as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_fabric_workspaces","list_capacities","list_lakehouses",
                      "list_notebooks","list_pipelines","list_workspace_items",
                      "run_notebook","create_lakehouse"}:
            from tools import fabric as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_leads","list_opportunities","list_accounts","list_contacts",
                      "get_crm_record","search_crm","get_crm_pipeline",
                      "create_crm_record","update_crm_record","assign_crm_record"}:
            from tools import crm as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_plugins","get_plugin_steps","get_plugin_details",
                      "register_plugin","update_plugin_step","disable_plugin_step"}:
            from tools import plugins as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_custom_connectors","get_connector_details","get_connector_actions",
                      "create_custom_connector","update_custom_connector"}:
            from tools import connectors as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_environments","get_environment_details","get_environment_capacity",
                      "list_environment_makers","create_environment","delete_environment",
                      "copy_environment","assign_environment_admin"}:
            from tools import environments as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_security_roles","get_role_privileges","get_user_roles",
                      "list_role_members","list_system_admins","list_business_units","list_users",
                      "assign_role_to_user","remove_role_from_user",
                      "create_security_role","clone_security_role","update_security_role",
                      "set_table_privileges","list_field_security_profiles",
                      "get_field_security_profile_permissions","assign_field_security_profile_to_user",
                      "list_dlp_policies","get_dlp_policy_details","check_connector_dlp_status",
                      "create_dlp_policy","update_dlp_policy","delete_dlp_policy"}:
            from tools import security as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"get_failing_flows","get_audit_log","get_api_call_stats",
                      "get_solution_checker_results","get_dataverse_capacity","get_connector_health",
                      "check_environment_health","check_plugin_health","get_failing_flows_detail",
                      "list_plugin_steps","list_system_jobs","list_audit_logs",
                      "get_entity_change_history","get_api_usage","get_storage_consumption",
                      "get_flow_error_summary"}:
            from tools import monitor as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name in {"list_ado_repos","list_ado_pipelines","get_ado_pipeline_runs",
                      "get_ado_pull_requests","trigger_ado_pipeline","create_ado_pr"}:
            from tools import ado as m
            return _with_retry(lambda: json.dumps(getattr(m, name)(**args), default=str, indent=2), name)

        elif name == "search_knowledge_base":
            from tools import knowledge as m
            return _with_retry(lambda: json.dumps(m.search_knowledge_base(**args), default=str, indent=2), name)

        elif name == "update_memory_entry":
            from tools import memory as m
            return _with_retry(lambda: json.dumps(m.update_memory_entry(**args), default=str, indent=2), name)

        elif name == "get_memory_summary":
            from tools import memory as m
            return _with_retry(lambda: json.dumps(m.get_memory_summary(), default=str, indent=2), name)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except ImportError as e:
        return json.dumps({"error": f"Tool module not yet built: {e}. Coming in the next phase.", "tool": name})
    except Exception as e:
        return json.dumps({"error": str(e), "tool": name})


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIRMATION SYSTEM — rich write previews
# ══════════════════════════════════════════════════════════════════════════════

def build_write_preview(tool_name: str, args: dict) -> str:
    """Build a human-readable preview of a write operation."""
    previews = {
        "export_solution": lambda a: (
            f"📦 Export Solution\n"
            f"   Name:   {a.get('solution_name')}\n"
            f"   Type:   {'Managed' if a.get('managed', True) else 'Unmanaged'} (.zip)\n"
            f"   Command: pac solution export --name {a.get('solution_name')} {'--managed' if a.get('managed', True) else ''}"
        ),
        "import_solution": lambda a: (
            f"📥 Import Solution\n"
            f"   File:   {a.get('file_path')}\n"
            f"   Target: {a.get('target_env_url') or 'Active environment'}\n"
            f"   ⚠  This will overwrite existing customizations"
        ),
        "publish_solution": lambda a: (
            f"🚀 Publish Solution\n"
            f"   Solution: {a.get('solution_name')}\n"
            f"   This publishes all pending customizations"
        ),
        "create_solution": lambda a: (
            f"🆕 Create New Solution\n"
            f"   Display Name:  {a.get('name')}\n"
            f"   Unique Name:   {a.get('unique_name')}\n"
            f"   Publisher:     {a.get('publisher_prefix', 'new')}\n"
            f"   Version:       {a.get('version', '1.0.0.0')}"
        ),
        "delete_solution": lambda a: (
            f"🗑  Delete Solution  ⚠  IRREVERSIBLE\n"
            f"   Solution: {a.get('solution_name')}\n"
            f"   This permanently removes the solution and its customizations"
        ),
        "run_alm_pipeline": lambda a: (
            f"🚀 Run ALM Deployment Pipeline\n"
            f"   Pipeline:  {a.get('pipeline_name')}\n"
            f"   Solution:  {a.get('solution_name')}\n"
            f"   This will deploy the solution to the next stage environment"
        ),
        "create_table": lambda a: (
            f"🗄  Create Dataverse Table\n"
            f"   Display Name: {a.get('display_name')}\n"
            f"   Schema Name:  {a.get('schema_name')}\n"
            f"   Type:         {a.get('table_type', 'Standard')}"
        ),
        "delete_table": lambda a: (
            f"🗑  Delete Dataverse Table  ⚠  IRREVERSIBLE\n"
            f"   Table: {a.get('table_name')}\n"
            f"   This deletes the table AND all its data"
        ),
        "create_column": lambda a: (
            f"📋 Add Column to Table\n"
            f"   Table:    {a.get('table_name')}\n"
            f"   Name:     {a.get('display_name')} ({a.get('schema_name')})\n"
            f"   Type:     {a.get('type')}\n"
            f"   Required: {a.get('required_level', 'None')}"
        ),
        "create_record": lambda a: (
            f"➕ Create {a.get('table', 'Record')}\n"
            f"   Data: {json.dumps(a.get('data', {}), indent=2)[:400]}"
        ),
        "update_record": lambda a: (
            f"✏️  Update {a.get('table', 'Record')}\n"
            f"   ID:      {a.get('record_id')}\n"
            f"   Changes: {json.dumps(a.get('data', {}), indent=2)[:400]}"
        ),
        "delete_record": lambda a: (
            f"🗑  Delete Record  ⚠  IRREVERSIBLE\n"
            f"   Table: {a.get('table')}\n"
            f"   ID:    {a.get('record_id')}"
        ),
        "enable_flow": lambda a: (
            f"▶️  Enable Flow\n"
            f"   Flow: {a.get('flow_name') or a.get('flow_id')}"
        ),
        "disable_flow": lambda a: (
            f"⏸  Disable Flow\n"
            f"   Flow: {a.get('flow_name') or a.get('flow_id')}"
        ),
        "trigger_flow": lambda a: (
            f"▶️  Manually Trigger Flow\n"
            f"   Flow: {a.get('flow_id')}\n"
            f"   Body: {json.dumps(a.get('body', {}), indent=2)[:300]}"
        ),
        "publish_agent": lambda a: (
            f"🤖 Publish Copilot Studio Agent\n"
            f"   Agent: {a.get('agent_name') or a.get('agent_id')}\n"
            f"   This makes all topic changes live"
        ),
        "assign_role_to_user": lambda a: (
            f"🛡  Assign Security Role\n"
            f"   User: {a.get('user_email')}\n"
            f"   Role: {a.get('role_name')}"
        ),
        "create_environment": lambda a: (
            f"🌍 Create Environment\n"
            f"   Name:     {a.get('display_name')}\n"
            f"   Type:     {a.get('type')}\n"
            f"   Region:   {a.get('region', 'unitedkingdom')}\n"
            f"   Database: {'Yes' if a.get('provision_database', True) else 'No'}"
        ),
        "trigger_dataset_refresh": lambda a: (
            f"🔄 Trigger Power BI Dataset Refresh\n"
            f"   Dataset: {a.get('dataset_name') or a.get('dataset_id')}"
        ),
        "create_crm_record": lambda a: (
            f"➕ Create D365 CRM Record\n"
            f"   Entity: {a.get('entity')}\n"
            f"   Data:   {json.dumps(a.get('data', {}), indent=2)[:400]}"
        ),
        "trigger_ado_pipeline": lambda a: (
            f"🔷 Trigger Azure DevOps Pipeline\n"
            f"   Pipeline: {a.get('pipeline_name') or a.get('pipeline_id')}\n"
            f"   Branch:   {a.get('branch', 'main')}"
        ),
    }
    fn = previews.get(tool_name)
    if fn:
        return fn(args)
    return f"Operation: {tool_name}\nArgs:\n{json.dumps(args, indent=2)}"


def confirm_write_operation(tool_name: str, args: dict) -> bool:
    """Show preview and ask for confirmation. Returns True if confirmed."""
    preview = build_write_preview(tool_name, args)
    if RICH:
        console.print()
        console.print(Panel(preview, title="[bold yellow]⚠  Confirm Action[/bold yellow]", border_style="yellow"))
        try:
            return Confirm.ask("[yellow]Proceed?[/yellow]", default=False)
        except (KeyboardInterrupt, EOFError):
            return False
    else:
        print(f"\n{'─'*50}\n⚠  CONFIRM:\n{preview}\n{'─'*50}")
        resp = input("Proceed? [y/N]: ").strip().lower()
        return resp in ("y", "yes")


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

def _build_system_prompt(user_message: str = "", tool_section: str = "home", plan_mode: bool = False, env_context: str = "") -> str:
    today = datetime.datetime.now().strftime("%A, %d %B %Y — %H:%M")
    env   = get_active_environment()

    # Inject memory
    memory_ctx = ""
    try:
        from tools import memory
        memory_ctx = memory.get_relevant_memory_context(user_message)
    except Exception:
        pass

    prompt = f"""# Power Platform Agent
Today: {today}
Active Environment: {env.get('name', 'Not set')} ({env.get('url', 'No URL configured')})
Current Section: {tool_section}

## Identity
You are an expert Power Platform Architect and Admin assistant. You have deep expertise across
the entire Power Platform: Dataverse, Canvas Apps, Model-Driven Apps, Power Automate, Copilot Studio,
Power Pages, Power BI, Microsoft Fabric, D365 CRM, Plugins, Custom Connectors, ALM, Environments,
Security, and DLP. You also have full knowledge of Azure services as they relate to Power Platform:
Azure DevOps, Azure AD/Entra ID, Azure Key Vault, Azure Functions, Azure API Management, Azure Service Bus,
Azure Monitor, and Microsoft Fabric.

You are direct, precise, and never make things up. If you don't have data, you fetch it.
You speak like a senior consultant — confident, clear, and action-oriented.

## Behavioural Rules

### Rule 1 — READ vs WRITE
- READ tools (list, get, query, search) → execute immediately, no confirmation needed.
- WRITE tools (create, update, delete, export, import, publish, assign, trigger) → ALWAYS show what you're about to do and wait for the user to say "yes" before executing.
- When in doubt, treat as WRITE.

### Rule 2 — Never guess IDs
If you need a solution name, flow ID, app ID, or any identifier you don't have — always fetch the parent list first.
Example: "export MySolution" → if solution_name is ambiguous, call list_solutions() first to confirm the exact name.

### Rule 3 — Parallel reads
When a request requires multiple independent data fetches, call ALL read tools in a single response.
Example: "org health check" → call get_failing_flows + get_dataverse_capacity + list_solutions in parallel.

### Rule 4 — One clarifying question
If a request is ambiguous in a way that could cause the wrong action, ask ONE targeted question.
Never ask multiple questions at once.

### Rule 5 — Response format
- Use markdown with headers (##), bullets, and **bold** for key info.
- Lead with the answer, then details.
- For lists (solutions, flows, apps), show a clean table or structured list with the most useful fields first.
- End with "**What next?**" when there's a natural follow-up action.

### Rule 6 — Power Platform expertise
- Always use correct PP terminology (logical names, schema names, publisher prefix, managed/unmanaged).
- For FetchXML: build well-formed XML with proper entity/attribute references.
- For solutions: always check managed vs unmanaged before suggesting exports.
- For security: always check existing roles before assigning new ones.
- For DLP: always check existing policies before creating new ones.

### Rule 7 — Context awareness
You are in section: **{tool_section}**. Focus your tool calls and responses on that domain.
If asked about something outside the current section, still answer it — just note you're switching context.

## Tool Chaining Examples

### "Export ContosoSalesHub to production"
1. list_solutions() → confirm ContosoSalesHub exists and is unmanaged
2. export_solution(solution_name="ContosoSalesHub", managed=True) → show confirmation preview
3. After yes: import_solution(file_path="ContosoSalesHub_managed.zip", target_env_url="...prod URL...") → show confirmation preview

### "Fix failing flows"
1. get_failing_flows(hours=24) → see what's broken
2. get_flow_errors(flow_id="...") → understand why
3. enable_flow(flow_id="...") → re-enable after confirmation

### "Create a new column on the Account table"
1. get_table_columns(table_name="account") → check it doesn't already exist
2. create_column(...) → show confirmation preview
3. publish_solution() → after confirmation, to apply changes
"""

    if tool_section == "command":
        # Override Rule 7 for full-autonomy Command Center mode
        prompt = prompt.replace(
            f"### Rule 7 — Context awareness\nYou are in section: **{tool_section}**. Focus your tool calls and responses on that domain.\nIf asked about something outside the current section, still answer it — just note you're switching context.",
            """### Rule 7 — Command Center (Full Autonomy)
You are in **Command Center** mode — you have unrestricted access to ALL 55+ Power Platform tools.
- Use any combination of tools across any domain in a single response.
- Proactively chain tools to give a COMPLETE answer: don't stop after one tool call if more data would make the answer better.
- For complex requests, PLAN your tool calls first (briefly tell the user what you're about to do), then execute.
- Lead with a brief plan, then show results section by section with clear headings (## Solutions, ## Flows, etc.).
- Always end with a summary and "**What next?**" with the most valuable follow-up actions."""
        )

    # Inject live env context if available (fetched once at session start)
    if env_context:
        prompt += f"\n\n## Live Environment Snapshot\n{env_context}"

    # Always inject the PP domain knowledge reference
    prompt += f"\n{PP_KNOWLEDGE_REFERENCE}"

    if plan_mode:
        prompt += """

### Rule 8 — Plan Before Build
For ANY request that involves 2 or more write operations, creating new things from scratch, or making changes across multiple components:
1. Output a numbered **Plan** FIRST — before calling any tools.
   Format:
   ## 📋 Plan: [Short Title]
   1. **[Tool/Action]** — [what it does & expected result]
   2. **[Tool/Action]** — [what it does & expected result]
   ...
   > Reply **"proceed"** to execute this plan, or **"modify [step N]: ..."** to change a step.
2. Wait for the user to confirm or modify.
3. Only after confirmation, start calling tools — one step at a time, reporting results.
4. If a step fails, pause and show the error before continuing.
Note: For simple READ-only requests (listing, searching, querying), skip the plan and answer directly."""

    if memory_ctx:
        prompt += f"\n\n## What I Know About Your Org\n{memory_ctx}"

    return prompt


# ══════════════════════════════════════════════════════════════════════════════
#  CONTEXT SUMMARISER — prevents context window overflow
# ══════════════════════════════════════════════════════════════════════════════

def _summarise_history(history: list, provider: str, model: str) -> list:
    KEEP_RECENT = 8
    if len(history) <= KEEP_RECENT:
        return history

    split = len(history) - KEEP_RECENT
    while split > 0 and history[split].get("role") == "tool":
        split -= 1

    old_turns = history[:split]
    recent    = history[split:]

    lines = []
    for m in old_turns:
        role = m.get("role", "")
        if role == "user":
            lines.append(f"User: {m.get('content', '')[:200]}")
        elif role == "assistant" and m.get("content"):
            lines.append(f"Assistant: {m.get('content', '')[:200]}")
        elif role == "assistant" and m.get("tool_calls"):
            names = [tc.get("name", "?") for tc in (m.get("tool_calls") or [])]
            lines.append(f"Assistant called: {', '.join(names)}")

    if not lines:
        return history

    try:
        summary_result = call_llm(
            messages=[
                {"role": "system", "content": "Summarise the conversation in under 120 words. Focus on what PP actions were taken, data fetched, and key facts. Third person, past tense. No fluff."},
                {"role": "user", "content": "\n".join(lines)},
            ],
            provider=provider,
            model=model,
        )
        summary = summary_result.get("content") or "Previous context summarised."
        return [
            {"role": "user", "content": f"[Context from earlier in this conversation]\n{summary}"},
            {"role": "assistant", "content": "Understood. Continuing from where we left off."},
        ] + recent
    except Exception:
        return history


# ══════════════════════════════════════════════════════════════════════════════
#  AGENTIC LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_agent(
    user_message: str,
    history: list,
    provider: str | None = None,
    model: str | None = None,
    tool_section: str = "home",
    auto_confirm_reads: bool = True,
    stream_callback=None,
    plan_mode: bool = False,
) -> dict:
    """
    Main agentic loop. Processes user_message with multi-turn tool calling.

    Args:
        user_message:       The user's plain-English request
        history:            Conversation history (list of {role, content} dicts)
        provider:           LLM provider override (None = auto-detect)
        model:              Model override (None = default for provider)
        tool_section:       Which nav section we're in (for system prompt context)
        auto_confirm_reads: If True, execute READ tools without asking
        stream_callback:    Optional fn(event_dict) called for each tool call event

    Returns:
        {
            "reply":        str,         # final text response
            "history":      list,        # updated history
            "tool_trace":   list,        # [{name, args, result, ms, read_or_write}]
            "pending_write": dict | None # If a write needs confirmation: {name, args, preview}
        }
    """
    _provider = provider or detect_provider()
    _model    = model    or get_active_model(_provider)

    # ── Env context: fetch once on the very first turn of a session ────────────
    env_ctx = ""
    if not history:
        env_url = get_active_environment().get("url", "")
        if env_url:
            # Fetch in a background thread so it doesn't block the first LLM call
            _ctx_holder: list[str] = []
            def _bg_fetch():
                _ctx_holder.append(_fetch_env_context(env_url))
            bg = threading.Thread(target=_bg_fetch, daemon=True)
            bg.start()
            bg.join(timeout=6)          # allow up to 6 s before giving up
            env_ctx = _ctx_holder[0] if _ctx_holder else _ENV_CONTEXT_CACHE.get(env_url, "")

    system_prompt = _build_system_prompt(user_message, tool_section, plan_mode=plan_mode, env_context=env_ctx)

    # Build messages
    messages = [{"role": "system", "content": system_prompt}]
    messages += _summarise_history(history, _provider, _model)
    messages.append({"role": "user", "content": user_message})

    tool_trace    = []
    pending_write = None
    MAX_TURNS     = 10

    for turn in range(MAX_TURNS):
        result = call_llm(
            messages=messages,
            tools=PP_TOOLS,
            provider=_provider,
            model=_model,
        )

        content    = result.get("content") or ""
        tool_calls = result.get("tool_calls") or []

        if not tool_calls:
            # Final text response
            messages.append({"role": "assistant", "content": content})
            return {
                "reply":         content,
                "history":       messages[1:],   # exclude system prompt
                "tool_trace":    tool_trace,
                "pending_write": None,
                "provider":      _provider,
                "model":         _model,
            }

        # ── Check for any WRITE tool in this batch ─────────────────────────────
        has_write = any(
            tc.get("name", "") in WRITE_TOOLS for tc in tool_calls
        )
        if has_write and auto_confirm_reads:
            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("arguments", {})
                if isinstance(args, str):
                    try:    args = json.loads(args)
                    except: args = {}
                if name in WRITE_TOOLS:
                    if stream_callback:
                        stream_callback({"event": "tool_start", "name": name, "args": args, "write": True})
                    preview = build_write_preview(name, args)
                    pending_write = {
                        "name": name, "args": args, "preview": preview,
                        "tool_call_id": tc.get("id", "tc_0"),
                    }
                    return {
                        "reply":         content or f"I need your confirmation before running `{name}`.",
                        "history":       messages[1:],
                        "tool_trace":    tool_trace,
                        "pending_write": pending_write,
                        "provider":      _provider,
                        "model":         _model,
                    }

        # ── Execute READ tools in parallel ─────────────────────────────────────
        tool_results = []
        error_count  = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tool_calls), 6)) as pool:
            # Parse args and emit tool_start events BEFORE submitting so the
            # streaming UI sees them immediately
            submitted: list[tuple] = []
            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("arguments", {})
                if isinstance(args, str):
                    try:    args = json.loads(args)
                    except: args = {}
                is_write = name in WRITE_TOOLS
                if stream_callback:
                    stream_callback({"event": "tool_start", "name": name, "args": args, "write": is_write})
                t_start = time.time()
                future  = pool.submit(dispatch_tool, name, args)
                submitted.append((future, tc, name, args, t_start, is_write))

            # Collect results in submission order (preserves tool_call_id mapping)
            for future, tc, name, args, t_start, is_write in submitted:
                try:
                    raw_result = future.result(timeout=60)
                except Exception as exc:
                    raw_result = json.dumps({"error": str(exc)})

                elapsed_ms = int((time.time() - t_start) * 1000)

                # Compress before the LLM sees it
                compressed = _compress_result(name, raw_result)

                # Track errors for self-correction logic
                try:
                    _parsed = json.loads(raw_result)
                    if isinstance(_parsed, dict) and ("error" in _parsed or "errors" in _parsed):
                        error_count += 1
                except Exception:
                    pass

                trace_entry = {
                    "name":   name,
                    "args":   args,
                    "result": compressed[:500] if len(compressed) > 500 else compressed,
                    "ms":     elapsed_ms,
                    "type":   "write" if is_write else "read",
                }
                tool_trace.append(trace_entry)

                if stream_callback:
                    stream_callback({"event": "tool_done", **trace_entry})

                tool_results.append({
                    "role":         "tool",
                    "tool_call_id": tc.get("id", "tc_0"),
                    "name":         name,
                    "content":      compressed,
                })

        # Add assistant message with tool calls + results to history
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        messages.extend(tool_results)

        # ── Self-correction: if every tool in this turn failed, nudge the LLM ──
        if error_count > 0 and error_count >= len(tool_calls):
            messages.append({
                "role":    "user",
                "content": (
                    f"⚠️ All {error_count} tool call(s) returned errors. "
                    "Review the error details above, correct your parameters or approach, and try again. "
                    "Do not repeat the same failed call — use a different strategy, fallback tool, "
                    "or ask me for clarification if the information is genuinely unavailable."
                ),
            })

    return {
        "reply":         "I reached the maximum number of tool calls. Please try a more specific request.",
        "history":       messages[1:],
        "tool_trace":    tool_trace,
        "pending_write": None,
        "provider":      _provider,
        "model":         _model,
    }


def execute_confirmed_write(pending: dict, history: list, provider: str, model: str, tool_section: str) -> dict:
    """Execute a write tool after the user confirmed it."""
    name = pending["name"]
    args = pending["args"]

    t_start    = time.time()
    result     = dispatch_tool(name, args)
    elapsed_ms = int((time.time() - t_start) * 1000)

    # Continue the agentic loop with the write result
    tool_call_id = pending.get("tool_call_id", "tc_0")
    messages = [{"role": "system", "content": _build_system_prompt("", tool_section, plan_mode=False)}]
    messages += history
    messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": tool_call_id, "name": name, "arguments": args}]})
    messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": result})

    final = call_llm(messages=messages, provider=provider, model=model)
    reply = final.get("content") or f"✅ `{name}` executed successfully."

    messages.append({"role": "assistant", "content": reply})

    return {
        "reply":    reply,
        "history":  messages[1:],
        "tool_trace": [{"name": name, "args": args, "result": result[:500], "ms": elapsed_ms, "type": "write"}],
        "pending_write": None,
        "provider": provider,
        "model":    model,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    provider = detect_provider()
    model    = get_active_model(provider)
    history  = []

    if RICH:
        console.print(Panel(
            f"[bold]Power Platform Agent[/bold]\n"
            f"LLM: [cyan]{provider.title()} / {model}[/cyan]\n"
            f"Env: [green]{get_active_environment().get('name', 'Not configured')}[/green]\n\n"
            f"Type [bold]exit[/bold] to quit · [bold]switch <model>[/bold] to change LLM",
            title="⚡ PP Agent",
            border_style="blue",
        ))
    else:
        print(f"\n⚡ Power Platform Agent | {provider}/{model}")
        print("Type 'exit' to quit\n")

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Goodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "bye"):
            print("👋 Goodbye!")
            break

        result = run_agent(user_input, history, provider=provider, model=model)
        history = result["history"]

        # Show tool trace
        if result["tool_trace"] and RICH:
            for t in result["tool_trace"]:
                badge = "[yellow]WRITE[/yellow]" if t["type"] == "write" else "[green]READ[/green]"
                console.print(f"  {badge} [dim]{t['name']}[/dim] [{t['ms']}ms]")

        if result.get("pending_write"):
            pw = result["pending_write"]
            confirmed = confirm_write_operation(pw["name"], pw["args"])
            if confirmed:
                result = execute_confirmed_write(pw, history, provider, model, "cli")
                history = result["history"]
                print(f"\nAgent: {result['reply']}")
            else:
                print("Cancelled.")
        else:
            if RICH:
                console.print(Markdown(result["reply"]))
            else:
                print(f"\nAgent: {result['reply']}")


if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg == "briefing":
        result = run_agent("Give me a full org health briefing: failing flows, capacity, and solution count", [], tool_section="home")
        print(result["reply"])
    else:
        main()
