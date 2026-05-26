"""
app.py — PP Agent Web UI
========================
Flask server that serves the Power Platform Agent browser interface.
Each tool section has its own conversation history and chat workspace.

Run:
    python app.py
Then open:  http://localhost:5005
"""

from __future__ import annotations
import os
import sys
import json
import uuid
import time
import queue
import sqlite3
import threading
import traceback
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, Response, render_template_string, stream_with_context, session, redirect, url_for
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

# ── .env helpers ──────────────────────────────────────────────────────────────
ENV_PATH = Path(__file__).parent / ".env"

def _read_env_file() -> dict[str, str]:
    """Read current .env values (raw strings)."""
    if not ENV_PATH.exists():
        return {}
    result = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result

def _write_env_file(updates: dict[str, str]) -> None:
    """Update specific keys in .env, preserve comments and order."""
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()
    else:
        lines = []
    # Map existing keys → line index
    key_idx: dict[str, int] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip()
            key_idx[k] = i
    # Apply updates
    for k, v in updates.items():
        if k in key_idx:
            lines[key_idx[k]] = f"{k}={v}"
        else:
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    load_dotenv(override=True)
    # Refresh os.environ for current process
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        elif k in os.environ:
            del os.environ[k]

_SENSITIVE = {"MINIMAX_API_KEY","ANTHROPIC_API_KEY","OPENAI_API_KEY","GEMINI_API_KEY",
              "OPENROUTER_API_KEY","PP_CLIENT_SECRET","ADO_PAT","FLASK_SECRET"}

def _mask(k: str, v: str) -> str:
    if k in _SENSITIVE and v and not v.startswith("your_"):
        return v[:4] + "•" * max(0, len(v) - 8) + v[-4:] if len(v) > 8 else "••••••••"
    return v

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", os.urandom(32).hex())

