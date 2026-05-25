"""
tools/pp_knowledge.py — Power Platform Domain Knowledge
=========================================================
Structured reference data used by the agent's system prompt and tool layer.
Import sections you need; the agent injects the full REFERENCE string into
its system prompt so the LLM has accurate PP knowledge without hallucinating.

Sections:
  COLUMN_TYPES        — PP column type → Dataverse API metadata
  PLURAL_NAMES        — Logical name → OData collection name
  ODATA_OPERATORS     — FetchXML / OData filter quick-reference
  FETCHXML_PATTERNS   — Copy-paste FetchXML templates
  FLOW_TEMPLATES      — Minimal flow definition skeletons
  CONNECTION_REFS     — Connector reference internal names
  SOLUTION_COMPONENTS — Component type codes for AddSolutionComponent
  PRIVILEGE_LEVELS    — Security privilege access level constants
  DELEGATION_RULES    — Canvas Power Fx delegation guide
  REFERENCE           — Full combined string for system-prompt injection
"""

from __future__ import annotations

# ── 1. Column Types ──────────────────────────────────────────────────────────
# Maps friendly type name → (AttributeType, @odata.type for attribute metadata API)
COLUMN_TYPES: dict[str, dict] = {
    "text": {
        "AttributeType": "String",
        "odata_type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "extra": {"MaxLength": 100, "FormatName": {"Value": "Text"}},
    },
    "multiline_text": {
        "AttributeType": "Memo",
        "odata_type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
        "extra": {"MaxLength": 2000},
    },
    "whole_number": {
        "AttributeType": "Integer",
        "odata_type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
        "extra": {"MinValue": -2147483648, "MaxValue": 2147483647},
    },
    "decimal": {
        "AttributeType": "Decimal",
        "odata_type": "Microsoft.Dynamics.CRM.DecimalAttributeMetadata",
        "extra": {"MinValue": -100000000000, "MaxValue": 100000000000, "Precision": 2},
    },
    "currency": {
        "AttributeType": "Money",
        "odata_type": "Microsoft.Dynamics.CRM.MoneyAttributeMetadata",
        "extra": {"MinValue": 0, "MaxValue": 1000000000, "Precision": 2},
    },
    "boolean": {
        "AttributeType": "Boolean",
        "odata_type": "Microsoft.Dynamics.CRM.BooleanAttributeMetadata",
        "extra": {
            "OptionSet": {
                "TrueOption": {"Value": 1, "Label": {"@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": "Yes", "LanguageCode": 1033}]}},
                "FalseOption": {"Value": 0, "Label": {"@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": "No", "LanguageCode": 1033}]}},
            }
        },
    },
    "date_only": {
        "AttributeType": "DateTime",
        "odata_type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
        "extra": {"Format": "DateOnly", "DateTimeBehavior": {"Value": "UserLocal"}},
    },
    "date_time": {
        "AttributeType": "DateTime",
        "odata_type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
        "extra": {"Format": "DateAndTime", "DateTimeBehavior": {"Value": "UserLocal"}},
    },
    "choice": {
        "AttributeType": "Picklist",
        "odata_type": "Microsoft.Dynamics.CRM.PicklistAttributeMetadata",
        "extra": {},  # OptionSet populated separately
    },
    "choices": {
        "AttributeType": "MultiSelectPicklist",
        "odata_type": "Microsoft.Dynamics.CRM.MultiSelectPicklistAttributeMetadata",
        "extra": {},
    },
    "lookup": {
        "AttributeType": "Lookup",
        "odata_type": "Microsoft.Dynamics.CRM.LookupAttributeMetadata",
        "extra": {},  # Targets populated separately
    },
    "file": {
        "AttributeType": "File",
        "odata_type": "Microsoft.Dynamics.CRM.FileAttributeMetadata",
        "extra": {"MaxSizeInKB": 32768},
    },
    "image": {
        "AttributeType": "Virtual",
        "odata_type": "Microsoft.Dynamics.CRM.ImageAttributeMetadata",
        "extra": {"MaxSizeInKB": 10240},
    },
    "auto_number": {
        "AttributeType": "String",
        "odata_type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "extra": {"AutoNumberFormat": "ALT-{SEQNUM:5}", "MaxLength": 100},
    },
    "float": {
        "AttributeType": "Double",
        "odata_type": "Microsoft.Dynamics.CRM.DoubleAttributeMetadata",
        "extra": {"MinValue": -1e10, "MaxValue": 1e10, "Precision": 5},
    },
    "duration": {
        "AttributeType": "Integer",
        "odata_type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
        "extra": {"Format": "Duration"},
    },
    "url": {
        "AttributeType": "String",
        "odata_type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "extra": {"MaxLength": 200, "FormatName": {"Value": "Url"}},
    },
    "email": {
        "AttributeType": "String",
        "odata_type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "extra": {"MaxLength": 100, "FormatName": {"Value": "Email"}},
    },
    "phone": {
        "AttributeType": "String",
        "odata_type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "extra": {"MaxLength": 50, "FormatName": {"Value": "Phone"}},
    },
}


