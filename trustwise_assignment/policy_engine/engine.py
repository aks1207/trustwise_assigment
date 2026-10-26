"""Evaluate one piece of content against a policy.

Flow: pick the rules that apply to the mode, run one judge call per rule in parallel, parse and retry
each, combine with `decision.decide`, and return the safe result plus the full audit record.

Everything that varies between runs (ids, nonces, clocks, sleeps) is injectable, so a test with a
scripted judge gets byte-identical output apart from the fields it chooses to randomise.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .decision import RuleOutcome, decide, risk_contribution
from .judge import RETRYABLE_KINDS, Judge, JudgeCallError, JudgeReply, JudgeRequest
from .parsing import parse_judge_response
from .policy import MODES, Policy, Rule

SCHEMA_VERSION = "1.1"
ENGINE_VERSION = "0.2.0"
REPAIR_NOTE = (
    "\n\nYour previous reply could not be used ({reason}). "
    'Reply with only one JSON object: {{"verdict": "PASS" | "FAIL" | "UNCERTAIN", '
    '"confidence": <number 0-1>, "reasoning": "<text>"}}'
)
_PLACEHOLDER = re.compile(r"\{(rule_id|description|mode|mode_note|nonce|content)\}")


@dataclass(frozen=True)
class EvaluationOutcome:
    result: dict[str, Any]   # safe to log: no content, no prompts
    audit: dict[str, Any]    # restricted: content, exact prompts, raw replies


def content_sha256(mode: str, text: str) -> str:
    """Hash of mode + exact text. Same preimage as the schemas document."""
    payload = json.dumps({"mode": mode, "text": text}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render(template: str, values: dict[str, str]) -> str:
    """Single-pass substitution. Text inserted for {content} is never re-scanned, so content
    that contains '{nonce}' or '{rule_id}' stays literal."""
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), template)


def escape_content(text: str, escaping: str) -> str:
    """What the judge sees in place of {content}. "html" escapes & < > so nothing in the content can look like a
    tag, including a fake closing tag for the content block. The audit keeps the original text."""
    return html.escape(text, quote=False) if escaping == "html" else text


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Engine:
    def __init__(
        self,
        policy: Policy,
        judge: Judge,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(8),
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        clock: Callable[[], datetime] = _utcnow,
        timer: Callable[[], float] = time.perf_counter,
        max_parallel_rules: int | None = None,
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.policy = policy
        self.judge = judge
        self._sleep = sleep
        self._nonce = nonce_factory
        self._new_id = id_factory
        self._clock = clock
        self._timer = timer
        self._limit = asyncio.Semaphore(max_parallel_rules) if max_parallel_rules else None
        self._audit_sink = audit_sink

    # ------------------------------------------------------------------ public API
    def evaluate_sync(self, text: str, mode: str) -> EvaluationOutcome:
        return asyncio.run(self.evaluate(text, mode))

    async def evaluate(self, text: str, mode: str) -> EvaluationOutcome:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        if not isinstance(text, str):
            raise TypeError("text must be a string")

        p = self.policy
        request_id = self._new_id()
        started_at = self._clock()
        t0 = self._timer()
        deadline = t0 + p.evaluation_deadline_s if p.evaluation_deadline_s else None

        truncated = len(text) > p.max_content_chars
        judged_text = text[: p.max_content_chars] if truncated else text

        applicable = p.rules_for(mode)
        skipped = [{"rule_id": r.id, "reason": "not_applicable_to_mode"} for r in p.rules if r not in applicable]
        # gather keeps input order, so results are in policy order however the calls finish
        runs = await asyncio.gather(*(self._run_rule(r, mode, judged_text, deadline) for r in applicable))

        decision = decide(p, [run.outcome for run in runs], truncated, mode)
        calls = [c for run in runs for c in run.audit["calls"]]
        weighted = p.evaluation_strategy == "weighted_threshold"
        result = {
            "schema_version": SCHEMA_VERSION,
            "execution_kind": self.judge.execution_kind,
            "request_id": request_id,
            "policy_name": p.name,
            "policy_version": p.version,
            "policy_sha256": p.sha256,
            "mode": mode,
            "evaluated_at": iso(started_at),
            "evaluation_strategy": p.evaluation_strategy,
            "threshold": p.threshold if weighted else None,
            "score": decision.score,
            "final_verdict": decision.final_verdict,
            "decision_reason": decision.decision_reason,
            "triggered_rules": decision.triggered_rules,
            "uncertain_rules": decision.uncertain_rules,
            "review_reasons": decision.review_reasons,
            "rule_results": [run.result for run in runs],
            "skipped_rules": skipped,
            "content": {"sha256": content_sha256(mode, text), "chars": len(text), "truncated": truncated},
            "totals": _totals(calls),
            "total_latency_ms": _ms(self._timer() - t0),
        }
        audit = {
            "schema_version": SCHEMA_VERSION,
            "execution_kind": self.judge.execution_kind,
            "request_id": request_id,
            "engine_version": ENGINE_VERSION,
            "policy": {"name": p.name, "version": p.version, "sha256": p.sha256},
            "mode": mode,
            "content": {
                "text": text,
                "sha256": result["content"]["sha256"],
                "truncated_text": judged_text if truncated else None,
            },
            "started_at": iso(started_at),
            "completed_at": iso(self._clock()),
            "rules": [run.audit for run in runs],
            "result": result,
        }
        # An evaluation that cannot be audited is not returned: a failing sink raises to the caller.
        if self._audit_sink is not None:
            self._audit_sink(audit)
        return EvaluationOutcome(result=result, audit=audit)

    # ------------------------------------------------------------------ one rule
    async def _run_rule(self, rule: Rule, mode: str, text: str, deadline: float | None) -> "_RuleRun":
        if self._limit is None:
            return await self._run_rule_inner(rule, mode, text, deadline)
        async with self._limit:
            return await self._run_rule_inner(rule, mode, text, deadline)

    async def _run_rule_inner(self, rule: Rule, mode: str, text: str, deadline: float | None) -> "_RuleRun":
        p, js = self.policy, self.policy.judge
        calls: list[dict[str, Any]] = []
        parsed: dict[str, Any] | None = None
        last_error: dict[str, str] | None = None
        repair_reason: str | None = None
        t_rule = self._timer()

        for attempt in range(1, js.max_retries + 2):
            nonce = self._fresh_nonce(text)
            system_prompt = render(p.judge_system_prompt, {"nonce": nonce}) + "\n\nRULE\n" + rule.judge_prompt
            user_prompt = render(p.judge_user_template, {
                "rule_id": rule.id,
                "description": rule.description,
                "mode": mode,
                "mode_note": p.mode_notes[mode],
                "nonce": nonce,
                "content": escape_content(text, p.content_escaping),
            })
            if repair_reason:
                user_prompt += REPAIR_NOTE.format(reason=repair_reason)

            call_id = self._new_id()
            started = self._clock()
            t_call = self._timer()
            timeout = js.timeout_s
            limited_by_deadline = False
            if deadline is not None:
                remaining = deadline - t_call
                if remaining <= 0:
                    last_error = {"type": "evaluation_deadline", "message": "evaluation deadline passed before dispatch"}
                    calls.append(_call_record(call_id, attempt, self.judge.provider, None, started, started, 0,
                                              system_prompt, user_prompt, None, "no_response", None, last_error, None))
                    break
                if remaining < timeout:
                    timeout, limited_by_deadline = remaining, True

            request = JudgeRequest(call_id, rule.id, attempt, system_prompt, user_prompt,
                                   js.temperature, js.max_output_tokens, timeout)
            reply: JudgeReply | None = None
            provider_request_id: str | None = None
            try:
                reply = await asyncio.wait_for(self.judge(request), timeout)
                provider_request_id = reply.provider_request_id
            except asyncio.TimeoutError:
                kind = "evaluation_deadline" if limited_by_deadline else "timeout"
                last_error = {"type": kind, "message": f"no judge response within {timeout:g}s"}
            except JudgeCallError as exc:
                last_error = {"type": exc.kind, "message": exc.message}
                provider_request_id = exc.provider_request_id
            except Exception as exc:  # a broken judge adapter must fail closed, not crash the evaluation
                last_error = {"type": "transport_error", "message": f"{type(exc).__name__}: {exc}"}
            latency = _ms(self._timer() - t_call)
            completed = self._clock()

            if reply is None:
                calls.append(_call_record(call_id, attempt, self.judge.provider, provider_request_id, started,
                                          completed, latency, system_prompt, user_prompt, None, "no_response",
                                          None, last_error, None))
            else:
                pr = parse_judge_response(reply.raw_text)
                if pr.status == "ok":
                    parsed, last_error, repair_reason = pr.parsed, None, None
                else:
                    last_error = {"type": "unparseable", "message": f"{pr.status}: {pr.message}"}
                    repair_reason = pr.message
                calls.append(_call_record(call_id, attempt, reply.provider, provider_request_id, started,
                                          completed, latency, system_prompt, user_prompt, reply.raw_text,
                                          pr.status, pr.parsed, last_error, reply))
            if parsed is not None:
                break
            if last_error["type"] not in RETRYABLE_KINDS or attempt > js.max_retries:
                break
            await self._sleep(js.backoff(attempt))

        latency_ms = _ms(self._timer() - t_rule)
        if parsed is not None:
            outcome = RuleOutcome(rule.id, parsed["verdict"], parsed["confidence"])
            reasoning = parsed["reasoning"]
            action = {"FAIL": rule.action_for(mode), "PASS": "none", "UNCERTAIN": "review"}[parsed["verdict"]]
        else:
            outcome = RuleOutcome(rule.id, "UNCERTAIN", 0.0, errored=True)
            reasoning = f"No valid judge response after {len(calls)} attempt(s) ({last_error['type']})."
            action = "review"

        result = {
            "rule_id": rule.id,
            "verdict": outcome.verdict,
            "confidence": outcome.confidence,
            "reasoning": reasoning,
            "action": action,
            "weight": rule.weight,
            "risk_contribution": round(risk_contribution(rule.weight, outcome), 6),
            "judge_model": self.judge.model,
            "attempts": len(calls),
            "latency_ms": latency_ms,
            "error": None if parsed is not None else dict(last_error),
        }
        audit = {
            "rule_id": rule.id,
            "judge_model": self.judge.model,
            "judge_params": {
                **self.judge.params(),
                "temperature": js.temperature,
                "max_output_tokens": js.max_output_tokens,
                "timeout_s": js.timeout_s,
            },
            "calls": calls,
            "final_response": parsed,
            "final_error": None if parsed is not None else dict(last_error),
        }
        return _RuleRun(outcome, result, audit)

    def _fresh_nonce(self, text: str) -> str:
        # The content must not contain the delimiter it is wrapped in. Retry a few times, then refuse.
        for _ in range(5):
            nonce = self._nonce()
            if nonce not in text:
                return nonce
        raise RuntimeError("could not generate a nonce absent from the content")


@dataclass(frozen=True)
class _RuleRun:
    outcome: RuleOutcome
    result: dict[str, Any]
    audit: dict[str, Any]


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def _call_record(call_id, attempt, provider, provider_request_id, started, completed, latency,
                 system_prompt, user_prompt, raw, status, parsed, error, reply: JudgeReply | None) -> dict[str, Any]:
    cost = reply.cost_usd if reply else None
    return {
        "call_id": call_id,
        "attempt": attempt,
        "provider": provider,
        "provider_request_id": provider_request_id,
        "started_at": iso(started),
        "completed_at": iso(completed),
        "latency_ms": latency,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "raw_response": raw,
        "parse_status": status,
        "parsed": parsed,
        "error": None if error is None else dict(error),
        "usage": {
            "input_tokens": reply.input_tokens if reply else None,
            "output_tokens": reply.output_tokens if reply else None,
            "cost_usd": cost,
            "pricing_version": reply.pricing_version if (reply and cost is not None) else None,
        },
    }


def _totals(calls: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str):
        values = [c["usage"][key] for c in calls]
        if any(v is None for v in values):
            return None
        return round(sum(values), 8) if key == "cost_usd" else sum(values)

    return {
        "attempt_count": len(calls),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cost_usd": total("cost_usd"),
    }
