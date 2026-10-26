"""LiteLLM proxy settings for the live judges. Single source used by litellm_judge.py.

Reasoning is off for both judges, using the switch that actually disables it:
  luna   reasoning_effort "none"
  haiku  thinking {"type": "disabled"}
"""

from __future__ import annotations

from typing import Any

DEFAULT_URL = "http://localhost:4000"

ENV_API_KEY = "LITELLM_API_KEY"
ENV_BASE_URL = "LITELLM_BASE_URL"
ENV_USER = "LITELLM_USER"

LUNA_MODEL = "test_openai/responses/gpt-6-luna"
HAIKU_MODEL = "test_anthropic/claude-haiku-5-5"

LUNA_REASONING_OFF: dict[str, Any] = {"reasoning_effort": "none"}
HAIKU_REASONING_OFF: dict[str, Any] = {"thinking": {"type": "disabled"}}

# Structured output for judge replies. Mirrors schemas/judge_response.schema.json
# (tests/test_litellm_judge.py checks they agree).
VERDICT_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "judge_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": ["PASS", "FAIL", "UNCERTAIN"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reasoning": {"type": "string"},
            },
            "required": ["verdict", "confidence", "reasoning"],
        },
    },
}
