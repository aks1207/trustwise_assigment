"""The loader covers every rule in the policy and rejects policies the engine cannot act on."""

import hashlib

import pytest
import yaml

from policy_engine.policy import PolicyError, parse_policy
from tests.conftest import POLICY_PATH

EXPECTED_RULES = {
    "no_pii": ({"input": "redact", "output": "block"}, 1.0, ("input", "output")),
    "no_jailbreak": ({"input": "block"}, 1.0, ("input",)),
    "no_prompt_injection": ({"input": "block"}, 1.0, ("input",)),
    "no_prompt_extraction": ({"input": "block"}, 1.0, ("input",)),
    "no_toxic_language": ({"input": "block", "output": "block"}, 0.8, ("input", "output")),
    "no_unsafe_content": ({"input": "block", "output": "block"}, 0.8, ("input", "output")),
}


def test_loads_every_rule(policy):
    assert policy.name == "content_safety_policy"
    assert policy.evaluation_strategy == "weighted_threshold" and policy.threshold == 0.5
    assert {r.id: (r.on_fail, r.weight, r.applies_to) for r in policy.rules} == EXPECTED_RULES
    assert policy.sha256 == hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()
    jailbreak = next(r for r in policy.rules if r.id == "no_jailbreak")
    assert "DAN" in jailbreak.judge_prompt and "PASS when" in jailbreak.judge_prompt


def mutate(change):
    raw = yaml.safe_load(POLICY_PATH.read_bytes())
    change(raw)
    return yaml.safe_dump(raw).encode()


@pytest.mark.parametrize("change,match", [
    (lambda p: p.pop("threshold"), "threshold"),
    (lambda p: p.update(evaluation_strategy="majority"), "evaluation_strategy"),
    (lambda p: p.update(threshold=1.5), "threshold"),
    (lambda p: p.update(default_action="block"), "default_action"),
    (lambda p: p["rules"][0].update(weight=0), "weight"),
    (lambda p: p["rules"][0].update(weight=True), "weight"),
    (lambda p: p["rules"][0].update(on_fail="quarantine"), "on_fail"),
    (lambda p: p["rules"][1].update(on_fail="warn"), "on_fail"),                                  # no WARN outcome
    (lambda p: p.update(verdict_order=["ALLOW", "WARN", "REDACT", "REVIEW", "BLOCK"]), "verdict_order"),
    (lambda p: p["rules"][0].update(on_fail={"input": "redact"}), "exactly"),                     # output missing
    (lambda p: p["rules"][1].update(on_fail={"input": "block", "output": "block"}), "exactly"),   # rule has no output
    (lambda p: p["rules"][0].update(on_fail={"input": "redact", "output": "erase"}), "on_fail"),
    (lambda p: p["rules"][0].update(on_fail=["block"]), "on_fail"),
    (lambda p: p["rules"][0].update(applies_to=["system"]), "applies_to"),
    (lambda p: p["rules"][0].pop("judge_prompt"), "judge_prompt"),
    (lambda p: p["rules"].append(dict(p["rules"][0])), "duplicate"),
    (lambda p: p.update(judge_user_template="no placeholders"), "placeholders"),
    (lambda p: p["engine"].update(on_judge_error="fail_open"), "fail_closed"),
    (lambda p: p.update(rules=[r for r in p["rules"] if "output" not in r["applies_to"]]), "output"),
])
def test_rejects_bad_policy(change, match):
    with pytest.raises(PolicyError, match=match):
        parse_policy(mutate(change))


def test_single_action_applies_to_every_mode():
    raw = yaml.safe_load(POLICY_PATH.read_bytes())
    raw["rules"][0]["on_fail"] = "block"
    p = parse_policy(yaml.safe_dump(raw).encode())
    assert p.rules[0].on_fail == {"input": "block", "output": "block"}


def test_overrides_for_threshold_sweeps(policy):
    p = policy.with_overrides(threshold=0.4)
    assert p.threshold == 0.4 and p.sha256 == policy.sha256
    with pytest.raises(PolicyError):
        policy.with_overrides(threshold=0)
    with pytest.raises(PolicyError):
        policy.with_overrides(rules=())
