"""Sanity-check the policy and output schemas. No LLM calls, no network.

    python tools/check_policy.py

Checks:
  1. policy file has every required field and sensible values
  2. example result / audit files validate against the JSON Schemas
  3. the scoring and decision rules in POLICY_DESIGN.md behave as documented (reference `decide`)
  4. every (category, mode) in the dataset has at least one applicable rule (coverage matrix)

`decide` here is a reference for the maths in the design doc. The engine implements the same rules.
"""
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policy" / "content_safety_policy.yaml"
SCHEMAS = ROOT / "schemas"
EXAMPLES = ROOT / "examples"
DATASET = ROOT / "sanity_subset.csv"
# Text behind evaluation_result.output_judge_timeout.json. The safe result stores only the hash.
TIMEOUT_TEXT = "Q3 summary:  revenue held steady. No customer records were included."

REQUIRED_POLICY = ["name", "version", "default_action", "evaluation_strategy", "threshold", "rules"]
REQUIRED_RULE = ["id", "description", "judge_prompt", "on_fail", "weight", "applies_to"]
STRATEGIES = {"all", "any", "weighted_threshold"}
ACTIONS = {"block", "redact"}

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


# ------------------------------------------------------------------ reference decision logic
def on_fail_for(rule: dict, mode: str | None) -> str:
    """Reference reading of on_fail: a single action, or a {mode: action} mapping."""
    v = rule["on_fail"]
    if isinstance(v, str):
        return v
    if mode is None:
        assert len(set(v.values())) == 1, f"{rule['id']}: per-mode on_fail needs a mode"
        return next(iter(v.values()))
    return v[mode]


def decide(policy: dict, rule_results: list[dict], truncated: bool = False, mode: str | None = None) -> dict:
    """rule_results: [{rule_id, verdict, confidence, error}] for rules that ran. Returns the decision fields."""
    rules = {r["id"]: r for r in policy["rules"]}
    order = policy["verdict_order"]
    strategy = policy["evaluation_strategy"]
    fails = [r for r in rule_results if r["verdict"] == "FAIL"]
    passes = [r for r in rule_results if r["verdict"] == "PASS"]
    uncertain = [r for r in rule_results if r["verdict"] == "UNCERTAIN"]

    score = None
    review_reasons: list[str] = []
    if strategy == "weighted_threshold":
        keep = 1.0
        for r in fails:
            keep *= 1 - rules[r["rule_id"]]["weight"] * r["confidence"]
        score = 1 - keep
        eps = 1e-9  # a score that is a boundary in exact arithmetic (0.4 = 1 - 0.75 * 0.8) must count as reaching it
        violated = score >= policy["threshold"] - eps
        if not violated and score > 0 and score >= policy["threshold"] - policy["scoring"]["review_margin"] - eps:
            review_reasons.append("score_in_review_margin")
    elif strategy == "all":  # ALLOW needs every rule to PASS
        violated = bool(fails)
    else:  # "any": ALLOW needs at least one PASS
        violated = not passes and bool(fails) and not uncertain

    uncertain_counts = strategy != "any" or not passes
    if uncertain and uncertain_counts:
        review_reasons.append("judge_error" if any(r.get("error") for r in uncertain) else "uncertain_rule")
    if truncated and not violated:
        review_reasons.append("content_truncated")

    outcomes = ["ALLOW"]
    if violated:
        outcomes.append(max((on_fail_for(rules[r["rule_id"]], mode).upper() for r in fails), key=order.index))
    if review_reasons:
        outcomes.append("REVIEW")
    final_verdict = max(outcomes, key=order.index)
    if final_verdict == "ALLOW":
        decision_reason = "no_trigger"
    elif final_verdict == "REVIEW":
        decision_reason = "needs_review"
    elif strategy == "weighted_threshold":
        decision_reason = "threshold_met"
    elif strategy == "all":
        decision_reason = "one_violation"
    else:
        decision_reason = "all_rules_failed"
    return {
        "score": None if score is None else round(score, 6),
        "final_verdict": final_verdict,
        "decision_reason": decision_reason,
        "triggered_rules": [r["rule_id"] for r in fails] if violated else [],
        "uncertain_rules": [r["rule_id"] for r in uncertain],
        "review_reasons": review_reasons,
    }


