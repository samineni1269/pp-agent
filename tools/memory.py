"""
tools/memory.py — Persistent Agent Memory
==========================================
Stores key facts and summaries across sessions in a local JSON file.
Used by the agent to remember previous conversations, org facts, and preferences.
"""

from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime

MEMORY_FILE = Path(__file__).parent.parent / "agent_memory.json"


def _load() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    MEMORY_FILE.write_text(json.dumps(data, indent=2, default=str))


# ── READ ──────────────────────────────────────────────────────────────────────

def get_memory(key: str | None = None) -> dict | str | None:
    """Get all memory or a specific key."""
    mem = _load()
    if key:
        return mem.get(key)
    return mem


def list_memory_keys() -> list[str]:
    """List all stored memory keys."""
    return list(_load().keys())


def search_memory(query: str) -> dict:
    """Search memory values for a keyword."""
    mem = _load()
    results = {}
    q = query.lower()
    for k, v in mem.items():
        if q in k.lower() or q in str(v).lower():
            results[k] = v
    return results


# ── WRITE ─────────────────────────────────────────────────────────────────────

def set_memory(key: str, value: str) -> dict:
    """Store a value in persistent memory."""
    mem = _load()
    mem[key] = {"value": value, "updated": datetime.now().isoformat()}
    _save(mem)
    return {"success": True, "key": key}


def append_memory(key: str, value: str) -> dict:
    """Append to an existing memory list, or create it."""
    mem = _load()
    existing = mem.get(key, {})
    if isinstance(existing, dict) and "items" in existing:
        existing["items"].append({"value": value, "time": datetime.now().isoformat()})
    else:
        existing = {"items": [{"value": value, "time": datetime.now().isoformat()}]}
    mem[key] = existing
    _save(mem)
    return {"success": True, "key": key, "count": len(existing["items"])}


def delete_memory(key: str) -> dict:
    """Delete a memory key."""
    mem = _load()
    if key not in mem:
        return {"error": f"Key '{key}' not found"}
    del mem[key]
    _save(mem)
    return {"success": True, "deleted": key}


def clear_all_memory() -> dict:
    """Wipe all memory (destructive!)."""
    _save({})
    return {"success": True, "message": "All memory cleared"}


def summarise_memory() -> str:
    """Return a human-readable summary of stored memory for prompt injection."""
    mem = _load()
    if not mem:
        return "No persistent memory stored yet."
    lines = []
    for k, v in list(mem.items())[:20]:
        if isinstance(v, dict) and "value" in v:
            lines.append(f"- {k}: {v['value']}")
        elif isinstance(v, dict) and "items" in v:
            items = v["items"]
            lines.append(f"- {k}: {len(items)} entries, latest: {items[-1]['value'] if items else '—'}")
        else:
            lines.append(f"- {k}: {str(v)[:80]}")
    return "\n".join(lines)


# ── AGENT-FACING ALIASES (used by agent.py dispatch & system prompt) ──────────

def update_memory_entry(key: str, value: str) -> dict:
    """
    Agent-facing alias for set_memory().
    Called by the LLM tool 'update_memory_entry'.
    """
    return set_memory(key, value)


def get_memory_summary() -> dict:
    """
    Return a structured summary of all memory keys.
    Called by the LLM tool 'get_memory_summary'.
    """
    mem = _load()
    if not mem:
        return {"message": "No persistent memory stored yet.", "entries": []}
    entries = []
    for k, v in list(mem.items())[:30]:
        if isinstance(v, dict) and "value" in v:
            entries.append({"key": k, "value": v["value"], "updated": v.get("updated", "")})
        elif isinstance(v, dict) and "items" in v:
            items = v["items"]
            latest = items[-1]["value"] if items else ""
            entries.append({"key": k, "value": f"{len(items)} entries (latest: {latest})", "updated": ""})
        else:
            entries.append({"key": k, "value": str(v)[:200], "updated": ""})
    return {"entries": entries, "total": len(mem)}


def get_relevant_memory_context(query: str = "") -> str:
    """
    Return memory facts most relevant to the current query.
    If query is empty, returns all facts (up to 20 entries).
    Injected into the system prompt under '## What I Know About Your Org'.
    """
    mem = _load()
    if not mem:
        return ""

    lines = []
    query_lower = query.lower()

    # Score each entry: exact key/value match scores higher
    scored = []
    for k, v in mem.items():
        val_str = ""
        if isinstance(v, dict) and "value" in v:
            val_str = v["value"]
        elif isinstance(v, dict) and "items" in v:
            items = v.get("items", [])
            val_str = items[-1]["value"] if items else ""
        else:
            val_str = str(v)

        if query_lower:
            score = 0
            if query_lower in k.lower():
                score += 2
            if query_lower in val_str.lower():
                score += 1
            scored.append((score, k, val_str))
        else:
            scored.append((0, k, val_str))

    # Sort by relevance descending, take top 15
    scored.sort(key=lambda x: x[0], reverse=True)
    for score, k, val_str in scored[:15]:
        if val_str:
            lines.append(f"- **{k}**: {val_str[:200]}")

    return "\n".join(lines) if lines else ""
