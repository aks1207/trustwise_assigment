"""Combine per-rule verdicts into one final verdict. Pure function, no I/O.

Mirrors the reference `decide` in tools/check_policy.py; tests/test_decision.py checks they agree.
See docs/POLICY_DESIGN.md sections 3 and 4 for the reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass

from .policy import VERDICT_ORDER, Policy

# Guards the threshold comparison against float noise such as 1 - (1 - 0.5) != 0.5.
EPS = 1e-9


@dataclass(frozen=True)
class RuleOutcome:
    rule_id: str
    verdict: str          # PASS | FAIL | UNCERTAIN
    confidence: float
    errored: bool = False


@dataclass(frozen=True)
class Decision:
    score: float | None
    final_verdict: str
    decision_reason: str
    triggered_rules: list[str]
    uncertain_rules: list[str]
    review_reasons: list[str]


def risk_contribution(weight: float, outcome: RuleOutcome) -> float:
    return weight * outcome.confidence if outcome.verdict == "FAIL" else 0.0


def decide(policy: Policy, outcomes: list[RuleOutcome], truncated: bool = False, mode: str | None = None) -> Decision:
    """`mode` picks each rule's on_fail action. It may be omitted only when every rule that failed has the same
    action in every mode."""
    weights = {r.id: r.weight for r in policy.rules}
    rules = {r.id: r for r in policy.rules}

    def action(rule_id: str) -> str:
        rule = rules[rule_id]
        if mode is not None:
            return rule.action_for(mode)
        actions = set(rule.on_fail.values())
        if len(actions) != 1:
            raise ValueError(f"rule {rule_id} has per-mode on_fail; decide() needs the mode")
        return actions.pop()
    strategy = policy.evaluation_strategy
    fails = [o for o in outcomes if o.verdict == "FAIL"]
    passes = [o for o in outcomes if o.verdict == "PASS"]
    uncertain = [o for o in outcomes if o.verdict == "UNCERTAIN"]

    score: float | None = None
    review_reasons: list[str] = []
    if strategy == "weighted_threshold":
        keep = 1.0
        for o in fails:
            keep *= 1.0 - risk_contribution(weights[o.rule_id], o)
        score = 1.0 - keep
        violated = score >= policy.threshold - EPS
        if not violated and score > 0 and score >= policy.threshold - policy.review_margin - EPS:
            review_reasons.append("score_in_review_margin")
    elif strategy == "all":   # ALLOW needs every applicable rule to PASS
        violated = bool(fails)
    else:                     # "any": ALLOW needs at least one PASS
        violated = not passes and bool(fails) and not uncertain

    # Under "any" a single PASS is enough to allow, so an UNCERTAIN sibling does not hold the item.
    if uncertain and (strategy != "any" or not passes):
        review_reasons.append("judge_error" if any(o.errored for o in uncertain) else "uncertain_rule")
    if truncated and not violated:
        review_reasons.append("content_truncated")

    outcomes_reached = ["ALLOW"]
    if violated:
        outcomes_reached.append(max((action(o.rule_id).upper() for o in fails), key=VERDICT_ORDER.index))
    if review_reasons:
        outcomes_reached.append("REVIEW")
    final = max(outcomes_reached, key=VERDICT_ORDER.index)

    if final == "ALLOW":
        reason = "no_trigger"
    elif final == "REVIEW":
        reason = "needs_review"
    else:
        reason = {"weighted_threshold": "threshold_met", "all": "one_violation", "any": "all_rules_failed"}[strategy]

    return Decision(
        score=None if score is None else round(score, 6),
        final_verdict=final,
        decision_reason=reason,
        triggered_rules=[o.rule_id for o in fails] if violated else [],
        uncertain_rules=[o.rule_id for o in uncertain],
        review_reasons=review_reasons,
    )