# ── 2. Plural Entity Names ───────────────────────────────────────────────────
# Logical (singular) name → OData collection name for Web API URLs
PLURAL_NAMES: dict[str, str] = {
    # Standard tables
    "account": "accounts",
    "contact": "contacts",
    "lead": "leads",
    "opportunity": "opportunities",
    "incident": "incidents",
    "quote": "quotes",
    "order": "salesorders",
    "invoice": "invoices",
    "product": "products",
    "task": "tasks",
    "activitypointer": "activitypointers",
    "email": "emails",
    "phonecall": "phonecalls",
    "appointment": "appointments",
    "note": "annotations",
    # System / metadata
    "systemuser": "systemusers",
    "team": "teams",
    "businessunit": "businessunits",
    "role": "roles",
    "solution": "solutions",
    "publisher": "publishers",
    "workflow": "workflows",          # Flows (background workflows / modern flows)
    "flowrun": "flowruns",
    "pluginassembly": "pluginassemblies",
    "plugintype": "plugintypes",
    "sdkmessageprocessingstep": "sdkmessageprocessingsteps",
    "processsession": "processsessions",
    # Catalog / connectors
    "connector": "connectors",
    "connectionreference": "connectionreferences",
    "environmentvariabledefinition": "environmentvariabledefinitions",
    "environmentvariablevalue": "environmentvariablevalues",
    # Canvas apps
    "canvasapp": "canvasapps",
    # Custom table (example pattern)
    # "cr123_myentity" → "cr123_myentities"  (append 's', or replace 'y' with 'ies')
}


def get_plural(logical_name: str) -> str:
    """
    Return OData collection name for a given logical entity name.
    Falls back to a naive pluralisation if not in the lookup table.
    """
    if logical_name in PLURAL_NAMES:
        return PLURAL_NAMES[logical_name]
    # Naive fallback: custom tables
    if logical_name.endswith("y"):
        return logical_name[:-1] + "ies"
    return logical_name + "s"


# ── 3. OData / FetchXML Operators ───────────────────────────────────────────
ODATA_OPERATORS: dict[str, str] = {
    # Comparison
    "eq": "equals — e.g. statecode eq 0",
    "ne": "not equal",
    "gt": "greater than",
    "ge": "greater than or equal",
    "lt": "less than",
    "le": "less than or equal",
    # Logical
    "and": "logical AND",
    "or": "logical OR",
    "not": "logical NOT",
    # String functions
    "contains(field,'val')": "field contains substring",
    "startswith(field,'val')": "field starts with",
    "endswith(field,'val')": "field ends with",
    # Null checks
    "field eq null": "field is empty / not set",
    "field ne null": "field has a value",
    # Date functions
    "Microsoft.Dynamics.CRM.Today()": "today (date-only comparison)",
    "Microsoft.Dynamics.CRM.LastXDays(PropertyName='x',PropertyValue=7)": "records from last 7 days",
    "Microsoft.Dynamics.CRM.ThisYear()": "records in current year",
    # Lookup / navigation
    "_fieldname_value eq <guid>": "filter by lookup GUID (use underscore prefix + _value suffix)",
}

