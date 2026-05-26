"""
tools/reflection.py — Reflection Pattern (Critic LLM Pass)
===========================================================
After the agent produces a response, this module runs a lightweight
"critic" LLM call to assess quality and optionally improve the answer.

Strategy:
  - Score the response on 3 axes: completeness, accuracy, actionability
  - If score < threshold, request an improved version from the LLM
  - If score >= threshold, return the original response unchanged

Usage:
    from tools.reflection import reflect
    final_reply = reflect(user_msg, agent_reply, provider, model)

The reflect() call adds ~0.5–1.5s latency. It is always enabled by default
but can be disabled via REFLECTION_ENABLED=false in .env.
"""

from __future__ import annotations
import os

# Minimum quality score (0–10) below which reflection triggers a rewrite
REFLECTION_THRESHOLD = float(os.getenv("REFLECTION_THRESHOLD", "5"))
REFLECTION_ENABLED   = os.getenv("REFLECTION_ENABLED", "true").lower() not in ("false", "0", "no")

_CRITIC_PROMPT = """\
You are a strict quality reviewer for a Power Platform AI assistant.

Evaluate the AGENT RESPONSE below against the USER QUESTION.
Score it from 0–10 on these three axes:
  1. Completeness  — Does it fully answer what was asked?
  2. Accuracy      — Are Power Platform facts, names, and concepts correct?
  3. Actionability — Does the user have a clear next step?

If ALL three scores are >= 6, reply with:
  PASS

If any score is below 6, reply with an improved version of the response.
Start the improved version with: IMPROVED:
(Then write the complete improved response, not just the parts to change.)

Keep the same markdown formatting style as the original.
Do NOT explain your scoring — just PASS or IMPROVED:<improved text>.

---
USER QUESTION:
{user_question}

AGENT RESPONSE:
{agent_response}
"""


def reflect(
    user_message:   str,
    agent_response: str,
    provider:       str | None = None,
    model:          str | None = None,
) -> str:
    """
    Run the reflection/critic pass on an agent response.

    Returns:
        The original response if it passes, or an improved version.
        Falls back to the original on any error.
    """
    if not REFLECTION_ENABLED:
        return agent_response

    if not agent_response or not agent_response.strip():
        return agent_response

    # Skip reflection for very short responses (greetings, one-liners)
    if len(agent_response.strip()) < 80:
        return agent_response

    try:
        from tools.llm_provider import call_llm, detect_provider, get_active_model

        _provider = provider or detect_provider()
        _model    = model    or get_active_model(_provider)

        critic_msg = _CRITIC_PROMPT.format(
            user_question  = user_message[:500],
            agent_response = agent_response[:3000],
        )

        result = call_llm(
            messages=[{"role": "user", "content": critic_msg}],
            provider    = _provider,
            model       = _model,
            temperature = 0.2,
            max_tokens  = 2000,
        )

        critic_output = (result.get("content") or "").strip()

        if critic_output.startswith("PASS"):
            return agent_response

        if critic_output.startswith("IMPROVED:"):
            improved = critic_output[len("IMPROVED:"):].strip()
            if improved and len(improved) > 50:
                return improved

        # Any other output → return original (don't trust unexpected formats)
        return agent_response

    except Exception:
        # Reflection is never allowed to break the main response
        return agent_response


def reflection_enabled() -> bool:
    """Return whether reflection is currently enabled."""
    return REFLECTION_ENABLED
