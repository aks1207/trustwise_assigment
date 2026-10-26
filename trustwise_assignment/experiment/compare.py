"""Compare two batches item by item, per model (e.g. before and after a prompt change).

    python -m experiment.compare <batch_a> <batch_b>

For each model: mean rates over the repeats in each batch, the items whose majority verdict changed (and in
which direction for the label), and how often each rule's verdict changed. Writes metrics/compare_<a>_vs_<b>.json.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean

from .dataset import ROOT, load_rows
from .metrics import HARMFUL, batch_sample, item_weights, rates, read_jsonl

RUNS = ROOT / "runs"
METRICS = ROOT / "metrics"


def load_batch(batch: str, runs_dir: Path = RUNS) -> dict[str, list[list[dict]]]:
    """model -> list of runs (each a list of result lines), completed runs only."""
    out: dict[str, list[list[dict]]] = defaultdict(list)
    for d in sorted(p for p in (runs_dir / batch).iterdir() if p.is_dir() and (p / "completed.json").exists()):
        meta = json.loads((d / "meta.json").read_text())
        out[meta["model_name"]].append(read_jsonl(d / "results.jsonl"))
    return dict(out)


def majority(verdicts: list[str]) -> str:
    return Counter(verdicts).most_common(1)[0][0]


def _mean_rates(runs: list[list[dict]], weights=None) -> dict:
    rs = [rates(r, weights) for r in runs]
    out = {"review_rate": round(fmean(r["review_rate"] for r in rs), 4)}
    for mode in ("strict", "lenient"):
        out[mode] = {k: round(fmean(r[mode][k] for r in rs), 4) for k in rs[0][mode]}
    return out


def compare(a: str, b: str, runs_dir: Path = RUNS) -> dict:
    A, B = load_batch(a, runs_dir), load_batch(b, runs_dir)
    sa, sb = batch_sample(runs_dir / a, ROOT), batch_sample(runs_dir / b, ROOT)
    if sa["_sha256"] != sb["_sha256"]:
        raise SystemExit(f"{a} and {b} use different samples; item-by-item comparison needs the same items")
    w = item_weights(sa)
    text = {r.id: r.text for r in load_rows()}
    report: dict = {"a": a, "b": b, "sample": {"path": sa["_path"], "seed": sa["seed"]}, "models": {}}
    for model in sorted(set(A) & set(B)):
        def per_item(runs):
            items: dict[str, dict] = {}
            for run in runs:
                for l in run:
                    it = items.setdefault(l["item_id"], {"stratum": l["stratum"], "label": l["label"], "verdicts": [], "rules": defaultdict(list)})
                    it["verdicts"].append(l["result"]["final_verdict"])
                    for x in l["result"]["rule_results"]:
                        it["rules"][x["rule_id"]].append(x["verdict"])
            return items
        ia, ib = per_item(A[model]), per_item(B[model])
        changed, rule_changes = [], Counter()
        for iid in ia:
            ma, mb = majority(ia[iid]["verdicts"]), majority(ib[iid]["verdicts"])
            for rid in ia[iid]["rules"]:
                if majority(ia[iid]["rules"][rid]) != majority(ib[iid]["rules"][rid]):
                    rule_changes[rid] += 1
            if ma != mb:
                label = ia[iid]["label"]
                was_right = (ma in HARMFUL or ma == "REVIEW") == label
                now_right = (mb in HARMFUL or mb == "REVIEW") == label
                changed.append({
                    "item_id": iid, "stratum": ia[iid]["stratum"], "label_harmful": label,
                    "verdicts_a": ia[iid]["verdicts"], "verdicts_b": ib[iid]["verdicts"],
                    "effect": "fixed" if now_right and not was_right else "broke" if was_right and not now_right else "neutral",
                    "text_preview": text[iid][:140],
                })
        report["models"][model] = {
            "runs": {a: len(A[model]), b: len(B[model])},
            "raw": {a: _mean_rates(A[model]), b: _mean_rates(B[model])},
            "reweighted_to_population": {a: _mean_rates(A[model], w), b: _mean_rates(B[model], w)},
            "majority_verdict_changed": changed,
            "rule_majority_changes": dict(rule_changes),
        }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("a")
    ap.add_argument("b")
    args = ap.parse_args()
    rep = compare(args.a, args.b)
    METRICS.mkdir(exist_ok=True)
    out = METRICS / f"compare_{args.a}_vs_{args.b}.json"
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
    for model, m in rep["models"].items():
        print(f"\n== {model}  (runs {m['runs']})")
        for kind in ("raw", "reweighted_to_population"):
            ra, rb = m[kind][args.a], m[kind][args.b]
            print(f"  [{kind}] strict acc {ra['strict']['accuracy']:.3f} -> {rb['strict']['accuracy']:.3f} | "
                  f"FA {ra['strict']['false_allow_rate']:.3f} -> {rb['strict']['false_allow_rate']:.3f} | "
                  f"FB {ra['strict']['false_block_rate']:.3f} -> {rb['strict']['false_block_rate']:.3f} | "
                  f"review {ra['review_rate']:.3f} -> {rb['review_rate']:.3f}")
        print(f"  rule majority changes: {m['rule_majority_changes']}")
        for c in m["majority_verdict_changed"]:
            print(f"  {c['effect']:<8}{c['stratum']:<42}{c['verdicts_a']} -> {c['verdicts_b']}  {c['text_preview'][:70]!r}")
    print(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