def content_sha256(mode: str, text: str) -> str:
    """SHA-256 of the canonical {"mode","text"} JSON object. Same preimage as the schemas."""
    payload = json.dumps({"mode": mode, "text": text}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 1. policy structure
def check_policy(policy: dict) -> None:
    for k in REQUIRED_POLICY:
        check(k in policy, f"policy missing '{k}'")
    check(policy.get("evaluation_strategy") in STRATEGIES, "evaluation_strategy not in all|any|weighted_threshold")
    check(0 < policy.get("threshold", -1) < 1, "threshold must be in (0,1)")
    check(policy.get("default_action") == "allow", "default_action should be allow")
    check(policy["verdict_order"] == ["ALLOW", "REDACT", "REVIEW", "BLOCK"], "verdict_order changed")
    check(policy.get("review", {}).get("block_survives_sibling_uncertain") is True, "reviewer rule missing")
    ids = [r["id"] for r in policy["rules"]]
    check(len(ids) == len(set(ids)), "duplicate rule ids")
    for r in policy["rules"]:
        for k in REQUIRED_RULE:
            check(k in r, f"rule {r.get('id')} missing '{k}'")
        check(0 < r["weight"] <= 1, f"rule {r['id']} weight outside (0,1]")
        actions = [r["on_fail"]] if isinstance(r["on_fail"], str) else list(r["on_fail"].values())
        check(all(a in ACTIONS for a in actions), f"rule {r['id']} on_fail not in {ACTIONS}")
        if isinstance(r["on_fail"], dict):
            check(set(r["on_fail"]) == set(r["applies_to"]), f"rule {r['id']} per-mode on_fail must match applies_to")
        check(set(r["applies_to"]) <= {"input", "output"} and r["applies_to"], f"rule {r['id']} applies_to invalid")
        check(len(r["judge_prompt"].strip()) > 200, f"rule {r['id']} judge_prompt suspiciously short")
        for word in ("FAIL when", "PASS when", "UNCERTAIN when"):
            check(word in r["judge_prompt"], f"rule {r['id']} judge_prompt lacks a '{word}' section")
    check("{nonce}" in policy["judge_user_template"] and "{content}" in policy["judge_user_template"], "user template placeholders")
    for mode in ("input", "output"):
        check(any(mode in r["applies_to"] for r in policy["rules"]), f"no rule applies to mode {mode}")


# ------------------------------------------------------------------ 2. schemas + examples
def check_schemas() -> None:
    resources = []
    for p in SCHEMAS.glob("*.json"):
        s = json.loads(p.read_text())
        Draft202012Validator.check_schema(s)
        resources.append((s["$id"], Resource.from_contents(s)))
    registry = Registry().with_resources(resources)
    validators = {
        "evaluation_result": Draft202012Validator(json.loads((SCHEMAS / "evaluation_result.schema.json").read_text()), registry=registry),
        "audit_record": Draft202012Validator(json.loads((SCHEMAS / "audit_record.schema.json").read_text()), registry=registry),
    }
    for p in sorted(EXAMPLES.glob("*.json")):
        kind = "audit_record" if p.name.startswith("audit_record") else "evaluation_result"
        errs = list(validators[kind].iter_errors(json.loads(p.read_text())))
        for e in errs[:5]:
            failures.append(f"{p.name}: {'/'.join(map(str, e.absolute_path))}: {e.message[:120]}")
    # audit record and result agree, and the content hash binds mode to the exact text
    audit = json.loads((EXAMPLES / "audit_record.input_pii.json").read_text())
    check(audit["result"]["request_id"] == audit["request_id"], "audit/result request_id mismatch")
    check(audit["execution_kind"] == audit["result"]["execution_kind"], "audit/result execution_kind mismatch")
    check(audit["result"] == json.loads((EXAMPLES / "evaluation_result.input_pii.json").read_text()), "audit result is not the input PII example")
    bound = content_sha256(audit["mode"], audit["content"]["text"])
    check(audit["content"]["sha256"] == bound, "audit content hash is not mode+text")
    check(audit["result"]["content"]["sha256"] == bound, "result content hash is not mode+text")
    check(audit["result"]["content"]["chars"] == len(audit["content"]["text"]), "content chars != text length")
    timeout = json.loads((EXAMPLES / "evaluation_result.output_judge_timeout.json").read_text())
    check(len(TIMEOUT_TEXT) == timeout["content"]["chars"], "timeout fixture length drifted")
    check(content_sha256("output", TIMEOUT_TEXT) == timeout["content"]["sha256"], "timeout content hash is not mode+text")
    calls = [call for rule in audit["rules"] for call in rule["calls"]]
    totals = audit["result"]["totals"]
    check(totals["attempt_count"] == len(calls), "attempt_count != number of audit calls")
    check(totals["attempt_count"] == sum(r["attempts"] for r in audit["result"]["rule_results"]), "attempt_count != sum of rule attempts")
    for key in ("input_tokens", "output_tokens", "cost_usd"):
        values = [call["usage"][key] for call in calls]
        expected = None if any(v is None for v in values) else (round(sum(values), 8) if key == "cost_usd" else sum(values))
        check(totals[key] == expected, f"totals.{key} {totals[key]} != rolled up {expected}")
        for call in calls:
            if call["usage"]["cost_usd"] is None:
                check(call["usage"]["pricing_version"] is None, "pricing_version set without a cost")
    # example results follow the documented maths
    policy_sha = hashlib.sha256(POLICY.read_bytes()).hexdigest()
    policy = yaml.safe_load(POLICY.read_text())
    for p in sorted(EXAMPLES.glob("evaluation_result.*.json")):
        res = json.loads(p.read_text())
        check(res["policy_sha256"] == policy_sha and res["policy_version"] == policy["version"],
              f"{p.name}: policy hash/version is stale; the example no longer matches the policy file")
        got = decide(policy, res["rule_results"], mode=res["mode"])
        for k in ("score", "final_verdict", "decision_reason", "triggered_rules", "uncertain_rules", "review_reasons"):
            want = res[k]
            if k == "score" and want is not None:
                check(math.isclose(got[k], want, abs_tol=1e-3), f"{p.name}: score {want} != recomputed {got[k]}")
            else:
                check(got[k] == want, f"{p.name}: {k} {want} != recomputed {got[k]}")


# ------------------------------------------------------------------ 3. documented behaviours
def check_behaviour(policy: dict) -> None:
    def run(strategy, results, threshold=0.5, truncated=False, mode="input"):
        pol = {**policy, "evaluation_strategy": strategy, "threshold": threshold}
        return decide(pol, [{"error": None, **r} for r in results], truncated, mode)

    f = lambda rid, c: {"rule_id": rid, "verdict": "FAIL", "confidence": c}
    p = lambda rid: {"rule_id": rid, "verdict": "PASS", "confidence": 0.95}
    u = lambda rid, err=None: {"rule_id": rid, "verdict": "UNCERTAIN", "confidence": 0.0, "error": err}

    clean = [p("no_pii"), p("no_toxic_language"), p("no_unsafe_content")]
    check(run("weighted_threshold", clean)["final_verdict"] == "ALLOW", "clean should ALLOW")
    check(run("weighted_threshold", [f("no_pii", 0.95)] + clean[1:])["final_verdict"] == "REDACT", "input PII FAIL -> REDACT")
    check(run("weighted_threshold", [f("no_pii", 0.95)] + clean[1:], mode="output")["final_verdict"] == "BLOCK",
          "output PII FAIL -> BLOCK (per-mode on_fail)")
    check(run("weighted_threshold", [f("no_toxic_language", 0.9)] + clean[:1])["final_verdict"] == "BLOCK", "toxicity FAIL -> BLOCK")
    check(run("weighted_threshold", [u("no_pii")] + clean[1:])["final_verdict"] == "REVIEW", "UNCERTAIN -> REVIEW")
    check(run("weighted_threshold", [u("no_pii", {"type": "timeout"})] + clean[1:])["review_reasons"] == ["judge_error"], "error -> judge_error")
    check(run("weighted_threshold", clean, truncated=True)["final_verdict"] == "REVIEW", "truncated clean -> REVIEW")
    # redact + block both fail: block wins
    check(run("weighted_threshold", [f("no_pii", 0.9), f("no_toxic_language", 0.9)])["final_verdict"] == "BLOCK", "block beats redact")
    # a BLOCK is not released by a sibling UNCERTAIN
    held = run("weighted_threshold", [f("no_jailbreak", 0.95), u("no_pii")])
    check(held["final_verdict"] == "BLOCK", "BLOCK must survive a sibling UNCERTAIN")
    check(held["decision_reason"] == "threshold_met", "BLOCK reason should be threshold_met")
    check("no_pii" in held["uncertain_rules"], "sibling UNCERTAIN should stay visible")
    # unsafe_content (w 0.8): conf 0.6 -> 0.48. Moves with threshold +-0.1
    one = [f("no_unsafe_content", 0.6)] + clean[:2]
    check(run("weighted_threshold", one, 0.5)["final_verdict"] == "REVIEW", "0.48 sits in the review band at 0.5")
    check(run("weighted_threshold", one, 0.4)["final_verdict"] == "BLOCK", "0.48 blocks at 0.4")
    check(run("weighted_threshold", one, 0.6)["final_verdict"] == "ALLOW", "0.48 allowed at 0.6 (below the band [0.5, 0.6))")
    check(run("weighted_threshold", one, 0.7)["final_verdict"] == "ALLOW", "0.48 allowed at 0.7")
    # all / any
    check(run("all", [f("no_pii", 0.2)] + clean[1:])["final_verdict"] == "REDACT", "all: any FAIL violates")
    check(run("any", [f("no_pii", 0.9)] + clean[1:])["final_verdict"] == "ALLOW", "any: one PASS is enough")
    check(run("any", [f("no_pii", 0.9), f("no_toxic_language", 0.9)])["final_verdict"] == "BLOCK", "any: all FAIL violates")


# ------------------------------------------------------------------ 4. dataset coverage
EXPECTED = {  # (category, mode, source_subcategory) -> rules that should FAIL (any of)
    ("pii", "input"): ["no_pii"],
    ("pii", "output"): ["no_pii"],
    ("prompt_manipulation", "input", "jailbreak"): ["no_jailbreak"],
    ("prompt_manipulation", "input", "prompt_injection"): ["no_prompt_injection"],
    ("prompt_manipulation", "input", "pii_output"): ["no_prompt_extraction"],
    ("toxicity", "input"): ["no_toxic_language", "no_unsafe_content"],
    ("toxicity", "output"): ["no_toxic_language", "no_unsafe_content"],
}


def coverage(policy: dict) -> None:
    rules = policy["rules"]
    counts: Counter = Counter()
    for row in csv.DictReader(DATASET.open(newline="")):
        cat, mode = row["category"], row["mode"]
        sub = json.loads(row["provenance"]).get("source_subcategory") if cat == "prompt_manipulation" else None
        key = (cat, mode, sub) if sub else (cat, mode)
        counts[(key, row["label"] == "true")] += 1
    print("\nCoverage matrix (dataset category/mode -> rules that run, rules expected to FAIL)")
    print(f"{'category / mode / sub':<46}{'rows':>6}  {'label':<8} {'rules run':<10} expected FAIL")
    for (key, harmful), n in sorted(counts.items(), key=lambda kv: (not kv[0][1], kv[0][0])):
        mode = key[1]
        running = [r["id"] for r in rules if mode in r["applies_to"]]
        exp = EXPECTED.get(key, []) if harmful else []
        if harmful:
            check(exp, f"no expectation for harmful {key}")
            check(all(e in running for e in exp), f"{key}: expected rule does not run in mode {mode}")
        print(f"{'/'.join(k for k in key if k):<46}{n:>6}  {'harmful' if harmful else 'benign':<8} {len(running):<10} {', '.join(exp) or '(all PASS)'}")
    runs = {m: sum(m in r["applies_to"] for r in rules) for m in ("input", "output")}
    print(f"\njudge calls per item: input={runs['input']}, output={runs['output']}")
    print("min FAIL confidence for a single rule to reach each threshold (weight -> thr 0.4 / 0.5 / 0.6):")
    for r in rules:
        print(f"  {r['id']:<22} w={r['weight']:<4} " + " / ".join(f"{t / r['weight']:.2f}" if t / r["weight"] <= 1 else "never" for t in (0.4, 0.5, 0.6)))


def main() -> int:
    policy = yaml.safe_load(POLICY.read_text())
    check_policy(policy)
    check_schemas()
    check_behaviour(policy)
    coverage(policy)
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for m in failures:
            print("  -", m)
        return 1
    print("OK: policy, schemas, examples and documented behaviour are consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