# ── Password gate — set APP_PASSWORD env var to enable ────────────────────────
_APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PP Agent — Sign In</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0c0c0d; color:#e8e8ec; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         min-height:100vh; display:flex; align-items:center; justify-content:center; }
  .card { background:#111113; border:1px solid #1c1c1e; border-radius:16px; padding:40px 48px;
          width:360px; text-align:center; }
  .logo { font-size:36px; margin-bottom:16px; }
  h1 { font-size:18px; font-weight:600; margin-bottom:6px; }
  p  { font-size:13px; color:#56566a; margin-bottom:28px; }
  input { width:100%; background:#141416; border:1px solid #1c1c1e; border-radius:8px;
          color:#e8e8ec; font-size:14px; padding:10px 14px; outline:none; margin-bottom:14px; }
  input:focus { border-color:#5c6bc0; }
  button { width:100%; background:#5c6bc0; border:none; border-radius:8px; color:#fff;
           font-size:14px; font-weight:600; padding:11px; cursor:pointer; }
  button:hover { background:#4a5aae; }
  .err { color:#f87171; font-size:13px; margin-top:10px; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">🔮</div>
  <h1>PP Agent</h1>
  <p>Power Platform AI Assistant</p>
  <form method="post" action="/login">
    <input type="password" name="password" placeholder="Enter access password" autofocus>
    <button type="submit">Access Agent</button>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
  </form>
</div>
</body>
</html>"""


def _login_required(f):
    """Decorator: redirect to /login if APP_PASSWORD is set and user isn't authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _APP_PASSWORD and not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == _APP_PASSWORD:
            session["authenticated"] = True
            return redirect("/")
        error = "Incorrect password — try again."
    return render_template_string(_LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.before_request
def _require_auth():
    """Block all routes (except /login /logout) if APP_PASSWORD is set and user not authenticated."""
    if not _APP_PASSWORD:
        return  # password gate disabled
    public = {"/login", "/logout"}
    if request.path not in public and not session.get("authenticated"):
        # API calls get 401, browser requests get redirect
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not authenticated"}), 401
        return redirect(url_for("login"))


# ── SQLite-backed conversation history ────────────────────────────────────────
_DB_PATH = Path(__file__).parent / "chat_history.db"

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS histories (
            tool_id    TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

_DB_CONN  = _db_conn()
_DB_LOCK  = threading.Lock()

def _load_all_histories() -> dict[str, list]:
    try:
        with _DB_LOCK:
            rows = _DB_CONN.execute("SELECT tool_id, data FROM histories").fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}
    except Exception:
        return {}

def _persist_history(tool_id: str, history: list) -> None:
    try:
        with _DB_LOCK:
            _DB_CONN.execute(
                "INSERT OR REPLACE INTO histories (tool_id, data, updated_at) VALUES (?, ?, ?)",
                (tool_id, json.dumps(history, default=str), datetime.now().isoformat()),
            )
            _DB_CONN.commit()
    except Exception:
        pass

# Load persisted histories at startup
_histories: dict[str, list] = _load_all_histories()

# ── Pending writes waiting for confirmation ───────────────────────────────────
_pending_writes: dict[str, dict] = {}

# ── Active SSE streaming jobs ─────────────────────────────────────────────────
_stream_jobs: dict[str, dict] = {}

# ── Active session info ───────────────────────────────────────────────────────
_session = {
    "provider": os.getenv("LLM_PROVIDER", ""),
    "model":    os.getenv("LLM_MODEL",    ""),
    "tool_calls_made": 0,
    "messages_sent":   0,
    "start_time":      datetime.now().isoformat(),
}

# ── Navigation config ─────────────────────────────────────────────────────────
TOOLS_NAV = [
    # ── COMMAND CENTER — first, always ────────────────────────────────────────
    {
        "id": "command",
        "label": "Command Center",
        "icon": "✨",
        "color": "#818cf8",
        "is_command": True,
        "chips": [
            "Full environment health check — show everything broken",
            "List all solutions and the flows inside each one",
            "What apps exist and which tables/flows do they use?",
            "Show open opportunities, related accounts and active flows",
            "Deploy ContosoSalesHub solution to production",
            "Find all disabled flows and re-enable them",
            "Show my security roles, DLP policies and environment capacity",
            "Scan for any failing plugins, flows, or portal issues",
        ],
    },
    # ── INDIVIDUAL TOOLS ──────────────────────────────────────────────────────
    {
        "id": "solutions",
        "label": "Solutions",
        "icon": "🗂️",
        "color": "#6366f1",
        "chips": [
            "List all solutions in my environment",
            "Export solution as managed",
            "Import solution from file",
            "Create new solution",
            "Publish all customisations",
            "Check solution health and ALM compliance",
            "Show solution layers and customisation order",
            "Add a component to a solution",
            "Delete solution from environment",
        ],
    },
    {
        "id": "canvas",
        "label": "Canvas Apps",
        "icon": "🎨",
        "color": "#8b5cf6",
        "chips": [
            "List all canvas apps",
            "Export canvas app",
            "Show canvas app details and connections",
        ],
    },
    {
        "id": "mda",
        "label": "Model-Driven Apps",
        "icon": "🏗️",
        "color": "#a855f7",
        "chips": [
            "List model-driven apps",
            "Show app site map and navigation",
            "List app modules and roles",
        ],
    },
    {
        "id": "flows",
        "label": "Power Automate",
        "icon": "⚡",
        "color": "#0ea5e9",
        "chips": [
            "List all cloud flows",
            "Show failed flow runs",
            "Enable / disable a flow",
            "Trigger a flow manually",
            "Create a new scheduled flow",
            "Show error details for a failed flow run",
            "Scan all flows and list every failure",
            "Delete a flow permanently",
        ],
    },
    {
        "id": "copilot",
        "label": "Copilot Studio",
        "icon": "🤖",
        "color": "#06b6d4",
        "chips": [
            "List all copilot agents",
            "Show agent topics and trigger phrases",
            "Publish an agent to its channels",
        ],
    },
    {
        "id": "pages",
        "label": "Power Pages",
        "icon": "🌐",
        "color": "#10b981",
        "chips": [
            "List portal sites",
            "Show portal table permissions",
            "List portal web roles",
        ],
    },
    {
        "id": "powerbi",
        "label": "Power BI",
        "icon": "📊",
        "color": "#f59e0b",
        "chips": [
            "List workspaces",
            "Show datasets in workspace",
            "Trigger dataset refresh",
            "List reports",
        ],
    },
    {
        "id": "fabric",
        "label": "Microsoft Fabric",
        "icon": "🧵",
        "color": "#ef4444",
        "chips": [
            "List all Fabric workspaces",
            "List Fabric capacities and SKUs",
            "List lakehouses in a workspace",
            "List notebooks in a workspace",
            "List data pipelines in a workspace",
            "Run a Fabric notebook",
        ],
    },
    {
        "id": "dataverse",
        "label": "Dataverse",
        "icon": "🗄️",
        "color": "#3b82f6",
        "chips": [
            "List all tables",
            "Query records from a table",
            "Run FetchXML query",
            "Show table schema and columns",
            "Create a new custom table",
            "Add a column to a table",
            "Create a lookup relationship between tables",
            "Create a many-to-many relationship",
            "Create a global choice column",
            "Create a new record",
            "Update a record",
            "Delete a record",
            "Bulk create records from data",
            "Count records in a table",
        ],
    },
    {
        "id": "plugins",
        "label": "Plugins",
        "icon": "🔌",
        "color": "#64748b",
        "chips": [
            "List plugin assemblies",
            "Show plugin steps for a plugin",
            "Audit all plugins — isolation mode and health",
        ],
    },
    {
        "id": "crm",
        "label": "D365 CRM",
        "icon": "💼",
        "color": "#d97706",
        "chips": [
            "Show open opportunities",
            "List active leads",
            "List accounts",
            "Create a new CRM record",
            "Show sales pipeline summary",
        ],
    },
    {
        "id": "connectors",
        "label": "Custom Connectors",
        "icon": "🔗",
        "color": "#ec4899",
        "chips": [
            "List custom connectors",
            "Create a custom connector from OpenAPI spec",
        ],
    },
    {
        "id": "alm",
        "label": "ALM Pipelines",
        "icon": "🚀",
        "color": "#8b5cf6",
        "chips": [
            "Show deployment pipelines",
            "Run ALM deployment pipeline",
        ],
    },
    {
        "id": "ado",
        "label": "Azure DevOps",
        "icon": "⚙️",
        "color": "#0284c7",
        "chips": [
            "List Azure DevOps repositories",
            "Trigger an ADO pipeline build",
        ],
    },
    {
        "id": "environments",
        "label": "Environments",
        "icon": "🌍",
        "color": "#16a34a",
        "chips": [
            "List all environments",
            "Show environment capacity usage",
            "Create a sandbox environment",
        ],
    },
    {
        "id": "security",
        "label": "Security & DLP",
        "icon": "🔒",
        "color": "#dc2626",
        "chips": [
            "List security roles",
            "Show DLP policies",
            "Get roles assigned to a user",
            "Assign a role to a user",
            "Clone a security role",
            "Set table privileges on a role",
            "List field security profiles",
        ],
    },
    {
        "id": "monitor",
        "label": "Monitor",
        "icon": "📈",
        "color": "#7c3aed",
        "chips": [
            "Check environment health",
            "List audit log events",
            "Show Dataverse storage consumption",
            "Audit all plugins for health issues",
            "Scan all flows for recent failures",
        ],
    },
    {
        "id": "knowledge",
        "label": "Knowledge Base",
        "icon": "📚",
        "color": "#0891b2",
        "chips": [
            "Search knowledge base for PP guidance",
            "Save a fact to persistent memory",
        ],
    },
    {
        "id": "settings",
        "label": "Settings",
        "icon": "⚙️",
        "color": "#6b7280",
        "chips": [],
        "is_config": True,
    },
]

# ── Known environments (for quick-switch pills in the UI) ────────────────────

def _get_known_envs() -> list[dict]:
    """
    Read the PP_ENVIRONMENTS env var — a JSON array of {name, url} dicts.
    Falls back to the single PP_ENV_URL / PP_ENV_NAME pair.

    Example .env entry:
        PP_ENVIRONMENTS=[{"name":"Dev","url":"https://orgXXX.crm11.dynamics.com/"},
                         {"name":"Prod","url":"https://orgYYY.crm11.dynamics.com/"}]
    """
    raw = os.getenv("PP_ENVIRONMENTS", "").strip()
    if raw:
        try:
            envs = json.loads(raw)
            if isinstance(envs, list):
                return [e for e in envs if e.get("url")]
        except Exception:
            pass
    # Fallback to single env
    url  = os.getenv("PP_ENV_URL",  "").strip()
    name = os.getenv("PP_ENV_NAME", "Dev").strip()
    return [{"name": name, "url": url}] if url else []


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
@_login_required
def index():
    return render_template_string(HTML_TEMPLATE, tools=TOOLS_NAV)


@app.route("/api/status")
def api_status():
    """Auth + session status for the right panel."""
    try:
        from tools.auth import get_auth_status
        auth = get_auth_status()
    except Exception as e:
        auth = {"connected": False, "error": str(e)}

    try:
        from tools.llm_provider import detect_provider, get_active_model
        provider = _session["provider"] or detect_provider()
        model    = _session["model"]    or get_active_model(provider)
    except Exception:
        provider = "unknown"
        model    = "unknown"

    return jsonify({
        "auth":          auth,
        "provider":      provider,
        "model":         model,
        "tool_calls":    _session["tool_calls_made"],
        "messages_sent": _session["messages_sent"],
        "start_time":    _session["start_time"],
    })


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """
    Restart the Flask server without touching the terminal.

    Strategy: spawn a detached bash script that
      1. waits 1.5 s for Flask to release the port
      2. kills the current PID
      3. starts a fresh Python process using the same executable + args
    The new process inherits the venv because sys.executable is the full
    path to the venv's python binary.
    """
    pid     = os.getpid()
    cwd     = os.path.abspath(os.path.dirname(__file__))
    python  = sys.executable                        # e.g. .../venv/bin/python3
    args    = " ".join(f"'{a}'" for a in sys.argv)  # e.g. 'app.py'

    script = (
        f"sleep 1.5 && "
        f"kill {pid} 2>/dev/null; "
        f"sleep 0.5 && "
        f"cd '{cwd}' && "
        f"'{python}' {args} "
        f"> '{cwd}/server.log' 2>&1 &"
    )

    def _spawn():
        time.sleep(0.3)   # let HTTP response reach the browser first
        subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,   # detach so it survives when we die
        )

    threading.Thread(target=_spawn, daemon=True).start()
    return jsonify({"ok": True, "message": "Restarting…"})


@app.route("/api/models")
def api_models():
    """Return model list for the picker."""
    try:
        from tools.llm_provider import MODEL_OPTIONS
        return jsonify(MODEL_OPTIONS)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/switch-model", methods=["POST"])
def api_switch_model():
    data = request.get_json(force=True)
    _session["provider"] = data.get("provider", "")
    _session["model"]    = data.get("model",    "")
    return jsonify({"ok": True, "provider": _session["provider"], "model": _session["model"]})


@app.route("/api/environments")
def api_environments():
    """Return the list of known environments for the quick-switch pills."""
    try:
        from tools.auth import get_active_environment
        active = get_active_environment()
    except Exception:
        active = {}
    envs = _get_known_envs()
    active_url = active.get("url", os.getenv("PP_ENV_URL", ""))
    return jsonify({"environments": envs, "active_url": active_url})


@app.route("/api/switch-env", methods=["POST"])
def api_switch_env():
    data = request.get_json(force=True)
    url  = data.get("url", "").strip()
    name = data.get("name", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        from tools.auth import set_active_environment
        set_active_environment(url, name)
        # Bust the env context cache so the next session fetches fresh data
        try:
            import agent as ag
            ag._ENV_CONTEXT_CACHE.pop(url, None)
        except Exception:
            pass
        return jsonify({"ok": True, "url": url, "name": name or url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Main chat endpoint — runs the agent and returns response."""
    data       = request.get_json(force=True)
    message    = data.get("message", "").strip()
    tool_id    = data.get("tool_id", "general")
    provider   = data.get("provider") or _session["provider"] or None
    model      = data.get("model")    or _session["model"]    or None
    plan_mode  = data.get("plan_mode", False)

    if not message:
        return jsonify({"error": "message required"}), 400

    # Get or init history for this tool section
    history = _histories.get(tool_id, [])

    _session["messages_sent"] += 1

    try:
        import agent as ag

        result = ag.run_agent(
            user_message=message,
            history=history,
            provider=provider,
            model=model,
            tool_section=tool_id,
            auto_confirm_reads=True,
            plan_mode=plan_mode,
        )

        _histories[tool_id] = result["history"]
        _persist_history(tool_id, result["history"])
        _session["tool_calls_made"] += len(result.get("tool_trace", []))

        # If agent wants to do a write, park it
        pending = result.get("pending_write")
        pending_id = None
        if pending:
            pending_id = str(uuid.uuid4())
            _pending_writes[pending_id] = {
                "pending": pending,
                "history": result["history"],
                "provider": result.get("provider"),
                "model":    result.get("model"),
                "tool_id":  tool_id,
            }

        return jsonify({
            "reply":       result.get("reply", ""),
            "tool_trace":  result.get("tool_trace", []),
            "pending_id":  pending_id,
            "pending":     pending,
            "provider":    result.get("provider"),
            "model":       result.get("model"),
        })

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({
            "reply":      f"❌ Agent error: {e}",
            "tool_trace": [{"tool": "error", "error": tb}],
        })


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    """User confirmed a pending write — execute it."""
    data       = request.get_json(force=True)
    pending_id = data.get("pending_id")
    confirmed  = data.get("confirmed", False)

    if not pending_id or pending_id not in _pending_writes:
        return jsonify({"error": "pending write not found"}), 404

    pw = _pending_writes.pop(pending_id)

    if not confirmed:
        return jsonify({"reply": "❌ Operation cancelled.", "tool_trace": []})

    try:
        import agent as ag
        result = ag.execute_confirmed_write(
            pending=pw["pending"],
            history=pw["history"],
            provider=pw.get("provider"),
            model=pw.get("model"),
            tool_section=pw.get("tool_id", "general"),
        )
        _histories[pw["tool_id"]] = result["history"]
        _persist_history(pw["tool_id"], result["history"])
        _session["tool_calls_made"] += 1
        return jsonify({
            "reply":      result.get("reply", ""),
            "tool_trace": result.get("tool_trace", []),
            "provider":   result.get("provider"),
            "model":      result.get("model"),
        })
    except Exception as e:
        return jsonify({"reply": f"❌ Execution error: {e}", "tool_trace": []}), 500


@app.route("/api/clear-history", methods=["POST"])
def api_clear_history():
    data    = request.get_json(force=True)
    tool_id = data.get("tool_id", "general")
    _histories.pop(tool_id, None)
    try:
        with _DB_LOCK:
            _DB_CONN.execute("DELETE FROM histories WHERE tool_id = ?", (tool_id,))
            _DB_CONN.commit()
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/history/<tool_id>")
def api_get_history(tool_id):
    """Return the conversation history for a tool section."""
    history = _histories.get(tool_id, [])
    display = [m for m in history if m.get("role") in ("user", "assistant")]
    return jsonify(display)


# ─────────────────────────────────────────────────────────────────────────────
#  SSE STREAMING  — /api/chat/start  →  /api/chat/stream/<id>
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/chat/start", methods=["POST"])
def api_chat_start():
    """
    Register a streaming chat job.
    Returns {stream_id} immediately; client then opens EventSource on /api/chat/stream/<id>.
    """
    data      = request.get_json(force=True)
    message   = data.get("message", "").strip()
    tool_id   = data.get("tool_id", "general")
    provider  = data.get("provider") or _session["provider"] or None
    model     = data.get("model")    or _session["model"]    or None
    plan_mode = data.get("plan_mode", False)

    if not message:
        return jsonify({"error": "message required"}), 400

    stream_id = str(uuid.uuid4())
    q = queue.Queue()
    _stream_jobs[stream_id] = {"queue": q, "result": None, "done": False}

    history = _histories.get(tool_id, [])
    _session["messages_sent"] += 1

    def _run():
        try:
            import agent as ag
            result = ag.run_agent(
                user_message=message,
                history=history,
                provider=provider,
                model=model,
                tool_section=tool_id,
                auto_confirm_reads=True,
                stream_callback=lambda ev: q.put(ev),
                plan_mode=plan_mode,
            )
            _stream_jobs[stream_id]["result"] = result
            # Persist history
            _histories[tool_id] = result["history"]
            _persist_history(tool_id, result["history"])
            _session["tool_calls_made"] += len(result.get("tool_trace", []))
            # Park pending write if any
            pending = result.get("pending_write")
            pending_id = None
            if pending:
                pending_id = str(uuid.uuid4())
                _pending_writes[pending_id] = {
                    "pending":  pending,
                    "history":  result["history"],
                    "provider": result.get("provider"),
                    "model":    result.get("model"),
                    "tool_id":  tool_id,
                }
                _stream_jobs[stream_id]["pending_id"] = pending_id
        except Exception as exc:
            q.put({"event": "error", "message": str(exc), "trace": traceback.format_exc()})
        finally:
            q.put(None)  # sentinel — stream generator will close

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"stream_id": stream_id})


@app.route("/api/chat/stream/<stream_id>")
def api_chat_stream(stream_id):
    """
    SSE endpoint — streams tool events then the final reply.
    Events: tool_start | tool_done | error | done
    """
    if stream_id not in _stream_jobs:
        return jsonify({"error": "stream not found"}), 404

    job = _stream_jobs[stream_id]
    q   = job["queue"]

    @stream_with_context
    def generate():
        while True:
            try:
                event = q.get(timeout=120)
            except queue.Empty:
                yield "data: {\"event\":\"error\",\"message\":\"timeout\"}\n\n"
                break

            if event is None:
                # Agent finished — send final done event
                result     = job.get("result") or {}
                pending_id = job.get("pending_id")
                pending    = result.get("pending_write") if not pending_id else _pending_writes.get(pending_id, {}).get("pending")
                done_event = {
                    "event":      "done",
                    "reply":      result.get("reply", ""),
                    "tool_trace": result.get("tool_trace", []),
                    "pending_id": pending_id,
                    "pending":    pending,
                    "provider":   result.get("provider"),
                    "model":      result.get("model"),
                }
                yield f"data: {json.dumps(done_event, default=str)}\n\n"
                break

            yield f"data: {json.dumps(event, default=str)}\n\n"

        # Clean up job after a delay
        def _cleanup():
            time.sleep(30)
            _stream_jobs.pop(stream_id, None)
        threading.Thread(target=_cleanup, daemon=True).start()

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":      "keep-alive",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SETTINGS API
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/settings/get")
def api_settings_get():
    """Return current settings (sensitive values masked)."""
    raw = _read_env_file()
    # Merge with live env so values set before .env existed show up
    merged = {
        # LLM
        "LLM_PROVIDER":       os.getenv("LLM_PROVIDER", raw.get("LLM_PROVIDER", "")),
        "LLM_MODEL":          os.getenv("LLM_MODEL",    raw.get("LLM_MODEL",    "")),
        "MINIMAX_API_KEY":    raw.get("MINIMAX_API_KEY",    ""),
        "ANTHROPIC_API_KEY":  raw.get("ANTHROPIC_API_KEY",  ""),
        "OPENAI_API_KEY":     raw.get("OPENAI_API_KEY",     ""),
        "GEMINI_API_KEY":     raw.get("GEMINI_API_KEY",     ""),
        "OPENROUTER_API_KEY": raw.get("OPENROUTER_API_KEY", ""),
        # Power Platform
        "PP_CLIENT_ID":       os.getenv("PP_CLIENT_ID",   raw.get("PP_CLIENT_ID",   "")),
        "PP_TENANT_ID":       os.getenv("PP_TENANT_ID",   raw.get("PP_TENANT_ID",   "common")),
        "PP_CLIENT_SECRET":   raw.get("PP_CLIENT_SECRET", ""),
        "PP_ENV_URL":         os.getenv("PP_ENV_URL",     raw.get("PP_ENV_URL",     "")),
        "PP_ENV_NAME":        os.getenv("PP_ENV_NAME",    raw.get("PP_ENV_NAME",    "")),
        "PP_ENV_ID":          os.getenv("PP_ENV_ID",      raw.get("PP_ENV_ID",      "")),
        "PP_GEO":             os.getenv("PP_GEO",         raw.get("PP_GEO",         "unitedstates")),
        # Azure DevOps
        "ADO_ORG":            os.getenv("ADO_ORG",     raw.get("ADO_ORG",     "")),
        "ADO_PAT":            raw.get("ADO_PAT",    ""),
        "ADO_PROJECT":        os.getenv("ADO_PROJECT",raw.get("ADO_PROJECT", "")),
        # General
        "PORT":               os.getenv("PORT", raw.get("PORT", "5005")),
    }
    # Return masked display + raw "has_value" flag
    result = {}
    for k, v in merged.items():
        placeholder = v.startswith("your_") if v else False
        result[k] = {
            "display": _mask(k, v) if (v and not placeholder) else "",
            "has_value": bool(v and not placeholder),
        }
    return jsonify(result)


@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    """Save settings to .env. Only writes non-empty, non-placeholder values."""
    data = request.get_json(force=True) or {}
    updates = {}
    for k, v in data.items():
        v = str(v).strip()
        if v and not v.startswith("your_") and "••••" not in v:
            updates[k] = v
    if not updates:
        return jsonify({"ok": False, "error": "No valid values to save"})
    _write_env_file(updates)
    return jsonify({"ok": True, "saved": list(updates.keys())})


@app.route("/api/settings/test", methods=["POST"])
def api_settings_test():
    """Test a specific connection type: llm | powerplatform | ado"""
    data  = request.get_json(force=True) or {}
    which = data.get("type", "powerplatform")

    if which == "powerplatform":
        try:
            from tools.auth import get_auth_status
            status = get_auth_status()
            if status.get("connected"):
                return jsonify({"ok": True, "message": f"✅ Connected as {status.get('account','unknown')}"})
            elif status.get("account"):
                return jsonify({"ok": False, "message": f"⚠️ Token found but environment URL may be wrong. Account: {status['account']}"})
            else:
                return jsonify({"ok": False, "message": "❌ Not connected — click 'Connect' to sign in via browser"})
        except Exception as e:
            return jsonify({"ok": False, "message": f"❌ {e}"})

    elif which == "llm":
        try:
            from tools.llm_provider import detect_provider, get_active_model, call_llm
            provider = detect_provider()
            model    = get_active_model(provider)
            result   = call_llm([{"role":"user","content":"Reply with just: ok"}],
                                  provider=provider, model=model, max_tokens=10)
            return jsonify({"ok": True, "message": f"✅ {provider} / {model} — working"})
        except Exception as e:
            return jsonify({"ok": False, "message": f"❌ {e}"})

    elif which == "ado":
        try:
            from tools.ado import list_pipelines
            pipelines = list_pipelines()
            if pipelines and "note" in str(pipelines[0]):
                return jsonify({"ok": False, "message": "❌ ADO_ORG or ADO_PAT not set"})
            return jsonify({"ok": True, "message": f"✅ Connected — {len(pipelines)} pipeline(s) found"})
        except Exception as e:
            return jsonify({"ok": False, "message": f"❌ {e}"})

    return jsonify({"ok": False, "message": "Unknown test type"})


@app.route("/api/settings/connect-pp", methods=["POST"])
def api_settings_connect_pp():
    """Start the MSAL device code flow — returns code + URL immediately, completes in background."""
    client_id = os.getenv("PP_CLIENT_ID", "").strip()
    if not client_id:
        return jsonify({"ok": False, "message": "PP_CLIENT_ID not configured — save settings first"})
    try:
        from tools.auth import initiate_device_flow_web
        flow = initiate_device_flow_web()
        return jsonify({"ok": True, "flow": flow})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/settings/auth-poll")
def api_settings_auth_poll():
    """Poll the background device code auth state: idle | pending | success | failed."""
    from tools.auth import get_device_flow_state, get_auth_status
    state = get_device_flow_state()
    if state["status"] == "success":
        auth = get_auth_status()
        return jsonify({"status": "success", "account": auth.get("account")})
    return jsonify(state)


# ─────────────────────────────────────────────────────────────────────────────
#  OBSERVABILITY — /api/traces  (reads from SQLite)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/traces")
@_login_required
def api_traces():
    """Return the most recent agent traces for the right-panel observer."""
    try:
        from tools.observability import get_traces, get_trace_stats
        limit  = min(int(request.args.get("limit", 30)), 100)
        traces = get_traces(limit=limit)
        stats  = get_trace_stats()
        return jsonify({"ok": True, "traces": traces, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "traces": [], "stats": {}})


@app.route("/api/traces/clear", methods=["POST"])
@_login_required
def api_traces_clear():
    """Clear old traces (default: older than 30 days)."""
    try:
        from tools.observability import clear_traces
        days   = int(request.json.get("days", 30) if request.json else 30)
        result = clear_traces(older_than_days=days)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
#  HTML TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PP Agent — Power Platform AI</title>
<style>
  :root {
    --bg:        #0c0c0d;
    --bg2:       #111113;
    --bg3:       #141416;
    --bg4:       #1e1e22;
    --border:    #1c1c1e;
    --text:      #e8e8ec;
    --text2:     #8b8b9a;
    --text3:     #56566a;
    --accent:    #5c6bc0;
    --accent2:   #4a5aae;
    --green:     #10b981;
    --red:       #ef4444;
    --yellow:    #f59e0b;
    --radius:    10px;
    --sidebar-w: 220px;
    --right-w:   260px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── TOPBAR ──────────────────────────────────────────────────────────────── */
  .topbar {
    height: 54px;
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    padding: 0 16px;
    gap: 12px;
    flex-shrink: 0;
    z-index: 100;
  }
  .topbar-logo {
    font-size: 18px;
    font-weight: 700;
    background: linear-gradient(135deg, #5c6bc0, #818cf8);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    white-space: nowrap;
  }
  .topbar-logo span { font-size: 13px; opacity: .6; font-weight: 400; margin-left: 4px; }
  .topbar-sep { flex: 1; }

  .env-badge {
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    transition: border-color .2s;
  }
  .env-badge:hover { border-color: var(--accent); }
  .env-badge .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); flex-shrink: 0; }
  .env-badge .dot.offline { background: var(--red); }

  /* ── ENV QUICK-SWITCH PILLS ───────────────────────────────────────────────── */
  .env-pills {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .env-pill {
    font-size: 11px;
    padding: 3px 9px;
    border-radius: 99px;
    border: 1px solid var(--border);
    background: var(--bg3);
    color: var(--text2);
    cursor: pointer;
    transition: all .15s;
    white-space: nowrap;
  }
  .env-pill:hover { border-color: var(--accent); color: var(--text); }
  .env-pill.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    font-weight: 600;
  }
  .env-pill-sep { color: var(--text3); font-size: 10px; user-select: none; }

  .model-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    transition: border-color .2s;
    white-space: nowrap;
  }
  .model-btn:hover { border-color: var(--accent); }
  .model-btn .provider-dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--accent); flex-shrink: 0;
  }

  /* ── LAYOUT ──────────────────────────────────────────────────────────────── */
  .layout {
    display: flex;
    flex: 1;
    overflow: hidden;
    min-height: 0;
  }

  /* ── SIDEBAR ─────────────────────────────────────────────────────────────── */
  .sidebar {
    width: var(--sidebar-w);
    background: var(--bg2);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    overflow-x: hidden;
    flex-shrink: 0;
    min-height: 0;
  }
  .sidebar::-webkit-scrollbar { width: 4px; }
  .sidebar::-webkit-scrollbar-thumb { background: var(--bg4); border-radius: 4px; }

  .sidebar-section {
    padding: 10px 8px 4px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: var(--text3);
  }

  /* ── COMMAND CENTER nav item ─────────────────────────────────────────────── */
  .cmd-item-wrap {
    padding: 8px 8px 4px;
  }
  .nav-item.command-item {
    margin: 0;
    padding: 11px 14px;
    background: linear-gradient(135deg, rgba(92,107,192,.18), rgba(129,140,248,.12));
    border: 1px solid rgba(92,107,192,.35);
    border-radius: 8px;
    color: var(--text);
    font-size: 13px;
    font-weight: 600;
  }
  .nav-item.command-item::before { display: none; }
  .nav-item.command-item:hover {
    background: linear-gradient(135deg, rgba(92,107,192,.28), rgba(129,140,248,.22));
    border-color: rgba(92,107,192,.6);
  }
  .nav-item.command-item.active {
    background: linear-gradient(135deg, rgba(92,107,192,.35), rgba(129,140,248,.28));
    border-color: #5c6bc0;
    box-shadow: 0 0 12px rgba(92,107,192,.3);
  }
  .nav-item.command-item .icon { font-size: 16px; }
  .nav-item.command-item .cmd-sub {
    font-size: 10px;
    color: var(--text3);
    font-weight: 400;
    margin-top: 1px;
  }
  .cmd-separator {
    height: 1px;
    background: var(--border);
    margin: 8px 10px 4px;
  }

  /* ── Command Center panel header ────────────────────────────────────────── */
  .cmd-panel-header {
    padding: 18px 22px 14px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(135deg, rgba(92,107,192,.08), rgba(129,140,248,.05));
    flex-shrink: 0;
  }
  .cmd-panel-title {
    font-size: 18px;
    font-weight: 700;
    background: linear-gradient(135deg, #818cf8, #a5b4fc);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 3px;
  }
  .cmd-panel-sub {
    font-size: 12px;
    color: var(--text3);
  }
  /* ── Plan Mode toggle ───────────────────────────────────────────────────── */
  .plan-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12px;
    cursor: pointer;
    transition: all .2s;
    color: var(--text2);
    user-select: none;
  }
  .plan-toggle.active {
    background: rgba(92,107,192,.15);
    border-color: #5c6bc0;
    color: var(--text);
  }
  .plan-toggle .toggle-dot {
    width: 28px; height: 16px;
    background: var(--bg4);
    border-radius: 8px;
    position: relative;
    transition: background .2s;
    flex-shrink: 0;
  }
  .plan-toggle.active .toggle-dot { background: #5c6bc0; }
  .plan-toggle .toggle-dot::after {
    content: '';
    position: absolute;
    width: 12px; height: 12px;
    background: white;
    border-radius: 50%;
    top: 2px; left: 2px;
    transition: left .2s;
  }
  .plan-toggle.active .toggle-dot::after { left: 14px; }

  /* ── Plan card (agent response when plan_mode=true) ─────────────────────── */
  .plan-card {
    background: linear-gradient(135deg, rgba(92,107,192,.08), rgba(129,140,248,.05));
    border: 1px solid rgba(92,107,192,.35);
    border-radius: 12px;
    padding: 16px 18px;
    margin-top: 8px;
  }
  .plan-card-title {
    font-size: 13px;
    font-weight: 700;
    color: #818cf8;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .plan-card-body {
    font-size: 13px;
    line-height: 1.7;
    color: var(--text2);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .plan-card-actions {
    margin-top: 14px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .btn-proceed {
    background: linear-gradient(135deg,#5c6bc0,#818cf8);
    border: none;
    border-radius: 7px;
    color: white;
    padding: 8px 20px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity .15s;
  }
  .btn-proceed:hover { opacity: .85; }
  .btn-modify {
    background: none;
    border: 1px solid var(--border);
    border-radius: 7px;
    color: var(--text2);
    padding: 8px 16px;
    font-size: 13px;
    cursor: pointer;
    transition: border-color .15s;
  }
  .btn-modify:hover { border-color: var(--accent); color: var(--text); }

  .cmd-empty-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 40px 20px;
  }
  .cmd-empty-icon {
    font-size: 52px;
    filter: drop-shadow(0 0 20px rgba(92,107,192,.5));
  }
  .cmd-empty-title {
    font-size: 16px;
    font-weight: 700;
    background: linear-gradient(135deg, #818cf8, #a5b4fc);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .cmd-empty-sub {
    font-size: 13px;
    color: var(--text3);
    text-align: center;
    max-width: 380px;
    line-height: 1.6;
  }
  .cmd-tool-count {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: center;
    margin-top: 4px;
  }
  .cmd-badge-section-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--text3);
    padding: 10px 2px 4px;
    width: 100%;
    text-align: left;
  }
  .cmd-badge {
    font-size: 11px;
    padding: 5px 12px;
    border-radius: 20px;
    border: 1px solid var(--border);
    cursor: pointer;
    transition: background .15s, border-color .15s, color .15s, transform .1s;
    background: var(--bg3);
    color: var(--text2);
    font-family: inherit;
  }
  .cmd-badge:hover {
    background: rgba(92,107,192,.15);
    border-color: #5c6bc0;
    color: var(--text);
    transform: translateY(-1px);
  }
  .cmd-badge:active { transform: translateY(0); }

  .nav-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    margin: 1px 6px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    color: var(--text2);
    transition: all .15s;
    position: relative;
  }
  .nav-item:hover { background: var(--bg3); color: var(--text); }
  .nav-item.active { background: var(--bg3); color: var(--text); }
  .nav-item.active::before {
    content: '';
    position: absolute;
    left: 0; top: 4px; bottom: 4px;
    width: 3px;
    border-radius: 3px;
    background: var(--item-color, var(--accent));
  }
  .nav-item .icon { font-size: 15px; flex-shrink: 0; }
  .nav-item .label { flex: 1; }
  .nav-item .badge {
    font-size: 10px;
    background: var(--item-color, var(--accent));
    color: white;
    border-radius: 10px;
    padding: 1px 5px;
    opacity: 0;
    transition: opacity .2s;
  }
  .nav-item.has-history .badge { opacity: 1; }

  /* ── MAIN CHAT AREA ──────────────────────────────────────────────────────── */
  .main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-height: 0;
  }

  .tool-panel {
    display: none;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }
  .tool-panel.active { display: flex; }

  .tool-header {
    padding: 14px 20px 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }
  .tool-header .tool-icon { font-size: 22px; }
  .tool-header .tool-name { font-size: 16px; font-weight: 600; }
  .tool-header .tool-desc { font-size: 12px; color: var(--text3); margin-top: 1px; }
  .tool-header-right { margin-left: auto; display: flex; gap: 8px; }
  .btn-ghost {
    background: none;
    border: 1px solid var(--border);
    color: var(--text2);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    transition: all .15s;
  }
  .btn-ghost:hover { border-color: var(--accent); color: var(--text); }

  /* ── MESSAGES ──────────────────────────────────────────────────────────────*/
  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .messages::-webkit-scrollbar { width: 6px; }
  .messages::-webkit-scrollbar-thumb { background: var(--bg4); border-radius: 4px; }

  .msg {
    display: flex;
    gap: 10px;
    animation: fadeIn .2s ease;
  }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

  .msg-avatar {
    width: 30px; height: 30px;
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px;
    flex-shrink: 0;
    margin-top: 2px;
  }
  .msg.user .msg-avatar { background: var(--accent); }
  .msg.assistant .msg-avatar { background: var(--bg3); }

  .msg-body { flex: 1; min-width: 0; }
  .msg-meta { font-size: 11px; color: var(--text3); margin-bottom: 4px; display: flex; gap: 8px; align-items: center; }
  .msg-content {
    font-size: 14px;
    line-height: 1.65;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .msg.user .msg-content {
    background: var(--bg3);
    border-radius: 10px;
    padding: 10px 14px;
    display: inline-block;
    max-width: 85%;
  }

  /* code blocks */
  .msg-content code {
    background: var(--bg3);
    padding: 2px 5px;
    border-radius: 4px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 12px;
    color: #a5f3fc;
  }
  .msg-content pre {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    overflow-x: auto;
    margin: 8px 0;
  }
  .msg-content pre code { background: none; padding: 0; }

  /* tool trace */
  .tool-trace {
    margin-top: 8px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .trace-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 12px;
  }
  .trace-item .trace-icon { flex-shrink: 0; margin-top: 1px; }
  .trace-item .trace-name { color: #a5f3fc; font-weight: 600; }
  .trace-item .trace-result { color: var(--text2); margin-top: 2px; white-space: pre-wrap; word-break: break-word; }
  .trace-item.error .trace-result { color: var(--red); }
  .trace-item.success .trace-result { color: var(--green); }

  /* pending write card */
  .write-card {
    background: #0e1035;
    border: 1px solid #3d4a9e;
    border-radius: 10px;
    padding: 14px 16px;
    margin-top: 8px;
  }
  .write-card-header { font-size: 13px; font-weight: 600; color: #a5b4fc; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
  .write-card-preview { font-size: 12px; color: var(--text2); white-space: pre-wrap; margin-bottom: 12px; }
  .write-card-actions { display: flex; gap: 8px; }
  .btn-confirm {
    background: var(--accent);
    color: white;
    border: none;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 13px;
    cursor: pointer;
    transition: background .15s;
  }
  .btn-confirm:hover { background: var(--accent2); }
  .btn-cancel {
    background: none;
    border: 1px solid var(--red);
    color: var(--red);
    border-radius: 6px;
    padding: 7px 14px;
    font-size: 13px;
    cursor: pointer;
  }

  /* typing indicator */
  .typing-wrap { display: flex; flex-direction: column; gap: 4px; }
  .typing-status {
    font-size: 11px;
    color: var(--accent);
    min-height: 14px;
    font-style: italic;
    opacity: .85;
    animation: fadeIn .2s;
  }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: .85; } }
  .typing { display: flex; gap: 4px; align-items: center; padding: 4px 0; }
  .typing span {
    width: 7px; height: 7px;
    background: var(--text3);
    border-radius: 50%;
    animation: bounce .9s ease infinite;
  }
  .typing span:nth-child(2) { animation-delay: .15s; }
  .typing span:nth-child(3) { animation-delay: .3s; }
  @keyframes bounce { 0%,80%,100% { transform: scale(1); } 40% { transform: scale(1.5); } }

  /* empty state */
  .empty-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    opacity: .6;
  }
  .empty-state .es-icon { font-size: 40px; }
  .empty-state .es-title { font-size: 15px; font-weight: 600; }
  .empty-state .es-sub { font-size: 13px; color: var(--text3); text-align: center; max-width: 320px; }

  /* ── CHIPS ───────────────────────────────────────────────────────────────── */
  .chips-bar {
    padding: 8px 20px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    border-top: 1px solid var(--border);
    background: var(--bg2);
  }
  .chip {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 5px 12px;
    font-size: 12px;
    color: var(--text2);
    cursor: pointer;
    white-space: nowrap;
    transition: all .15s;
  }
  .chip:hover { border-color: var(--item-color, var(--accent)); color: var(--text); }

  /* ── INPUT BAR ───────────────────────────────────────────────────────────── */
  .input-bar {
    padding: 12px 20px;
    background: var(--bg2);
    border-top: 1px solid var(--border);
    display: flex;
    align-items: flex-end;
    gap: 10px;
    flex-shrink: 0;
  }
  .input-wrap {
    flex: 1;
    display: flex;
    align-items: flex-end;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 10px;
    transition: border-color .2s;
  }
  .input-wrap:focus-within { border-color: var(--accent); }
  textarea.chat-input {
    flex: 1;
    background: none;
    border: none;
    outline: none;
    color: var(--text);
    font-size: 14px;
    font-family: inherit;
    padding: 10px 14px;
    resize: none;
    max-height: 140px;
    line-height: 1.5;
    min-height: 42px;
  }
  .send-btn {
    background: var(--accent);
    border: none;
    border-radius: 8px;
    color: white;
    padding: 9px 16px;
    font-size: 14px;
    cursor: pointer;
    transition: background .15s, transform .1s;
    flex-shrink: 0;
    align-self: flex-end;
    margin-bottom: 2px;
    margin-right: 2px;
  }
  .send-btn:hover:not(:disabled) { background: var(--accent2); transform: translateY(-1px); }
  .send-btn:disabled { opacity: .4; cursor: not-allowed; transform: none; }

  /* ── RIGHT PANEL ─────────────────────────────────────────────────────────── */
  .right-panel {
    width: var(--right-w);
    background: var(--bg2);
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    overflow-x: hidden;
    flex-shrink: 0;
    min-height: 0;
  }
  .right-panel::-webkit-scrollbar { width: 4px; }
  .right-panel::-webkit-scrollbar-thumb { background: var(--bg4); border-radius: 4px; }

  .rp-section { padding: 14px 14px 10px; border-bottom: 1px solid var(--border); }
  .rp-title { font-size: 11px; font-weight: 700; letter-spacing: .8px; text-transform: uppercase; color: var(--text3); margin-bottom: 10px; }
  .rp-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .rp-label { font-size: 12px; color: var(--text3); }
  .rp-value { font-size: 12px; color: var(--text); font-weight: 500; }
  .rp-value.green { color: var(--green); }
  .rp-value.red   { color: var(--red); }
  .rp-value.yellow{ color: var(--yellow); }

  .status-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 4px; }
  .status-dot.on  { background: var(--green); }
  .status-dot.off { background: var(--red); }

  /* ── MODALS ──────────────────────────────────────────────────────────────── */
  .modal-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,.7);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 999;
  }
  .modal-overlay.show { display: flex; }
  .modal {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px;
    width: 480px;
    max-width: 90vw;
    max-height: 80vh;
    overflow-y: auto;
    animation: modalIn .2s ease;
  }
  @keyframes modalIn { from { opacity: 0; transform: scale(.95); } to { opacity: 1; transform: none; } }
  .modal-title { font-size: 16px; font-weight: 700; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
  .modal-body { font-size: 13px; color: var(--text2); margin-bottom: 18px; }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end; }
  .modal label { display: block; font-size: 12px; color: var(--text3); margin-bottom: 5px; margin-top: 12px; }
  .modal input {
    width: 100%;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 8px 10px;
    font-size: 13px;
    outline: none;
  }
  .modal input:focus { border-color: var(--accent); }

  /* model picker grid */
  .model-grid { display: flex; flex-direction: column; gap: 4px; }
  .model-option {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    transition: border-color .15s;
  }
  .model-option:hover { border-color: var(--accent); }
  .model-option.selected { border-color: var(--accent); background: #1e2050; }
  .model-option .mo-provider {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .5px;
    color: var(--text3);
    width: 70px;
    flex-shrink: 0;
  }
  .model-option .mo-label { font-size: 13px; flex: 1; }
  .model-option .mo-check { color: var(--accent); font-size: 14px; display: none; }
  .model-option.selected .mo-check { display: block; }

  /* toast */
  .toast {
    position: fixed;
    bottom: 24px; left: 50%;
    transform: translateX(-50%) translateY(60px);
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 18px;
    font-size: 13px;
    z-index: 9999;
    transition: transform .3s ease;
    pointer-events: none;
  }
  .toast.show { transform: translateX(-50%) translateY(0); }
  .toast.success { border-color: var(--green); color: var(--green); }
  .toast.error   { border-color: var(--red);   color: var(--red); }

  /* ── SETTINGS ────────────────────────────────────────────────────────────── */
  .settings-body {
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 20px 28px 40px;
    /* block layout — NOT flex — so overflow-y:auto works reliably */
  }
  .settings-body::-webkit-scrollbar { width: 6px; }
  .settings-body::-webkit-scrollbar-thumb { background: var(--bg4); border-radius: 4px; }
  .settings-loading { color: var(--text3); font-size: 14px; padding: 40px 0; text-align: center; }

  .cfg-section {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 20px;
  }
  .cfg-section-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    background: var(--bg3);
  }
  .cfg-section-header .cfg-icon { font-size: 18px; }
  .cfg-section-header .cfg-title { font-size: 14px; font-weight: 700; flex: 1; }
  .cfg-section-header .cfg-status {
    font-size: 12px;
    padding: 3px 10px;
    border-radius: 20px;
    background: var(--bg4);
    color: var(--text3);
  }
  .cfg-section-header .cfg-status.ok  { background: #052e16; color: var(--green); }
  .cfg-section-header .cfg-status.err { background: #2d0a0a; color: var(--red); }
  .cfg-section-header .cfg-test-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--text2);
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 11px;
    cursor: pointer;
    transition: border-color .15s;
  }
  .cfg-section-header .cfg-test-btn:hover { border-color: var(--accent); color: var(--text); }

  .cfg-fields {
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .cfg-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .cfg-row.single { grid-template-columns: 1fr; }
  .cfg-row.triple { grid-template-columns: 1fr 1fr 1fr; }

  .cfg-field { display: flex; flex-direction: column; gap: 5px; }
  .cfg-field label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .5px;
    color: var(--text3);
  }
  .cfg-field label .lbl-badge {
    display: inline-block;
    margin-left: 6px;
    font-size: 9px;
    padding: 1px 5px;
    border-radius: 3px;
    background: var(--bg4);
    text-transform: none;
    letter-spacing: 0;
    color: var(--text3);
  }
  .cfg-field label .lbl-badge.required { background: #2d1f00; color: var(--yellow); }
  .cfg-field label .lbl-badge.optional { background: var(--bg4); color: var(--text3); }

  .cfg-input-wrap { position: relative; display: flex; align-items: center; }
  .cfg-input {
    width: 100%;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 7px;
    color: var(--text);
    padding: 9px 12px;
    font-size: 13px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    outline: none;
    transition: border-color .2s;
  }
  .cfg-input::placeholder { color: var(--text3); font-family: inherit; }
  .cfg-input:focus { border-color: var(--accent); }
  .cfg-input.has-value { border-color: #374151; }
  .cfg-input.has-value:not(:focus) { color: var(--text2); }

  .cfg-eye-btn {
    position: absolute;
    right: 8px;
    background: none;
    border: none;
    color: var(--text3);
    cursor: pointer;
    font-size: 14px;
    padding: 2px 4px;
  }
  .cfg-eye-btn:hover { color: var(--text); }

  .cfg-hint { font-size: 11px; color: var(--text3); margin-top: 2px; }
  .cfg-hint a { color: var(--accent); text-decoration: none; }
  .cfg-hint a:hover { text-decoration: underline; }

  .cfg-provider-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .cfg-provider-chip {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg3);
    font-size: 12px;
    color: var(--text2);
    cursor: default;
  }
  .cfg-provider-chip .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--bg4); }
  .cfg-provider-chip.active .dot { background: var(--green); }
  .cfg-provider-chip.active { color: var(--text); border-color: var(--green); }

  .cfg-actions {
    padding: 16px 18px;
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .cfg-save-btn {
    background: var(--accent);
    border: none;
    border-radius: 7px;
    color: white;
    padding: 9px 22px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: background .15s;
  }
  .cfg-save-btn:hover { background: var(--accent2); }
  .cfg-save-btn:disabled { opacity: .5; cursor: not-allowed; }
  .cfg-result { font-size: 12px; color: var(--text3); flex: 1; }
  .cfg-result.ok  { color: var(--green); }
  .cfg-result.err { color: var(--red); }

  .connect-btn {
    background: #052e16;
    border: 1px solid var(--green);
    color: var(--green);
    border-radius: 7px;
    padding: 9px 18px;
    font-size: 13px;
    cursor: pointer;
    transition: background .15s;
  }
  .connect-btn:hover { background: #064e1a; }
</style>
</head>
<body>

<!-- TOP BAR -->
<div class="topbar">
  <div class="topbar-logo">◈ PP Agent <span>Power Platform AI</span></div>
  <div class="topbar-sep"></div>
  <!-- Environment quick-switch pills (populated by loadEnvPills()) -->
  <div class="env-pills" id="env-pills"></div>
  <div class="env-badge" onclick="openEnvModal()" id="env-badge" style="margin-left:4px;">
    <div class="dot offline" id="env-dot"></div>
    <span id="env-name">Not connected</span>
    <span>▾</span>
  </div>
  <div class="model-btn" onclick="openModelPicker()" id="model-btn">
    <div class="provider-dot" id="provider-dot"></div>
    <span id="model-display">MiniMax M2.7</span>
    <span>▾</span>
  </div>
</div>

<div class="layout">

  <!-- SIDEBAR -->
  <nav class="sidebar">
    {% for t in tools %}
    {% if t.id == 'command' %}
    <div class="cmd-item-wrap">
      <div class="nav-item command-item" id="nav-{{ t.id }}"
           style="--item-color:#818cf8"
           onclick="switchTool('{{ t.id }}')">
        <span class="icon">{{ t.icon }}</span>
        <div style="flex:1;">
          <div class="label">{{ t.label }}</div>
          <div class="cmd-sub">75+ tools · one prompt</div>
        </div>
        <span class="badge">✓</span>
      </div>
    </div>
    <div class="cmd-separator"></div>
    {% else %}
    {% if t.id == 'solutions' %}<div class="sidebar-section">Build &amp; Configure</div>{% endif %}
    {% if t.id == 'powerbi' %}<div class="sidebar-section">Data &amp; Analytics</div>{% endif %}
    {% if t.id == 'plugins' %}<div class="sidebar-section">Extend</div>{% endif %}
    {% if t.id == 'alm' %}<div class="sidebar-section">DevOps &amp; ALM</div>{% endif %}
    {% if t.id == 'environments' %}<div class="sidebar-section">Admin</div>{% endif %}
    {% if t.id == 'knowledge' %}<div class="sidebar-section">Agent</div>{% endif %}
    <div class="nav-item" id="nav-{{ t.id }}"
         style="--item-color: {{ t.color }}"
         onclick="switchTool('{{ t.id }}')">
      <span class="icon">{{ t.icon }}</span>
      <span class="label">{{ t.label }}</span>
      <span class="badge">✓</span>
    </div>
    {% endif %}
    {% endfor %}
  </nav>

  <!-- MAIN CONTENT -->
  <div class="main">
    {% for t in tools %}
    {% if t.is_config is defined and t.is_config %}
    <!-- ══ SETTINGS CONFIG PANEL ══════════════════════════════════════════════ -->
    <div class="tool-panel" id="panel-settings" data-tool="settings" style="--item-color:#6b7280;">
      <div class="tool-header" style="flex-shrink:0;">
        <span class="tool-icon">⚙️</span>
        <div>
          <div class="tool-name">Settings &amp; Connections</div>
          <div class="tool-desc">Configure API keys, environment, and credentials</div>
        </div>
        <div class="tool-header-right">
          <button class="btn-ghost" onclick="loadSettings()">↺ Refresh</button>
        </div>
      </div>
      <div class="settings-body" id="settings-body">
        <div class="settings-loading">Loading configuration…</div>
      </div>
    </div>
    {% elif t.is_command is defined and t.is_command %}
    <!-- ══ COMMAND CENTER PANEL ═════════════════════════════════════════════════ -->
    <div class="tool-panel active" id="panel-command" data-tool="command" style="--item-color:#818cf8;">
      <div class="cmd-panel-header">
        <div style="display:flex;align-items:center;gap:10px;">
          <span style="font-size:24px;">✨</span>
          <div>
            <div class="cmd-panel-title">Command Center</div>
            <div class="cmd-panel-sub">Describe your goal in plain English — the agent picks and chains the right tools automatically</div>
          </div>
          <div style="margin-left:auto;display:flex;align-items:center;gap:8px;">
            <button class="plan-toggle active" id="plan-toggle-btn" onclick="togglePlanMode()" title="Plan Mode: agent shows a numbered plan before executing any writes">
              <div class="toggle-dot"></div>
              <span>Plan Mode</span>
            </button>
            <button class="btn-ghost" onclick="clearHistory('command')">🗑 Clear</button>
          </div>
        </div>
      </div>
      <div class="messages" id="msgs-command">
        <div class="cmd-empty-state" id="empty-command">
          <div class="cmd-empty-icon">✨</div>
          <div class="cmd-empty-title">All 75+ Tools. One Prompt.</div>
          <div class="cmd-empty-sub">Type any goal — from a single action to a full org health check — and the agent will figure out which tools to call, in what order, and chain the results together.</div>

          <div class="cmd-badge-section-label">📋 Browse &amp; Inspect</div>
          <div class="cmd-tool-count">
            <button class="cmd-badge" onclick="cmdBadgeClick('List all solutions and run a health check on each one')">🗂️ Solutions</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('List all flows, show which ones are failing, and give me the error details for each failed run')">⚡ Flows</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('List all canvas apps and show who has access to each')">🎨 Canvas</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('List the most important Dataverse tables and their row counts')">🗄️ Dataverse</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('List all Copilot Studio agents and their topic counts')">🤖 Copilot</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('List all Power BI workspaces and reports')">📊 Power BI</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Show open opportunities, their stage, and estimated value')">💼 D365 CRM</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('List open work items and recent pipeline runs in Azure DevOps')">⚙️ Azure DevOps</button>
          </div>

          <div class="cmd-badge-section-label">🔨 Build &amp; Create</div>
          <div class="cmd-tool-count">
            <button class="cmd-badge" onclick="cmdBadgeClick('Create a new Dataverse table called Project with columns: name (text), status (choice: Active/On Hold/Completed), budget (currency), start date (date only), and a lookup to Account')">🗄️ Create Table</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Create a daily recurrence flow that sends an email summary of all open opportunities to my inbox')">⚡ Create Flow</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Create a new unmanaged solution called ContosoSales with the contoso publisher prefix, version 1.0.0.0')">📦 New Solution</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Create a new security role called Field Agent, clone it from Sales Person, and set read/write access on the Account and Contact tables at Business Unit level')">🔐 Create Role</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Create a global choice called Project Status with options: Planning (100000000), Active (100000001), On Hold (100000002), Completed (100000003)')">🎛️ Global Choice</button>
          </div>

          <div class="cmd-badge-section-label">🏥 Health &amp; Monitor</div>
          <div class="cmd-tool-count">
            <button class="cmd-badge" onclick="cmdBadgeClick('Run a full org health check: check environment connectivity, list all failing flows with error details, check plugin assembly health, and summarise any issues')">🏥 Org Health</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Show all security roles and which users are assigned to each. Flag any users with System Administrator role')">🔒 Security Audit</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Check plugin health — show all assemblies, which steps are disabled, and any that run outside sandbox isolation')">🔬 Plugin Health</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Show all environments and their capacity usage')">🌍 Environments</button>
            <button class="cmd-badge" onclick="cmdBadgeClick('Show the Dataverse audit log for the last 24 hours — what records were created, updated, or deleted')">📋 Audit Log</button>
          </div>
        </div>
      </div>
      <div class="chips-bar" style="--item-color:#818cf8;">
        {% for chip in t.chips %}
        <div class="chip" onclick="sendChip(this, 'command')">{{ chip }}</div>
        {% endfor %}
      </div>
      <div class="input-bar">
        <div class="input-wrap" style="border-color:rgba(92,107,192,.4);">
          <textarea class="chat-input" id="input-command"
            placeholder="Describe your goal… e.g. 'Show all failing flows and the solutions they belong to'"
            rows="1"
            onkeydown="handleKey(event, 'command')"
            oninput="autoResize(this)"></textarea>
        </div>
        <button class="send-btn" id="send-command"
          style="background:linear-gradient(135deg,#5c6bc0,#818cf8);"
          onclick="sendMessage('command')">Send ✦</button>
      </div>
    </div>
    {% else %}
    <div class="tool-panel" id="panel-{{ t.id }}"
         data-tool="{{ t.id }}" style="--item-color: {{ t.color }}">

      <!-- Header -->
      <div class="tool-header">
        <span class="tool-icon">{{ t.icon }}</span>
        <div>
          <div class="tool-name">{{ t.label }}</div>
          <div class="tool-desc">Ask anything in plain English</div>
        </div>
        <div class="tool-header-right">
          <button class="btn-ghost" onclick="clearHistory('{{ t.id }}')">🗑 Clear</button>
        </div>
      </div>

      <!-- Messages -->
      <div class="messages" id="msgs-{{ t.id }}">
        <div class="empty-state" id="empty-{{ t.id }}">
          <div class="es-icon">{{ t.icon }}</div>
          <div class="es-title">{{ t.label }}</div>
          <div class="es-sub">Ask anything about {{ t.label }}. Use the chips below for quick starts.</div>
        </div>
      </div>

      <!-- Chips -->
      <div class="chips-bar" style="--item-color: {{ t.color }}">
        {% for chip in t.chips %}
        <div class="chip" onclick="sendChip(this, '{{ t.id }}')">{{ chip }}</div>
        {% endfor %}
      </div>

      <!-- Input -->
      <div class="input-bar">
        <div class="input-wrap">
          <textarea class="chat-input" id="input-{{ t.id }}"
            placeholder="Ask about {{ t.label }}…"
            rows="1"
            onkeydown="handleKey(event, '{{ t.id }}')"
            oninput="autoResize(this)"></textarea>
        </div>
        <button class="send-btn" id="send-{{ t.id }}"
          onclick="sendMessage('{{ t.id }}')">Send ➤</button>
      </div>
    </div>
    {% endif %}
    {% endfor %}
  </div>

  <!-- RIGHT PANEL -->
  <div class="right-panel">
    <div class="rp-section">
      <div class="rp-title">Connection</div>
      <div class="rp-row">
        <span class="rp-label">Status</span>
        <span class="rp-value" id="rp-status"><span class="status-dot off"></span>Checking…</span>
      </div>
      <div class="rp-row">
        <span class="rp-label">Account</span>
        <span class="rp-value" id="rp-account">—</span>
      </div>
      <div class="rp-row">
        <span class="rp-label">Environment</span>
        <span class="rp-value" id="rp-env">—</span>
      </div>
    </div>
    <div class="rp-section">
      <div class="rp-title">Model</div>
      <div class="rp-row">
        <span class="rp-label">Provider</span>
        <span class="rp-value" id="rp-provider">—</span>
      </div>
      <div class="rp-row">
        <span class="rp-label">Model</span>
        <span class="rp-value" id="rp-model">—</span>
      </div>
    </div>
    <div class="rp-section">
      <div class="rp-title">Session Stats</div>
      <div class="rp-row">
        <span class="rp-label">Messages</span>
        <span class="rp-value" id="rp-messages">0</span>
      </div>
      <div class="rp-row">
        <span class="rp-label">Tool calls</span>
        <span class="rp-value" id="rp-tools">0</span>
      </div>
      <div class="rp-row">
        <span class="rp-label">Started</span>
        <span class="rp-value" id="rp-started">—</span>
      </div>
    </div>
    <div class="rp-section" id="obs-section">
      <div class="rp-title" style="display:flex;justify-content:space-between;align-items:center;">
        <span>Agent Traces</span>
        <span style="font-size:10px;color:var(--text3);font-weight:400;cursor:pointer;" onclick="loadTraces()" title="Refresh">↺</span>
      </div>
      <div id="obs-stats" style="display:flex;gap:10px;margin:6px 0 8px;flex-wrap:wrap;">
        <div style="font-size:11px;color:var(--text2);">Loading…</div>
      </div>
      <div id="obs-list" style="display:flex;flex-direction:column;gap:4px;max-height:200px;overflow-y:auto;"></div>
      <div style="margin-top:6px;">
        <div id="obs-domain" style="font-size:11px;color:var(--text3);padding-top:4px;border-top:1px solid var(--border);">Domain: —</div>
      </div>
    </div>
    <div class="rp-section">
      <div class="rp-title">Phase 3 Capabilities</div>
      <div style="display:flex;flex-direction:column;gap:4px;padding-top:2px;">
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Create table + add columns (19 types)
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Lookup &amp; N:N relationships
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Bulk record creation ($batch)
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Create flows (4 trigger types)
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Failing flow scanner + error detail
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Solution health check + layers
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Clone role + set table privileges
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Field security profiles
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> Plugin health audit
        </div>
        <div style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px;">
          <span style="color:#22c55e;font-size:10px;">●</span> PP domain knowledge in every prompt
        </div>
      </div>
    </div>
    <div class="rp-section">
      <div class="rp-title">Quick Links</div>
      <div style="display:flex;flex-direction:column;gap:5px;padding-top:2px;">
        <a href="https://make.powerapps.com" target="_blank" style="font-size:12px;color:var(--accent);text-decoration:none;">🔗 Power Apps Studio</a>
        <a href="https://make.powerautomate.com" target="_blank" style="font-size:12px;color:var(--accent);text-decoration:none;">🔗 Power Automate</a>
        <a href="https://copilotstudio.microsoft.com" target="_blank" style="font-size:12px;color:var(--accent);text-decoration:none;">🔗 Copilot Studio</a>
        <a href="https://admin.powerplatform.microsoft.com" target="_blank" style="font-size:12px;color:var(--accent);text-decoration:none;">🔗 Admin Center</a>
        <a href="https://app.powerbi.com" target="_blank" style="font-size:12px;color:var(--accent);text-decoration:none;">🔗 Power BI</a>
      </div>
    </div>
    <div class="rp-section" style="border-bottom:none;">
      <div class="rp-title">Server</div>
      <button id="restart-btn" onclick="restartServer()" style="
        width:100%; padding:8px 0; border-radius:7px;
        background:rgba(239,68,68,.1); border:1px solid rgba(239,68,68,.3);
        color:#f87171; font-size:12px; font-weight:600; cursor:pointer;
        transition:background .15s, border-color .15s;
        font-family:inherit;
      " onmouseover="this.style.background='rgba(239,68,68,.2)';this.style.borderColor='#f87171'"
         onmouseout="this.style.background='rgba(239,68,68,.1)';this.style.borderColor='rgba(239,68,68,.3)'">
        🔄 Restart Server
      </button>
    </div>
  </div>

</div><!-- /layout -->

<!-- RESTART OVERLAY -->
<div id="restart-overlay" style="
  display:none; position:fixed; inset:0; z-index:9999;
  background:rgba(0,0,0,.75); backdrop-filter:blur(6px);
  align-items:center; justify-content:center; flex-direction:column; gap:16px;
">
  <div style="font-size:40px; animation:spin 1s linear infinite;">🔄</div>
  <div style="font-size:16px; font-weight:700; color:#fff;" id="restart-msg">Restarting server…</div>
  <div style="font-size:12px; color:rgba(255,255,255,.5);">Page will reload automatically when ready</div>
  <div style="width:240px; height:3px; background:rgba(255,255,255,.1); border-radius:2px; margin-top:4px;">
    <div id="restart-bar" style="height:100%; width:0%; background:linear-gradient(90deg,#5c6bc0,#818cf8); border-radius:2px; transition:width .3s;"></div>
  </div>
</div>
<style>
@keyframes spin { to { transform: rotate(360deg); } }
</style>

<!-- ENV MODAL -->
<div class="modal-overlay" id="env-modal">
  <div class="modal">
    <div class="modal-title">🌍 Switch Environment</div>
    <div class="modal-body">Enter your Dataverse environment URL to switch the active connection.</div>
    <label>Environment URL</label>
    <input id="env-url-input" type="text" placeholder="https://yourorg.crm11.dynamics.com">
    <label>Display Name (optional)</label>
    <input id="env-name-input" type="text" placeholder="CRM Demo">
    <div class="modal-actions" style="margin-top:16px;">
      <button class="btn-ghost" onclick="closeModal('env-modal')">Cancel</button>
      <button class="btn-confirm" onclick="applyEnvSwitch()">Switch</button>
    </div>
  </div>
</div>

<!-- MODEL PICKER MODAL -->
<div class="modal-overlay" id="model-modal">
  <div class="modal">
    <div class="modal-title">🤖 Select Model</div>
    <div class="model-grid" id="model-grid">
      <div style="color:var(--text3);font-size:13px;">Loading models…</div>
    </div>
    <div class="modal-actions" style="margin-top:16px;">
      <button class="btn-ghost" onclick="closeModal('model-modal')">Cancel</button>
      <button class="btn-confirm" onclick="applyModelSwitch()">Apply</button>
    </div>
  </div>
</div>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let currentTool  = 'command';
let sending      = false;
let selectedModel = null;  // {provider, model, label}
let planMode     = true;   // Plan Mode ON by default for Command Center

// ── Plan Mode toggle ──────────────────────────────────────────────────────────
function togglePlanMode() {
  planMode = !planMode;
  const btn = document.getElementById('plan-toggle-btn');
  if (btn) btn.classList.toggle('active', planMode);
}

// ── Command Center badge click — inject starter prompt ────────────────────────
function cmdBadgeClick(prompt) {
  const input = document.getElementById('input-command');
  if (!input) return;
  input.value = prompt;
  input.focus();
  // Auto-resize textarea if needed
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  // Scroll the empty state out so the input is visible
  input.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── Navigation ────────────────────────────────────────────────────────────────
function switchTool(id) {
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tool-panel').forEach(el => el.classList.remove('active'));
  document.getElementById('nav-' + id).classList.add('active');
  const panel = document.getElementById('panel-' + id);
  if (panel) panel.classList.add('active');
  currentTool = id;
  if (id === 'settings') {
    loadSettings();
  } else {
    const inp = document.getElementById('input-' + id);
    if (inp) setTimeout(() => inp.focus(), 50);
  }
}
// Activate Command Center on load
document.getElementById('nav-command').classList.add('active');

// ── Auto-resize textarea ──────────────────────────────────────────────────────
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

// ── Key handling ──────────────────────────────────────────────────────────────
function handleKey(e, toolId) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage(toolId);
  }
}

// ── Send chip ─────────────────────────────────────────────────────────────────
function sendChip(el, toolId) {
  const input = document.getElementById('input-' + toolId);
  input.value = el.textContent;
  sendMessage(toolId);
}

// ── Send message — SSE streaming version ──────────────────────────────────────
async function sendMessage(toolId) {
  const input   = document.getElementById('input-' + toolId);
  const message = input.value.trim();
  if (!message || sending) return;

  sending = true;
  document.getElementById('send-' + toolId).disabled = true;

  // Hide empty state
  const empty = document.getElementById('empty-' + toolId);
  if (empty) empty.style.display = 'none';

  // Add user message
  appendMessage(toolId, 'user', message);
  input.value = '';
  input.style.height = 'auto';

  // Show typing indicator
  const typingId = 'typing-' + Date.now();
  appendTypingStream(toolId, typingId);

  // Collect tool trace events during streaming
  const liveTrace = [];

  try {
    // Step 1: POST to register the job and get stream_id
    const startRes = await fetch('/api/chat/start', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message,
        tool_id:   toolId,
        provider:  selectedModel?.provider || null,
        model:     selectedModel?.model    || null,
        plan_mode: toolId === 'command' ? planMode : false,
      }),
    });
    if (!startRes.ok) throw new Error('Failed to start chat job');
    const { stream_id } = await startRes.json();

    // Step 2: Open EventSource to stream events
    await new Promise((resolve, reject) => {
      const es = new EventSource('/api/chat/stream/' + stream_id);

      es.onmessage = (e) => {
        let ev;
        try { ev = JSON.parse(e.data); } catch { return; }

        if (ev.event === 'tool_start') {
          updateTypingStatus(typingId, ev.name, ev.args);
          liveTrace.push({ name: ev.name, args: ev.args, pending: true, write: ev.write });
        }

        if (ev.event === 'tool_done') {
          // Update the last matching pending trace entry
          const idx = liveTrace.findLastIndex(t => t.name === ev.name && t.pending);
          if (idx >= 0) {
            liveTrace[idx] = { name: ev.name, args: ev.args, result: ev.result, ms: ev.ms, type: ev.type };
          }
          updateTypingStatus(typingId, null, null);
        }

        if (ev.event === 'error') {
          es.close();
          removeTyping(typingId);
          appendMessage(toolId, 'assistant', '❌ Agent error: ' + (ev.message || 'unknown'));
          reject(new Error(ev.message));
        }

        if (ev.event === 'done') {
          es.close();
          removeTyping(typingId);

          const trace = ev.tool_trace?.length ? ev.tool_trace : liveTrace.filter(t => !t.pending);

          if (ev.pending_id && ev.pending) {
            if (ev.reply) appendMessage(toolId, 'assistant', ev.reply, trace);
            appendWriteCard(toolId, ev.pending_id, ev.pending);
          } else {
            appendMessage(toolId, 'assistant', ev.reply || '', trace);
          }

          if (ev.provider) updateModelDisplay(ev.provider, ev.model);
          if (ev.domain) updateDomain(ev.domain);
          document.getElementById('nav-' + toolId).classList.add('has-history');
          loadStatus();
          loadTraces();
          resolve();
        }
      };

      es.onerror = () => {
        es.close();
        removeTyping(typingId);
        appendMessage(toolId, 'assistant', '❌ Stream connection lost.');
        reject(new Error('SSE error'));
      };
    });

  } catch (err) {
    removeTyping(typingId);
    if (!err.message.startsWith('Agent error')) {
      appendMessage(toolId, 'assistant', '❌ Network error: ' + err.message);
    }
  }

  sending = false;
  document.getElementById('send-' + toolId).disabled = false;
}

// ── Confirm write ─────────────────────────────────────────────────────────────
async function confirmWrite(pendingId, confirmed, toolId) {
  const card = document.getElementById('card-' + pendingId);
  if (card) card.style.opacity = '.5';

  const typingId = 'typing-' + Date.now();
  appendTyping(toolId, typingId);

  const res = await fetch('/api/confirm', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pending_id: pendingId, confirmed}),
  });
  const data = await res.json();
  if (card) card.remove();
  removeTyping(typingId);
  appendMessage(toolId, 'assistant', data.reply, data.tool_trace);
  loadStatus();
}

// ── DOM helpers ───────────────────────────────────────────────────────────────
function appendMessage(toolId, role, content, trace) {
  const msgs = document.getElementById('msgs-' + toolId);
  const div  = document.createElement('div');
  div.className = 'msg ' + role;

  const avatar = role === 'user' ? '👤' : '⚡';
  const name   = role === 'user' ? 'You' : 'PP Agent';
  const now    = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

  div.innerHTML = `
    <div class="msg-avatar">${avatar}</div>
    <div class="msg-body">
      <div class="msg-meta"><strong>${name}</strong> <span>${now}</span></div>
      <div class="msg-content">${escHtml(content)}</div>
      ${buildTrace(trace)}
    </div>`;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function appendWriteCard(toolId, pendingId, pending) {
  const msgs = document.getElementById('msgs-' + toolId);
  const div  = document.createElement('div');
  div.id = 'card-' + pendingId;
  div.className = 'write-card';
  div.innerHTML = `
    <div class="write-card-header">⚠️ Confirm Operation</div>
    <div class="write-card-preview">${escHtml(pending.preview || JSON.stringify(pending, null, 2))}</div>
    <div class="write-card-actions">
      <button class="btn-confirm" onclick="confirmWrite('${pendingId}', true, '${toolId}')">✅ Yes, execute</button>
      <button class="btn-cancel"  onclick="confirmWrite('${pendingId}', false, '${toolId}')">❌ Cancel</button>
    </div>`;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function buildTrace(trace) {
  if (!trace || !trace.length) return '';
  const items = trace.map(t => {
    const isErr = t.error ? ' error' : ' success';
    const res   = t.error ? ('Error: ' + t.error) : (t.result ? JSON.stringify(t.result, null, 2) : '');
    return `<div class="trace-item${isErr}">
      <span class="trace-icon">${t.error ? '❌' : '✅'}</span>
      <div>
        <div class="trace-name">${t.tool || ''}(${escHtml(JSON.stringify(t.args || {}))})</div>
        <div class="trace-result">${escHtml(res.slice(0, 400))}${res.length > 400 ? '…' : ''}</div>
      </div>
    </div>`;
  });
  return `<div class="tool-trace">${items.join('')}</div>`;
}

function appendTyping(toolId, id) {
  appendTypingStream(toolId, id);
}

function appendTypingStream(toolId, id) {
  const msgs = document.getElementById('msgs-' + toolId);
  const div  = document.createElement('div');
  div.className = 'msg assistant';
  div.id = id;
  div.innerHTML = `
    <div class="msg-avatar">⚡</div>
    <div class="msg-body">
      <div class="msg-meta"><strong>PP Agent</strong></div>
      <div class="typing-wrap">
        <div class="typing"><span></span><span></span><span></span></div>
        <div class="typing-status" id="status-${id}"></div>
      </div>
    </div>`;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function updateTypingStatus(typingId, toolName, args) {
  const el = document.getElementById('status-' + typingId);
  if (!el) return;
  if (!toolName) {
    el.textContent = '';
    return;
  }
  const argsStr = args && Object.keys(args).length
    ? ' — ' + Object.entries(args).slice(0, 2).map(([k,v]) => `${k}: ${String(v).slice(0,30)}`).join(', ')
    : '';
  el.textContent = `🔧 ${toolName.replace(/_/g,' ')}${argsStr}`;
}

function removeTyping(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Restart server ────────────────────────────────────────────────────────────
async function restartServer() {
  const btn     = document.getElementById('restart-btn');
  const overlay = document.getElementById('restart-overlay');
  const msg     = document.getElementById('restart-msg');
  const bar     = document.getElementById('restart-bar');

  if (!confirm('Restart the PP Agent server? All conversation history will be cleared.')) return;

  // Show overlay
  overlay.style.display = 'flex';
  btn.disabled = true;

  try {
    await fetch('/api/restart', { method: 'POST' });
  } catch (_) {
    // Server may close before response — that's fine
  }

  // Animate progress bar while waiting for server to come back
  let pct = 0;
  const fill = setInterval(() => {
    pct = Math.min(pct + 2, 90);
    bar.style.width = pct + '%';
  }, 100);

  // Poll /api/status until server responds
  let attempts = 0;
  const poll = setInterval(async () => {
    attempts++;
    msg.textContent = `Restarting server… (${attempts}s)`;
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      if (r.ok) {
        clearInterval(poll);
        clearInterval(fill);
        bar.style.width = '100%';
        msg.textContent = '✅ Ready — reloading…';
        setTimeout(() => window.location.reload(), 500);
      }
    } catch (_) { /* not ready yet */ }
    if (attempts > 30) {
      clearInterval(poll);
      clearInterval(fill);
      msg.textContent = '⚠️ Server taking too long — try reloading manually';
      bar.style.background = '#ef4444';
      bar.style.width = '100%';
    }
  }, 1000);
}

// ── Clear history ─────────────────────────────────────────────────────────────
async function clearHistory(toolId) {
  await fetch('/api/clear-history', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tool_id: toolId}),
  });
  const msgs = document.getElementById('msgs-' + toolId);
  // Remove all messages except empty state
  Array.from(msgs.children).forEach(c => {
    if (!c.classList.contains('empty-state')) c.remove();
  });
  const empty = document.getElementById('empty-' + toolId);
  if (empty) empty.style.display = '';
  document.getElementById('nav-' + toolId).classList.remove('has-history');
  showToast('History cleared', 'success');
}

// ── Status panel ──────────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const data = await (await fetch('/api/status')).json();
    const auth = data.auth || {};

    const dot   = document.getElementById('env-dot');
    const badge = document.getElementById('env-name');
    if (auth.connected) {
      dot.className   = 'dot';
      badge.textContent = auth.env?.name || 'Connected';
    } else {
      dot.className   = 'dot offline';
      badge.textContent = auth.env?.name || 'Not connected';
    }

    document.getElementById('rp-status').innerHTML =
      auth.connected
        ? '<span class="status-dot on"></span><span class="rp-value green">Connected</span>'
        : '<span class="status-dot off"></span><span class="rp-value red">Disconnected</span>';

    document.getElementById('rp-account').textContent = auth.account || '—';
    document.getElementById('rp-env').textContent     = auth.env?.name || '—';
    document.getElementById('rp-provider').textContent = data.provider || '—';
    document.getElementById('rp-model').textContent   = (data.model || '—').replace('MiniMax-', '').replace('claude-', '');
    document.getElementById('rp-messages').textContent = data.messages_sent || 0;
    document.getElementById('rp-tools').textContent   = data.tool_calls || 0;

    const t = new Date(data.start_time || Date.now());
    document.getElementById('rp-started').textContent = t.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});

    updateModelDisplay(data.provider, data.model);
  } catch (e) { /* ignore */ }
}

function updateModelDisplay(provider, model) {
  if (!provider) return;
  document.getElementById('model-display').textContent = (model || provider).replace('MiniMax-M','M').replace('claude-','').replace('gpt-','GPT-');
}

// ── Environment quick-switch pills ────────────────────────────────────────────
let _activeEnvUrl = '';

async function loadEnvPills() {
  try {
    const data = await (await fetch('/api/environments')).json();
    _activeEnvUrl = data.active_url || '';
    const envs  = data.environments || [];
    const pills = document.getElementById('env-pills');
    if (!pills || envs.length < 2) return;   // only show if multiple envs configured

    pills.innerHTML = envs.map(e => {
      const active = e.url === _activeEnvUrl ? ' active' : '';
      return `<div class="env-pill${active}" data-url="${e.url}" data-name="${e.name}"
                   onclick="switchEnv(this)" title="${e.url}">${e.name}</div>`;
    }).join('<span class="env-pill-sep">|</span>');
  } catch { /* ignore */ }
}

async function switchEnv(el) {
  const url  = el.dataset.url;
  const name = el.dataset.name;
  if (url === _activeEnvUrl) return;

  document.querySelectorAll('.env-pill').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  _activeEnvUrl = url;

  try {
    await fetch('/api/switch-env', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({url, name}),
    });
    showToast(`Switched to ${name}`, 'success');
    loadStatus();
  } catch (e) {
    showToast('Failed to switch environment', 'error');
  }
}

// ── Model picker ──────────────────────────────────────────────────────────────
let modelList = [];

async function openModelPicker() {
  const modal = document.getElementById('model-modal');
  modal.classList.add('show');
  if (!modelList.length) {
    try {
      modelList = await (await fetch('/api/models')).json();
    } catch (e) {}
  }
  renderModelGrid();
}

function renderModelGrid() {
  const grid = document.getElementById('model-grid');
  grid.innerHTML = modelList.map(m => {
    const sel = selectedModel && selectedModel.model === m.model ? ' selected' : '';
    return `<div class="model-option${sel}" data-provider="${m.provider}" data-model="${m.model}" data-label="${m.label}" onclick="selectModel(this)">
      <span class="mo-provider">${m.provider}</span>
      <span class="mo-label">${m.label}</span>
      <span class="mo-check">✓</span>
    </div>`;
  }).join('');
}

function selectModel(el) {
  selectedModel = {provider: el.dataset.provider, model: el.dataset.model, label: el.dataset.label};
  document.querySelectorAll('.model-option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
}

async function applyModelSwitch() {
  if (!selectedModel) { closeModal('model-modal'); return; }
  await fetch('/api/switch-model', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(selectedModel),
  });
  closeModal('model-modal');
  updateModelDisplay(selectedModel.provider, selectedModel.model);
  showToast('Model switched to ' + selectedModel.label, 'success');
  loadStatus();
}

// ── Env modal ─────────────────────────────────────────────────────────────────
function openEnvModal() {
  document.getElementById('env-modal').classList.add('show');
}

async function applyEnvSwitch() {
  const url  = document.getElementById('env-url-input').value.trim();
  const name = document.getElementById('env-name-input').value.trim();
  if (!url) { showToast('URL required', 'error'); return; }
  const res = await fetch('/api/switch-env', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, name}),
  });
  const data = await res.json();
  closeModal('env-modal');
  if (data.ok) {
    showToast('Switched to ' + (name || url), 'success');
    loadStatus();
  } else {
    showToast(data.error || 'Switch failed', 'error');
  }
}

// ── Generic modal close ───────────────────────────────────────────────────────
function closeModal(id) {
  document.getElementById(id).classList.remove('show');
}
document.querySelectorAll('.modal-overlay').forEach(el => {
  el.addEventListener('click', e => { if (e.target === el) el.classList.remove('show'); });
});

// ── Toast ─────────────────────────────────────────────────────────────────────
let _toastTimer;
function showToast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + (type || '');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 2800);
}

// ── Settings ───────────────────────────────────────────────────────────────────
let _rawSettings = {};

async function loadSettings() {
  const body = document.getElementById('settings-body');
  if (!body) return;
  body.innerHTML = '<div class="settings-loading">Loading configuration…</div>';
  try {
    const data = await (await fetch('/api/settings/get')).json();
    _rawSettings = data;
    body.innerHTML = renderSettings(data);
  } catch (e) {
    body.innerHTML = `<div class="settings-loading">❌ Failed to load: ${e.message}</div>`;
  }
}

function renderSettings(d) {
  const v = k => (d[k] && d[k].has_value) ? d[k].display : '';
  const placeholder = k => {
    const map = {
      MINIMAX_API_KEY: 'sk-cp-your-token-plan-key',
      ANTHROPIC_API_KEY: 'sk-ant-your-key',
      OPENAI_API_KEY: 'sk-your-key',
      GEMINI_API_KEY: 'AIza...',
      OPENROUTER_API_KEY: 'sk-or-...',
      PP_CLIENT_ID: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
      PP_TENANT_ID: 'common  (or your tenant GUID)',
      PP_CLIENT_SECRET: 'leave blank for device code login',
      PP_ENV_URL: 'https://yourorg.crm11.dynamics.com',
      PP_ENV_NAME: 'CRM Demo',
      PP_ENV_ID: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
      PP_GEO: 'unitedstates',
      ADO_ORG: 'mycompany',
      ADO_PAT: 'your-personal-access-token',
      ADO_PROJECT: 'PowerPlatformDev',
      LLM_PROVIDER: 'auto-detect (leave blank)',
      LLM_MODEL: 'auto-detect (leave blank)',
    };
    return map[k] || '';
  };

  const field = (k, label, hint, badge='optional', type='text') => {
    const val = v(k);
    const isSecret = ['MINIMAX_API_KEY','ANTHROPIC_API_KEY','OPENAI_API_KEY','GEMINI_API_KEY',
      'OPENROUTER_API_KEY','PP_CLIENT_SECRET','ADO_PAT'].includes(k);
    return `
      <div class="cfg-field">
        <label>${label} <span class="lbl-badge ${badge}">${badge}</span></label>
        <div class="cfg-input-wrap">
          <input class="cfg-input ${val ? 'has-value' : ''}"
            type="${isSecret ? 'password' : 'text'}"
            id="cfg-${k}" name="${k}"
            value="${escHtml(val)}"
            placeholder="${escHtml(placeholder(k))}">
          ${isSecret ? `<button class="cfg-eye-btn" type="button" onclick="toggleSecret('cfg-${k}')">👁</button>` : ''}
        </div>
        ${hint ? `<div class="cfg-hint">${hint}</div>` : ''}
      </div>`;
  };

  const hasLLM = ['MINIMAX_API_KEY','ANTHROPIC_API_KEY','OPENAI_API_KEY','GEMINI_API_KEY','OPENROUTER_API_KEY']
    .filter(k => d[k] && d[k].has_value);
  const hasPP  = d['PP_CLIENT_ID'] && d['PP_CLIENT_ID'].has_value && d['PP_ENV_URL'] && d['PP_ENV_URL'].has_value;
  const hasADO = d['ADO_ORG'] && d['ADO_ORG'].has_value && d['ADO_PAT'] && d['ADO_PAT'].has_value;

  return `
  <!-- ── LLM PROVIDERS ─────────────────────────────────────── -->
  <div class="cfg-section">
    <div class="cfg-section-header">
      <span class="cfg-icon">🤖</span>
      <span class="cfg-title">LLM API Keys</span>
      <span class="cfg-status ${hasLLM.length ? 'ok' : ''}">${hasLLM.length ? hasLLM.length + ' key(s) configured' : 'No keys yet'}</span>
      <button class="cfg-test-btn" onclick="testConnection('llm', this)">Test LLM</button>
    </div>
    <div class="cfg-fields">
      <div class="cfg-row">
        ${field('MINIMAX_API_KEY', 'MiniMax API Key', '<a href="https://platform.minimax.io/user-center/payment/token-plan" target="_blank">Get Token Plan key</a> (sk-cp-... starts with this)', 'default')}
        ${field('ANTHROPIC_API_KEY', 'Anthropic Claude Key', '<a href="https://console.anthropic.com/settings/keys" target="_blank">Get key</a>')}
      </div>
      <div class="cfg-row triple">
        ${field('OPENAI_API_KEY', 'OpenAI Key', '<a href="https://platform.openai.com/api-keys" target="_blank">Get key</a>')}
        ${field('GEMINI_API_KEY', 'Gemini Key', '<a href="https://aistudio.google.com/apikey" target="_blank">Get key</a>')}
        ${field('OPENROUTER_API_KEY', 'OpenRouter Key', '<a href="https://openrouter.ai/keys" target="_blank">Get key</a>')}
      </div>
      <div class="cfg-row">
        ${field('LLM_PROVIDER', 'Provider Override', 'Leave blank to auto-detect from keys above. Options: minimax, claude, openai, gemini, openrouter')}
        ${field('LLM_MODEL', 'Model Override', 'Leave blank to use default model for the provider')}
      </div>
    </div>
    <div class="cfg-actions">
      <button class="cfg-save-btn" onclick="saveSection(['MINIMAX_API_KEY','ANTHROPIC_API_KEY','OPENAI_API_KEY','GEMINI_API_KEY','OPENROUTER_API_KEY','LLM_PROVIDER','LLM_MODEL'], this)">💾 Save LLM Keys</button>
      <span class="cfg-result" id="result-llm"></span>
    </div>
  </div>

  <!-- ── POWER PLATFORM ────────────────────────────────────── -->
  <div class="cfg-section">
    <div class="cfg-section-header">
      <span class="cfg-icon">⚡</span>
      <span class="cfg-title">Power Platform &amp; Dataverse</span>
      <span class="cfg-status ${hasPP ? 'ok' : 'err'}">${hasPP ? 'Configured' : 'Not configured'}</span>
      <button class="cfg-test-btn" onclick="testConnection('powerplatform', this)">Test Connection</button>
    </div>
    <div class="cfg-fields">
      <div style="background:#1a0f2e;border:1px solid #5b21b6;border-radius:8px;padding:12px 14px;font-size:12px;color:#c4b5fd;margin-bottom:4px;">
        <strong>📋 How to get these:</strong> Azure Portal → Entra ID → App Registrations → your app registration<br>
        Required API permissions: <em>Dynamics CRM → user_impersonation</em>, <em>PowerApps Service → User</em>
      </div>
      <div class="cfg-row">
        ${field('PP_CLIENT_ID', 'Azure App Client ID', 'Application (client) ID from your Azure App Registration', 'required')}
        ${field('PP_TENANT_ID', 'Tenant ID', 'Use "common" for any account, or paste your tenant GUID for org-only', 'required')}
      </div>
      <div class="cfg-row">
        ${field('PP_CLIENT_SECRET', 'Client Secret', 'Optional — only needed for headless/service principal auth. Leave blank for device code (browser) login.', 'optional')}
        ${field('PP_GEO', 'Geography', 'Your Power Platform region: unitedstates, europe, asia, etc.', 'optional')}
      </div>
      <div class="cfg-row">
        ${field('PP_ENV_URL', 'Environment URL', 'e.g. https://yourorg.crm11.dynamics.com — Admin Center → Environments → your env', 'required')}
        ${field('PP_ENV_NAME', 'Environment Display Name', 'Friendly label shown in the top bar (e.g. "CRM Demo")', 'optional')}
      </div>
      <div class="cfg-row single">
        ${field('PP_ENV_ID', 'Environment ID (GUID)', 'Admin Center → Environments → your env → Details → Environment ID. Needed for Power Automate + Canvas App APIs.', 'required')}
      </div>
    </div>
    <div class="cfg-actions">
      <button class="cfg-save-btn" onclick="saveSection(['PP_CLIENT_ID','PP_TENANT_ID','PP_CLIENT_SECRET','PP_ENV_URL','PP_ENV_NAME','PP_ENV_ID','PP_GEO'], this)">💾 Save PP Settings</button>
      <button class="connect-btn" id="signin-btn" onclick="connectPP()">🔐 Sign In to PP</button>
      <span class="cfg-result" id="result-pp"></span>
    </div>
    <div id="device-code-card" style="display:none;margin-top:14px;background:#0d1f0d;border:1px solid #1a3a1a;border-radius:10px;padding:16px 20px;">
      <div style="font-size:12px;color:#6ee7b7;margin-bottom:10px;font-weight:600;letter-spacing:.04em;">SIGN IN TO MICROSOFT</div>
      <div style="margin-bottom:12px;">
        <div style="font-size:12px;color:#9ca3af;margin-bottom:4px;">1. Open this URL in your browser:</div>
        <a id="device-url" href="#" target="_blank" style="font-size:13px;color:#34d399;text-decoration:underline;word-break:break-all;"></a>
      </div>
      <div style="margin-bottom:14px;">
        <div style="font-size:12px;color:#9ca3af;margin-bottom:6px;">2. Enter this code when prompted:</div>
        <div style="display:flex;align-items:center;gap:10px;">
          <span id="device-code" style="font-size:22px;font-weight:700;color:#f9fafb;letter-spacing:.18em;font-family:monospace;background:#1f2937;padding:8px 16px;border-radius:6px;border:1px solid #374151;"></span>
          <button onclick="copyDeviceCode()" style="background:#1f2937;border:1px solid #374151;color:#9ca3af;border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer;" title="Copy code">📋 Copy</button>
        </div>
      </div>
      <div id="auth-status-msg" style="font-size:12px;color:#9ca3af;">Waiting for you to sign in…</div>
    </div>
  </div>

  <!-- ── AZURE DEVOPS ──────────────────────────────────────── -->
  <div class="cfg-section">
    <div class="cfg-section-header">
      <span class="cfg-icon">⚙️</span>
      <span class="cfg-title">Azure DevOps</span>
      <span class="cfg-status ${hasADO ? 'ok' : ''}">${hasADO ? 'Configured' : 'Optional'}</span>
      <button class="cfg-test-btn" onclick="testConnection('ado', this)">Test ADO</button>
    </div>
    <div class="cfg-fields">
      <div class="cfg-row triple">
        ${field('ADO_ORG', 'Organisation', 'Your ADO org name (e.g. mycompany from dev.azure.com/mycompany)', 'optional')}
        ${field('ADO_PROJECT', 'Default Project', 'Default project name used when no project is specified', 'optional')}
        ${field('ADO_PAT', 'Personal Access Token', '<a href="https://dev.azure.com" target="_blank">dev.azure.com</a> → User Settings → Personal Access Tokens', 'optional')}
      </div>
    </div>
    <div class="cfg-actions">
      <button class="cfg-save-btn" onclick="saveSection(['ADO_ORG','ADO_PROJECT','ADO_PAT'], this)">💾 Save ADO Settings</button>
      <span class="cfg-result" id="result-ado"></span>
    </div>
  </div>

  <!-- ── SAVE ALL ──────────────────────────────────────────── -->
  <div style="display:flex;gap:12px;align-items:center;padding:4px 0 16px;">
    <button class="cfg-save-btn" style="padding:11px 30px;font-size:14px;" onclick="saveAll()">✅ Save All Settings</button>
    <span style="font-size:12px;color:var(--text3);">Settings are saved to <code style="background:var(--bg3);padding:2px 6px;border-radius:4px;">.env</code> file in the project folder</span>
  </div>
  `;
}

function toggleSecret(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.type = el.type === 'password' ? 'text' : 'password';
}

async function saveSection(keys, btn) {
  const data = {};
  keys.forEach(k => {
    const el = document.getElementById('cfg-' + k);
    if (el) {
      const val = el.value.trim();
      if (val && !val.includes('••')) data[k] = val;
    }
  });
  if (Object.keys(data).length === 0) {
    showToast('No changes to save', '');
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const res  = await fetch('/api/settings/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
  const json = await res.json();
  btn.disabled = false;
  btn.textContent = '💾 Saved!';
  setTimeout(() => { btn.textContent = btn.textContent.replace('Saved!', btn.dataset.label || 'Save'); }, 2000);
  if (json.ok) {
    showToast('✅ Saved ' + json.saved.length + ' setting(s)', 'success');
    loadStatus();
  } else {
    showToast('❌ ' + json.error, 'error');
  }
}

async function saveAll() {
  const data = {};
  document.querySelectorAll('.cfg-input').forEach(el => {
    const k = el.name || el.id.replace('cfg-', '');
    const val = el.value.trim();
    if (k && val && !val.includes('••')) data[k] = val;
  });
  const res  = await fetch('/api/settings/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
  const json = await res.json();
  if (json.ok) {
    showToast('✅ All settings saved (' + json.saved.length + ' values)', 'success');
    loadStatus();
    loadSettings();
  } else {
    showToast('❌ ' + json.error, 'error');
  }
}

async function testConnection(type, btn) {
  const originalText = btn.textContent;
  btn.textContent = 'Testing…';
  btn.disabled = true;
  const res  = await fetch('/api/settings/test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({type})});
  const json = await res.json();
  btn.disabled = false;
  btn.textContent = originalText;
  const resultId = 'result-' + (type === 'powerplatform' ? 'pp' : type);
  const el = document.getElementById(resultId);
  if (el) {
    el.textContent = json.message;
    el.className = 'cfg-result ' + (json.ok ? 'ok' : 'err');
  }
  showToast(json.message, json.ok ? 'success' : 'error');
}

let _authPollTimer = null;

async function connectPP() {
  const btn  = document.getElementById('signin-btn');
  const el   = document.getElementById('result-pp');
  const card = document.getElementById('device-code-card');

  btn.disabled = true;
  btn.textContent = '⏳ Starting…';
  if (el) { el.textContent = ''; el.className = 'cfg-result'; }

  const res  = await fetch('/api/settings/connect-pp', {method:'POST'});
  const data = await res.json();

  if (!data.ok) {
    btn.disabled = false;
    btn.textContent = '🔐 Sign In to PP';
    if (el) { el.textContent = data.message; el.className = 'cfg-result err'; }
    return;
  }

  const flow = data.flow;
  document.getElementById('device-url').href        = flow.verification_uri;
  document.getElementById('device-url').textContent = flow.verification_uri;
  document.getElementById('device-code').textContent = flow.user_code;
  document.getElementById('auth-status-msg').textContent = 'Waiting for you to sign in…';
  card.style.display = 'block';
  btn.textContent = '⏳ Waiting…';

  if (_authPollTimer) clearInterval(_authPollTimer);
  _authPollTimer = setInterval(async () => {
    const pr   = await fetch('/api/settings/auth-poll');
    const poll = await pr.json();

    if (poll.status === 'success') {
      clearInterval(_authPollTimer);
      card.style.display = 'none';
      btn.disabled = false;
      btn.textContent = '✅ Signed In';
      if (el) { el.textContent = `Signed in as ${poll.account || 'unknown'}`; el.className = 'cfg-result ok'; }
      showToast('Signed in to Power Platform ✅', 'success');
      loadStatus();
    } else if (poll.status === 'failed') {
      clearInterval(_authPollTimer);
      card.style.display = 'none';
      btn.disabled = false;
      btn.textContent = '🔐 Sign In to PP';
      if (el) { el.textContent = poll.error || 'Sign-in failed'; el.className = 'cfg-result err'; }
      showToast('Sign-in failed: ' + (poll.error || 'unknown'), 'error');
    }
  }, 3000);
}

function copyDeviceCode() {
  const code = document.getElementById('device-code').textContent;
  navigator.clipboard.writeText(code).then(() => showToast('Code copied!', 'success'));
}

// ── Boot ──────────────────────────────────────────────────────────────────────
loadStatus();
loadEnvPills();
setInterval(loadStatus, 30000);

// ── Agent Traces (Observability) ──────────────────────────────────────────
async function loadTraces() {
  try {
    const res  = await fetch('/api/traces?limit=20');
    const data = await res.json();
    if (!data.ok) return;

    // Stats bar
    const s = data.stats || {};
    document.getElementById('obs-stats').innerHTML = `
      <div style="font-size:10px;color:var(--text3);background:var(--bg4);
           border-radius:4px;padding:2px 6px;">${s.total_traces ?? 0} calls</div>
      <div style="font-size:10px;color:var(--text3);background:var(--bg4);
           border-radius:4px;padding:2px 6px;">${s.avg_response_ms ?? 0}ms avg</div>
      <div style="font-size:10px;color:var(--text3);background:var(--bg4);
           border-radius:4px;padding:2px 6px;">${s.avg_tools_per_turn ?? 0} tools/turn</div>
    `;

    // Trace list
    const list = document.getElementById('obs-list');
    if (!data.traces || !data.traces.length) {
      list.innerHTML = '<div style="font-size:11px;color:var(--text3);">No traces yet.</div>';
      return;
    }
    list.innerHTML = data.traces.slice(0, 10).map(t => {
      const ts  = new Date(t.ts * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      const msg = (t.user_msg || '').slice(0, 40);
      const tc  = t.tool_count;
      const ms  = t.total_ms;
      return `<div style="font-size:11px;color:var(--text2);border-left:2px solid var(--accent);
                           padding:3px 6px;border-radius:0 4px 4px 0;background:var(--bg4);"
                   title="${(t.user_msg||'').replace(/"/g,'&quot;')}">
        <span style="color:var(--text3);font-size:10px;">${ts}</span>
        <span style="margin-left:4px;">${msg}…</span>
        <span style="float:right;color:var(--text3);font-size:10px;">${tc}🔧 ${ms}ms</span>
      </div>`;
    }).join('');
  } catch(e) {
    // silently ignore — observability is optional
  }
}

// Update domain label in right panel after each response
function updateDomain(domain) {
  const labels = {
    dataverse: '🗄️ Dataverse', flows: '⚡ Flows', security: '🔒 Security',
    crm: '💼 CRM', admin: '🏛️ Admin', general: '🤖 General'
  };
  const el = document.getElementById('obs-domain');
  if (el) el.textContent = 'Domain: ' + (labels[domain] || domain || '—');
}

loadTraces();
setInterval(loadTraces, 60000);
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    port = int(os.getenv("PORT", 5005))
    print(f"""
╔══════════════════════════════════════════════════════╗
║  ⚡  PP Agent — Power Platform AI                    ║
╠══════════════════════════════════════════════════════╣
║  Open:  http://localhost:{port:<27}║
║  Stop:  Ctrl + C                                     ║
╚══════════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=port, debug="--debug" in sys.argv, threaded=True)
