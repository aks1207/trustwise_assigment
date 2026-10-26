"""The engine's decide() agrees with the independent reference in tools/check_policy.py."""

import importlib.util
import random

import pytest

from policy_engine.decision import RuleOutcome, decide
from tests.conftest import ROOT


@pytest.fixture(scope="module")
def reference():
    spec = importlib.util.spec_from_file_location("check_policy", ROOT / "tools" / "check_policy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.decide


@pytest.mark.parametrize("strategy", ["weighted_threshold", "all", "any"])
def test_matches_reference_on_random_cases(policy, reference, strategy):
    rng = random.Random(1234)
    p = policy.with_overrides(evaluation_strategy=strategy)
    raw = {**p.raw, "evaluation_strategy": strategy}
    for _ in range(2000):
        mode = rng.choice(["input", "output"])
        outcomes = []
        for rule in p.rules_for(mode):
            v = rng.choice(["PASS", "PASS", "FAIL", "UNCERTAIN"])
            errored = v == "UNCERTAIN" and rng.random() < 0.3
            c = 0.0 if errored else round(rng.random(), 2)
            outcomes.append(RuleOutcome(rule.id, v, c, errored))
        truncated = rng.random() < 0.1
        ours = decide(p, outcomes, truncated, mode)
        ref = reference(raw, [{"rule_id": o.rule_id, "verdict": o.verdict, "confidence": o.confidence,
                               "error": {"type": "timeout"} if o.errored else None} for o in outcomes], truncated, mode)
        assert ours.final_verdict == ref["final_verdict"], outcomes
        assert ours.decision_reason == ref["decision_reason"]
        assert ours.triggered_rules == ref["triggered_rules"]
        assert ours.uncertain_rules == ref["uncertain_rules"]
        assert ours.review_reasons == ref["review_reasons"]
        if strategy == "weighted_threshold":
            assert ours.score == pytest.approx(ref["score"], abs=1e-6)


def test_mode_is_required_when_actions_differ_by_mode(policy):
    with pytest.raises(ValueError):
        decide(policy, [RuleOutcome("no_pii", "FAIL", 0.95)])
    assert decide(policy, [RuleOutcome("no_pii", "FAIL", 0.95)], mode="input").final_verdict == "REDACT"
    assert decide(policy, [RuleOutcome("no_pii", "FAIL", 0.95)], mode="output").final_verdict == "BLOCK"


def test_exact_threshold_counts_as_met(policy):
    # 1.0 * 0.5 hits the 0.5 threshold exactly; float noise must not turn it into a miss
    d = decide(policy, [RuleOutcome("no_jailbreak", "FAIL", 0.5)])
    assert d.final_verdict == "BLOCK"
