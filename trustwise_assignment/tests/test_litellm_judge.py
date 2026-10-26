"""The live adapter's request shape and error mapping, against an in-process mock transport."""

import asyncio
import json

import httpx
import pytest

from policy_engine import JudgeCallError, ScriptedJudge
from policy_engine.judge import JudgeRequest, verdict
from policy_engine.constants import VERDICT_RESPONSE_FORMAT
from policy_engine.litellm_judge import LiteLLMJudge
from tests.conftest import SCHEMAS

REQ = JudgeRequest("call-1", "no_pii", 1, "SYSTEM", "USER", 0.0, 300, 20.0)


def judge_with(handler, preset="luna", **kwargs):
    return LiteLLMJudge(preset, api_key="test-key", url="http://proxy.test/v1/chat/completions",
                        user="tester", transport=httpx.MockTransport(handler), **kwargs)


def ok_handler(seen):
    def handler(request):
        seen.append(request)
        body = {"id": "chatcmpl-1", "model": "gpt-6-luna",
                "choices": [{"message": {"content": verdict("PASS", 0.9)}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 20}}
        return httpx.Response(200, json=body, headers={"x-litellm-call-id": "lt-1", "x-litellm-response-cost": "0.00012"})
    return handler


def test_luna_request_disables_reasoning_and_maps_usage():
    seen = []
    reply = asyncio.run(judge_with(ok_handler(seen))(REQ))
    body = json.loads(seen[0].content)
    assert body["model"] == "test_openai/responses/gpt-6-luna"
    assert body["reasoning_effort"] == "none" and "thinking" not in body
    assert body["messages"] == [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "USER"}]
    assert body["temperature"] == 0.0 and body["max_tokens"] == 300 and body["user"] == "tester"
    assert body["metadata"]["engine_call_id"] == "call-1"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert reply.raw_text == verdict("PASS", 0.9)
    assert (reply.input_tokens, reply.output_tokens, reply.cost_usd) == (120, 20, 0.00012)
    assert reply.provider_request_id == "lt-1" and reply.pricing_version


def test_haiku_request_disables_thinking():
    seen = []
    asyncio.run(judge_with(ok_handler(seen), preset="haiku")(REQ))
    body = json.loads(seen[0].content)
    assert body["model"] == "test_anthropic/claude-haiku-5-5"
    assert body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body


def test_missing_cost_is_null_not_zero():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    reply = asyncio.run(judge_with(handler)(REQ))
    assert reply.cost_usd is None and reply.pricing_version is None and reply.input_tokens is None


@pytest.mark.parametrize("status,kind", [(429, "rate_limit"), (500, "server_error"), (503, "server_error"),
                                         (408, "timeout"), (400, "client_error"), (401, "client_error")])
def test_http_errors_map_to_kinds(status, kind):
    def handler(request):
        return httpx.Response(status, text="nope", headers={"x-litellm-call-id": "lt-9"})
    with pytest.raises(JudgeCallError) as exc:
        asyncio.run(judge_with(handler)(REQ))
    assert exc.value.kind == kind and exc.value.provider_request_id == "lt-9"


def test_transport_and_timeout_errors():
    def timeout(request):
        raise httpx.ReadTimeout("slow")
    def refused(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(JudgeCallError) as exc:
        asyncio.run(judge_with(timeout)(REQ))
    assert exc.value.kind == "timeout"
    with pytest.raises(JudgeCallError) as exc:
        asyncio.run(judge_with(refused)(REQ))
    assert exc.value.kind == "transport_error"


def test_malformed_proxy_body_is_server_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})
    with pytest.raises(JudgeCallError) as exc:
        asyncio.run(judge_with(handler)(REQ))
    assert exc.value.kind == "server_error"


def test_requires_api_key(monkeypatch):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    with pytest.raises(ValueError):
        LiteLLMJudge("luna")


def test_live_adapter_through_engine(make_engine, evaluate):
    seen = []
    out = evaluate(make_engine(judge_with(ok_handler(seen))), "hello", mode="output")
    assert out.result["execution_kind"] == "live"     # adapter is live even when the transport is mocked here
    assert out.result["totals"] == {"attempt_count": 3, "input_tokens": 360, "output_tokens": 60, "cost_usd": 0.00036}
    assert len(seen) == 3


# ------------------------------------------------------------------ structured output
@pytest.mark.parametrize("preset", ["luna", "haiku"])
def test_structured_output_sent_by_default(preset):
    seen = []
    judge = judge_with(ok_handler(seen), preset=preset)
    asyncio.run(judge(REQ))
    body = json.loads(seen[0].content)
    assert body["response_format"] == VERDICT_RESPONSE_FORMAT
    assert judge.params()["response_format"] == VERDICT_RESPONSE_FORMAT     # recorded in the audit


def test_structured_output_can_be_turned_off():
    seen = []
    judge = judge_with(ok_handler(seen), structured_output=False)
    asyncio.run(judge(REQ))
    assert "response_format" not in json.loads(seen[0].content)
    assert judge.params()["response_format"] is None


def test_response_format_matches_judge_schema():
    contract = json.loads((SCHEMAS / "judge_response.schema.json").read_text())
    sent = VERDICT_RESPONSE_FORMAT["json_schema"]["schema"]
    assert VERDICT_RESPONSE_FORMAT["json_schema"]["strict"] is True
    assert set(sent["properties"]) == set(contract["properties"])
    assert sorted(sent["required"]) == sorted(contract["required"])
    assert sent["additionalProperties"] is False and contract["additionalProperties"] is False
    assert sent["properties"]["verdict"]["enum"] == contract["properties"]["verdict"]["enum"]
    for key in ("minimum", "maximum"):
        assert sent["properties"]["confidence"][key] == contract["properties"]["confidence"][key]


def test_out_of_range_confidence_still_rejected_with_structured_output(make_engine, evaluate):
    # Haiku ignores the schema's bounds live; the parser must still catch it and fail closed.
    def handler(request):
        body = {"choices": [{"message": {"content": '{"verdict": "FAIL", "confidence": 7, "reasoning": "x"}'}}]}
        return httpx.Response(200, json=body)
    out = evaluate(make_engine(judge_with(handler, preset="haiku")), "x", mode="output")
    assert out.result["final_verdict"] == "REVIEW"
    assert {r["error"]["type"] for r in out.result["rule_results"]} == {"unparseable"}
    call = out.audit["rules"][0]["calls"][0]
    assert call["parse_status"] == "invalid_values"
    assert out.audit["rules"][0]["judge_params"]["response_format"] == VERDICT_RESPONSE_FORMAT
