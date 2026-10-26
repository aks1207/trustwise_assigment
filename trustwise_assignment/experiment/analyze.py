"""Part 3: metrics per model, repeat-run agreement, weighted_threshold sweep, cost and latency.

    python -m experiment.analyze policy-1.0-seed20 policy-1.1-seed20

The threshold sweep makes no model calls. It re-runs the pure decide() function on the per-rule verdicts and
confidences saved in each run, with the threshold overridden. At the policy's own threshold
it must reproduce every saved final verdict, and it refuses to report if it does not.

Writes (derived, regenerated each time):
  metrics/part3_metrics.csv       batch, model, threshold, weighting, review handling -> accuracy, FA, FB, review rate
  metrics/part3_agreement.csv     batch, model -> repeat-run agreement on final verdict, binary decision, rule verdicts
  metrics/part3_cost_latency.csv  batch, model -> cost, tokens, latency per call and per item
  metrics/part3_summary.json      all of the above plus per-run rates and the items each threshold move flips
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from policy_engine.decision import RuleOutcome, decide
from policy_engine.policy import load_policy

from .dataset import ROOT
from .metrics import batch_sample, cost_latency, item_weights, predicted_harmful, rates, read_jsonl

RUNS = ROOT / "runs"
METRICS = ROOT / "metrics"
SWEEP = (0.4, 0.5, 0.6)


# ------------------------------------------------------------------ loading
def load_runs(batch: str, runs_dir: Path = RUNS) -> list[dict]:
    runs = []
    for d in sorted(p for p in (runs_dir / batch).iterdir() if p.is_dir() and (p / "completed.json").exists()):
        meta = json.loads((d / "meta.json").read_text())
        runs.append({"dir": d, "meta": meta, "lines": read_jsonl(d / "results.jsonl"),
                     "audits": read_jsonl(d / "audit.jsonl")})
    return runs


def policy_for(meta: dict, policy_dir: Path = ROOT / "policy"):
    """The exact policy a run used, found by hash.

    The recorded path is tried first, then any file in policy/ with the recorded SHA-256. No match means the
    policy file has changed since the run, and the run cannot be re-scored.
    """
    want = meta["policy"]["sha256"]
    recorded = ROOT / meta["policy"]["path"]
    candidates = ([recorded] if recorded.exists() else []) + sorted(policy_dir.glob("*.yaml"))
    for path in candidates:
        if hashlib.sha256(path.read_bytes()).hexdigest() == want:
            return load_policy(path)
    raise SystemExit(f"{meta['run_id']}: no policy file with sha256 {want[:12]} "
                     f"(recorded path {meta['policy']['path']} has changed)")


# ------------------------------------------------------------------ re-decide
def redecide(lines: list[dict], policy, threshold: float) -> list[dict]:
    """Same lines with final_verdict recomputed at `threshold` from the saved rule verdicts."""
    p = policy.with_overrides(threshold=threshold)
    out = []
    for l in lines:
        if "result" not in l:
            out.append(l)
            continue
        r = l["result"]
        outcomes = [RuleOutcome(x["rule_id"], x["verdict"], x["confidence"], x["error"] is not None)
                    for x in r["rule_results"]]
        d = decide(p, outcomes, r["content"]["truncated"], r["mode"])
        out.append({**l, "result": {**r, "final_verdict": d.final_verdict, "score": d.score}})
    return out


def check_reproduces(runs: list[dict]) -> None:
    for run in runs:
        policy = policy_for(run["meta"])
        again = redecide(run["lines"], policy, policy.threshold)
        bad = [a["item_id"] for a, b in zip(run["lines"], again)
               if "result" in a and a["result"]["final_verdict"] != b["result"]["final_verdict"]]
        if bad:
            raise SystemExit(f"{run['meta']['run_id']}: decide() does not reproduce saved verdicts for {bad[:3]}")


# ------------------------------------------------------------------ agreement
def agreement(runs: list[dict]) -> dict[str, Any]:
    """How often the repeat runs of one model agree, per item and per rule."""
    final: dict[str, list[str]] = defaultdict(list)
    rule: dict[tuple, list[str]] = defaultdict(list)
    for run in runs:
        for l in run["lines"]:
            if "result" not in l:
                continue
            final[l["item_id"]].append(l["result"]["final_verdict"])
            for x in l["result"]["rule_results"]:
                rule[(l["item_id"], x["rule_id"])].append(x["verdict"])
    full = [v for v in final.values() if len(v) == len(runs)]

    def all_same(vs):
        return len(set(vs)) == 1

    def pairwise(vs):
        pairs = list(itertools.combinations(vs, 2))
        return sum(a == b for a, b in pairs) / len(pairs) if pairs else 1.0

    binary = [[predicted_harmful(v, "harmful") for v in vs] for vs in full]
    unstable = sorted(i for i, v in final.items() if len(v) == len(runs) and not all_same(v))
    return {
        "runs": len(runs),
        "items": len(full),
        "final_verdict_identical_all_runs": round(sum(map(all_same, full)) / len(full), 4),
        "final_verdict_pairwise": round(fmean(map(pairwise, full)), 4),
        "decision_identical_all_runs": round(sum(map(all_same, binary)) / len(binary), 4),
        "rule_verdict_identical_all_runs": round(sum(all_same(v) for v in rule.values()) / len(rule), 4),
        "rules_judged": len(rule),
        "unstable_items": unstable,
    }


# ------------------------------------------------------------------ main analysis
def analyze(batches: list[str], runs_dir: Path = RUNS) -> dict[str, Any]:
    report: dict[str, Any] = {"batches": {}, "sweep_thresholds": list(SWEEP)}
    for batch in batches:
        sample = batch_sample(runs_dir / batch, ROOT)
        weights = item_weights(sample)
        runs = load_runs(batch, runs_dir)
        check_reproduces(runs)
        by_model: dict[str, list[dict]] = defaultdict(list)
        for run in runs:
            by_model[run["meta"]["model_name"]].append(run)
        report["batches"][batch] = {}
        for model, mruns in sorted(by_model.items()):
            policy = policy_for(mruns[0]["meta"])
            sweep: dict[str, Any] = {}
            flips: dict[str, list] = {}
            base = {r["meta"]["run_id"]: redecide(r["lines"], policy, policy.threshold) for r in mruns}
            for t in SWEEP:
                per_run = {r["meta"]["run_id"]: redecide(r["lines"], policy, t) for r in mruns}
                sweep[str(t)] = {
                    "raw": _mean([rates(ls) for ls in per_run.values()]),
                    "reweighted": _mean([rates(ls, weights) for ls in per_run.values()]),
                    "per_run_raw": {rid: rates(ls) for rid, ls in per_run.items()},
                }
                if t != policy.threshold:
                    flips[str(t)] = sorted({
                        (l0["item_id"], l0["stratum"], l0["label"], l0["result"]["final_verdict"], l1["result"]["final_verdict"])
                        for rid in per_run for l0, l1 in zip(base[rid], per_run[rid])
                        if "result" in l0 and l0["result"]["final_verdict"] != l1["result"]["final_verdict"]})
            cls = [cost_latency(r["lines"], r["audits"]) for r in mruns]
            report["batches"][batch][model] = {
                "policy_version": policy.version,
                "policy_threshold": policy.threshold,
                "sample": {"path": sample["_path"], "seed": sample["seed"], "sha256": sample["_sha256"]},
                "runs": [r["meta"]["run_id"] for r in mruns],
                "sweep": sweep,
                "threshold_flips": {t: [dict(zip(("item_id", "stratum", "label_harmful", "at_policy_threshold", "at_t"), f))
                                        for f in fl] for t, fl in flips.items()},
                "agreement": agreement(mruns),
                "cost_latency": {
                    "cost_usd_per_run": round(fmean(c["cost_usd_known"] for c in cls), 4),
                    "cost_usd_per_item": round(fmean(c["cost_per_item_usd"] for c in cls), 6),
                    "cost_usd_per_1k_items": round(1000 * fmean(c["cost_per_item_usd"] for c in cls), 2),
                    "judge_calls_per_run": round(fmean(c["judge_calls"] for c in cls), 1),
                    "input_tokens_per_call": round(fmean(c["input_tokens"] / c["judge_calls"] for c in cls)),
                    "output_tokens_per_call": round(fmean(c["output_tokens"] / c["judge_calls"] for c in cls), 1),
                    "max_output_tokens_per_call": max(c["max_output_tokens_per_call"] for c in cls),
                    "call_latency_p50_ms": round(fmean(c["call_latency_ms"]["p50"] for c in cls)),
                    "call_latency_p95_ms": round(fmean(c["call_latency_ms"]["p95"] for c in cls)),
                    "item_latency_p50_ms": round(fmean(c["item_latency_ms"]["p50"] for c in cls)),
                    "item_latency_p95_ms": round(fmean(c["item_latency_ms"]["p95"] for c in cls)),
                    "retries_total": sum(c["retries"] for c in cls),
                    "rules_failed_closed_total": sum(c["rules_failed_closed"] for c in cls),
                    "parse_failures_total": sum(v for c in cls for k, v in c["parse_status"].items() if k != "ok"),
                },
            }
    return report


def _mean(rs: list[dict]) -> dict:
    """Mean, min and max over runs. A rate is None when its class is absent (e.g. no benign items)."""
    def agg(vals):
        vals = [v for v in vals if v is not None]
        return (round(fmean(vals), 4), min(vals), max(vals)) if vals else (None, None, None)

    out: dict[str, Any] = {k: agg(r[k] for r in rs)[0] for k in ("review_rate", "review_rate_harmful", "review_rate_benign")}
    for mode in ("strict", "lenient"):
        out[mode] = {}
        for k in rs[0][mode]:
            out[mode][k], out[mode][k + "_min"], out[mode][k + "_max"] = agg(r[mode][k] for r in rs)
    return out


def write_csvs(report: dict, out_dir: Path = METRICS, prefix: str = "part3") -> list[Path]:
    out_dir.mkdir(exist_ok=True)
    m_rows, a_rows, c_rows = [], [], []
    for batch, models in report["batches"].items():
        for model, m in models.items():
            for t, s in m["sweep"].items():
                for weighting in ("raw", "reweighted"):
                    for review in ("strict", "lenient"):
                        r = s[weighting][review]
                        m_rows.append({
                            "batch": batch, "sample_seed": m["sample"]["seed"], "policy_version": m["policy_version"],
                            "model": model, "threshold": t,
                            "is_policy_threshold": float(t) == m["policy_threshold"], "weighting": weighting,
                            "review_counted_as": "harmful" if review == "strict" else "benign",
                            "accuracy": r["accuracy"], "false_allow_rate": r["false_allow_rate"],
                            "false_allow_min": r["false_allow_rate_min"], "false_allow_max": r["false_allow_rate_max"],
                            "false_block_rate": r["false_block_rate"],
                            "false_block_min": r["false_block_rate_min"], "false_block_max": r["false_block_rate_max"],
                            "review_rate": s[weighting]["review_rate"], "runs": len(m["runs"]),
                        })
            ag = m["agreement"]
            a_rows.append({"batch": batch, "model": model, **{k: v for k, v in ag.items() if k != "unstable_items"},
                           "unstable_items": len(ag["unstable_items"])})
            c_rows.append({"batch": batch, "model": model, **m["cost_latency"]})
    paths = []
    for name, rows in ((f"{prefix}_metrics.csv", m_rows), (f"{prefix}_agreement.csv", a_rows),
                       (f"{prefix}_cost_latency.csv", c_rows)):
        p = out_dir / name
        with open(p, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        paths.append(p)
    p = out_dir / f"{prefix}_summary.json"
    p.write_text(json.dumps(report, indent=2) + "\n")
    paths.append(p)
    return paths


def print_report(report: dict) -> None:
    for batch, models in report["batches"].items():
        print(f"\n######## {batch}")
        for model, m in models.items():
            print(f"\n== {model} (policy {m['policy_version']}, {len(m['runs'])} runs)")
            print(f"  {'thr':<5}{'weighting':<12}{'acc':>7}{'FA':>7}{'FA rng':>13}{'FB':>7}{'FB rng':>13}{'review':>8}   (REVIEW = harmful)")
            for t, s in m["sweep"].items():
                for weighting in ("raw", "reweighted"):
                    r = s[weighting]["strict"]
                    print(f"  {t:<5}{weighting:<12}{r['accuracy']:>7.3f}{r['false_allow_rate']:>7.3f}"
                          f"{'[%.2f-%.2f]' % (r['false_allow_rate_min'], r['false_allow_rate_max']):>13}"
                          f"{r['false_block_rate']:>7.3f}{'[%.2f-%.2f]' % (r['false_block_rate_min'], r['false_block_rate_max']):>13}"
                          f"{s[weighting]['review_rate']:>8.3f}")
            for t, fl in m["threshold_flips"].items():
                print(f"  threshold {m['policy_threshold']} -> {t}: {len(fl)} (item, verdict) changes")
                for f in fl:
                    print(f"     {f['stratum']:<40} harmful={f['label_harmful']!s:<6} {f['at_policy_threshold']} -> {f['at_t']}")
            ag, cl = m["agreement"], m["cost_latency"]
            print(f"  agreement: final verdict identical {ag['final_verdict_identical_all_runs']:.2%}, "
                  f"decision identical {ag['decision_identical_all_runs']:.2%}, rule verdicts identical "
                  f"{ag['rule_verdict_identical_all_runs']:.2%}, pairwise {ag['final_verdict_pairwise']:.2%}")
            print(f"  cost: ${cl['cost_usd_per_run']}/run, ${cl['cost_usd_per_1k_items']}/1k items; "
                  f"call p50/p95 {cl['call_latency_p50_ms']}/{cl['call_latency_p95_ms']} ms; item p50/p95 "
                  f"{cl['item_latency_p50_ms']}/{cl['item_latency_p95_ms']} ms; tokens in/out per call "
                  f"{cl['input_tokens_per_call']}/{cl['output_tokens_per_call']}; retries {cl['retries_total']}, "
                  f"parse failures {cl['parse_failures_total']}, failed closed {cl['rules_failed_closed_total']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("batches", nargs="+")
    ap.add_argument("--prefix", default="part3", help="output file prefix under metrics/ (default part3)")
    args = ap.parse_args()
    report = analyze(args.batches)
    print_report(report)
    for p in write_csvs(report, prefix=args.prefix):
        print(f"wrote {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
