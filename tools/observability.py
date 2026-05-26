"""
tools/observability.py — Agent Observability
=============================================
Logs every agent interaction to a local SQLite database.
Provides a JSON API endpoint helper for the right-panel trace viewer.

Schema:
  traces(id, ts, session_id, user_msg, reply, provider, model,
         tool_count, total_ms, tool_trace_json)
"""

from __future__ import annotations
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "observability.db"


# ── Database setup ────────────────────────────────────────────────────────────

def _init_db() -> None:
    """Create tables if they don't exist."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                id           TEXT PRIMARY KEY,
                ts           REAL NOT NULL,
                session_id   TEXT NOT NULL DEFAULT '',
                user_msg     TEXT NOT NULL DEFAULT '',
                reply        TEXT NOT NULL DEFAULT '',
                provider     TEXT NOT NULL DEFAULT '',
                model        TEXT NOT NULL DEFAULT '',
                tool_count   INTEGER NOT NULL DEFAULT 0,
                total_ms     INTEGER NOT NULL DEFAULT 0,
                tool_trace   TEXT NOT NULL DEFAULT '[]'
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_traces_ts
            ON traces(ts DESC)
        """)


@contextmanager
def _conn():
    """Thread-safe SQLite connection context manager."""
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ── WRITE ─────────────────────────────────────────────────────────────────────

def log_trace(
    user_msg:    str,
    reply:       str,
    tool_trace:  list,
    provider:    str = "",
    model:       str = "",
    total_ms:    int = 0,
    session_id:  str = "",
) -> str:
    """
    Persist one agent interaction. Returns the trace ID.

    tool_trace should be the list returned by run_agent()["tool_trace"]:
        [{"name": str, "args": dict, "result": str, "ms": int, "type": str}]
    """
    _init_db()
    trace_id = str(uuid.uuid4())
    ts       = time.time()

    # Sanitise long fields
    user_msg_s = user_msg[:2000] if user_msg else ""
    reply_s    = reply[:4000]    if reply    else ""

    # Redact credentials in tool trace before storing
    try:
        from tools.guardrails import sanitise_for_log
        safe_trace = []
        for entry in (tool_trace or []):
            safe_trace.append({
                "name":   entry.get("name", ""),
                "args":   entry.get("args", {}),
                "result": sanitise_for_log(str(entry.get("result", "")), max_chars=300),
                "ms":     entry.get("ms", 0),
                "type":   entry.get("type", "read"),
            })
    except Exception:
        safe_trace = []

    with _conn() as con:
        con.execute(
            """INSERT INTO traces
               (id, ts, session_id, user_msg, reply, provider, model,
                tool_count, total_ms, tool_trace)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                trace_id,
                ts,
                session_id or "",
                user_msg_s,
                reply_s,
                provider,
                model,
                len(tool_trace or []),
                total_ms,
                json.dumps(safe_trace, default=str),
            ),
        )

    return trace_id


# ── READ ──────────────────────────────────────────────────────────────────────

def get_traces(limit: int = 50, offset: int = 0) -> list[dict]:
    """Return the most recent traces as a list of dicts."""
    _init_db()
    with _conn() as con:
        rows = con.execute(
            """SELECT id, ts, session_id, user_msg, reply, provider, model,
                      tool_count, total_ms, tool_trace
               FROM traces
               ORDER BY ts DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()

    result = []
    for row in rows:
        try:
            tool_trace = json.loads(row["tool_trace"])
        except Exception:
            tool_trace = []

        result.append({
            "id":          row["id"],
            "ts":          row["ts"],
            "session_id":  row["session_id"],
            "user_msg":    row["user_msg"],
            "reply":       row["reply"][:500] if row["reply"] else "",
            "provider":    row["provider"],
            "model":       row["model"],
            "tool_count":  row["tool_count"],
            "total_ms":    row["total_ms"],
            "tool_trace":  tool_trace,
        })

    return result


def get_trace_stats() -> dict:
    """Return aggregate stats for the observability dashboard."""
    _init_db()
    with _conn() as con:
        total = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        avg_ms = con.execute(
            "SELECT AVG(total_ms) FROM traces WHERE total_ms > 0"
        ).fetchone()[0] or 0
        avg_tools = con.execute(
            "SELECT AVG(tool_count) FROM traces"
        ).fetchone()[0] or 0
        by_provider = con.execute(
            "SELECT provider, COUNT(*) as cnt FROM traces GROUP BY provider ORDER BY cnt DESC"
        ).fetchall()

    return {
        "total_traces": total,
        "avg_response_ms": round(avg_ms),
        "avg_tools_per_turn": round(avg_tools, 1),
        "by_provider": [{"provider": r[0], "count": r[1]} for r in by_provider],
    }


def clear_traces(older_than_days: int = 30) -> dict:
    """Delete traces older than N days."""
    _init_db()
    cutoff = time.time() - (older_than_days * 86400)
    with _conn() as con:
        deleted = con.execute(
            "DELETE FROM traces WHERE ts < ?", (cutoff,)
        ).rowcount
    return {"deleted": deleted}
