"""Live judge that calls a model through the LiteLLM proxy (OpenAI-compatible chat completions).

Model ids, reasoning switches and the response format come from constants.py. Reasoning is off for both:
  luna   reasoning_effort "none"
  haiku  thinking {"type": "disabled"}

Structured output is on by default: every call sends VERDICT_RESPONSE_FORMAT, a strict JSON schema
with the three judge keys. Checked live on 2026-10-25: both models then return exactly those keys and a
valid verdict word. Luna also respects confidence's 0..1 bounds; Haiku accepts the bounds but ignores them
(it returned confidence 7 when told to). So the schema guarantees shape, not values, and parsing.py stays
the authority on what a valid reply is.

Config comes from the environment: LITELLM_API_KEY (required), LITELLM_BASE_URL, LITELLM_USER.
Tests never construct this against the network; they pass an httpx.MockTransport.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from .constants import (
    DEFAULT_URL,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_USER,
    HAIKU_MODEL,
    HAIKU_REASONING_OFF,
    LUNA_MODEL,
    LUNA_REASONING_OFF,
    VERDICT_RESPONSE_FORMAT,
)
from .judge import JudgeCallError, JudgeReply, JudgeRequest


@dataclass(frozen=True)
class ModelPreset:
    name: str                 # short name used in run ids and reports
    model: str                # LiteLLM model id
    provider: str
    extra_body: dict[str, Any] = field(default_factory=dict)
    send_temperature: bool = True


PRESETS = {
    "luna": ModelPreset("luna", LUNA_MODEL, "litellm/openai", LUNA_REASONING_OFF),
    "haiku": ModelPreset("haiku", HAIKU_MODEL, "litellm/anthropic", HAIKU_REASONING_OFF),
}


class LiteLLMJudge:
    execution_kind = "live"

    def __init__(
        self,
        preset: ModelPreset | str,
        *,
        api_key: str | None = None,
        url: str | None = None,
        user: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        tags: tuple[str, ...] = ("feature:trustwise-policy-engine",),
        structured_output: bool = True,
    ):
        self.preset = PRESETS[preset] if isinstance(preset, str) else preset
        self.model = self.preset.model
        self.provider = self.preset.provider
        self._api_key = api_key or os.environ.get(ENV_API_KEY)
        if not self._api_key:
            raise ValueError(f"{ENV_API_KEY} is not set")
        self._url = url or os.environ.get(ENV_BASE_URL, DEFAULT_URL)
        self._user = user or os.environ.get(ENV_USER)
        self._tags = list(tags)
        self.structured_output = structured_output
        self._client = httpx.AsyncClient(transport=transport)

    def params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"preset": self.preset.name, **self.preset.extra_body}
        params["response_format"] = VERDICT_RESPONSE_FORMAT if self.structured_output else None
        return params

    def build_body(self, request: JudgeRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": request.max_output_tokens,
            "metadata": {"tags": self._tags, "engine_call_id": request.call_id, "rule_id": request.rule_id},
            **self.preset.extra_body,
        }
        if self.preset.send_temperature:
            body["temperature"] = request.temperature
        if self._user:
            body["user"] = self._user
        if self.structured_output:
            body["response_format"] = VERDICT_RESPONSE_FORMAT
        return body

    async def __call__(self, request: JudgeRequest) -> JudgeReply:
        try:
            resp = await self._client.post(
                self._url,
                json=self.build_body(request),
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=request.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise JudgeCallError("timeout", f"{type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            raise JudgeCallError("transport_error", f"{type(exc).__name__}: {exc}") from exc

        req_id = resp.headers.get("x-litellm-call-id")
        if resp.status_code != 200:
            detail = resp.text[:300]
            if resp.status_code == 429:
                kind = "rate_limit"
            elif resp.status_code >= 500:
                kind = "server_error"
            elif resp.status_code == 408:
                kind = "timeout"
            else:
                kind = "client_error"
            raise JudgeCallError(kind, f"HTTP {resp.status_code}: {detail}", provider_request_id=req_id)

        try:
            payload = resp.json()
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise JudgeCallError("server_error", f"malformed proxy response: {type(exc).__name__}",
                                 provider_request_id=req_id) from exc
        usage = payload.get("usage") or {}
        cost_header = resp.headers.get("x-litellm-response-cost")
        try:
            cost = float(cost_header) if cost_header not in (None, "", "None") else None
        except ValueError:
            cost = None
        return JudgeReply(
            raw_text=message.get("content") or "",
            provider=self.provider,
            provider_request_id=req_id or payload.get("id"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cost_usd=cost,
            pricing_version="litellm-proxy-response-cost" if cost is not None else None,
            served_model=payload.get("model"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
