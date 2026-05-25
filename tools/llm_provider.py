"""
tools/llm_provider.py — Multi-LLM Provider
============================================
Supports: MiniMax M2.7 (default), Claude, GPT-4o, Gemini, OpenRouter
MiniMax uses the OpenAI-compatible endpoint with tool calling support.

Priority (auto-detect): minimax → claude → openai → gemini → openrouter
Override: set LLM_PROVIDER in .env
"""

import os
import json
import time
from dotenv import load_dotenv

load_dotenv()

# ── Provider constants ─────────────────────────────────────────────────────────
MINIMAX  = "minimax"
CLAUDE   = "claude"
OPENAI   = "openai"
GEMINI   = "gemini"
OPENROUTER = "openrouter"

# ── Default models per provider ───────────────────────────────────────────────
DEFAULT_MODELS = {
    MINIMAX:    "MiniMax-M2.7",
    CLAUDE:     "claude-sonnet-4-6",
    OPENAI:     "gpt-4o",
    GEMINI:     "gemini-2.5-flash",
    OPENROUTER: "anthropic/claude-sonnet-4-5",
}

# ── All selectable models for the UI model picker ─────────────────────────────
MODEL_OPTIONS = [
    # MiniMax
    {"provider": MINIMAX,    "model": "MiniMax-M2.7",               "label": "MiniMax M2.7 (default)"},
    {"provider": MINIMAX,    "model": "MiniMax-M2.5",               "label": "MiniMax M2.5"},
    # Claude
    {"provider": CLAUDE,     "model": "claude-opus-4-6",            "label": "Claude Opus 4.6"},
    {"provider": CLAUDE,     "model": "claude-sonnet-4-6",          "label": "Claude Sonnet 4.6"},
    {"provider": CLAUDE,     "model": "claude-haiku-4-5-20251001",  "label": "Claude Haiku 4.5"},
    # OpenAI
    {"provider": OPENAI,     "model": "gpt-4o",                     "label": "GPT-4o"},
    {"provider": OPENAI,     "model": "gpt-4o-mini",                "label": "GPT-4o Mini"},
    {"provider": OPENAI,     "model": "o3",                         "label": "OpenAI o3"},
    # Gemini
    {"provider": GEMINI,     "model": "gemini-2.5-pro",             "label": "Gemini 2.5 Pro"},
    {"provider": GEMINI,     "model": "gemini-2.5-flash",           "label": "Gemini 2.5 Flash"},
    # OpenRouter
    {"provider": OPENROUTER, "model": "google/gemini-2.5-pro",      "label": "OpenRouter: Gemini 2.5 Pro"},
    {"provider": OPENROUTER, "model": "deepseek/deepseek-r2",       "label": "OpenRouter: DeepSeek R2"},
    {"provider": OPENROUTER, "model": "meta-llama/llama-4-maverick","label": "OpenRouter: Llama 4 Maverick"},
]


def detect_provider() -> str:
    """Auto-detect provider from .env keys. MiniMax is first priority."""
    override = os.getenv("LLM_PROVIDER", "").strip().lower()
    if override:
        return override

    if os.getenv("MINIMAX_API_KEY"):
        return MINIMAX
    if os.getenv("ANTHROPIC_API_KEY"):
        return CLAUDE
    if os.getenv("OPENAI_API_KEY"):
        return OPENAI
    if os.getenv("GEMINI_API_KEY"):
        return GEMINI
    if os.getenv("OPENROUTER_API_KEY"):
        return OPENROUTER

    raise RuntimeError(
        "❌ No LLM API key found. Add at least one key to your .env file.\n"
        "   Priority: MINIMAX_API_KEY → ANTHROPIC_API_KEY → OPENAI_API_KEY → GEMINI_API_KEY → OPENROUTER_API_KEY"
    )


def get_active_model(provider: str) -> str:
    """Get model name — from LLM_MODEL env var or default for provider."""
    override = os.getenv("LLM_MODEL", "").strip()
    return override if override else DEFAULT_MODELS.get(provider, DEFAULT_MODELS[MINIMAX])


# ─────────────────────────────────────────────────────────────────────────────
#  UNIFIED CALL — single function that works for all providers
# ─────────────────────────────────────────────────────────────────────────────

