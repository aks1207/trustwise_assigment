"""Same policy + same mocked judge replies -> same verdict and matching audit trail."""

import json

from policy_engine import AuditLog, ScriptedJudge, strip_volatile
from policy_engine.judge import JudgeCallError, verdict

TEXT = "Book the flight under passport number X4471982."


def scripted():
    return ScriptedJudge({
        "no_pii": [JudgeCallError("server_error", "HTTP 502"), verdict("FAIL", 0.93, "A passport number.")],
        "no_jailbreak": [verdict("PASS", 0.98)],
        "no_toxic_language": ["not json", verdict("PASS", 0.9)],
    })


def test_same_inputs_same_verdict_and_audit(make_engine, evaluate):
    frozen_timer = lambda: 0.0   # latencies come from the timer; freeze it for a byte-level comparison
    a = evaluate(make_engine(scripted(), timer=frozen_timer), TEXT)
    b = evaluate(make_engine(scripted(), timer=frozen_timer), TEXT)
    assert a.result["final_verdict"] == b.result["final_verdict"] == "REDACT"
    # deterministic ids, nonces, clock and timer: byte-identical
    assert json.dumps(a.audit, sort_keys=True) == json.dumps(b.audit, sort_keys=True)


def test_matching_audit_with_real_ids_and_clocks(policy, evaluate):
    """Production factories: ids, nonces and timestamps differ, everything else matches."""
    from policy_engine import Engine
    from tests.conftest import SleepRecorder

    def run():
        return evaluate(Engine(policy, scripted(), sleep=SleepRecorder(), nonce_factory=lambda: "fixednonce"), TEXT)

    a, b = run(), run()
    assert a.result["request_id"] != b.result["request_id"]
    assert strip_volatile(a.audit) == strip_volatile(b.audit)
    assert strip_volatile(a.result) == strip_volatile(b.result)


def test_random_nonces_change_only_the_prompts(policy, evaluate):
    from policy_engine import Engine
    from tests.conftest import SleepRecorder

    a = evaluate(Engine(policy, scripted(), sleep=SleepRecorder()), TEXT)
    b = evaluate(Engine(policy, scripted(), sleep=SleepRecorder()), TEXT)
    assert strip_volatile(a.result) == strip_volatile(b.result)

    def without_prompts(audit):
        clean = strip_volatile(audit)
        for r in clean["rules"]:
            for c in r["calls"]:
                c.pop("system_prompt"), c.pop("user_prompt")
        return clean

    assert without_prompts(a.audit) == without_prompts(b.audit)


def test_audit_log_is_append_only(tmp_path, make_engine, evaluate):
    log = AuditLog(tmp_path / "audit" / "run.jsonl")
    evaluate(make_engine(scripted(), audit_sink=log), TEXT)
    evaluate(make_engine(scripted(), audit_sink=log), TEXT)
    records = log.read()
    assert len(records) == 2
    assert records[0]["policy"]["sha256"] == records[1]["policy"]["sha256"]
    assert strip_volatile(records[0]) == strip_volatile(records[1])


def test_audit_records_prompts_raw_replies_and_policy(make_engine, evaluate, policy):
    out = evaluate(make_engine(scripted()), TEXT)
    audit = out.audit
    assert audit["policy"] == {"name": policy.name, "version": policy.version, "sha256": policy.sha256}
    assert audit["request_id"] == out.result["request_id"]
    pii = next(r for r in audit["rules"] if r["rule_id"] == "no_pii")
    assert pii["calls"][1]["raw_response"] == verdict("FAIL", 0.93, "A passport number.")
    assert TEXT in pii["calls"][1]["user_prompt"]
    assert policy.judge_system_prompt.splitlines()[0] in pii["calls"][1]["system_prompt"]
    # the safe result carries no content
    assert TEXT not in json.dumps(out.result)


def test_failing_audit_sink_fails_the_evaluation(make_engine):
    import asyncio
    import pytest

    def broken(record):
        raise OSError("disk full")

    with pytest.raises(OSError):
        asyncio.run(make_engine(scripted(), audit_sink=broken).evaluate(TEXT, "input"))