FETCHXML_PATTERNS: dict[str, str] = {
    "basic": """
<fetch top='50'>
  <entity name='account'>
    <attribute name='name'/>
    <attribute name='statecode'/>
    <filter>
      <condition attribute='statecode' operator='eq' value='0'/>
    </filter>
  </entity>
</fetch>""",

    "linked_entity": """
<fetch top='50'>
  <entity name='opportunity'>
    <attribute name='name'/>
    <attribute name='estimatedvalue'/>
    <link-entity name='account' from='accountid' to='customerid' alias='acct' link-type='outer'>
      <attribute name='name'/>
    </link-entity>
    <filter>
      <condition attribute='statecode' operator='eq' value='0'/>
    </filter>
  </entity>
</fetch>""",

    "aggregate_count": """
<fetch aggregate='true'>
  <entity name='opportunity'>
    <attribute name='opportunityid' alias='count' aggregate='count'/>
    <attribute name='statecode' groupby='true' alias='state'/>
  </entity>
</fetch>""",

    "aggregate_sum": """
<fetch aggregate='true'>
  <entity name='opportunity'>
    <attribute name='estimatedvalue' alias='total' aggregate='sum'/>
    <attribute name='ownerid' groupby='true' alias='owner'/>
  </entity>
</fetch>""",

    "paginated": """
<!-- First page -->
<fetch top='100' page='1' paging-cookie=''>
  <entity name='contact'>
    <attribute name='fullname'/>
    <attribute name='emailaddress1'/>
  </entity>
</fetch>
<!-- Use @Microsoft.Dynamics.CRM.fetchxmlpagingcookie from response for next page -->""",

    "date_filter": """
<fetch>
  <entity name='task'>
    <attribute name='subject'/>
    <attribute name='scheduledend'/>
    <filter>
      <condition attribute='scheduledend' operator='last-x-days' value='7'/>
      <condition attribute='statecode' operator='eq' value='0'/>
    </filter>
  </entity>
</fetch>""",

    "order_by": """
<fetch top='25'>
  <entity name='lead'>
    <attribute name='fullname'/>
    <attribute name='createdon'/>
    <order attribute='createdon' descending='true'/>
  </entity>
</fetch>""",
}


# ── 4. Flow Templates ────────────────────────────────────────────────────────
FLOW_TEMPLATES: dict[str, dict] = {
    "recurrence": {
        "description": "Scheduled / timer trigger",
        "trigger": {
            "Recurrence": {
                "type": "Recurrence",
                "recurrence": {
                    "frequency": "Day",
                    "interval": 1
                }
            }
        },
    },
    "http_trigger": {
        "description": "Instant / button / HTTP request trigger (can call from canvas app)",
        "trigger": {
            "manual": {
                "type": "Request",
                "kind": "Http",
                "inputs": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "recordId": {"type": "string"}
                        }
                    }
                }
            }
        },
    },
    "dataverse_create": {
        "description": "Fires when a Dataverse row is created",
        "trigger": {
            "When_a_row_is_added": {
                "type": "OpenApiConnectionWebhook",
                "inputs": {
                    "host": {"connectionName": "shared_commondataserviceforapps", "operationId": "SubscribeWebhookTrigger"},
                    "parameters": {
                        "subscriptionRequest/message": 1,       # 1=Create, 2=Delete, 3=Update
                        "subscriptionRequest/entityname": "account",
                        "subscriptionRequest/scope": 4          # 4=Organisation
                    }
                }
            }
        },
    },
    "dataverse_update": {
        "description": "Fires when a Dataverse row is updated",
        "trigger": {
            "When_a_row_is_modified": {
                "type": "OpenApiConnectionWebhook",
                "inputs": {
                    "host": {"connectionName": "shared_commondataserviceforapps", "operationId": "SubscribeWebhookTrigger"},
                    "parameters": {
                        "subscriptionRequest/message": 3,
                        "subscriptionRequest/entityname": "account",
                        "subscriptionRequest/scope": 4
                    }
                }
            }
        },
    },
    "approval": {
        "description": "Start an approval process",
        "action_snippet": {
            "Start_and_wait_for_an_approval": {
                "type": "OpenApiConnection",
                "inputs": {
                    "host": {"connectionName": "shared_approvals", "operationId": "StartAndWaitForAnApproval"},
                    "parameters": {
                        "approvalType": "Approve/Reject - First to respond",
                        "WebhookApprovalCreationInput/title": "Approval request for @{triggerOutputs()?['body/name']}",
                        "WebhookApprovalCreationInput/assignedTo": "approver@org.com",
                        "WebhookApprovalCreationInput/details": "Please review this record."
                    }
                }
            }
        },
    },
    "send_email": {
        "description": "Send an email via Office 365",
        "action_snippet": {
            "Send_an_email_(V2)": {
                "type": "OpenApiConnection",
                "inputs": {
                    "host": {"connectionName": "shared_office365", "operationId": "SendEmailV2"},
                    "parameters": {
                        "emailMessage/To": "recipient@example.com",
                        "emailMessage/Subject": "Subject here",
                        "emailMessage/Body": "<p>Body here</p>"
                    }
                }
            }
        },
    },
}


