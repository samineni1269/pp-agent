"""
tools/knowledge.py — RAG Knowledge Base
=========================================
Persists notes, org context, and past tool results for retrieval.
Uses a simple JSON store with keyword search (no vector DB required).
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from datetime import datetime

KB_FILE = Path(__file__).parent.parent / "knowledge_base.json"


def _load() -> list[dict]:
    if KB_FILE.exists():
        try:
            return json.loads(KB_FILE.read_text())
        except Exception:
            return []
    return []


def _save(entries: list[dict]) -> None:
    KB_FILE.write_text(json.dumps(entries, indent=2, default=str))


# ── READ ──────────────────────────────────────────────────────────────────────

def search_knowledge_base(query: str, top: int = 10) -> list[dict]:
    """Search the knowledge base for relevant entries."""
    entries = _load()
    query_lower = query.lower()
    scored = []
    for e in entries:
        text = (e.get("title", "") + " " + e.get("content", "") + " " + " ".join(e.get("tags", []))).lower()
        score = sum(1 for word in query_lower.split() if word in text)
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:top]]


def list_knowledge_entries(tag: str | None = None) -> list[dict]:
    """List all knowledge entries, optionally filtered by tag."""
    entries = _load()
    if tag:
        entries = [e for e in entries if tag in e.get("tags", [])]
    return sorted(entries, key=lambda x: x.get("created", ""), reverse=True)


def get_entry(entry_id: str) -> dict | None:
    """Get a specific knowledge entry by ID."""
    return next((e for e in _load() if e.get("id") == entry_id), None)


# ── WRITE ─────────────────────────────────────────────────────────────────────

def add_knowledge_entry(title: str, content: str, tags: list[str] | None = None) -> dict:
    """Add a new entry to the knowledge base."""
    entries = _load()
    entry = {
        "id":      f"kb_{len(entries)+1:04d}",
        "title":   title,
        "content": content,
        "tags":    tags or [],
        "created": datetime.now().isoformat(),
    }
    entries.append(entry)
    _save(entries)
    return {"success": True, "id": entry["id"]}


def update_knowledge_entry(entry_id: str, content: str | None = None, tags: list[str] | None = None) -> dict:
    """Update an existing knowledge entry."""
    entries = _load()
    for e in entries:
        if e["id"] == entry_id:
            if content is not None: e["content"] = content
            if tags    is not None: e["tags"]    = tags
            e["updated"] = datetime.now().isoformat()
            _save(entries)
            return {"success": True}
    return {"error": f"Entry {entry_id} not found"}


def delete_knowledge_entry(entry_id: str) -> dict:
    """Delete a knowledge entry."""
    entries = _load()
    new = [e for e in entries if e["id"] != entry_id]
    if len(new) == len(entries):
        return {"error": f"Entry {entry_id} not found"}
    _save(new)
    return {"success": True, "deleted": entry_id}
