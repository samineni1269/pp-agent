"""
tools/guardrails.py — Input/Output Guardrails
===============================================
Validates user inputs before agent processing and agent outputs before delivery.

Checks:
  Input  — length cap, prompt injection patterns, credential leak scan
  Output — empty/refusal detection, credential leak scan, minimum quality check
"""

from __future__ import annotations
import re

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_INPUT_CHARS  = 2000
MAX_OUTPUT_CHARS = 20_000   # hard cap on response before truncation

# Patterns that look like prompt injection attempts
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"forget\s+(everything|all|your)\s+(you|instructions?|above)",
    r"you\s+are\s+now\s+a?\s*(different|new|another|evil|unrestricted)\s+(AI|model|assistant|bot)",
    r"disregard\s+(your|all|the)\s+(guidelines?|rules?|instructions?|training)",
    r"act\s+as\s+(a\s+)?(jailbroken|uncensored|unfiltered|evil|DAN)",
    r"do\s+anything\s+now",
    r"jailbreak",
    r"pretend\s+(you|there)\s+(are|have\s+no)\s+(no\s+)?(rules?|restrictions?|guidelines?)",
    r"system\s+prompt\s*[:=]",
    r"\[SYSTEM\]",
    r"<system>",
]

# Patterns that look like leaked credentials / secrets
_CREDENTIAL_PATTERNS = [
    r"[A-Za-z0-9+/]{40,}={0,2}",          # long base64 strings (API keys)
    r"sk-[A-Za-z0-9]{20,}",               # OpenAI-style secret keys
    r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}",  # Bearer tokens
    r"password\s*[:=]\s*\S{6,}",          # password = something
    r"secret\s*[:=]\s*\S{6,}",            # secret = something
    r"api[_\-]?key\s*[:=]\s*\S{6,}",      # api_key = something
    r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+",  # JWT tokens
]

# Phrases that indicate the agent is refusing or confused
_REFUSAL_PATTERNS = [
    r"^(sorry,?\s+)?(I\s+)?(cannot|can't|am\s+unable\s+to)\s+",
    r"^I\s+don't\s+(have\s+access|know)",
    r"^As\s+an\s+AI",
    r"^I\s+apologize",
]

_COMPILED_INJECTION   = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]
_COMPILED_CREDENTIALS = [re.compile(p, re.IGNORECASE) for p in _CREDENTIAL_PATTERNS]
_COMPILED_REFUSALS    = [re.compile(p, re.IGNORECASE) for p in _REFUSAL_PATTERNS]


# ── INPUT GUARDRAILS ─────────────────────────────────────────────────────────

def check_input(user_message: str) -> dict:
    """
    Validate the user's input before sending to the agent.

    Returns:
        {"ok": True}  — safe to proceed
        {"ok": False, "reason": str, "code": str}  — blocked
    """
    if not user_message or not user_message.strip():
        return {"ok": False, "reason": "Message is empty.", "code": "EMPTY"}

    if len(user_message) > MAX_INPUT_CHARS:
        return {
            "ok":     False,
            "reason": f"Message too long ({len(user_message)} chars). Max is {MAX_INPUT_CHARS}.",
            "code":   "TOO_LONG",
        }

    # Prompt injection detection
    for pattern in _COMPILED_INJECTION:
        if pattern.search(user_message):
            return {
                "ok":     False,
                "reason": "Message contains patterns that look like prompt injection. Please rephrase.",
                "code":   "INJECTION",
            }

    # Credential leak scan on input
    cred = _find_credential(user_message)
    if cred:
        return {
            "ok":     False,
            "reason": "Your message appears to contain sensitive credentials. Remove them before sending.",
            "code":   "CREDENTIAL_IN_INPUT",
        }

    return {"ok": True}


# ── OUTPUT GUARDRAILS ────────────────────────────────────────────────────────

def check_output(response: str) -> dict:
    """
    Validate the agent's response before delivering to the user.

    Returns:
        {"ok": True, "response": str}  — safe, (possibly truncated) response
        {"ok": False, "reason": str, "code": str}  — blocked/flagged
    """
    if not response or not response.strip():
        return {"ok": False, "reason": "Agent returned an empty response.", "code": "EMPTY_OUTPUT"}

    # Hard truncate if response is enormous
    if len(response) > MAX_OUTPUT_CHARS:
        response = response[:MAX_OUTPUT_CHARS] + "\n\n…[response truncated]"

    # Credential leak scan on output
    cred = _find_credential(response)
    if cred:
        # Redact rather than block — replace the match
        response = _redact_credentials(response)

    return {"ok": True, "response": response}


def is_refusal(response: str) -> bool:
    """Return True if the response looks like an AI refusal."""
    if not response:
        return True
    for pattern in _COMPILED_REFUSALS:
        if pattern.search(response.strip()):
            return True
    return False


# ── HELPERS ──────────────────────────────────────────────────────────────────

def _find_credential(text: str) -> str | None:
    """Return the first matched credential pattern, or None."""
    for pattern in _COMPILED_CREDENTIALS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


def _redact_credentials(text: str) -> str:
    """Replace credential-looking strings with [REDACTED]."""
    for pattern in _COMPILED_CREDENTIALS:
        text = pattern.sub("[REDACTED]", text)
    return text


def sanitise_for_log(text: str, max_chars: int = 500) -> str:
    """Prepare a string for logging — redact creds and truncate."""
    text = _redact_credentials(text or "")
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text