def call_llm(
    messages: list,
    tools: list | None = None,
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> dict:
    """
    Unified LLM call. Returns a dict:
    {
        "content":    str | None,        # text response
        "tool_calls": list | None,       # [{name, arguments}] if tools called
        "provider":   str,               # which provider was used
        "model":      str,               # which model was used
    }
    """
    _provider = provider or detect_provider()
    _model    = model    or get_active_model(_provider)

    if _provider == MINIMAX:
        return _call_minimax(messages, tools, _model, temperature, max_tokens)
    elif _provider == CLAUDE:
        return _call_claude(messages, tools, _model, temperature, max_tokens)
    elif _provider == OPENAI:
        return _call_openai(messages, tools, _model, temperature, max_tokens)
    elif _provider == GEMINI:
        return _call_gemini(messages, tools, _model, temperature, max_tokens)
    elif _provider == OPENROUTER:
        return _call_openrouter(messages, tools, _model, temperature, max_tokens)
    else:
        raise ValueError(f"Unknown provider: {_provider}")


# ─────────────────────────────────────────────────────────────────────────────
#  MINIMAX — OpenAI-compatible endpoint, Token Plan key
# ─────────────────────────────────────────────────────────────────────────────

def _call_minimax(messages, tools, model, temperature, max_tokens):
    from openai import OpenAI
    client = OpenAI(
        api_key=os.getenv("MINIMAX_API_KEY"),
        base_url="https://api.minimax.io/v1",
    )
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if tools:
        kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        kwargs["tool_choice"] = "auto"

    last_err = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(**kwargs)
            # MiniMax sometimes returns choices=None on transient errors
            if not resp.choices:
                raw = getattr(resp, "model_extra", {}) or {}
                base = raw.get("base_resp", {}) or {}
                raise RuntimeError(
                    f"MiniMax returned empty choices (attempt {attempt+1}/3). "
                    f"base_resp={base}"
                )
            return _parse_openai_response(resp.choices[0].message, model, MINIMAX)
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 ** attempt)   # 1s, 2s backoff
    raise RuntimeError(f"MiniMax failed after 3 attempts: {last_err}")


# ─────────────────────────────────────────────────────────────────────────────
#  CLAUDE — Anthropic SDK
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude(messages, tools, model, temperature, max_tokens):
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Extract system prompt from messages
    system = ""
    filtered = []
    for m in messages:
        if m.get("role") == "system":
            system = m.get("content", "")
        else:
            filtered.append(m)

    # Convert tool result messages for Anthropic format
    anthropic_messages = _to_anthropic_messages(filtered)

    kwargs = dict(model=model, max_tokens=max_tokens, messages=anthropic_messages)
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = [
            {"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("parameters", {})}
            for t in tools
        ]

    resp = client.messages.create(**kwargs)

    tool_calls = []
    content_text = ""
    for block in resp.content:
        if block.type == "text":
            content_text += block.text
        elif block.type == "tool_use":
            tool_calls.append({"name": block.name, "arguments": block.input, "id": block.id})

    return {
        "content":    content_text or None,
        "tool_calls": tool_calls or None,
        "provider":   CLAUDE,
        "model":      model,
        "_raw":       resp,
    }


def _to_anthropic_messages(messages):
    """Convert OpenAI-format messages to Anthropic format."""
    result = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            result.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": str(m.get("content", ""))}]
            })
        elif role == "assistant" and m.get("tool_calls"):
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function", tc)
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id", "tc_0"), "name": fn.get("name", ""), "input": args})
            result.append({"role": "assistant", "content": blocks})
        else:
            result.append({"role": role, "content": m.get("content", "")})
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  OPENAI — standard OpenAI SDK
# ─────────────────────────────────────────────────────────────────────────────

def _call_openai(messages, tools, model, temperature, max_tokens):
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if tools:
        kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        kwargs["tool_choice"] = "auto"
    resp = client.chat.completions.create(**kwargs)
    return _parse_openai_response(resp.choices[0].message, model, OPENAI)


# ─────────────────────────────────────────────────────────────────────────────
#  GEMINI — via OpenAI-compatible endpoint
# ─────────────────────────────────────────────────────────────────────────────

def _call_gemini(messages, tools, model, temperature, max_tokens):
    from openai import OpenAI
    client = OpenAI(
        api_key=os.getenv("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if tools:
        kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        kwargs["tool_choice"] = "auto"
    resp = client.chat.completions.create(**kwargs)
    return _parse_openai_response(resp.choices[0].message, model, GEMINI)


# ─────────────────────────────────────────────────────────────────────────────
#  OPENROUTER — OpenAI-compatible with router base URL
# ─────────────────────────────────────────────────────────────────────────────

def _call_openrouter(messages, tools, model, temperature, max_tokens):
    from openai import OpenAI
    client = OpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
        default_headers={"HTTP-Referer": "https://pp-agent.local", "X-Title": "PP Agent"},
    )
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if tools:
        kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        kwargs["tool_choice"] = "auto"
    resp = client.chat.completions.create(**kwargs)
    return _parse_openai_response(resp.choices[0].message, model, OPENROUTER)


# ─────────────────────────────────────────────────────────────────────────────
#  SHARED HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_openai_response(msg, model, provider):
    """Parse OpenAI-format response message into unified dict."""
    tool_calls = None
    if msg.tool_calls:
        tool_calls = []
        for tc in msg.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            tool_calls.append({
                "id":        tc.id,
                "name":      tc.function.name,
                "arguments": args,
            })
    return {
        "content":    msg.content,
        "tool_calls": tool_calls,
        "provider":   provider,
        "model":      model,
    }


def provider_display_name(provider: str) -> str:
    return {
        MINIMAX:    "MiniMax",
        CLAUDE:     "Claude",
        OPENAI:     "OpenAI",
        GEMINI:     "Gemini",
        OPENROUTER: "OpenRouter",
    }.get(provider, provider.title())