# ── 5. Connection Reference Names ────────────────────────────────────────────
# Internal connector name used in flow definitions (host.connectionName)
CONNECTION_REFS: dict[str, str] = {
    "Office 365 Outlook": "shared_office365",
    "Microsoft Dataverse": "shared_commondataserviceforapps",
    "Microsoft Dataverse (legacy)": "shared_dynamicscrmonline",
    "SharePoint": "shared_sharepointonline",
    "Microsoft Teams": "shared_teams",
    "Approvals": "shared_approvals",
    "OneDrive for Business": "shared_onedriveforbusiness",
    "Azure DevOps": "shared_visualstudioteamservices",
    "SQL Server": "shared_sql",
    "HTTP": "shared_http",
    "Excel Online (Business)": "shared_excelonlinebusiness",
    "Azure Blob Storage": "shared_azureblob",
    "Service Bus": "shared_servicebus",
    "Event Grid": "shared_eventgrid",
    "Planner": "shared_planner",
    "Forms": "shared_microsoftforms",
    "Power BI": "shared_powerbi",
    "Azure Key Vault": "shared_keyvault",
    "Outlook Tasks": "shared_outlooktasks",
    "LinkedIn": "shared_linkedin",
}


# ── 6. Solution Component Type Codes ────────────────────────────────────────
# Used with AddSolutionComponent API (componenttype parameter)
SOLUTION_COMPONENT_TYPES: dict[str, int] = {
    "Entity": 1,
    "Attribute": 2,
    "Relationship": 3,
    "AttributePicklistValue": 4,
    "AttributeLookupValue": 5,
    "ViewAttribute": 6,
    "LocalizedLabel": 7,
    "RelationshipExtraCondition": 8,
    "OptionSet": 9,
    "EntityRelationship": 10,
    "EntityRelationshipRole": 11,
    "EntityRelationshipRelationships": 12,
    "ManagedProperty": 13,
    "Form": 24,
    "Organization": 25,
    "SavedQuery": 26,          # View
    "Workflow": 29,            # Flow / process
    "Report": 31,
    "ReportEntity": 32,
    "ReportCategory": 33,
    "ReportVisibility": 34,
    "Attachment": 35,
    "EmailTemplate": 36,
    "ContractTemplate": 37,
    "KBArticleTemplate": 38,
    "MailMergeTemplate": 39,
    "DuplicateRule": 44,
    "DuplicateRuleCondition": 45,
    "EntityMap": 46,
    "AttributeMap": 47,
    "RibbonCommand": 48,
    "RibbonContextGroup": 49,
    "RibbonCustomization": 50,
    "RibbonRule": 52,
    "RibbonTabToCommandMap": 53,
    "RibbonDiff": 55,
    "SavedQueryVisualization": 59,  # Chart
    "SystemForm": 60,
    "WebResource": 61,
    "SiteMap": 62,
    "ConnectionRole": 63,
    "FieldSecurityProfile": 70,
    "FieldPermission": 71,
    "PluginType": 90,
    "PluginAssembly": 91,
    "SDKMessageProcessingStep": 92,
    "SDKMessageProcessingStepImage": 93,
    "ServiceEndpoint": 95,
    "Role": 20,
    "RolePrivilege": 21,
    "DisplayString": 22,
    "DisplayStringMap": 23,
    "AppModule": 80,
    "AppModuleRoles": 82,
    "AppModuleComponent": 83,
    "Canvas App": 300,
    "Connector": 371,
    "ConnectionReference": 10330,
    "EnvironmentVariableDefinition": 380,
    "EnvironmentVariableValue": 381,
    "ProcessTrigger": 33,
    "FlowSession": 10,
}


