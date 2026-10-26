"""Retry path, timeout path, and fail-closed behaviour when the judge never answers usefully."""

import dataclasses

import pytest

from policy_engine import JudgeCallError, ScriptedJudge
from policy_engine.judge import Delay, verdict


def rule(result, rule_id):
    return next(r for r in result["rule_results"] if r["rule_id"] == rule_id)


def audit_calls(audit, rule_id):
    return next(r for r in audit["rules"] if r["rule_id"] == rule_id)["calls"]


def fast_policy(policy, timeout_s=0.05, **judge_changes):
    return dataclasses.replace(policy, judge=dataclasses.replace(policy.judge, timeout_s=timeout_s, **judge_changes))


# ------------------------------------------------------------------ retry
def test_server_error_then_success(make_engine, evaluate):
    judge = ScriptedJudge({"no_pii": [JudgeCallError("server_error", "HTTP 503"), verdict("FAIL", 0.9)]})
    engine = make_engine(judge)
    out = evaluate(engine, "call me on 07700 900123")
    pii = rule(out.result, "no_pii")
    assert pii["verdict"] == "FAIL" and pii["attempts"] == 2 and pii["error"] is None
    calls = audit_calls(out.audit, "no_pii")
    assert [c["parse_status"] for c in calls] == ["no_response", "ok"]
    assert calls[0]["error"] == {"type": "server_error", "message": "HTTP 503"}
    assert calls[0]["raw_response"] is None
    assert engine._sleep.calls == [0.5]                       # backoff from the policy
    assert out.result["final_verdict"] == "REDACT"
    assert out.result["totals"]["attempt_count"] == 7         # 6 rules + 1 retry


def test_unparseable_then_success_sends_repair_note(make_engine, evaluate):
    judge = ScriptedJudge({"no_toxic_language": ["Sure! The verdict is PASS.", verdict("PASS", 0.9)]})
    out = evaluate(make_engine(judge), "hello")
    calls = audit_calls(out.audit, "no_toxic_language")
    assert [c["parse_status"] for c in calls] == ["invalid_json", "ok"]
    assert calls[0]["raw_response"] == "Sure! The verdict is PASS."   # kept exactly as received
    assert calls[0]["error"]["type"] == "unparseable"
    assert "could not be used" not in calls[0]["user_prompt"]
    assert "could not be used" in calls[1]["user_prompt"]
    assert out.result["final_verdict"] == "ALLOW"


def test_retries_exhausted_fails_closed(make_engine, evaluate, policy):
    bad = '{"verdict": "PASS"}'                                       # missing confidence and reasoning
    judge = ScriptedJudge({"no_pii": [bad, bad, bad]})
    engine = make_engine(judge)
    out = evaluate(engine, "hello", mode="output")
    pii = rule(out.result, "no_pii")
    assert pii["attempts"] == policy.judge.max_retries + 1 == 3
    assert pii["verdict"] == "UNCERTAIN" and pii["confidence"] == 0.0 and pii["action"] == "review"
    assert pii["error"]["type"] == "unparseable"
    assert out.result["final_verdict"] == "REVIEW"            # never ALLOW on an unjudged rule
    assert out.result["review_reasons"] == ["judge_error"]
    assert engine._sleep.calls == [0.5, 2.0]
    assert [c["parse_status"] for c in audit_calls(out.audit, "no_pii")] == ["missing_fields"] * 3


def test_client_error_is_not_retried(make_engine, evaluate):
    judge = ScriptedJudge({"no_unsafe_content": [JudgeCallError("client_error", "HTTP 400: content filter")]})
    engine = make_engine(judge)
    out = evaluate(engine, "x", mode="output")
    r = rule(out.result, "no_unsafe_content")
    assert r["attempts"] == 1 and r["error"]["type"] == "client_error"
    assert engine._sleep.calls == []
    assert out.result["final_verdict"] == "REVIEW"


def test_rate_limit_is_retried(make_engine, evaluate):
    judge = ScriptedJudge({"no_pii": [JudgeCallError("rate_limit", "HTTP 429")] * 2 + [verdict("PASS", 0.9)]})
    out = evaluate(make_engine(judge), "x", mode="output")
    assert rule(out.result, "no_pii")["attempts"] == 3
    assert out.result["final_verdict"] == "ALLOW"


def test_unexpected_judge_exception_fails_closed(make_engine, evaluate):
    class Broken(ScriptedJudge):
        async def __call__(self, request):
            raise KeyError("bug in adapter")
    out = evaluate(make_engine(Broken()), "x", mode="output")
    assert out.result["final_verdict"] == "REVIEW"
    assert {r["error"]["type"] for r in out.result["rule_results"]} == {"transport_error"}


def test_errored_block_is_not_downgraded(make_engine, evaluate):
    # A confident FAIL on one rule still blocks while a sibling's judge is down.
    judge = ScriptedJudge({"no_prompt_injection": [verdict("FAIL", 0.95)],
                           "no_pii": [JudgeCallError("server_error", "down")] * 3})
    out = evaluate(make_engine(judge), "x")
    assert out.result["final_verdict"] == "BLOCK"
    assert out.result["uncertain_rules"] == ["no_pii"]


# ------------------------------------------------------------------ timeout
def test_timeout_then_success(make_engine, evaluate, policy):
    judge = ScriptedJudge({"no_pii": [Delay(1.0), verdict("PASS", 0.9)]})
    out = evaluate(make_engine(judge, policy_override=fast_policy(policy)), "x", mode="output")
    calls = audit_calls(out.audit, "no_pii")
    assert calls[0]["error"]["type"] == "timeout" and calls[0]["parse_status"] == "no_response"
    assert calls[1]["parse_status"] == "ok"
    assert out.result["final_verdict"] == "ALLOW"


def test_timeout_every_attempt_goes_to_review(make_engine, evaluate, policy):
    judge = ScriptedJudge({"no_pii": [Delay(1.0)] * 3})
    out = evaluate(make_engine(judge, policy_override=fast_policy(policy)), "x", mode="output")
    pii = rule(out.result, "no_pii")
    assert pii["attempts"] == 3 and pii["error"]["type"] == "timeout"
    assert out.result["final_verdict"] == "REVIEW"
    assert out.result["review_reasons"] == ["judge_error"]
    # the slow rule did not stop the others from finishing
    assert all(r["verdict"] == "PASS" for r in out.result["rule_results"] if r["rule_id"] != "no_pii")


def test_evaluation_deadline_stops_retries(make_engine, evaluate, policy):
    p = dataclasses.replace(fast_policy(policy, timeout_s=5.0), evaluation_deadline_s=0.05)
    judge = ScriptedJudge({"no_pii": [Delay(1.0)] * 3})
    out = evaluate(make_engine(judge, policy_override=p), "x", mode="output")
    pii = rule(out.result, "no_pii")
    assert pii["error"]["type"] == "evaluation_deadline"
    assert pii["attempts"] == 1                               # a spent deadline is not retried
    assert out.result["final_verdict"] == "REVIEW"
