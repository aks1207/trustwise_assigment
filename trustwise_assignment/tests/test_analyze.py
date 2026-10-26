"""Part 3 analysis: the threshold sweep re-runs decide() faithfully; agreement is computed correctly."""

import asyncio
import json

import pytest

from experiment import analyze as an
from experiment.run import DEFAULT_POLICY, DEFAULT_SAMPLE, run_once
from policy_engine import ScriptedJudge
from policy_engine.judge import verdict


def make_batch(tmp_path, judges, limit=6):
    for i, judge in enumerate(judges, start=1):
        asyncio.run(run_once(judge=judge, model_name="mock", repeat=i, batch="b", policy_path=DEFAULT_POLICY,
                             sample_path=DEFAULT_SAMPLE, item_concurrency=2, limit=limit,
                             runs_dir=tmp_path, progress=False))
        # distinct run folders even within one second
        for d in (tmp_path / "b").iterdir():
            if d.name.endswith(f"-r{i}") and not d.name.startswith(f"x{i}"):
                d.rename(d.with_name(f"x{i}-" + d.name))
    return an.load_runs("b", tmp_path)


def weak_unsafe():
    # 0.8 weight x 0.6 confidence = 0.48 on every item
    return ScriptedJudge(default=verdict("PASS", 0.95), script={"no_unsafe_content": [verdict("FAIL", 0.6)] * 50})


def test_sweep_moves_a_weak_fail_across_thresholds(tmp_path):
    runs = make_batch(tmp_path, [weak_unsafe()])
    policy = an.policy_for(runs[0]["meta"])
    got = {t: {l["result"]["final_verdict"] for l in an.redecide(runs[0]["lines"], policy, t)} for t in (0.4, 0.5, 0.6, 0.7)}
    assert got == {0.4: {"BLOCK"}, 0.5: {"REVIEW"}, 0.6: {"ALLOW"}, 0.7: {"ALLOW"}}


def test_redecide_at_policy_threshold_reproduces_saved_verdicts(tmp_path):
    runs = make_batch(tmp_path, [weak_unsafe()])
    an.check_reproduces(runs)                     # no exception


def test_tampered_run_is_detected(tmp_path):
    runs = make_batch(tmp_path, [weak_unsafe()])
    runs[0]["lines"][0]["result"]["final_verdict"] = "ALLOW"
    with pytest.raises(SystemExit):
        an.check_reproduces(runs)


def test_changed_policy_file_is_detected(tmp_path):
    runs = make_batch(tmp_path, [weak_unsafe()])
    runs[0]["meta"]["policy"]["sha256"] = "0" * 64
    with pytest.raises(SystemExit):
        an.policy_for(runs[0]["meta"])


def test_agreement_counts_disagreeing_runs(tmp_path):
    same = make_batch(tmp_path / "s", [ScriptedJudge(), ScriptedJudge(), ScriptedJudge()])
    a = an.agreement(same)
    assert a["final_verdict_identical_all_runs"] == 1.0 and a["decision_identical_all_runs"] == 1.0
    # third run blocks everything on jailbreak-applicable items
    flip = ScriptedJudge(script={"no_toxic_language": [verdict("FAIL", 0.95)] * 50})
    mixed = make_batch(tmp_path / "m", [ScriptedJudge(), ScriptedJudge(), flip])
    m = an.agreement(mixed)
    assert m["final_verdict_identical_all_runs"] == 0.0
    assert m["final_verdict_pairwise"] == pytest.approx(1 / 3, abs=1e-4)
    assert len(m["unstable_items"]) == m["items"] == 6


def test_analyze_and_csvs(tmp_path, monkeypatch):
    make_batch(tmp_path, [weak_unsafe(), weak_unsafe()])
    report = an.analyze(["b"], runs_dir=tmp_path)
    m = report["batches"]["b"]["mock"]
    assert m["sweep"]["0.4"]["raw"]["review_rate"] == 0.0 and m["sweep"]["0.5"]["raw"]["review_rate"] == 1.0
    assert len(m["threshold_flips"]["0.4"]) == 6
    paths = an.write_csvs(report, tmp_path / "metrics")
    assert {p.name for p in paths} == {"part3_metrics.csv", "part3_agreement.csv", "part3_cost_latency.csv", "part3_summary.json"}
    assert json.loads((tmp_path / "metrics" / "part3_summary.json").read_text())["sweep_thresholds"] == [0.4, 0.5, 0.6]


def test_policy_found_by_hash_after_main_file_changes(tmp_path):
    # A run is matched to its policy by hash, even when the recorded path no longer exists.
    old = tmp_path / "content_safety_policy.yaml"
    old.write_bytes(DEFAULT_POLICY.read_bytes())
    import hashlib
    meta = {"run_id": "r", "policy": {"path": "policy/does_not_exist.yaml",
                                      "sha256": hashlib.sha256(old.read_bytes()).hexdigest()}}
    assert an.policy_for(meta, policy_dir=tmp_path).sha256 == meta["policy"]["sha256"]
    import re
    old.write_text(re.sub(r'^version: "[^"]+"', 'version: "9.9"', old.read_text(), count=1, flags=re.M))
    with pytest.raises(SystemExit):
        an.policy_for(meta, policy_dir=tmp_path)


def test_real_runs_resolve_to_their_recorded_policy():
    for batch, version in (("policy-1.0-seed20", "1.0"), ("policy-1.1-seed20", "1.1")):
        for run in an.load_runs(batch):
            assert an.policy_for(run["meta"]).version == version