# ── 7. Security Privilege Access Levels ─────────────────────────────────────
PRIVILEGE_LEVELS: dict[str, int] = {
    "None": 0,
    "User": 1,          # Records owned by the user
    "BusinessUnit": 2,  # Records owned by users in the same BU
    "ParentChild": 4,   # Records owned by users in the BU and child BUs
    "Organization": 8,  # All records in the organisation
}

PRIVILEGE_ACTIONS: dict[str, str] = {
    "Create": "prvCreate",
    "Read": "prvRead",
    "Write": "prvWrite",
    "Delete": "prvDelete",
    "Append": "prvAppend",       # Link this record to another
    "AppendTo": "prvAppendTo",   # Allow other records to link to this
    "Assign": "prvAssign",       # Transfer ownership
    "Share": "prvShare",         # Share read/write with another user
}


# ── 8. Canvas App Delegation Rules ──────────────────────────────────────────
DELEGATION_RULES: dict[str, str] = {
    "Delegable (Dataverse)": "Filter, Search, Sort, CountRows, LookUp — fully delegated",
    "Not delegable (Dataverse)": "StartsWith on non-primary columns, complex nested Filters, non-indexed columns in large tables",
    "Delegable (SharePoint)": "Filter eq/ne on indexed columns, StartsWith on Title only",
    "Not delegable (SharePoint)": "Search(), In operator, Filter on non-indexed columns, Sort on computed columns",
    "Delegation limit": "Default 500 records returned locally (can raise to 2000 in Settings). Always use server-side delegation.",
    "Best practice": "Use Dataverse for tables >2000 rows. Add table indexes on columns used in Filter/Sort.",
    "Avoid": "Collecting all records then filtering in memory (ClearCollect then Filter on collection)",
    "Pattern — delegable search": "Filter(MyTable, StartsWith(Name, SearchBox.Text)) — only on primary text column or indexed columns",
    "Pattern — count rows": "CountRows(Filter(MyTable, Status='Active')) — delegated on Dataverse",
}

CANVAS_KEY_PATTERNS: dict[str, str] = {
    "OnStart parallel load": "Concurrent(ClearCollect(colAccounts, Accounts), ClearCollect(colContacts, Contacts))",
    "Navigate with context": "Navigate(DetailScreen, None, {selectedItem: ThisItem})",
    "Current user": "User().Email  — basic; Office365Users.MyProfile() for full profile",
    "Form submit + navigate": "SubmitForm(EditForm1); If(IsEmpty(EditForm1.Errors), Navigate(ListScreen))",
    "New record": "NewForm(EditForm1); Navigate(EditScreen)",
    "Edit record": "EditForm(EditForm1); Navigate(EditScreen, None, {item: ThisItem})",
    "Delete record": "Remove(MyTable, Gallery1.Selected); Notify('Deleted', NotificationType.Success)",
    "Named formula (App.Formulas)": "envBaseUrl = 'https://org.crm.dynamics.com'  — lazy, no OnStart needed",
    "Global variable": "Set(gSelectedRecord, Gallery1.Selected)  — use sparingly",
    "Context variable": "UpdateContext({isLoading: true})  — screen-scoped",
    "Error handling": "If(IsError(result), Notify(FirstError.Message, NotificationType.Error))",
    "Collection ops": "Collect, ClearCollect, RemoveIf, UpdateIf — all work on in-memory collections",
    "Refresh table": "Refresh(MyTable)  — re-fetches from data source",
    "Patch (upsert)": "Patch(Accounts, Defaults(Accounts), {Name: txtName.Text, Phone: txtPhone.Text})",
    "Patch (update)": "Patch(Accounts, LookUp(Accounts, AccountId=GUID('...')), {Name: 'New Name'})",
}


