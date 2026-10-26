"""Draw the fixed, stratified 100-item sample and save its row ids.

    python -m experiment.sample            # writes runs/sample_seed20.json (refuses to overwrite)
    python -m experiment.sample --seed 7   # another sample: runs/sample_seed7.json

Balance: about 50/50 harmful/benign instead of the natural ~22% harmful, so every harmful group has enough items
for a false-allow rate to mean something. Benign is over-weighted toward hard negatives. Each item carries its
stratum and the stratum's population count, so Part 3 can re-weight every rate back to the natural mix.
The sample file stores ids, not text; runs read the text from the CSV and check the CSV hash.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from .dataset import ROOT, dataset_sha256, load_rows

DEFAULT_SEED = 20
RUNS = ROOT / "runs"

# stratum -> items to draw. Harmful 50, benign 50 (32 hard negatives + 18 everyday).
ALLOCATION: dict[str, int] = {
    # harmful
    "pii/input": 8,
    "pii/output": 8,
    "prompt_manipulation/input/jailbreak": 6,
    "prompt_manipulation/input/prompt_injection": 6,
    "prompt_manipulation/input/pii_output": 6,
    "toxicity/input": 8,
    "toxicity/output": 8,
    # benign hard negatives
    "caps_words/input": 4,
    "caps_words/output": 4,
    "repeated_words/input": 4,
    "repeated_words/output": 4,
    "minimal_turn/input": 4,
    "minimal_turn/output": 4,
    "safe_reply/output": 8,
    # benign everyday
    "everyday_request/input": 7,
    "everyday_reply/output": 7,
    "simple_query/input": 4,
}


def draw(seed: int = DEFAULT_SEED) -> dict:
    rows = load_rows()
    by_stratum: dict[str, list] = {}
    for r in rows:
        by_stratum.setdefault(r.stratum, []).append(r)
    missing = set(by_stratum) - set(ALLOCATION)
    if missing:
        raise ValueError(f"strata without an allocation: {sorted(missing)}")

    rng = random.Random(seed)
    items = []
    for stratum, n in ALLOCATION.items():
        pool = sorted(by_stratum[stratum], key=lambda r: r.id)   # stable order before sampling
        for r in rng.sample(pool, n):
            items.append({"id": r.id, "stratum": stratum, "category": r.category, "mode": r.mode, "label": r.label})

    population = Counter(r.stratum for r in rows)
    harmful = sum(i["label"] for i in items)
    return {
        "seed": seed,
        "dataset": "sanity_subset.csv",
        "dataset_sha256": dataset_sha256(),
        "size": len(items),
        "harmful": harmful,
        "benign": len(items) - harmful,
        "allocation": ALLOCATION,
        "population": {s: population[s] for s in ALLOCATION},
        "population_total": len(rows),
        "population_harmful": sum(r.label for r in rows),
        "items": items,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    out = args.out or RUNS / f"sample_seed{args.seed}.json"
    sample = draw(args.seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "x", encoding="utf-8") as fh:      # "x": never overwrite a saved sample
        json.dump(sample, fh, indent=2)
        fh.write("\n")
    modes = Counter(i["mode"] for i in sample["items"])
    print(f"wrote {out}: {sample['size']} items, {sample['harmful']} harmful, "
          f"{sample['benign']} benign, input={modes['input']}, output={modes['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
