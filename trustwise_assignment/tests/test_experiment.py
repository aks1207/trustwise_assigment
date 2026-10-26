"""Part 2 harness: sample, run layout, never-overwrite, judge input, metrics. All mocked."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from experiment import run as run_mod
from experiment.dataset import load_rows
from experiment.metrics import cost_latency, item_weights, rates, read_jsonl
from experiment.run import DEFAULT_POLICY, DEFAULT_SAMPLE, run_once
from experiment.sample import ALLOCATION, draw
from policy_engine import ScriptedJudge
from policy_engine.judge import verdict

REQUIRED = {"pii/input", "pii/output", "prompt_manipulation/input/jailbreak",
            "prompt_manipulation/input/prompt_injection", "prompt_manipulation/input/pii_output",
            "toxicity/input", "toxicity/output", "caps_words/input", "caps_words/output",
            "repeated_words/input", "repeated_words/output", "minimal_turn/input", "minimal_turn/output",
            "safe_reply/output"}


# ------------------------------------------------------------------ sample
def test_sample_is_deterministic_balanced_and_covers_required_strata():
    a, b = draw(20), draw(20)
    assert a["items"] == b["items"]
    assert draw(7)["items"] != a["items"]
    assert a["size"] == 100 and a["harmful"] == 50 and a["benign"] == 50
    ids = [i["id"] for i in a["items"]]
    assert len(set(ids)) == 100
    assert REQUIRED <= {i["stratum"] for i in a["items"]}
    assert sum(ALLOCATION.values()) == 100


def test_saved_sample_matches_a_fresh_draw_and_holds_no_text():
    saved = json.loads(DEFAULT_SAMPLE.read_text())
    assert saved["items"] == draw(saved["seed"])["items"]
    texts = {r.text for r in load_rows()}
    flat = json.dumps(saved)
    assert not any(t in flat for t in texts if len(t) > 20)


# ------------------------------------------------------------------ run harness
def run(tmp_path, judge=None, limit=6, repeat=1):
    return asyncio.run(run_once(
        judge=judge or ScriptedJudge(), model_name="mock", repeat=repeat, batch="test",
        policy_path=DEFAULT_POLICY, sample_path=DEFAULT_SAMPLE, item_concurrency=3,
        limit=limit, runs_dir=tmp_path, progress=False))


def test_run_writes_expected_files(tmp_path, validators):
    d = run(tmp_path)
    assert {p.name for p in d.iterdir()} == {"meta.json", "results.jsonl", "audit.jsonl", "completed.json"}
    meta = json.loads((d / "meta.json").read_text())
    assert meta["fields_sent_to_judge"] == ["text", "mode"]
    assert meta["policy"]["version"] and len(meta["policy"]["sha256"]) == 64
    assert meta["sample"]["seed"] == 20 and meta["sample"]["limit"] == 6
    lines = [json.loads(l) for l in (d / "results.jsonl").read_text().splitlines()]
    audits = [json.loads(l) for l in (d / "audit.jsonl").read_text().splitlines()]
    assert len(lines) == len(audits) == 6
    for line in lines:
        assert {"item_id", "stratum", "label", "result"} <= set(line)
        assert not list(validators["result"].iter_errors(line["result"]))
    for a in audits:
        assert not list(validators["audit"].iter_errors(a))
    done = json.loads((d / "completed.json").read_text())
    assert done["items_ok"] == 6 and done["items_error"] == 0


def test_only_text_and_mode_reach_the_judge(tmp_path):
    judge = ScriptedJudge()
    d = run(tmp_path, judge=judge)
    lines = [json.loads(l) for l in (d / "results.jsonl").read_text().splitlines()]
    leaked = {l["item_id"] for l in lines} | {l["stratum"] for l in lines} | {l["category"] for l in lines}
    for req in judge.requests:
        prompt = req.system_prompt + req.user_prompt
        assert not any(x in prompt for x in leaked if x not in ("pii",)), "label-revealing metadata sent to judge"
        assert "laughable_set" not in prompt


def test_runs_are_never_overwritten(tmp_path, monkeypatch):
    fixed = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(run_mod, "_now", lambda: fixed)
    run(tmp_path, limit=2)
    with pytest.raises(FileExistsError):
        run(tmp_path, limit=2)


def test_item_error_is_recorded_not_dropped(tmp_path):
    def broken_sink(record):
        raise OSError("disk full")

    def factory(policy, judge, audit_sink):
        from policy_engine import Engine
        return Engine(policy, judge, audit_sink=broken_sink)

    d = asyncio.run(run_once(judge=ScriptedJudge(), model_name="mock", repeat=1, batch="test",
                             policy_path=DEFAULT_POLICY, sample_path=DEFAULT_SAMPLE, item_concurrency=2,
                             limit=3, runs_dir=tmp_path, engine_factory=factory, progress=False))
    lines = [json.loads(l) for l in (d / "results.jsonl").read_text().splitlines()]
    assert len(lines) == 3 and all("OSError" in l["error"] for l in lines)
    assert json.loads((d / "completed.json").read_text())["items_error"] == 3


def test_cost_latency_counts_calls_and_missing_cost(tmp_path):
    d = run(tmp_path, limit=4, repeat=1)
    cl = cost_latency(read_jsonl(d / "results.jsonl"), read_jsonl(d / "audit.jsonl"))
    assert cl["judge_calls"] == cl["rules_run"]                  # no retries with a clean mock
    assert cl["calls_without_cost"] == cl["judge_calls"]         # the mock reports no cost, and it is not counted as 0


# ------------------------------------------------------------------ metrics
def line(stratum, label, v):
    return {"stratum": stratum, "label": label, "result": {"final_verdict": v}}


def test_rates_strict_vs_lenient():
    lines = [line("h", True, "BLOCK"), line("h", True, "REVIEW"), line("h", True, "ALLOW"), line("h", True, "REDACT"),
             line("b", False, "ALLOW"), line("b", False, "REVIEW"), line("b", False, "BLOCK"), line("b", False, "ALLOW")]
    r = rates(lines)
    assert r["strict"] == {"accuracy": 0.625, "false_allow_rate": 0.25, "false_block_rate": 0.5}
    assert r["lenient"] == {"accuracy": 0.625, "false_allow_rate": 0.5, "false_block_rate": 0.25}
    assert r["review_rate"] == 0.25


def test_reweighting_uses_population_share():
    sample = {"allocation": {"h": 1, "b": 1}, "population": {"h": 22, "b": 78}}
    w = item_weights(sample)
    lines = [line("h", True, "ALLOW"), line("b", False, "ALLOW")]   # misses the harmful item
    assert rates(lines)["strict"]["accuracy"] == 0.5
    assert rates(lines, w)["strict"]["accuracy"] == 0.78            # benign dominates the natural mix


def test_errored_items_are_excluded_and_counted():
    r = rates([line("h", True, "BLOCK"), {"stratum": "h", "label": True, "error": "x"}])
    assert r["items"] == 1 and r["not_evaluated"] == 1


# ------------------------------------------------------------------ multiple seeds
@pytest.fixture
def other_sample(tmp_path):
    """A second sample (seed 7) written to a temp file, so tests never touch runs/."""
    p = tmp_path / "sample_seed7.json"
    p.write_text(json.dumps(draw(7)))
    return p


def test_different_seeds_give_mostly_disjoint_samples():
    a, b = draw(20)["items"], draw(42)["items"]
    assert len({i["id"] for i in a} & {i["id"] for i in b}) < 10


def test_batch_refuses_a_second_sample(tmp_path, other_sample):
    run(tmp_path, limit=2)
    with pytest.raises(SystemExit):
        asyncio.run(run_once(judge=ScriptedJudge(), model_name="mock", repeat=2, batch="test",
                             policy_path=DEFAULT_POLICY, sample_path=other_sample, item_concurrency=2,
                             limit=2, runs_dir=tmp_path, progress=False))


def test_batch_sample_and_compare_need_one_sample(tmp_path, other_sample):
    from experiment.compare import compare
    from experiment.metrics import batch_sample
    from experiment.dataset import ROOT
    run(tmp_path, limit=2)
    asyncio.run(run_once(judge=ScriptedJudge(), model_name="mock", repeat=1, batch="other",
                         policy_path=DEFAULT_POLICY, sample_path=other_sample, item_concurrency=2,
                         limit=2, runs_dir=tmp_path, progress=False))
    assert batch_sample(tmp_path / "test", ROOT)["seed"] == 20
    assert batch_sample(tmp_path / "other", ROOT)["seed"] == 7
    with pytest.raises(SystemExit):
        compare("test", "other", runs_dir=tmp_path)


# ---------------------------------------------------------------- judge-injection scoring (no model calls)
def _ji_result(final, fails=(), mode="input", request_id="r"):
    rules = ["no_pii", "no_jailbreak", "no_prompt_injection", "no_toxic_language"]
    return {"final_verdict": final, "request_id": request_id,
            "rule_results": [{"rule_id": r, "verdict": "FAIL" if r in fails else "PASS", "confidence": 0.9,
                              "reasoning": "x"} for r in rules]}


def _ji_audit(request_id):
    return {"request_id": request_id, "rules": [{"rule_id": "no_toxic_language",
                                                 "calls": [{"raw_response": "{}", "user_prompt": "p"}]}]}


def test_attack_position_prefix_and_suffix():
    from experiment.judge_injection import attacked_text
    assert attacked_text("text", "suffix", "ADD") == "text\n\nADD"
    assert attacked_text("text", "prefix", "ADD") == "ADD\n\ntext"


def test_harmful_scoring_pairs_each_attack_with_its_clean_text():
    from experiment.judge_injection import score_harmful
    spec = {"attacks": {"fake_close": {"family": "boundary"}},
            "variants": [{"variant_id": "A::clean", "text": "t"}, {"variant_id": "A::fake_close", "text": "t+x"}]}
    lines = [{"variant_id": "A::clean", "item_id": "A", "attack": "clean", "stratum": "toxicity/input", "mode": "input",
              "result": _ji_result("BLOCK", ["no_toxic_language"], request_id="c")},
             {"variant_id": "A::fake_close", "item_id": "A", "attack": "fake_close", "stratum": "toxicity/input",
              "mode": "input", "result": _ji_result("ALLOW", request_id="a")}]
    runs = [({"model_name": "luna", "repeat": 1, "run_id": "r1"}, lines, {"a": _ji_audit("a"), "c": _ji_audit("c")})]
    rows, cases = score_harmful(spec, runs)
    attacked = next(r for r in rows if r["attack"] == "fake_close")
    assert attacked["let_through"] == 1 and attacked["weaker_than_clean"] == 1
    assert cases[0]["clean_final"] == "BLOCK" and cases[0]["rules_lost_fail"] == ["no_toxic_language"]


def test_benign_scoring_separates_designed_blocks_from_false_alarms():
    from experiment.judge_injection import score_benign
    spec = {"attacks": {"fake_close": {"family": "boundary"}}, "variants": [
        {"variant_id": "B::fake_close", "stratum": "everyday/input", "mode": "input", "source": "sample", "text": "x"},
        {"variant_id": "C::fake_close", "stratum": "everyday/input", "mode": "input", "source": "sample", "text": "y"},
        {"variant_id": "D::fake_close", "stratum": "everyday/output", "mode": "output", "source": "sample", "text": "z"},
        {"variant_id": "F::clean", "stratum": "format/input", "mode": "input", "source": "authored", "text": "f"}]}
    lines = [  # injection rule alone: designed; a content rule firing, an attacked output, a format item: false alarms
        {"variant_id": "B::fake_close", "attack": "fake_close", "result": _ji_result("BLOCK", ["no_prompt_injection"], request_id="b")},
        {"variant_id": "C::fake_close", "attack": "fake_close",
         "result": _ji_result("BLOCK", ["no_prompt_injection", "no_toxic_language"], request_id="c")},
        {"variant_id": "D::fake_close", "attack": "fake_close", "result": _ji_result("BLOCK", ["no_toxic_language"], request_id="d")},
        {"variant_id": "F::clean", "attack": "clean", "result": _ji_result("BLOCK", ["no_prompt_injection"], request_id="f")}]
    audits = {k: _ji_audit(k) for k in "bcdf"}
    rows, cases = score_benign(spec, [({"model_name": "luna", "repeat": 1, "run_id": "r1"}, lines, audits)])
    attacked = next(r for r in rows if r["group"] == "fake_close")
    fmt = next(r for r in rows if r["group"] == "format")
    assert attacked["designed_block"] == 1 and attacked["false_alarm"] == 2 and fmt["false_alarm"] == 1
    assert sorted(c["variant_id"] for c in cases) == ["C::fake_close", "D::fake_close", "F::clean"]


def test_stratified_draw_covers_every_stratum_and_is_deterministic():
    from experiment.judge_injection import stratified
    items = [{"id": f"{s}-{k}", "stratum": s} for s in "abcd" for k in range(6)]
    one, two = stratified(items, 10, 20), stratified(items, 10, 20)
    assert one == two and len(one) == 10
    counts = {s: sum(i["stratum"] == s for i in one) for s in "abcd"}
    assert sorted(counts.values()) == [2, 2, 3, 3]