# ── 9. Full Reference String for System Prompt ──────────────────────────────
def build_reference() -> str:
    """Return a compact reference block for injection into the agent system prompt."""
    col_types = "\n".join(f"  - {k}: {v['AttributeType']}" for k, v in COLUMN_TYPES.items())
    plural_sample = "\n".join(f"  - {k} → {v}" for k, v in list(PLURAL_NAMES.items())[:20])
    priv_levels = "\n".join(f"  - {k} = {v}" for k, v in PRIVILEGE_LEVELS.items())
    deleg = "\n".join(f"  - {k}: {v}" for k, v in DELEGATION_RULES.items())
    canvas_pats = "\n".join(f"  - {k}: `{v}`" for k, v in CANVAS_KEY_PATTERNS.items())
    flow_tpls = "\n".join(f"  - {k}: {v['description']}" for k, v in FLOW_TEMPLATES.items())
    conn_refs = "\n".join(f"  - {k} → {v}" for k, v in list(CONNECTION_REFS.items())[:10])
    sol_comps = "\n".join(f"  - {k} = {v}" for k, v in list(SOLUTION_COMPONENT_TYPES.items())[:15])

    return f"""
## Power Platform Reference

### Dataverse Column Types (create_table / add_column)
{col_types}

### OData Collection Names (plural — use in Web API URLs)
Key mappings (opportunity → opportunities, incident → incidents, workflow → workflows):
{plural_sample}
Rule: custom tables ending in 'y' → 'ies'; else append 's'.

### Security Privilege Levels
{priv_levels}
Actions: Create, Read, Write, Delete, Append, AppendTo, Assign, Share.

### FetchXML Operators
- eq, ne, gt, ge, lt, le — comparison
- like, not-like — wildcard (% = any chars)
- in, not-in — list membership
- null, not-null — empty check
- last-x-days, next-x-days, this-year, today — date shortcuts
- contains-values, does-not-contain-values — multi-select choice fields
aggregate attributes: count, sum, avg, min, max (use fetch aggregate='true')

### OData $filter Patterns
- Equality: statecode eq 0
- Lookup: _ownerid_value eq <guid>
- Contains: contains(name,'Contoso')
- Null check: emailaddress1 ne null
- Date: closedate lt 2025-01-01T00:00:00Z
- Combine: statecode eq 0 and contains(name,'Corp')

### Flow Trigger Templates
{flow_tpls}
Connection ref names: Dataverse → shared_commondataserviceforapps, Outlook → shared_office365
{conn_refs}

### Solution Component Type Codes (AddSolutionComponent API)
{sol_comps}

### Canvas App Delegation Guide
{deleg}

### Canvas Power Fx Key Patterns
{canvas_pats}

### Important Rules
1. Always use the correct plural OData collection name in Web API URLs.
2. When adding columns, include the @odata.type matching the attribute type.
3. Lookup columns require a separate relationship — create the column then the relationship.
4. Security privilege levels: 0=None 1=User 2=BU 4=ParentChild 8=Org.
5. Flows: use shared_commondataserviceforapps for Dataverse connections.
6. FetchXML must be URL-encoded when sent in GET requests (use POST /$batch or the fetchXml query param).
7. Canvas apps: always check delegation — non-delegable formulas silently miss records beyond the limit.
8. Solution ALM: always work in a solution; use environment variables for config that changes per env.
"""


# Singleton — build once, reuse
REFERENCE = build_reference()
