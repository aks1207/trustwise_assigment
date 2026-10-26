"""Judge interface and a scripted judge for tests.

A judge sends one prompt pair to a model and returns the raw text. It does not parse. Parsing,
retries, timeouts and the audit trail are the engine's job, so every judge (live or mock)
gets exactly the same handling.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Errors a judge may raise. `unparseable` and `evaluation_deadline` are added by the engine.
CALL_ERROR_KINDS = ("timeout", "rate_limit", "server_error", "transport_error", "client_error")
RETRYABLE_KINDS = frozenset({"timeout", "rate_limit", "server_error", "transport_error", "unparseable"})


@dataclass(frozen=True)
class JudgeRequest:
    call_id: str
    rule_id: str
    attempt: int
    system_prompt: str
    user_prompt: str
    temperature: float
    max_output_tokens: int
    timeout_s: float


@dataclass(frozen=True)
class JudgeReply:
    raw_text: str
    provider: str
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    pricing_version: str | None = None
    served_model: str | None = None


class JudgeCallError(Exception):
    """The call produced no model text. `kind` decides whether the engine retries."""

    def __init__(self, kind: str, message: str, *, provider_request_id: str | None = None):
        if kind not in CALL_ERROR_KINDS:
            raise ValueError(f"unknown judge error kind {kind!r}")
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.provider_request_id = provider_request_id


@runtime_checkable
class Judge(Protocol):
    model: str
    provider: str
    execution_kind: str  # "live" | "mock"

    def params(self) -> dict[str, Any]:
        """Model parameters as sent, for the audit trail."""

    async def __call__(self, request: JudgeRequest) -> JudgeReply: ...


# ------------------------------------------------------------------ scripted judge (tests, demos)
@dataclass
class Delay:
    """Sleep before producing `then`. Used to exercise the timeout path."""
    seconds: float
    then: Any = None


def verdict(v: str, confidence: float = 0.9, reasoning: str = "scripted") -> str:
    return json.dumps({"verdict": v, "confidence": confidence, "reasoning": reasoning})


@dataclass
class ScriptedJudge:
    """Returns pre-set replies per rule, in order. No network.

    script: rule_id -> list of steps. A step is a raw string, a dict (sent as JSON),
    a JudgeCallError (raised), or a Delay. When a rule's steps run out, `default` is used.
    """

    script: dict[str, list[Any]] = field(default_factory=dict)
    default: Any = field(default_factory=lambda: verdict("PASS", 0.95, "No violation."))
    model: str = "scripted-judge"
    provider: str = "scripted"
    execution_kind: str = "mock"
    requests: list[JudgeRequest] = field(default_factory=list)
    max_concurrency: int = 0
    _cursor: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _in_flight: int = 0

    def params(self) -> dict[str, Any]:
        return {"scripted": True}

    async def __call__(self, request: JudgeRequest) -> JudgeReply:
        self.requests.append(request)
        self._in_flight += 1
        self.max_concurrency = max(self.max_concurrency, self._in_flight)
        try:
            steps = self.script.get(request.rule_id, [])
            i = self._cursor[request.rule_id]
            self._cursor[request.rule_id] += 1
            step = steps[i] if i < len(steps) else self.default
            # yield once so parallel rules overlap, as a real network call would
            await asyncio.sleep(0)
            return await self._play(step, request)
        finally:
            self._in_flight -= 1

    async def _play(self, step: Any, request: JudgeRequest) -> JudgeReply:
        if isinstance(step, Delay):
            await asyncio.sleep(step.seconds)
            return await self._play(step.then if step.then is not None else self.default, request)
        if isinstance(step, JudgeCallError):
            raise step
        text = json.dumps(step) if isinstance(step, dict) else str(step)
        return JudgeReply(
            raw_text=text,
            provider=self.provider,
            provider_request_id=f"scripted-{request.rule_id}-{request.attempt}",
            input_tokens=len(request.system_prompt + request.user_prompt) // 4,
            output_tokens=len(text) // 4,
            cost_usd=None,
            pricing_version=None,
            served_model=self.model,
        )
