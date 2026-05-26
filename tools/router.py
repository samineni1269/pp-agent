"""
tools/router.py — Multi-Agent Domain Router
============================================
Classifies each user message into one of 6 specialist domains,
then returns the matching system prompt addendum and tool subset.

Routing strategy:
  1. Fast keyword match (no LLM cost)
  2. LLM fallback for ambiguous messages

Domains:
  dataverse  — tables, columns, relationships, records, FetchXML
  flows      — Power Automate, cloud flows, triggers, connectors
  security   — roles, users, DLP, field security, permissions
  crm        — leads, opportunities, accounts, contacts, cases
  admin      — environments, ALM, solutions, Fabric, Power BI
  general    — anything else (full tool set, no restriction)
"""

from __future__ import annotations
import re

# ── Domain definitions ────────────────────────────────────────────────────────

DOMAINS = ("dataverse", "flows", "security", "crm", "admin", "general")

_KEYWORDS: dict[str, list[str]] = {
    "dataverse": [
        "table", "column", "field", "record", "fetchxml", "odata",
        "relationship", "lookup", "choice", "dataverse", "entity",
        "schema", "publisher", "primary key", "query", "create record",
        "update record", "delete record", "custom table", "bulk",
    ],
    "flows": [
        "flow", "automate", "trigger", "action", "connector",
        "cloud flow", "scheduled flow", "instant flow", "run", "failed flow",
        "power automate", "http trigger", "recurrence", "approval flow",
        "logic app", "workflow",
    ],
    "security": [
        "security role", "permission", "privilege", "dlp", "dlp policy",
        "user access", "assign role", "field security", "business unit",
        "system admin", "data loss prevention", "sharing", "owner",
        "access level", "field level security",
    ],
    "crm": [
        "lead", "opportunity", "account", "contact", "case", "d365",
        "dynamics 365", "crm", "pipeline", "sales", "customer",
        "revenue", "close date", "quote", "order", "invoice",
    ],
    "admin": [
        "environment", "solution", "alm", "pipeline", "deploy",
        "export solution", "import solution", "fabric", "power bi",
        "workspace", "lakehouse", "notebook", "capacity", "publisher",
        "managed solution", "unmanaged", "version",
    ],
}

# System-prompt addendum per domain (injected into the agent's system prompt)
_DOMAIN_ADDENDA: dict[str, str] = {
    "dataverse": (
        "## Domain: Dataverse Specialist\n"
        "Focus on schema design, data modelling, record operations, and FetchXML. "
        "Always use logical names (e.g. account, new_customtable). "
        "Check existing columns before creating new ones."
    ),
    "flows": (
        "## Domain: Power Automate Specialist\n"
        "Focus on cloud flows — triggers, actions, connections, and error handling. "
        "Always check flow status before enabling/disabling. "
        "For failing flows, get error details before suggesting fixes."
    ),
    "security": (
        "## Domain: Security Specialist\n"
        "Focus on security roles, DLP policies, field security, and user permissions. "
        "Never create duplicate roles. Check existing privileges before modifying. "
        "Minimum-privilege principle: suggest least access required."
    ),
    "crm": (
        "## Domain: D365 CRM Specialist\n"
        "Focus on leads, opportunities, accounts, contacts, and the sales pipeline. "
        "Always use correct CRM entity names. Respect ownership and assignment rules."
    ),
    "admin": (
        "## Domain: Platform Admin Specialist\n"
        "Focus on environments, ALM pipelines, solutions, Fabric, and Power BI. "
        "Export solutions as managed for production deployments. "
        "Always validate the target environment before imports."
    ),
    "general": (
        "## Domain: General Power Platform\n"
        "Full access to all tools. Use whichever domain tools are most relevant."
    ),
}


# ── Classifier ────────────────────────────────────────────────────────────────

def classify(user_message: str, llm_fallback: bool = True) -> str:
    """
    Classify a user message into one of the 6 domains.
    Returns one of: "dataverse", "flows", "security", "crm", "admin", "general"
    """
    text = user_message.lower()

    # 1. Fast keyword scoring
    scores: dict[str, int] = {d: 0 for d in DOMAINS if d != "general"}
    for domain, keywords in _KEYWORDS.items():
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                scores[domain] += 1

    best_domain = max(scores, key=lambda d: scores[d])
    best_score  = scores[best_domain]

    if best_score >= 2:
        return best_domain

    if best_score == 1:
        # Single keyword match — confident enough without LLM
        return best_domain

    # 2. LLM fallback for ambiguous messages
    if llm_fallback:
        return _llm_classify(user_message)

    return "general"


def _llm_classify(user_message: str) -> str:
    """Ask the LLM to classify the domain."""
    try:
        from tools.llm_provider import call_llm
        result = call_llm(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a router for a Power Platform agent. "
                        "Classify the user's message into exactly one of these domains: "
                        "dataverse, flows, security, crm, admin, general. "
                        "Reply with ONLY the domain name, nothing else."
                    ),
                },
                {"role": "user", "content": user_message[:500]},
            ],
            temperature=0.0,
            max_tokens=10,
        )
        domain = (result.get("content") or "general").strip().lower()
        return domain if domain in DOMAINS else "general"
    except Exception:
        return "general"


def get_domain_addendum(domain: str) -> str:
    """Return the system-prompt addendum for the given domain."""
    return _DOMAIN_ADDENDA.get(domain, _DOMAIN_ADDENDA["general"])


def get_domain_label(domain: str) -> str:
    """Human-readable label for a domain."""
    labels = {
        "dataverse": "🗄️  Dataverse",
        "flows":     "⚡ Power Automate",
        "security":  "🔒 Security",
        "crm":       "💼 CRM",
        "admin":     "🏛️  Admin",
        "general":   "🤖 General",
    }
    return labels.get(domain, domain.title())
